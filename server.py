import os
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import List
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
import io
import json
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False
    print("Warning: HEIC support not available. Install with: pip install pillow-heif")

from inky.auto import auto

# Configuration
CONFIG = {
    "max_images": 10,  # Maximum number of images to store
    "cycle_interval": 600,  # Cycle interval in seconds (10 minutes)
    "display_size": (800, 480),  # E-ink display resolution
    "saturation": 0.5  # Default saturation for e-ink display
}

# Initialize FastAPI app
app = FastAPI()

# Enable CORS for React frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create directories for image storage
UPLOAD_DIR = Path("uploaded_images")
UPLOAD_DIR.mkdir(exist_ok=True)

# Global variables
current_image_index = 0
image_files: List[Path] = []
cycling_enabled = True
cycle_thread = None

# Mock inky module for development (remove this when using real hardware)
class MockInky:
    def set_image(self, image, saturation=None):
        print(f"Setting image with saturation: {saturation}")

    def show(self):
        print("Displaying image on e-ink display")

# Initialize inky (use real import when on actual hardware)
# from inky import InkyPHAT
# inky = InkyPHAT("black")
# inky = MockInky()  # Remove this line when using real hardware
inky = auto(ask_user=True, verbose=True)

def resize_and_crop_image(image: Image.Image, target_size: tuple) -> Image.Image:
    """Resize and crop image to target size maintaining aspect ratio."""
    target_width, target_height = target_size
    
    # Calculate aspect ratios
    img_ratio = image.width / image.height
    target_ratio = target_width / target_height
    
    if img_ratio > target_ratio:
        # Image is wider than target, crop width
        new_height = target_height
        new_width = int(target_height * img_ratio)
    else:
        # Image is taller than target, crop height
        new_width = target_width
        new_height = int(target_width / img_ratio)
    
    # Resize image
    image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
    
    # Crop to target size
    left = (new_width - target_width) // 2
    top = (new_height - target_height) // 2
    right = left + target_width
    bottom = top + target_height
    
    return image.crop((left, top, right, bottom))


def display_image(image_path: Path):
    """Display image on e-ink display."""
    try:
        image = Image.open(image_path)
        # Convert to RGB if necessary
        if image.mode != 'RGB':
            image = image.convert('RGB')
        
        # Display on e-ink
        try:
            inky.set_image(image, saturation=CONFIG["saturation"])
        except TypeError:
            inky.set_image(image)
        inky.show()
        
        print(f"Displayed image: {image_path.name}")
    except Exception as e:
        print(f"Error displaying image: {e}")


def cycle_images():
    """Background thread to cycle through images."""
    global current_image_index
    
    while cycling_enabled:
        if image_files:
            display_image(image_files[current_image_index])
            current_image_index = (current_image_index + 1) % len(image_files)
        
        time.sleep(CONFIG["cycle_interval"])


def start_cycling():
    """Start the image cycling thread."""
    global cycle_thread
    if cycle_thread is None or not cycle_thread.is_alive():
        cycle_thread = threading.Thread(target=cycle_images, daemon=True)
        cycle_thread.start()


def create_thumbnail(image_path: Path, size=(150, 90)):
    """Create a thumbnail for the image."""
    thumb_dir = UPLOAD_DIR / "thumbnails"
    thumb_dir.mkdir(exist_ok=True)
    
    thumb_path = thumb_dir / f"thumb_{image_path.name}"
    
    # Only create thumbnail if it doesn't exist
    if not thumb_path.exists():
        try:
            image = Image.open(image_path)
            image.thumbnail(size, Image.Resampling.LANCZOS)
            if image.mode != 'RGB':
                image = image.convert('RGB')
            image.save(thumb_path, "JPEG", quality=85)
        except Exception as e:
            print(f"Error creating thumbnail: {e}")
            return None
    
    return thumb_path


def load_existing_images():
    """Load existing images from upload directory."""
    global image_files
    image_files = sorted(
        [f for f in UPLOAD_DIR.iterdir() if f.suffix.lower() in ['.jpg', '.jpeg']],
        key=lambda x: x.stat().st_mtime
    )[-CONFIG["max_images"]:]
    
    # Create thumbnails for existing images
    for img in image_files:
        create_thumbnail(img)
    
    if image_files:
        display_image(image_files[0])


@app.on_event("startup")
async def startup_event():
    """Initialize the server and start image cycling."""
    load_existing_images()
    start_cycling()


@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...)):
    """Handle image upload."""
    try:
        # Check if file is HEIC and HEIC support is not available
        if file.filename.lower().endswith(('.heic', '.heif')) and not HEIC_SUPPORT:
            raise HTTPException(
                status_code=400, 
                detail="HEIC files are not supported. Please install pillow-heif: pip install pillow-heif"
            )
        
        # Read image data
        contents = await file.read()
        
        # Handle HEIC files
        if file.filename.lower().endswith(('.heic', '.heif')):
            try:
                image = Image.open(io.BytesIO(contents))
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Error processing HEIC file: {str(e)}"
                )
        else:
            image = Image.open(io.BytesIO(contents))
        
        # Resize and crop image
        processed_image = resize_and_crop_image(image, CONFIG["display_size"])
        
        # Convert to RGB if necessary
        if processed_image.mode != 'RGB':
            processed_image = processed_image.convert('RGB')
        
        # Save as JPEG
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"image_{timestamp}.jpg"
        filepath = UPLOAD_DIR / filename
        
        processed_image.save(filepath, "JPEG", quality=95)
        
        # Update image list
        global image_files
        image_files.append(filepath)
        
        # Create thumbnail
        create_thumbnail(filepath)
        
        # Remove oldest images if exceeding limit
        if len(image_files) > CONFIG["max_images"]:
            oldest = image_files.pop(0)
            oldest.unlink()
            # Remove thumbnail too
            thumb_path = UPLOAD_DIR / "thumbnails" / f"thumb_{oldest.name}"
            if thumb_path.exists():
                thumb_path.unlink()
        
        # Display the new image immediately
        display_image(filepath)
        
        return {"message": "Image uploaded successfully", "filename": filename}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/images")
async def get_images():
    """Get list of stored images."""
    return {
        "images": [f.name for f in image_files],
        "current_index": current_image_index,
        "total": len(image_files)
    }


@app.get("/api/config")
async def get_config():
    """Get current configuration."""
    return CONFIG


@app.post("/api/config")
async def update_config(config: dict):
    """Update configuration."""
    global CONFIG
    CONFIG.update(config)
    
    # Save config to file
    with open("config.json", "w") as f:
        json.dump(CONFIG, f)
    
    return {"message": "Configuration updated"}


@app.post("/api/cycle/{action}")
async def control_cycling(action: str):
    """Start or stop image cycling."""
    global cycling_enabled
    
    if action == "start":
        cycling_enabled = True
        start_cycling()
        return {"message": "Cycling started"}
    elif action == "stop":
        cycling_enabled = False
        return {"message": "Cycling stopped"}
    else:
        raise HTTPException(status_code=400, detail="Invalid action")


@app.get("/api/heic-support")
async def check_heic_support():
    """Check if HEIC support is available."""
    return {"supported": HEIC_SUPPORT}


@app.get("/api/thumbnail/{filename}")
async def get_thumbnail(filename: str):
    """Get thumbnail for an image."""
    thumb_path = UPLOAD_DIR / "thumbnails" / f"thumb_{filename}"
    if thumb_path.exists():
        return FileResponse(thumb_path)
    else:
        # Try to create thumbnail if it doesn't exist
        image_path = UPLOAD_DIR / filename
        if image_path.exists():
            thumb = create_thumbnail(image_path)
            if thumb:
                return FileResponse(thumb)
        raise HTTPException(status_code=404, detail="Thumbnail not found")


# Serve React frontend
@app.get("/")
async def serve_frontend():
    """Serve the React frontend."""
    return HTMLResponse(content="""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>E-ink Picture Frame</title>
    <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
    <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
    <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
        }
        .upload-area {
            border: 2px dashed #ccc;
            border-radius: 8px;
            padding: 40px;
            text-align: center;
            background-color: white;
            transition: all 0.3s;
            cursor: pointer;
        }
        .upload-area.drag-over {
            border-color: #4a90e2;
            background-color: #f0f7ff;
        }
        .upload-area:hover {
            border-color: #999;
        }
        .image-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
            gap: 15px;
            margin-top: 20px;
        }
        .image-item {
            background: white;
            padding: 8px;
            border-radius: 8px;
            text-align: center;
            cursor: pointer;
            transition: all 0.2s;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .image-item:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.15);
        }
        .image-item.current {
            border: 3px solid #4a90e2;
            box-shadow: 0 4px 12px rgba(74, 144, 226, 0.3);
        }
        .thumbnail {
            width: 100%;
            height: 90px;
            object-fit: cover;
            border-radius: 4px;
            margin-bottom: 8px;
            background-color: #f0f0f0;
        }
        .thumbnail-loading {
            width: 100%;
            height: 90px;
            background-color: #f0f0f0;
            border-radius: 4px;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #999;
            font-size: 12px;
        }
        .image-name {
            font-size: 12px;
            color: #666;
            word-break: break-all;
            margin-top: 4px;
        }
        .current-label {
            font-size: 11px;
            color: #4a90e2;
            font-weight: bold;
            margin-top: 4px;
        }
        .controls {
            margin: 20px 0;
            display: flex;
            gap: 10px;
            align-items: center;
        }
        button {
            padding: 8px 16px;
            border: none;
            border-radius: 4px;
            background-color: #4a90e2;
            color: white;
            cursor: pointer;
            font-size: 14px;
        }
        button:hover {
            background-color: #357abd;
        }
        button:disabled {
            background-color: #ccc;
            cursor: not-allowed;
        }
        .config {
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-top: 20px;
        }
        .config input {
            margin: 5px;
            padding: 5px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        .status {
            padding: 10px;
            margin: 10px 0;
            border-radius: 4px;
            background-color: #e8f5e9;
            color: #2e7d32;
        }
        .error {
            background-color: #ffebee;
            color: #c62828;
        }
    </style>
</head>
<body>
    <div id="root"></div>
    <script type="text/babel">
        const { useState, useEffect, useCallback } = React;

        function ImageThumbnail({ image, index, isCurrent, onClick }) {
            const [loading, setLoading] = useState(true);
            const [error, setError] = useState(false);

            return (
                <div
                    className={`image-item ${isCurrent ? 'current' : ''}`}
                    onClick={onClick}
                    title={`Click to display ${image}`}
                >
                    {loading && !error && (
                        <div className="thumbnail-loading">Loading...</div>
                    )}
                    {error && (
                        <div className="thumbnail-loading">No preview</div>
                    )}
                    <img 
                        src={`/api/thumbnail/${image}`} 
                        alt={image}
                        className="thumbnail"
                        style={{ display: loading || error ? 'none' : 'block' }}
                        onLoad={() => setLoading(false)}
                        onError={() => {
                            setLoading(false);
                            setError(true);
                        }}
                    />
                    <div className="image-name">{image.replace(/^image_/, '').replace('.jpg', '')}</div>
                    {isCurrent && <div className="current-label">Currently Displayed</div>}
                </div>
            );
        }

        function App() {
            const [images, setImages] = useState([]);
            const [currentIndex, setCurrentIndex] = useState(0);
            const [config, setConfig] = useState({});
            const [cycling, setCycling] = useState(true);
            const [dragOver, setDragOver] = useState(false);
            const [status, setStatus] = useState('');
            const [error, setError] = useState('');
            const [heicSupport, setHeicSupport] = useState(false);

            useEffect(() => {
                fetchImages();
                fetchConfig();
                checkHeicSupport();
                const interval = setInterval(fetchImages, 5000);
                return () => clearInterval(interval);
            }, []);

            const checkHeicSupport = async () => {
                try {
                    const response = await fetch('/api/heic-support');
                    const data = await response.json();
                    setHeicSupport(data.supported);
                } catch (err) {
                    console.error('Error checking HEIC support:', err);
                }
            };

            const fetchImages = async () => {
                try {
                    const response = await fetch('/api/images');
                    const data = await response.json();
                    setImages(data.images);
                    setCurrentIndex(data.current_index);
                } catch (err) {
                    console.error('Error fetching images:', err);
                }
            };

            const fetchConfig = async () => {
                try {
                    const response = await fetch('/api/config');
                    const data = await response.json();
                    setConfig(data);
                } catch (err) {
                    console.error('Error fetching config:', err);
                }
            };

            const handleDragOver = (e) => {
                e.preventDefault();
                setDragOver(true);
            };

            const handleDragLeave = () => {
                setDragOver(false);
            };

            const handleDrop = async (e) => {
                e.preventDefault();
                setDragOver(false);
                
                const files = Array.from(e.dataTransfer.files);
                const imageFile = files.find(file => file.type.startsWith('image/') || 
                    file.name.toLowerCase().endsWith('.heic') || 
                    file.name.toLowerCase().endsWith('.heif'));
                
                if (imageFile) {
                    await uploadFile(imageFile);
                } else {
                    setError('Please drop an image file');
                    setTimeout(() => setError(''), 3000);
                }
            };

            const handleFileSelect = async (e) => {
                const file = e.target.files[0];
                if (file) {
                    await uploadFile(file);
                }
            };

            const uploadFile = async (file) => {
                const formData = new FormData();
                formData.append('file', file);

                try {
                    setStatus('Uploading image...');
                    const response = await fetch('/api/upload', {
                        method: 'POST',
                        body: formData
                    });

                    if (response.ok) {
                        setStatus('Image uploaded successfully!');
                        fetchImages();
                        setTimeout(() => setStatus(''), 3000);
                    } else {
                        const errorData = await response.json();
                        throw new Error(errorData.detail || 'Upload failed');
                    }
                } catch (err) {
                    setError(err.message || 'Error uploading image');
                    setTimeout(() => setError(''), 5000);
                }
            };

            const displayImage = async (index) => {
                try {
                    await fetch(`/api/display/${index}`);
                    fetchImages();
                } catch (err) {
                    console.error('Error displaying image:', err);
                }
            };

            const toggleCycling = async () => {
                const action = cycling ? 'stop' : 'start';
                try {
                    await fetch(`/api/cycle/${action}`, { method: 'POST' });
                    setCycling(!cycling);
                } catch (err) {
                    console.error('Error toggling cycling:', err);
                }
            };

            const updateConfig = async () => {
                try {
                    await fetch('/api/config', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(config)
                    });
                    setStatus('Configuration updated');
                    setTimeout(() => setStatus(''), 3000);
                } catch (err) {
                    setError('Error updating configuration');
                    setTimeout(() => setError(''), 3000);
                }
            };

            return (
                <div className="container">
                    <h1>E-ink Picture Frame Control</h1>
                    
                    {status && <div className="status">{status}</div>}
                    {error && <div className="status error">{error}</div>}
                    
                    <div 
                        className={`upload-area ${dragOver ? 'drag-over' : ''}`}
                        onDragOver={handleDragOver}
                        onDragLeave={handleDragLeave}
                        onDrop={handleDrop}
                        onClick={() => document.getElementById('file-input').click()}
                    >
                        <h3>Drag and drop an image here</h3>
                        <p>or click to select a file</p>
                        <p style={{ fontSize: '14px', color: '#666', marginTop: '10px' }}>
                            Supported formats: JPEG, PNG, GIF, WebP
                            {heicSupport ? ', HEIC/HEIF' : ' (HEIC not supported - install pillow-heif)'}
                        </p>
                        <input
                            id="file-input"
                            type="file"
                            accept="image/*,.heic,.heif"
                            onChange={handleFileSelect}
                            style={{ display: 'none' }}
                        />
                    </div>

                    <div className="controls">
                        <button onClick={toggleCycling}>
                            {cycling ? 'Stop Cycling' : 'Start Cycling'}
                        </button>
                        <span>Cycling: {cycling ? 'ON' : 'OFF'}</span>
                    </div>

                    <div className="config">
                        <h3>Configuration</h3>
                        <div>
                            <label>
                                Max Images: 
                                <input
                                    type="number"
                                    value={config.max_images || 10}
                                    onChange={(e) => setConfig({...config, max_images: parseInt(e.target.value)})}
                                />
                            </label>
                        </div>
                        <div>
                            <label>
                                Cycle Interval (seconds): 
                                <input
                                    type="number"
                                    value={config.cycle_interval || 600}
                                    onChange={(e) => setConfig({...config, cycle_interval: parseInt(e.target.value)})}
                                />
                            </label>
                        </div>
                        <div>
                            <label>
                                Saturation: 
                                <input
                                    type="number"
                                    step="0.1"
                                    min="0"
                                    max="1"
                                    value={config.saturation || 0.5}
                                    onChange={(e) => setConfig({...config, saturation: parseFloat(e.target.value)})}
                                />
                            </label>
                        </div>
                        <button onClick={updateConfig}>Update Configuration</button>
                    </div>

                    <h3>Stored Images ({images.length})</h3>
                    <div className="image-grid">
                        {images.map((image, index) => (
                            <ImageThumbnail
                                key={image}
                                image={image}
                                index={index}
                                isCurrent={index === currentIndex}
                                onClick={() => displayImage(index)}
                            />
                        ))}
                    </div>
                </div>
            );
        }

        ReactDOM.render(<App />, document.getElementById('root'));
    </script>
</body>
</html>
    """)


if __name__ == "__main__":
    import uvicorn
    
    # Load saved configuration if exists
    if Path("config.json").exists():
        with open("config.json", "r") as f:
            CONFIG.update(json.load(f))
    
    # Run the server
    uvicorn.run(app, host="0.0.0.0", port=8000)
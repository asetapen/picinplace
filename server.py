import os
import sys
import time
import threading
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
import io
import json
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORT = True
except ImportError:
    HEIC_SUPPORT = False
    print("Warning: HEIC support not available. Install with: pip install pillow-heif")

MOCK_DISPLAY = "--mock" in sys.argv or os.environ.get("PICINPLACE_MOCK") == "1"

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
ORIGINALS_DIR = UPLOAD_DIR / "originals"
ORIGINALS_DIR.mkdir(exist_ok=True)
THUMB_DIR = UPLOAD_DIR / "thumbnails"
THUMB_DIR.mkdir(exist_ok=True)
CROPS_FILE = UPLOAD_DIR / "crops.json"
ORIGINAL_MAX_EDGE = 2000  # cap originals so 50 pictures don't fill the SD card

# Vendored JS libs (React/ReactDOM/Babel) served locally so the web UI works
# when the frame is running as a no-internet access point. Files live in
# static/ and are committed to the repo.
STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# In-memory map: display filename -> {"x": int, "y": int, "w": int, "h": int}
# (crop rect in original-image pixel coordinates). Persisted to crops.json.
crops: dict = {}
crops_lock = threading.Lock()

# Global variables
current_image_index = 0
image_files: List[Path] = []
cycling_enabled = True
cycle_thread = None
button_thread = None
dnd_active = False  # True while the DO NOT DISTURB screen is held (button B)

# Wakes the cycle thread early so a manual nav (button press) resets the
# countdown instead of letting it auto-advance right after you navigate.
cycle_wake = threading.Event()
# Serializes index update + display between the button thread and cycle thread.
nav_lock = threading.Lock()

# Inky Impression buttons (BCM pins) -> labels.
# A = play/pause, B = do not disturb, C = previous, D = next.
BUTTON_PINS = {5: "A", 6: "B", 16: "C", 24: "D"}
BUTTON_DEBOUNCE_S = 0.2

# Where the rendered DO NOT DISTURB screen is written before it's displayed.
DND_IMAGE_PATH = Path(tempfile.gettempdir()) / "picinplace_dnd.jpg"

# Mock inky module for development / running without hardware (--mock)
class MockInky:
    def set_image(self, image, saturation=None):
        print(f"[mock display] set_image saturation={saturation} size={image.size}")
        # Simulate the slow e-ink refresh so timings feel realistic in dev.
        time.sleep(0.2)

    def show(self):
        print("[mock display] show()")


def _init_display():
    if MOCK_DISPLAY:
        print("Running with --mock: no e-ink hardware will be used.")
        return MockInky()
    from inky.auto import auto
    return auto(ask_user=True, verbose=True)


inky = _init_display()

# Serialize display writes so concurrent clicks don't fight for the e-ink bus.
display_lock = threading.Lock()

def resize_and_crop_image(image: Image.Image, target_size: tuple) -> Image.Image:
    """Resize and crop image to target size maintaining aspect ratio (center crop)."""
    target_width, target_height = target_size
    img_ratio = image.width / image.height
    target_ratio = target_width / target_height

    if img_ratio > target_ratio:
        new_height = target_height
        new_width = int(target_height * img_ratio)
    else:
        new_width = target_width
        new_height = int(target_width / img_ratio)

    image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
    left = (new_width - target_width) // 2
    top = (new_height - target_height) // 2
    return image.crop((left, top, left + target_width, top + target_height))


# ---------------------------------------------------------------------------
# Framing: originals storage, face detection, crop persistence
# ---------------------------------------------------------------------------

def _load_crops():
    global crops
    if CROPS_FILE.exists():
        try:
            with open(CROPS_FILE) as f:
                crops = json.load(f)
        except Exception as e:
            print(f"Warning: couldn't read crops.json ({e}); starting empty")
            crops = {}
    else:
        crops = {}


def _save_crops():
    tmp = CROPS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(crops, f, indent=2)
    os.replace(tmp, CROPS_FILE)


def _clamp_crop(rect: dict, image_size: tuple, aspect: float) -> dict:
    """Snap a crop rect to image bounds, integer pixels, exact display aspect."""
    iw, ih = image_size
    x, y, w, h = float(rect["x"]), float(rect["y"]), float(rect["w"]), float(rect["h"])

    # Force aspect by adjusting whichever dim makes it fit inside the original
    if w / h > aspect:
        w = h * aspect
    else:
        h = w / aspect

    # Shrink to fit if larger than the image
    max_w = min(iw, ih * aspect)
    if w > max_w:
        w = max_w
        h = w / aspect

    # Clamp position
    x = max(0.0, min(iw - w, x))
    y = max(0.0, min(ih - h, y))
    return {"x": int(round(x)), "y": int(round(y)),
            "w": int(round(w)), "h": int(round(h))}


def _center_crop_rect(image_size: tuple, aspect: float) -> dict:
    iw, ih = image_size
    if iw / ih > aspect:
        ch = ih
        cw = ih * aspect
    else:
        cw = iw
        ch = iw / aspect
    return {"x": int((iw - cw) / 2), "y": int((ih - ch) / 2),
            "w": int(cw), "h": int(ch)}


# PIL's Image.Transpose constants rotate counterclockwise; we use clockwise
# rotation as the user-facing convention ("rotate right" = 90° CW), so the
# mapping is inverted from the obvious naming.
_ROTATE_MAP = {
    90:  Image.Transpose.ROTATE_270,   # 90° CW = 270° CCW in PIL terms
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


def _rotated(image: Image.Image, rotation: int) -> Image.Image:
    rotation = int(rotation) % 360
    if rotation == 0:
        return image
    if rotation not in _ROTATE_MAP:
        raise ValueError(f"rotation must be one of 0/90/180/270, got {rotation}")
    return image.transpose(_ROTATE_MAP[rotation])


def _stored_rotation(filename: str) -> int:
    """Crop entries from before rotation support don't have the field; treat as 0."""
    entry = crops.get(filename) or {}
    return int(entry.get("rotation", 0)) % 360


_face_cascade = None
def _get_face_cascade():
    global _face_cascade
    if _face_cascade is None:
        import cv2
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_cascade = cv2.CascadeClassifier(path)
        if _face_cascade.empty():
            raise RuntimeError(f"Failed to load Haar cascade from {path}")
    return _face_cascade


_CV_ROTATE_MAP = {
    90:  "ROTATE_90_CLOCKWISE",
    180: "ROTATE_180",
    270: "ROTATE_90_COUNTERCLOCKWISE",
}


def auto_crop_from_faces(original_path: Path, rotation: int = 0) -> dict:
    """Pick a crop rect (in rotated-image pixels) that frames detected faces.

    Falls back to a center crop if face detection finds nothing or errors out.
    """
    import cv2
    import numpy as np

    aspect = CONFIG["display_size"][0] / CONFIG["display_size"][1]
    img = cv2.imread(str(original_path))
    if img is None:
        # Pillow can sometimes open what cv2 won't; use it for dimensions
        with Image.open(original_path) as pim:
            rotated = _rotated(pim, rotation)
            return _center_crop_rect(rotated.size, aspect)
    if rotation % 360 != 0:
        img = cv2.rotate(img, getattr(cv2, _CV_ROTATE_MAP[int(rotation) % 360]))
    h, w = img.shape[:2]

    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        cascade = _get_face_cascade()
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5,
            minSize=(max(30, w // 40), max(30, h // 40)),
        )
    except Exception as e:
        print(f"Face detection failed for {original_path.name}: {e}")
        faces = []

    if len(faces) == 0:
        print(f"No faces detected in {original_path.name}; using center crop")
        return _center_crop_rect((w, h), aspect)

    x1 = min(int(f[0]) for f in faces)
    y1 = min(int(f[1]) for f in faces)
    x2 = max(int(f[0] + f[2]) for f in faces)
    y2 = max(int(f[1] + f[3]) for f in faces)
    bbox_w = x2 - x1
    bbox_h = y2 - y1
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0

    # Aim the crop at roughly 3x the face cluster height — leaves room for
    # shoulders/torso and breathing room around the edges. Widen if the face
    # cluster is wider than that height-derived crop.
    target_h = bbox_h * 3.0
    target_w = target_h * aspect
    if bbox_w * 2.0 > target_w:
        target_w = bbox_w * 2.0
        target_h = target_w / aspect

    # Bias the crop center downward so faces sit in the upper third
    # (headroom looks better than chin-room).
    cy_target = cy + target_h * 0.12

    rect = {"x": cx - target_w / 2, "y": cy_target - target_h / 2,
            "w": target_w, "h": target_h}
    rect = _clamp_crop(rect, (w, h), aspect)
    print(f"Auto-framed {original_path.name}: {len(faces)} face(s) -> {rect}")
    return rect


def _normalize_to_jpeg(image: Image.Image) -> Image.Image:
    """Auto-orient via EXIF, force RGB, and downscale to ORIGINAL_MAX_EDGE.

    EXIF orientation is baked in here so the stored original is always
    right-side-up — any further rotation is then a deliberate user choice,
    not a fight against phone-camera metadata.
    """
    image = ImageOps.exif_transpose(image)
    if image.mode != "RGB":
        image = image.convert("RGB")
    w, h = image.size
    longest = max(w, h)
    if longest > ORIGINAL_MAX_EDGE:
        scale = ORIGINAL_MAX_EDGE / longest
        image = image.resize((int(w * scale), int(h * scale)),
                             Image.Resampling.LANCZOS)
    return image


def apply_crop(filename: str, crop_rect: dict = None) -> Path:
    """Re-cut the displayed JPEG (and its thumbnail) from the stored original.

    `crop_rect` may include a `rotation` field (0/90/180/270, clockwise);
    if absent, the previously-stored rotation is reused. The crop x/y/w/h
    are interpreted in *rotated*-image pixel coordinates.
    """
    original_path = ORIGINALS_DIR / filename
    if not original_path.exists():
        raise FileNotFoundError(f"No original for {filename}")

    if crop_rect is not None and "rotation" in crop_rect:
        rotation = int(crop_rect["rotation"]) % 360
    else:
        rotation = _stored_rotation(filename)

    with Image.open(original_path) as src:
        if src.mode != "RGB":
            src = src.convert("RGB")
        oriented = _rotated(src, rotation)
        aspect = CONFIG["display_size"][0] / CONFIG["display_size"][1]
        rect = crop_rect or crops.get(filename) or _center_crop_rect(oriented.size, aspect)
        rect = _clamp_crop(rect, oriented.size, aspect)

        cropped = oriented.crop((rect["x"], rect["y"],
                                 rect["x"] + rect["w"],
                                 rect["y"] + rect["h"]))
        display_img = cropped.resize(tuple(CONFIG["display_size"]),
                                     Image.Resampling.LANCZOS)

        display_path = UPLOAD_DIR / filename
        tmp_path = display_path.with_suffix(".jpg.tmp")
        display_img.save(tmp_path, "JPEG", quality=95)
        os.replace(tmp_path, display_path)

    # Refresh thumbnail (delete + regenerate)
    thumb_path = THUMB_DIR / f"thumb_{filename}"
    if thumb_path.exists():
        thumb_path.unlink()
    create_thumbnail(display_path)

    with crops_lock:
        crops[filename] = {**rect, "rotation": rotation}
        _save_crops()
    return display_path


def migrate_existing_images():
    """For any displayed image that doesn't have an original on disk, treat
    the current 800x480 JPEG as its own original. The user can still re-position
    a crop inside it (degenerate), but at least nothing breaks."""
    for img in UPLOAD_DIR.iterdir():
        if not img.is_file() or img.suffix.lower() not in (".jpg", ".jpeg"):
            continue
        original_path = ORIGINALS_DIR / img.name
        if not original_path.exists():
            try:
                with Image.open(img) as src:
                    src = _normalize_to_jpeg(src)
                    src.save(original_path, "JPEG", quality=95)
                print(f"Migrated {img.name} -> originals/ (no pre-crop source available)")
            except Exception as e:
                print(f"Failed to migrate {img.name}: {e}")
                continue
        if img.name not in crops:
            with Image.open(original_path) as o:
                aspect = CONFIG["display_size"][0] / CONFIG["display_size"][1]
                crops[img.name] = _center_crop_rect(o.size, aspect)
    _save_crops()


def _push_to_eink(image_path: Path):
    """Slow path: actually drive the e-ink panel. Serialized via display_lock."""
    with display_lock:
        try:
            image = Image.open(image_path)
            if image.mode != 'RGB':
                image = image.convert('RGB')
            try:
                inky.set_image(image, saturation=CONFIG["saturation"])
            except TypeError:
                inky.set_image(image)
            inky.show()
            print(f"Pushed to e-ink: {image_path.name}")
        except Exception as e:
            print(f"Error pushing to e-ink: {e}")


def display_image(image_path: Path):
    """Full synchronous pipeline: update the web preview and the e-ink panel.

    Used by the cycle thread and startup, where blocking is fine.
    """
    create_mock_frame_display(image_path)
    _push_to_eink(image_path)


def display_image_async(image_path: Path):
    """Used by HTTP handlers: write the web preview synchronously (fast)
    so the next /api/mock-frame fetch sees the new image, then push to
    the (slow) e-ink panel from a background thread.
    """
    create_mock_frame_display(image_path)
    threading.Thread(target=_push_to_eink, args=(image_path,), daemon=True).start()


def create_mock_frame_display(image_path: Path):
    """Write the full-color preview shown in the web UI as 'Mock Frame'.

    Written atomically (temp file + os.replace) so a concurrent GET /api/mock-frame
    can't observe a half-rewritten file — that races with Starlette's Content-Length
    and raises h11 "Too much data for declared Content-Length".
    """
    try:
        mock_dir = Path("mock_frame")
        mock_dir.mkdir(exist_ok=True)

        image = Image.open(image_path)
        if image.size != CONFIG["display_size"]:
            image = resize_and_crop_image(image, CONFIG["display_size"])
        if image.mode != 'RGB':
            image = image.convert('RGB')

        mock_path = mock_dir / "current_display.jpg"
        tmp_path = mock_dir / "current_display.jpg.tmp"
        image.save(tmp_path, "JPEG", quality=95)
        os.replace(tmp_path, mock_path)

        print(f"Mock frame updated: {mock_path}")
    except Exception as e:
        print(f"Error creating mock frame display: {e}")


def cycle_images():
    """Background thread to cycle through images.

    Sleep first, then advance and display, so current_image_index always
    names what is currently on the panel (matching what load_existing_images
    already drew at startup, or whatever the user last selected).

    The sleep is an interruptible wait: a manual nav (button press) or a stop
    request sets cycle_wake, waking us early. A manual nav just restarts the
    countdown; only a real timeout advances the image.
    """
    global current_image_index

    while cycling_enabled:
        interrupted = cycle_wake.wait(timeout=CONFIG["cycle_interval"])
        cycle_wake.clear()
        if not cycling_enabled:
            break
        if interrupted:
            continue  # manual nav reset the timer; don't auto-advance
        if dnd_active:
            continue  # holding DO NOT DISTURB; don't replace it
        with nav_lock:
            if not image_files:
                continue
            current_image_index = (current_image_index + 1) % len(image_files)
            target = image_files[current_image_index]
        display_image(target)


def start_cycling():
    """Start the image cycling thread."""
    global cycle_thread
    if cycle_thread is None or not cycle_thread.is_alive():
        cycle_thread = threading.Thread(target=cycle_images, daemon=True)
        cycle_thread.start()


def _show_relative(delta: int):
    """Advance the displayed image by `delta` positions (wrapping) and reset the
    auto-cycle timer. Used by the physical buttons: C = -1 (previous), D = +1 (next).
    """
    global current_image_index, dnd_active
    with nav_lock:
        if not image_files:
            return
        dnd_active = False  # navigating returns to the photos
        current_image_index = (current_image_index + delta) % len(image_files)
        cycle_wake.set()  # reset the auto-cycle countdown after a manual nav
        target = image_files[current_image_index]
    display_image(target)


def _toggle_cycling():
    """Toggle the slideshow on/off (button A)."""
    global cycling_enabled
    if cycling_enabled:
        cycling_enabled = False
        cycle_wake.set()  # wake the cycle thread so it pauses promptly
        print("Cycling paused")
    else:
        cycling_enabled = True
        start_cycling()
        print("Cycling resumed")


_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Raspberry Pi OS / Debian
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",      # macOS
    "/Library/Fonts/Arial Bold.ttf",
]


def _load_font(size: int):
    from PIL import ImageFont
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10 scales the default
    except TypeError:
        return ImageFont.load_default()


def _fit_font(draw, lines, max_width: int, max_height: int):
    """Largest font (in our preferred face) that fits every line in the box."""
    longest = max(lines, key=len)
    chosen = _load_font(12)
    size = 12
    while size < 400:
        candidate = _load_font(size + 8)
        line_w = draw.textbbox((0, 0), longest, font=candidate)[2]
        line_h = draw.textbbox((0, 0), "Ag", font=candidate)[3]
        if line_w > max_width or line_h * len(lines) * 1.25 > max_height:
            break
        size += 8
        chosen = candidate
    return chosen


def _render_dnd_image() -> Path:
    """Render the 'DO NOT / DISTURB' screen (bold red on white) and save it."""
    from PIL import ImageDraw

    w, h = CONFIG["display_size"]
    img = Image.new("RGB", (int(w), int(h)), "white")
    draw = ImageDraw.Draw(img)

    lines = ["DO NOT", "DISTURB"]
    font = _fit_font(draw, lines, max_width=int(w * 0.85), max_height=int(h * 0.85))

    bboxes = [draw.textbbox((0, 0), ln, font=font) for ln in lines]
    heights = [b[3] - b[1] for b in bboxes]
    gap = int(0.15 * max(heights))
    total_h = sum(heights) + gap * (len(lines) - 1)
    y = (int(h) - total_h) // 2
    for ln, b, lh in zip(lines, bboxes, heights):
        lw = b[2] - b[0]
        x = (int(w) - lw) // 2 - b[0]
        draw.text((x, y - b[1]), ln, font=font, fill=(255, 0, 0))
        y += lh + gap

    img.save(DND_IMAGE_PATH, "JPEG", quality=95)
    return DND_IMAGE_PATH


def _enter_dnd():
    global dnd_active
    dnd_active = True
    cycle_wake.set()  # don't let a pending tick advance the slideshow
    display_image(_render_dnd_image())
    print("DO NOT DISTURB on")


def _exit_dnd():
    global dnd_active
    dnd_active = False
    cycle_wake.set()  # reset the timer so we don't advance the instant we return
    if image_files:
        display_image(image_files[current_image_index])
    print("DO NOT DISTURB off")


def _toggle_dnd():
    """Toggle the DO NOT DISTURB screen (button B)."""
    if dnd_active:
        _exit_dnd()
    else:
        _enter_dnd()


def _handle_press(label: str):
    if label == "A":
        _toggle_cycling()
    elif label == "B":
        _toggle_dnd()
    elif label == "C":
        _show_relative(-1)
    elif label == "D":
        _show_relative(1)


def _button_listener():
    """Block on Inky Impression button edges and dispatch A/B/C/D presses.

    Uses libgpiod v2 (gpiod + gpiodevice), matching Pimoroni's current Inky
    examples. Imports are lazy and guarded so dev/--mock and button-less boards
    are unaffected.
    """
    try:
        import gpiod
        import gpiodevice
        from gpiod.line import Bias, Direction, Edge
    except Exception as e:
        print(f"Buttons disabled (gpiod unavailable): {e}")
        return

    try:
        pins = list(BUTTON_PINS)
        settings = gpiod.LineSettings(
            direction=Direction.INPUT, bias=Bias.PULL_UP, edge_detection=Edge.FALLING
        )
        chip = gpiodevice.find_chip_by_platform()
        offsets = [chip.line_offset_from_id(pin) for pin in pins]
        request = chip.request_lines(
            consumer="picinplace-buttons", config=dict.fromkeys(offsets, settings)
        )
    except Exception as e:
        print(f"Buttons disabled (GPIO setup failed): {e}")
        return

    offset_to_pin = dict(zip(offsets, pins))
    last_press: dict = {}
    print("Inky buttons ready: A = play/pause, B = do not disturb, "
          "C = previous, D = next")

    while True:
        for event in request.read_edge_events():
            pin = offset_to_pin.get(event.line_offset)
            if pin is None:
                continue
            now = time.monotonic()
            if now - last_press.get(pin, 0.0) < BUTTON_DEBOUNCE_S:
                continue  # debounce: ignore contact bounce / rapid repeats
            last_press[pin] = now
            _handle_press(BUTTON_PINS[pin])


def start_button_listener():
    """Start the button-reading thread (no-op under --mock or if already running)."""
    global button_thread
    if MOCK_DISPLAY:
        print("Buttons disabled (--mock: no GPIO).")
        return
    if button_thread is not None and button_thread.is_alive():
        return
    button_thread = threading.Thread(target=_button_listener, daemon=True)
    button_thread.start()


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
        [f for f in UPLOAD_DIR.iterdir()
         if f.is_file() and f.suffix.lower() in ['.jpg', '.jpeg']],
        key=lambda x: x.stat().st_mtime
    )[-CONFIG["max_images"]:]

    for img in image_files:
        create_thumbnail(img)

    if image_files:
        display_image(image_files[0])


@app.on_event("startup")
async def startup_event():
    """Initialize the server and start image cycling."""
    _load_crops()
    load_existing_images()
    migrate_existing_images()  # backfill originals + crops for pre-existing JPEGs
    start_cycling()
    start_button_listener()


@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...)):
    """Handle image upload. Saves a downsized original, runs face detection
    to pick an initial crop, then writes the 800x480 display JPEG."""
    try:
        if file.filename.lower().endswith(('.heic', '.heif')) and not HEIC_SUPPORT:
            raise HTTPException(
                status_code=400,
                detail="HEIC files are not supported. Please install pillow-heif: pip install pillow-heif"
            )

        contents = await file.read()
        try:
            image = Image.open(io.BytesIO(contents))
        except Exception as e:
            raise HTTPException(status_code=400,
                                detail=f"Error opening image: {e}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"image_{timestamp}.jpg"
        original_path = ORIGINALS_DIR / filename

        # Persist the original (downsized to ORIGINAL_MAX_EDGE) so we can re-crop later.
        normalized = _normalize_to_jpeg(image)
        normalized.save(original_path, "JPEG", quality=95)

        # Auto-frame from face detection, then cut the display JPEG + thumbnail.
        initial_crop = auto_crop_from_faces(original_path)
        with crops_lock:
            crops[filename] = initial_crop
            _save_crops()
        filepath = apply_crop(filename)

        global image_files, current_image_index
        image_files.append(filepath)

        # Evict the oldest if over max_images (also drops its original + crop entry)
        if len(image_files) > CONFIG["max_images"]:
            oldest = image_files.pop(0)
            _remove_image_files(oldest.name)

        current_image_index = len(image_files) - 1
        display_image_async(filepath)

        return {"message": "Image uploaded successfully", "filename": filename}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def _remove_image_files(filename: str):
    """Delete the display JPEG, thumbnail, original, and crop entry for `filename`."""
    (UPLOAD_DIR / filename).unlink(missing_ok=True)
    (THUMB_DIR / f"thumb_{filename}").unlink(missing_ok=True)
    (ORIGINALS_DIR / filename).unlink(missing_ok=True)
    with crops_lock:
        crops.pop(filename, None)
        _save_crops()


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
        cycle_wake.set()  # wake the cycle thread so it stops promptly
        return {"message": "Cycling stopped"}
    else:
        raise HTTPException(status_code=400, detail="Invalid action")


@app.post("/api/display/{index}")
async def display_image_by_index(index: int):
    """Display a specific image by index."""
    global current_image_index
    
    if not image_files:
        raise HTTPException(status_code=404, detail="No images available")
    
    if index < 0 or index >= len(image_files):
        raise HTTPException(status_code=404, detail="Image index out of range")
    
    # Update the current index immediately so /api/images reflects the
    # new selection on the next poll, and push to the e-ink in the background.
    current_image_index = index
    display_image_async(image_files[index])

    return {"message": "Image displayed", "index": index}


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


@app.delete("/api/images/{filename}")
async def delete_image(filename: str):
    """Delete a specific image (display JPEG, thumbnail, original, crop entry)."""
    global image_files, current_image_index

    image_path = UPLOAD_DIR / filename
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")

    try:
        idx = next(i for i, f in enumerate(image_files) if f.name == filename)
    except StopIteration:
        raise HTTPException(status_code=404, detail="Image not tracked")

    image_files.pop(idx)
    _remove_image_files(filename)

    if image_files:
        current_image_index = current_image_index % len(image_files)
    else:
        current_image_index = 0

    return {"message": "Image deleted", "filename": filename}


def _resolve_rotation(rotation, filename: str) -> int:
    """Pick the rotation to use for a request: explicit query param if given,
    otherwise the stored rotation."""
    if rotation is None:
        return _stored_rotation(filename)
    try:
        r = int(rotation) % 360
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="rotation must be an integer")
    if r not in (0, 90, 180, 270):
        raise HTTPException(status_code=400, detail="rotation must be 0/90/180/270")
    return r


@app.get("/api/original/{filename}")
async def get_original(filename: str, rotation: int = None):
    """Serve the stored original, optionally rotated. Used by the re-frame UI
    to preview rotations without persisting."""
    p = ORIGINALS_DIR / filename
    if not p.exists():
        raise HTTPException(status_code=404, detail="Original not found")
    r = _resolve_rotation(rotation, filename)
    if r == 0:
        return FileResponse(p)
    # Render rotated to a JPEG in memory. Originals are capped at 2000px so
    # this is fast (~50ms on a Pi 4).
    with Image.open(p) as src:
        if src.mode != "RGB":
            src = src.convert("RGB")
        out = _rotated(src, r)
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=92)
    from fastapi.responses import Response
    return Response(content=buf.getvalue(), media_type="image/jpeg")


@app.get("/api/crop/{filename}")
async def get_crop(filename: str):
    """Return the current crop + rotation + the (post-rotation) original dimensions."""
    original_path = ORIGINALS_DIR / filename
    if not original_path.exists():
        raise HTTPException(status_code=404, detail="Original not found")
    rotation = _stored_rotation(filename)
    with Image.open(original_path) as o:
        rotated = _rotated(o, rotation)
        ow, oh = rotated.size
    aspect = CONFIG["display_size"][0] / CONFIG["display_size"][1]
    stored = crops.get(filename)
    if stored:
        rect = {k: stored[k] for k in ("x", "y", "w", "h")}
    else:
        rect = _center_crop_rect((ow, oh), aspect)
    return {
        "crop": rect,
        "rotation": rotation,
        "original_size": {"w": ow, "h": oh},
        "display_size": list(CONFIG["display_size"]),
    }


@app.post("/api/crop/{filename}")
async def set_crop(filename: str, rect: dict):
    """Replace the crop (and optionally rotation) for `filename` and re-render.
    Body: {"x": int, "y": int, "w": int, "h": int, "rotation": int?} where x/y/w/h
    are in *rotated*-image pixel coordinates."""
    original_path = ORIGINALS_DIR / filename
    if not original_path.exists():
        raise HTTPException(status_code=404, detail="Original not found")
    for k in ("x", "y", "w", "h"):
        if k not in rect:
            raise HTTPException(status_code=400, detail=f"Missing field: {k}")
    if "rotation" in rect and int(rect["rotation"]) % 360 not in (0, 90, 180, 270):
        raise HTTPException(status_code=400, detail="rotation must be 0/90/180/270")

    new_display_path = apply_crop(filename, rect)

    # If the re-framed image is the one currently shown, push to the panel.
    if image_files and 0 <= current_image_index < len(image_files):
        if image_files[current_image_index].name == filename:
            display_image_async(new_display_path)

    return {"message": "Crop updated", "crop": crops[filename]}


@app.post("/api/autocrop/{filename}")
async def autocrop(filename: str, rotation: int = None):
    """Suggest a face-detection crop in the given rotation, without persisting.
    The client decides whether to keep it (POST /api/crop) or discard."""
    original_path = ORIGINALS_DIR / filename
    if not original_path.exists():
        raise HTTPException(status_code=404, detail="Original not found")
    r = _resolve_rotation(rotation, filename)
    rect = auto_crop_from_faces(original_path, r)
    return {"crop": rect, "rotation": r}


@app.get("/api/mock-frame")
async def get_mock_frame():
    """Get the current mock frame display."""
    mock_path = Path("mock_frame") / "current_display.jpg"
    if mock_path.exists():
        return FileResponse(mock_path)
    else:
        raise HTTPException(status_code=404, detail="Mock frame not available")


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
    <script src="/static/react.production.min.js"></script>
    <script src="/static/react-dom.production.min.js"></script>
    <script src="/static/babel.min.js"></script>
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
        .delete-btn {
            background-color: #e53935;
            font-size: 12px;
            padding: 4px 10px;
            margin-top: 6px;
            width: 100%;
        }
        .delete-btn:hover {
            background-color: #b71c1c;
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
        .mock-frame {
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            text-align: center;
        }
        .frame-container {
            display: inline-block;
            border: 8px solid #333;
            border-radius: 12px;
            background: #f5f5f5;
            padding: 10px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        .frame-display {
            max-width: 400px;
            max-height: 240px;
            width: auto;
            height: auto;
            border-radius: 4px;
            display: block;
        }
        .frame-placeholder {
            width: 400px;
            height: 240px;
            background: #e0e0e0;
            border-radius: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #666;
            font-size: 16px;
        }
        .reframe-btn {
            background-color: #6f42c1;
            font-size: 12px;
            padding: 4px 10px;
            margin-top: 6px;
            width: 100%;
        }
        .reframe-btn:hover { background-color: #553098; }
        .modal-overlay {
            position: fixed; inset: 0;
            background: rgba(0,0,0,0.6);
            display: flex; align-items: center; justify-content: center;
            z-index: 1000;
        }
        .modal {
            background: white; border-radius: 8px; padding: 20px;
            max-width: 92vw; max-height: 92vh;
            overflow: auto;
            box-shadow: 0 8px 32px rgba(0,0,0,0.4);
        }
        .modal h3 { margin-top: 0; }
        .crop-stage {
            position: relative;
            display: inline-block;
            user-select: none;
            background: #000;
            line-height: 0;
        }
        .crop-image {
            display: block;
            max-width: min(80vw, 720px);
            max-height: 60vh;
            width: auto; height: auto;
            -webkit-user-drag: none;
            user-select: none;
            pointer-events: none;
        }
        .crop-handle {
            position: absolute;
            width: 14px; height: 14px;
            background: white;
            border: 2px solid #4a90e2;
            border-radius: 2px;
            box-sizing: border-box;
        }
        .handle-nw { top: -8px;    left: -8px;   cursor: nwse-resize; }
        .handle-ne { top: -8px;    right: -8px;  cursor: nesw-resize; }
        .handle-sw { bottom: -8px; left: -8px;   cursor: nesw-resize; }
        .handle-se { bottom: -8px; right: -8px;  cursor: nwse-resize; }
        .modal-actions {
            display: flex; gap: 10px; margin-top: 16px; align-items: center;
        }
        .btn-secondary {
            background-color: white;
            color: #333;
            border: 1px solid #ccc;
        }
        .btn-secondary:hover { background-color: #f0f0f0; }
        .modal-hint {
            margin: 4px 0 12px; color: #666; font-size: 13px;
        }
        .modal-loading {
            padding: 60px 40px; text-align: center; color: #888;
        }
        .modal-actions button[title^="Rotate"] {
            font-size: 18px;
            line-height: 1;
            padding: 6px 12px;
            min-width: 38px;
        }
    </style>
</head>
<body>
    <div id="root"></div>
    <script type="text/babel">
        const { useState, useEffect, useCallback, useRef } = React;

        function ImageThumbnail({ image, isCurrent, thumbVersion, onClick, onDelete, onReframe }) {
            const [loading, setLoading] = useState(true);
            const [error, setError] = useState(false);

            return (
                <div
                    className={`image-item ${isCurrent ? 'current' : ''}`}
                    title={`Click to display ${image}`}
                >
                    {loading && !error && (
                        <div className="thumbnail-loading">Loading...</div>
                    )}
                    {error && (
                        <div className="thumbnail-loading">No preview</div>
                    )}
                    <img
                        src={`/api/thumbnail/${image}?v=${thumbVersion || 0}`}
                        alt={image}
                        className="thumbnail"
                        style={{ display: loading || error ? 'none' : 'block', cursor: 'pointer' }}
                        onLoad={() => setLoading(false)}
                        onError={() => {
                            setLoading(false);
                            setError(true);
                        }}
                        onClick={onClick}
                    />
                    <div className="image-name" onClick={onClick} style={{ cursor: 'pointer' }}>{image.replace(/^image_/, '').replace('.jpg', '')}</div>
                    {isCurrent && <div className="current-label">Currently Displayed</div>}
                    <button
                        className="reframe-btn"
                        onClick={(e) => { e.stopPropagation(); onReframe(image); }}
                    >Reframe</button>
                    <button
                        className="delete-btn"
                        onClick={(e) => { e.stopPropagation(); onDelete(image); }}
                    >Delete</button>
                </div>
            );
        }

        function CropEditor({ filename, onSave, onClose, onError }) {
            const [crop, setCrop] = useState(null);
            const [origSize, setOrigSize] = useState(null);
            const [dispSize, setDispSize] = useState([800, 480]);
            const [rotation, setRotation] = useState(0);
            const [scale, setScale] = useState(1);
            const [imgLoaded, setImgLoaded] = useState(false);
            const [saving, setSaving] = useState(false);
            const [autoframing, setAutoframing] = useState(false);
            const imgRef = useRef(null);
            const dragRef = useRef(null);

            useEffect(() => {
                let cancelled = false;
                fetch(`/api/crop/${filename}`)
                    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
                    .then(data => {
                        if (cancelled) return;
                        setCrop(data.crop);
                        setOrigSize(data.original_size);
                        setDispSize(data.display_size);
                        setRotation(data.rotation || 0);
                    })
                    .catch(e => { if (!cancelled) onError('Could not load crop info: ' + e.message); });
                return () => { cancelled = true; };
            }, [filename]);

            const aspect = dispSize[0] / dispSize[1];
            const MIN_W = Math.max(80, dispSize[0] * 0.1);

            const measure = useCallback(() => {
                if (imgRef.current && origSize) {
                    const r = imgRef.current.getBoundingClientRect();
                    if (r.width > 0) setScale(r.width / origSize.w);
                }
            }, [origSize]);
            useEffect(() => {
                measure();
                window.addEventListener('resize', measure);
                return () => window.removeEventListener('resize', measure);
            }, [measure]);

            const clampRect = (x, y, w, h) => {
                if (!origSize) return { x, y, w, h };
                // Aspect-lock: expand whichever dim is smaller (relative to aspect).
                if (w / h > aspect) h = w / aspect;
                else w = h * aspect;
                if (w < MIN_W) { w = MIN_W; h = w / aspect; }
                // Shrink to fit in the original
                if (w > origSize.w) { w = origSize.w; h = w / aspect; }
                if (h > origSize.h) { h = origSize.h; w = h * aspect; }
                x = Math.max(0, Math.min(origSize.w - w, x));
                y = Math.max(0, Math.min(origSize.h - h, y));
                return { x, y, w, h };
            };

            const onMove = (e) => {
                const d = dragRef.current;
                if (!d) return;
                if (d.mode === 'move') {
                    const dx = (e.clientX - d.startClient.x) / d.scale;
                    const dy = (e.clientY - d.startClient.y) / d.scale;
                    setCrop(clampRect(d.startCrop.x + dx, d.startCrop.y + dy,
                                       d.startCrop.w, d.startCrop.h));
                    return;
                }
                // Resize: anchor (opposite corner) stays fixed.
                const mx = (e.clientX - d.imgOrigin.x) / d.scale;
                const my = (e.clientY - d.imgOrigin.y) / d.scale;
                let w = Math.abs(mx - d.anchor.x);
                let h = Math.abs(my - d.anchor.y);
                // Expand smaller dim to keep aspect.
                if (w / h > aspect) h = w / aspect;
                else w = h * aspect;
                const x = d.mode.includes('w') ? d.anchor.x - w : d.anchor.x;
                const y = d.mode.includes('n') ? d.anchor.y - h : d.anchor.y;
                setCrop(clampRect(x, y, w, h));
            };

            const onUp = () => {
                dragRef.current = null;
                document.removeEventListener('pointermove', onMove);
                document.removeEventListener('pointerup', onUp);
            };

            const startDrag = (mode) => (e) => {
                e.preventDefault();
                e.stopPropagation();
                if (!crop || !imgRef.current || !origSize) return;
                const rect = imgRef.current.getBoundingClientRect();
                const s = rect.width / origSize.w;
                dragRef.current = {
                    mode,
                    startClient: { x: e.clientX, y: e.clientY },
                    startCrop: { ...crop },
                    scale: s,
                    imgOrigin: { x: rect.left, y: rect.top },
                    anchor: mode === 'move' ? null : {
                        x: mode.includes('w') ? crop.x + crop.w : crop.x,
                        y: mode.includes('n') ? crop.y + crop.h : crop.y,
                    },
                };
                document.addEventListener('pointermove', onMove);
                document.addEventListener('pointerup', onUp);
            };

            const save = async () => {
                if (!crop) return;
                setSaving(true);
                try {
                    const res = await fetch(`/api/crop/${filename}`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            x: Math.round(crop.x), y: Math.round(crop.y),
                            w: Math.round(crop.w), h: Math.round(crop.h),
                            rotation: rotation,
                        }),
                    });
                    if (!res.ok) throw new Error((await res.json()).detail || 'HTTP ' + res.status);
                    onSave();
                } catch (e) {
                    onError('Save failed: ' + e.message);
                    setSaving(false);
                }
            };

            const autoFrame = async () => {
                setAutoframing(true);
                try {
                    const res = await fetch(`/api/autocrop/${filename}?rotation=${rotation}`,
                                           {method: 'POST'});
                    if (!res.ok) throw new Error((await res.json()).detail || 'HTTP ' + res.status);
                    const data = await res.json();
                    setCrop(clampRect(data.crop.x, data.crop.y, data.crop.w, data.crop.h));
                } catch (e) {
                    onError('Auto-frame failed: ' + e.message);
                } finally {
                    setAutoframing(false);
                }
            };

            const rotate = (delta) => {
                if (!origSize) return;
                const newRotation = ((rotation + delta) % 360 + 360) % 360;
                const dimsSwap = (delta % 180) !== 0;
                const newOrigSize = dimsSwap
                    ? { w: origSize.h, h: origSize.w }
                    : { ...origSize };
                // The old crop rect doesn't survive a 90° spin under the 5:3
                // aspect lock, so snap back to a centered crop in the new frame.
                const center = (() => {
                    const a = dispSize[0] / dispSize[1];
                    const iw = newOrigSize.w, ih = newOrigSize.h;
                    let cw, ch;
                    if (iw / ih > a) { ch = ih; cw = ih * a; }
                    else             { cw = iw; ch = iw / a; }
                    return { x: Math.round((iw - cw) / 2), y: Math.round((ih - ch) / 2),
                             w: Math.round(cw), h: Math.round(ch) };
                })();
                setImgLoaded(false);   // hide overlay until the new image renders
                setRotation(newRotation);
                setOrigSize(newOrigSize);
                setCrop(center);
            };

            // Hide the overlay until the post-rotation image is actually visible —
            // otherwise the crop rect briefly anchors to the previous frame's pixels.
            const overlay = (crop && origSize && imgLoaded) ? {
                position: 'absolute',
                left: crop.x * scale, top: crop.y * scale,
                width: crop.w * scale, height: crop.h * scale,
                border: '2px solid #4a90e2',
                boxShadow: '0 0 0 9999px rgba(0,0,0,0.5)',
                cursor: 'move',
                boxSizing: 'border-box',
            } : null;

            // Dismiss on actual backdrop press (not on a drag release that
            // happens to end on the backdrop). Compare target to currentTarget
            // so only direct presses on the overlay count.
            const onBackdropDown = (e) => {
                if (e.target === e.currentTarget) onClose();
            };

            const loaded = crop && origSize;
            const busy = autoframing || saving;

            return (
                <div className="modal-overlay" onMouseDown={onBackdropDown}>
                    <div className="modal" onMouseDown={e => e.stopPropagation()}>
                        <h3>Re-frame {filename}</h3>
                        <p className="modal-hint">
                            Drag inside the rectangle to move it; drag corners to resize.
                            Aspect locked to {dispSize[0]}×{dispSize[1]}.
                        </p>
                        {loaded ? (
                            <div className="crop-stage">
                                <img ref={imgRef}
                                     src={`/api/original/${filename}?rotation=${rotation}`}
                                     alt={filename}
                                     className="crop-image"
                                     onLoad={() => { setImgLoaded(true); measure(); }}
                                     draggable={false} />
                                {overlay && (
                                    <div style={overlay} onPointerDown={startDrag('move')}>
                                        <div className="crop-handle handle-nw" onPointerDown={startDrag('nw')} />
                                        <div className="crop-handle handle-ne" onPointerDown={startDrag('ne')} />
                                        <div className="crop-handle handle-sw" onPointerDown={startDrag('sw')} />
                                        <div className="crop-handle handle-se" onPointerDown={startDrag('se')} />
                                    </div>
                                )}
                            </div>
                        ) : (
                            <div className="modal-loading">Loading original…</div>
                        )}
                        {loaded && (
                            <div className="modal-actions">
                                <button onClick={() => rotate(-90)} disabled={busy}
                                        title="Rotate left (counterclockwise)">↺</button>
                                <button onClick={() => rotate(90)} disabled={busy}
                                        title="Rotate right (clockwise)">↻</button>
                                <button onClick={autoFrame} disabled={busy}>
                                    {autoframing ? 'Detecting…' : 'Auto-frame faces'}
                                </button>
                                <div style={{flex: 1}} />
                                <button className="btn-secondary" onClick={onClose}>Cancel</button>
                                <button onClick={save} disabled={busy}>
                                    {saving ? 'Saving…' : 'Save'}
                                </button>
                            </div>
                        )}
                    </div>
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
            const [mockFrameKey, setMockFrameKey] = useState(0);
            const [thumbVersion, setThumbVersion] = useState(0);
            const [reframing, setReframing] = useState(null);

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
                    setCurrentIndex(prev => {
                        // Whenever the displayed image changes (cycle thread
                        // advance, or anything else), re-fetch the mock preview.
                        if (prev !== data.current_index) {
                            setMockFrameKey(k => k + 1);
                        }
                        return data.current_index;
                    });
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
                        // Refresh mock frame display
                        setMockFrameKey(prev => prev + 1);
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
                // Optimistic: mark the clicked thumbnail as current immediately.
                // The mock preview is rewritten synchronously by the server before
                // the response returns, so we wait for the POST to land before
                // bumping mockFrameKey — otherwise we'd re-fetch the stale file.
                setCurrentIndex(index);
                try {
                    await fetch(`/api/display/${index}`, { method: 'POST' });
                    setMockFrameKey(prev => prev + 1);
                    fetchImages();
                } catch (err) {
                    console.error('Error displaying image:', err);
                }
            };

            const deleteImage = async (filename) => {
                try {
                    const response = await fetch(`/api/images/${filename}`, { method: 'DELETE' });
                    if (response.ok) {
                        setStatus('Image deleted');
                        fetchImages();
                        setTimeout(() => setStatus(''), 3000);
                    } else {
                        const errorData = await response.json();
                        throw new Error(errorData.detail || 'Delete failed');
                    }
                } catch (err) {
                    setError(err.message || 'Error deleting image');
                    setTimeout(() => setError(''), 5000);
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

                    <div className="mock-frame">
                        <h3>Current Display (Mock Frame)</h3>
                        <div className="frame-container">
                            <img 
                                src={`/api/mock-frame?v=${mockFrameKey}`}
                                alt="Current frame display"
                                className="frame-display"
                                onError={(e) => {
                                    e.target.style.display = 'none';
                                    e.target.nextSibling.style.display = 'block';
                                }}
                            />
                            <div className="frame-placeholder" style={{ display: 'none' }}>
                                No image displayed
                            </div>
                        </div>
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
                                isCurrent={index === currentIndex}
                                thumbVersion={thumbVersion}
                                onClick={() => displayImage(index)}
                                onDelete={deleteImage}
                                onReframe={(name) => setReframing(name)}
                            />
                        ))}
                    </div>

                    {reframing && (
                        <CropEditor
                            filename={reframing}
                            onSave={() => {
                                setReframing(null);
                                setStatus('Crop saved');
                                setMockFrameKey(k => k + 1);
                                setThumbVersion(v => v + 1);
                                fetchImages();
                                setTimeout(() => setStatus(''), 3000);
                            }}
                            onClose={() => setReframing(null)}
                            onError={(msg) => {
                                setError(msg);
                                setTimeout(() => setError(''), 5000);
                            }}
                        />
                    )}
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
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PicInPlace is an e-ink picture frame application that allows users to upload images through a web interface and displays them on an e-ink display. The system automatically cycles through uploaded images and provides a React-based web frontend for control.

## Key Components

- **server.py**: Main FastAPI application serving both the web API and React frontend
- **main.py**: Simple entry point (currently just prints hello message)
- **config.json**: Runtime configuration for display settings and behavior
- **sys/picinplace.service**: Systemd service file for auto-boot functionality
- **install.sh**: Installation script that sets up the systemd service

## Development Commands

### Running the Application
```bash
# Start the server (development)
uv run server.py

# Or run with uvicorn directly
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

### Dependencies Management
```bash
# Install dependencies
uv sync

# Add new dependency
uv add package-name
```

### Service Management
```bash
# Install as systemd service
./install.sh

# Control service
systemctl --user start picinplace.service
systemctl --user stop picinplace.service
systemctl --user status picinplace.service

# View logs
journalctl --user -u picinplace.service --output cat -f
```

## Architecture

### API Endpoints
- `POST /api/upload`: Upload and process images (handles HEIC with pillow-heif)
- `GET /api/images`: List stored images and current display state
- `GET /api/config`: Get current configuration
- `POST /api/config`: Update configuration
- `POST /api/cycle/{action}`: Start/stop image cycling
- `GET /api/heic-support`: Check HEIC format support
- `GET /api/thumbnail/{filename}`: Get image thumbnails
- `GET /`: Serve embedded React frontend

### Image Processing Pipeline
1. Images uploaded via web interface or drag-and-drop
2. HEIC files converted using pillow-heif (if available)
3. Images resized and cropped to e-ink display dimensions (800x480)
4. Converted to RGB and saved as JPEG
5. Thumbnails generated for web interface
6. Old images automatically removed when max_images exceeded

### Configuration System
Configuration is stored in `config.json` and includes:
- `max_images`: Maximum stored images (default: 10)
- `cycle_interval`: Time between image changes in seconds (default: 600)
- `display_size`: E-ink display resolution [width, height] (default: [800, 480])
- `saturation`: Image saturation for e-ink display (default: 0.8)

### E-ink Display Integration
- Uses `inky.auto` for automatic e-ink display detection
- Includes mock display class for development without hardware
- Images processed with appropriate saturation for e-ink rendering

### Threading Architecture
- Main FastAPI server runs in primary thread
- Background image cycling runs in daemon thread
- Thread-safe global state management for current image index

## File Structure

- `/uploaded_images/`: Storage for processed images
- `/uploaded_images/thumbnails/`: Generated thumbnails for web interface
- Images named with timestamp: `image_YYYYMMDD_HHMMSS.jpg`

## Development Notes

- Frontend is embedded in server.py as inline HTML/JavaScript/CSS
- Uses React via CDN (no build process required)
- HEIC support optional (requires pillow-heif package)
- Service hardcoded to run from `/home/adam/code/picinplace` directory
- Configuration persists across restarts via config.json
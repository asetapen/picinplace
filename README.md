# PicInPlace

A tiny picture-frame app for a Raspberry Pi + [Pimoroni Inky Impression](https://shop.pimoroni.com/products/inky-impression-7-3) (or similar e-ink display). You upload photos via a web UI from any device on the network; the Pi resizes, stores, and cycles through them on the e-ink panel.

Built for a desk picture frame at work. Includes a `--mock` mode so the whole thing runs on a laptop without any hardware.

## What's in the box

- `server.py` — FastAPI app: API, embedded React UI, image processing, e-ink driver glue, cycling thread.
- `config.json` — runtime config (image count, cycle interval, display size, saturation).
- `install.sh` / `stop.sh` — install/uninstall the systemd user service on the Pi.
- `sys/picinplace.service` — the systemd unit.
- `uploaded_images/` — processed JPEGs + `thumbnails/`.
- `mock_frame/current_display.jpg` — full-color preview of whatever is currently on the e-ink (shown in the web UI).

## Running

### On a laptop (no hardware required)

```bash
uv sync
uv run server.py --mock
```

Open <http://localhost:8000>. The "Mock Frame" panel in the UI shows what would be on the e-ink, in full color. You can also set `PICINPLACE_MOCK=1` instead of passing the flag.

### On the Raspberry Pi (real e-ink)

The e-ink driver (`inky`) is an optional extra because its `spidev` dependency only builds on Linux.

```bash
uv sync --extra hardware
uv run server.py
```

Then point a browser at `http://<pi-hostname>.local:8000`.

## Web UI

- Drag-and-drop or click to upload an image (JPEG/PNG/GIF/WebP/HEIC).
- Click any thumbnail to push it to the display. The selection indicator updates instantly; the actual e-ink refresh happens in the background (e-ink panels take 15-30s to redraw).
- "Stop/Start Cycling" pauses the auto-rotation.
- Adjust max image count, cycle interval, and saturation, then "Update Configuration" to persist.

## Configuration (`config.json`)

| Key              | Default       | Meaning                                                          |
| ---------------- | ------------- | ---------------------------------------------------------------- |
| `max_images`     | `10`          | Oldest images are deleted past this count.                       |
| `cycle_interval` | `600`         | Seconds between auto-rotations.                                  |
| `display_size`   | `[800, 480]`  | Target resolution images are cropped/resized to.                 |
| `saturation`     | `0.5`         | Passed to the Inky driver. `0` = grayscale, `1` = full color. Only affects the real e-ink — the mock preview is always full color. |

## API quick reference

| Method | Path                       | Purpose                                  |
| ------ | -------------------------- | ---------------------------------------- |
| POST   | `/api/upload`              | Upload a file (multipart).               |
| GET    | `/api/images`              | List stored images + current index.      |
| POST   | `/api/display/{index}`     | Show image at index. Returns immediately. |
| POST   | `/api/cycle/{start\|stop}` | Toggle auto-rotation.                    |
| GET    | `/api/thumbnail/{name}`    | Get a 150×90 thumbnail.                  |
| DELETE | `/api/images/{name}`       | Delete an image.                         |
| GET    | `/api/config`              | Get current config.                      |
| POST   | `/api/config`              | Patch config (any subset of keys).       |
| GET    | `/api/mock-frame`          | Full-color preview JPEG.                 |
| GET    | `/api/heic-support`        | Reports whether HEIC decoding is enabled. |

## Auto-boot on the Pi (systemd user service)

```bash
./install.sh                                # installs and starts the user service
systemctl --user status picinplace.service
journalctl --user -u picinplace.service -f  # tail the logs
./stop.sh                                   # stop + disable
```

The unit hardcodes `/home/adam/code/picinplace` — edit `sys/picinplace.service` if your path differs. For the service to survive a Pi reboot without you logging in, enable lingering once:

```bash
sudo loginctl enable-linger $USER
```

## Running the Pi as a Wi-Fi access point

Useful at work where personal devices can't join the corporate network: have the Pi broadcast its own SSID so a phone can join it and upload pictures directly. The Pi has no internet uplink while in AP mode, which is fine here — it's just the frame talking to your phone.

These instructions assume **Raspberry Pi OS Bookworm or newer**, which uses NetworkManager by default. Check with `nmcli --version`; if `nmcli` is missing you're on an older release and should follow the legacy `hostapd` path linked at the bottom.

### One-time setup

```bash
# Create a persistent hotspot named "PicInPlace" on wlan0.
sudo nmcli connection add type wifi ifname wlan0 con-name picinplace-ap \
  autoconnect yes ssid PicInPlace
sudo nmcli connection modify picinplace-ap \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  ipv4.method shared \
  ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk 'pick-a-good-password'
# Make it the preferred connection so it wins over any saved Wi-Fi networks.
sudo nmcli connection modify picinplace-ap connection.autoconnect-priority 100
sudo nmcli connection up picinplace-ap
```

The Pi will be reachable at **http://10.42.0.1:8000** from any device joined to the `PicInPlace` SSID (NetworkManager's `ipv4.method shared` uses the `10.42.0.0/24` range and runs a built-in DHCP server).

### Going back to a normal Wi-Fi client

```bash
sudo nmcli connection down picinplace-ap
sudo nmcli connection modify picinplace-ap autoconnect no
# Then connect to a regular network:
sudo nmcli device wifi connect 'YourSSID' password 'yourpassword'
```

To re-enable the hotspot later: `sudo nmcli connection up picinplace-ap`.

### Notes & caveats

- The Pi's built-in Wi-Fi has a single radio, so you can't be a client and an AP at the same time. While the hotspot is up, the Pi has no internet — perfect for the desk-frame use case, awkward for OTA updates. Plug into ethernet (or briefly disable the hotspot) when you need to `apt update`.
- 5 GHz AP mode is restricted by regulatory domain on the Pi's chipset; the snippet above sticks to 2.4 GHz (`band bg`) which is the most universally supported.
- If you're on **Bullseye or older** (no NetworkManager), the legacy path is `hostapd` + `dnsmasq` + a static IP on `wlan0`. The official guide at <https://www.raspberrypi.com/documentation/computers/configuration.html#setting-up-a-routed-wireless-access-point> still works.

## Adding/changing dependencies

```bash
uv add somepackage                # runtime dep
uv add --optional hardware foo    # only installed with --extra hardware
uv sync                           # apply lockfile
```

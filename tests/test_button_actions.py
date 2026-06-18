"""Button A (play/pause) and button B (DO NOT DISTURB) logic.

Pure logic + PIL rendering, so this all runs on macOS; only the GPIO wiring
needs the Pi.
"""
from pathlib import Path

from PIL import Image

import server


def test_toggle_cycling_pauses_then_resumes(monkeypatch):
    started = []
    monkeypatch.setattr(server, "start_cycling", lambda: started.append(True))

    server.cycling_enabled = True
    server.cycle_wake.clear()
    server._toggle_cycling()
    assert server.cycling_enabled is False
    assert server.cycle_wake.is_set(), "pausing should wake the cycle thread"

    server._toggle_cycling()
    assert server.cycling_enabled is True
    assert started == [True], "resuming should (re)start the cycle thread"


def test_render_dnd_image_is_red_on_white():
    path = server._render_dnd_image()
    assert path.exists()

    img = Image.open(path).convert("RGB")
    assert img.size == tuple(server.CONFIG["display_size"])

    px = img.load()
    w, h = img.size
    # Corner is the white background.
    assert all(c > 245 for c in px[3, 3]), "background should be white"
    # The text contributes strong red pixels somewhere.
    has_red = any(
        px[x, y][0] > 180 and px[x, y][1] < 90 and px[x, y][2] < 90
        for x in range(0, w, 4)
        for y in range(0, h, 4)
    )
    assert has_red, "expected red DO NOT DISTURB text"


def test_toggle_dnd_enters_and_exits(monkeypatch):
    shown = []
    monkeypatch.setattr(server, "display_image", shown.append)
    server.image_files = [Path("a.jpg"), Path("b.jpg")]
    server.current_image_index = 1
    server.dnd_active = False

    server._toggle_dnd()  # enter
    assert server.dnd_active is True
    assert shown[-1] == server.DND_IMAGE_PATH
    assert server.DND_IMAGE_PATH.exists()

    server._toggle_dnd()  # exit -> back to the current photo
    assert server.dnd_active is False
    assert shown[-1] == server.image_files[1]


def test_dnd_holds_then_navigation_clears_it(monkeypatch):
    monkeypatch.setattr(server, "display_image", lambda _p: None)
    server.image_files = [Path(f"{i}.jpg") for i in range(3)]
    server.current_image_index = 0
    server.dnd_active = True

    server._show_relative(1)  # pressing C/D returns to photos
    assert server.dnd_active is False
    assert server.current_image_index == 1


def test_handle_press_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_toggle_cycling", lambda: calls.append("A"))
    monkeypatch.setattr(server, "_toggle_dnd", lambda: calls.append("B"))
    monkeypatch.setattr(server, "_show_relative", lambda d: calls.append(("nav", d)))

    for label in ("A", "B", "C", "D"):
        server._handle_press(label)

    assert calls == ["A", "B", ("nav", -1), ("nav", 1)]

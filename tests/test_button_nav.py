"""Button navigation logic (Inky Impression C/D buttons).

These exercise the pure index/timer logic on macOS; the GPIO wiring itself can
only be confirmed on the Pi.
"""
from pathlib import Path

import server


def _set_images(n):
    server.image_files = [Path(f"img_{i}.jpg") for i in range(n)]


def test_next_wraps_forward(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "display_image", calls.append)
    _set_images(3)
    server.current_image_index = 2
    server.cycle_wake.clear()

    server._show_relative(1)  # D = next

    assert server.current_image_index == 0
    assert calls == [server.image_files[0]]
    assert server.cycle_wake.is_set(), "manual nav must reset the cycle timer"


def test_prev_wraps_backward(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "display_image", calls.append)
    _set_images(3)
    server.current_image_index = 0

    server._show_relative(-1)  # C = previous

    assert server.current_image_index == 2
    assert calls == [server.image_files[2]]


def test_step_through_middle(monkeypatch):
    monkeypatch.setattr(server, "display_image", lambda _p: None)
    _set_images(4)
    server.current_image_index = 1

    server._show_relative(1)
    assert server.current_image_index == 2
    server._show_relative(-1)
    assert server.current_image_index == 1


def test_empty_library_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "display_image", calls.append)
    server.image_files = []
    server.current_image_index = 0

    server._show_relative(1)

    assert server.current_image_index == 0
    assert calls == []


def test_button_listener_noops_in_mock():
    # Tests run with PICINPLACE_MOCK=1, so MOCK_DISPLAY is True: no GPIO, no thread.
    assert server.MOCK_DISPLAY is True
    server.start_button_listener()  # must not raise
    assert server.button_thread is None

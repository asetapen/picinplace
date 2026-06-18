# Inky Button Navigation — Design

**Date:** 2026-06-17
**Status:** Approved
**Component:** `server.py`

## Goal

Add physical button navigation on the Inky Impression: button **C** shows the
previous picture, button **D** shows the next picture. A manual press resets the
auto-cycle timer so the frame doesn't jump again immediately.

## Hardware

- Raspberry Pi 5 / Bookworm.
- Inky Impression buttons (BCM pins): A=5, B=6, **C=16**, **D=24**. Active-low
  (internal pull-up, falling edge on press).
- GPIO accessed via `gpiod` + `gpiodevice` (libgpiod v2) — the stack Pimoroni's
  current Inky examples ship with; native on Pi 5.
- Only C and D are wired. A and B are left unused (YAGNI).

## Behavior

- **C → previous:** `index = (index - 1) % n`, wrapping.
- **D → next:** `index = (index + 1) % n`, wrapping.
- Empty library → no-op.
- A manual press resets the cycle countdown.

## Implementation

### Shared nav helper `_show_relative(delta)`
- Return immediately if `image_files` is empty.
- Under a new `nav_lock`: update `current_image_index`, reset the cycle timer
  (`cycle_wake.set()`), then `display_image()` the new picture.

### Resettable cycle timer
- New `cycle_wake = threading.Event()`.
- `cycle_images()` replaces `time.sleep(interval)` with
  `interrupted = cycle_wake.wait(timeout=interval); cycle_wake.clear()`.
  - If `interrupted` (manual nav, or stop) → restart the wait without advancing.
  - Otherwise advance + display as today.
- `/api/cycle/stop` sets `cycle_wake` so the thread notices promptly instead of
  sleeping up to the full interval.

### Button listener
- `start_button_listener()` is called from `startup_event` after `start_cycling()`.
- Lazily imports `gpiod`/`gpiodevice`. If `MOCK_DISPLAY` is set or setup fails
  (dev machine, no buttons) → log and return; nothing else is affected.
- Daemon thread blocks on `request.read_edge_events()`; maps
  C→`_show_relative(-1)`, D→`_show_relative(+1)` with ~200 ms software debounce
  per button.

## Concurrency

- `nav_lock` serializes index update + display between the button thread and the
  cycle thread (both now write `current_image_index`).
- E-ink writes stay serialized by the existing `display_lock`.

## Out of scope

- A/B buttons.
- Web `/api/display/{index}` keeps its current behavior (does not reset the timer);
  only the physical buttons do.

## Testing

- **Unit (macOS / `--mock`):** `_show_relative` forward/back wrap, empty-list
  guard, timer-reset event set; `start_button_listener()` no-ops under `--mock`.
- **Hardware (Pi):** press C/D and confirm via
  `journalctl --user -u picinplace.service --output cat -f`.

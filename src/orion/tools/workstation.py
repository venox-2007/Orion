"""Windows workstation session and media control tools for ORION (Step 21)."""

import ctypes
import logging

logger = logging.getLogger("orion.tools.workstation")

# Windows Virtual Key Codes for Media & Volume
VK_VOLUME_MUTE = 0xAD       # 173
VK_VOLUME_DOWN = 0xAE       # 174
VK_VOLUME_UP = 0xAF         # 175
VK_MEDIA_NEXT_TRACK = 0xB0  # 176
VK_MEDIA_PREV_TRACK = 0xB1  # 177
VK_MEDIA_PLAY_PAUSE = 0xB3  # 179
KEYEVENTF_KEYUP = 0x0002


def _press_key(vk_code: int) -> None:
    """Simulate key press and release using keybd_event."""
    ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)


def lock_workstation() -> bool:
    """Lock the Windows workstation session."""
    res = ctypes.windll.user32.LockWorkStation()
    if not res:
        raise OSError("LockWorkStation failed")
    return True


def toggle_play_pause() -> bool:
    """Toggle media playback (play/pause)."""
    _press_key(VK_MEDIA_PLAY_PAUSE)
    return True


def next_track() -> bool:
    """Skip to next media track."""
    _press_key(VK_MEDIA_NEXT_TRACK)
    return True


def previous_track() -> bool:
    """Return to previous media track."""
    _press_key(VK_MEDIA_PREV_TRACK)
    return True


def toggle_mute() -> bool:
    """Toggle audio mute state."""
    _press_key(VK_VOLUME_MUTE)
    return True


def adjust_volume(direction: str, steps: int = 5) -> bool:
    """Adjust system volume up or down in discrete steps."""
    d = direction.strip().lower()
    if d not in ("up", "down"):
        raise ValueError(f"Invalid volume direction '{direction}'. Must be 'up' or 'down'.")
    vk = VK_VOLUME_UP if d == "up" else VK_VOLUME_DOWN
    for _ in range(max(1, steps)):
        _press_key(vk)
    return True

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

# Display & Monitor Power Constants (Step 23)
HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_OFF = 2
MONITOR_ON = -1


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


def set_volume_percentage(level: int) -> bool:
    """Set absolute system volume to a percentage (0-100)."""
    level = max(0, min(100, int(level)))
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(level / 100.0, None)
        return True
    except Exception:
        # Fallback with ctypes keybd_event:
        # First zero out volume by pressing VK_VOLUME_DOWN 50 times (each step is 2%)
        for _ in range(50):
            _press_key(VK_VOLUME_DOWN)
        # Then press VK_VOLUME_UP level // 2 times to reach the desired percentage
        for _ in range(level // 2):
            _press_key(VK_VOLUME_UP)
        return True


def turn_off_screen() -> bool:
    """Turn off the display monitor (Step 23)."""
    ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF)
    return True


def sleep_system() -> bool:
    """Put the Windows machine into sleep mode (Step 23)."""
    ctypes.windll.PowrProf.SetSuspendState(0, 1, 0)
    return True


def open_browser_url(url: str) -> bool:
    """Open a URL in the user's default web browser (Step 23)."""
    import webbrowser

    clean_url = url.strip()
    if not clean_url.startswith(("http://", "https://")):
        clean_url = f"https://{clean_url}"
    webbrowser.open(clean_url)
    return True


def open_web_search(query: str, engine: str = "google") -> bool:
    """Search Google or YouTube in the user's default browser (Step 23)."""
    import urllib.parse
    import webbrowser

    clean_query = query.strip()
    encoded = urllib.parse.quote_plus(clean_query)
    if engine.strip().lower() == "youtube":
        search_url = f"https://www.youtube.com/results?search_query={encoded}"
    else:
        search_url = f"https://www.google.com/search?q={encoded}"
    webbrowser.open(search_url)
    return True

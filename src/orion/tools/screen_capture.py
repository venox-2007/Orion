"""Desktop screenshot capture tool for ORION (Step 20)."""

import io
import logging
from PIL import ImageGrab

logger = logging.getLogger("orion.tools.screen_capture")


class ScreenCaptureError(Exception):
    """Raised when desktop screen capture fails."""
    pass


def take_desktop_screenshot(quality: int = 85) -> bytes:
    """Capture a screenshot of the Windows desktop and return it as JPEG bytes."""
    try:
        # Capture primary or virtual display
        img = ImageGrab.grab(all_screens=True)
        # Convert to RGB to ensure JPEG compatibility
        img_rgb = img.convert("RGB")

        buffer = io.BytesIO()
        img_rgb.save(buffer, format="JPEG", quality=quality, optimize=True)
        return buffer.getvalue()
    except Exception as e:
        logger.warning("Screen capture failed: %s", e)
        raise ScreenCaptureError(f"Failed to capture screen: {e}") from e

"""
Multi-monitor screenshot helper.

The tricky part is keeping THREE coordinate systems straight:

  1. PHYSICAL  — what mss.grab() returns. Real GPU pixels. e.g. 2560x1440.
  2. LOGICAL   — what the OS / Qt cursor uses after DPI scaling. e.g. 1707x960
                 on a 150%-DPI 2560x1440 display.
  3. DOWNSCALED — what we send to the LLM (1280-wide JPEG to keep tokens low).

The overlay plots in LOGICAL coordinates. The element-detector model sees the
downscaled image. So `detect_element` must return coords in LOGICAL space, with
the monitor's logical-origin offset applied for multi-monitor setups.

ScreenShot now carries every number needed to convert between them.
"""

import base64
import ctypes
import io
from dataclasses import dataclass
from typing import List

import mss
import mss.tools
from PIL import Image


@dataclass
class ScreenShot:
    index: int

    # Downscaled image actually sent to the LLM
    width: int            # downscaled width (pixels in JPEG)
    height: int           # downscaled height
    base64_jpeg: str

    # Real (physical) monitor size and origin in mss virtual-screen coords
    physical_width: int
    physical_height: int
    physical_left: int    # mss-reported origin (physical px)
    physical_top: int

    # DPI scale (physical / logical). 1.0 on normal displays, 1.5 on 150% DPI.
    dpi_scale: float

    # Convenience: where this monitor's top-left sits in LOGICAL screen space
    logical_left: int
    logical_top: int


def _query_dpi_scale() -> float:
    """Best-effort DPI scale for the primary monitor.
    Uses GetDpiForMonitor (per-monitor, modern) with
    GetDpiForSystem as fallback. Returns 1.0 if all else fails."""
    try:
        # Try per-monitor DPI first (most accurate on Win 8.1+)
        shcore = ctypes.windll.shcore
        user32 = ctypes.windll.user32
        pt = ctypes.wintypes.POINT(0, 0)
        hmon = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
        dx = ctypes.c_uint()
        dy = ctypes.c_uint()
        if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return max(1.0, dx.value / 96.0)
    except Exception:
        pass
    try:
        # Fallback: system DPI (primary monitor only)
        u = ctypes.windll.user32
        gdfs = getattr(u, "GetDpiForSystem", None)
        if gdfs:
            return max(1.0, gdfs() / 96.0)
    except Exception:
        pass
    return 1.0


# ponytail: 960px JPEG cuts vision processing ~44% with negligible quality loss
def capture_all_screens(max_width: int = 960) -> List[ScreenShot]:
    """Capture all monitors. Each ScreenShot carries everything needed
    to convert detection coords back into logical screen space."""
    dpi = _query_dpi_scale()
    results = []
    with mss.mss() as sct:
        # mss monitor index 0 is the combined virtual screen; 1+ are real monitors
        for i, monitor in enumerate(sct.monitors[1:], start=1):
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

            phys_w, phys_h = img.width, img.height
            phys_left = int(monitor.get("left", 0))
            phys_top  = int(monitor.get("top",  0))

            # Downscale only the JPEG we send to the LLM — keep physical numbers intact
            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize(
                    (max_width, int(img.height * ratio)),
                    Image.Resampling.LANCZOS,
                )

            buf = io.BytesIO()
            # ponytail: quality 50 reduces payload ~40%, LLM vision sees the same UI
            img.save(buf, format="JPEG", quality=50, optimize=True)
            encoded = base64.b64encode(buf.getvalue()).decode("utf-8")

            results.append(ScreenShot(
                index=i,
                width=img.width,
                height=img.height,
                base64_jpeg=encoded,
                physical_width=phys_w,
                physical_height=phys_h,
                physical_left=phys_left,
                physical_top=phys_top,
                dpi_scale=dpi,
                logical_left=int(round(phys_left / dpi)),
                logical_top=int(round(phys_top  / dpi)),
            ))
            if i == 1:
                print(f"[capture] monitor={i} phys={phys_w}x{phys_h} "
                      f"origin=({phys_left},{phys_top}) "
                      f"jpeg={img.width}x{img.height} dpi_scale={dpi:.2f} "
                      f"logical_origin=({results[-1].logical_left},{results[-1].logical_top})")

    return results


def capture_primary() -> ScreenShot:
    """Capture only the primary monitor."""
    screens = capture_all_screens()
    return screens[0] if screens else None


def screen_count() -> int:
    with mss.mss() as sct:
        return len(sct.monitors) - 1  # subtract virtual combined monitor

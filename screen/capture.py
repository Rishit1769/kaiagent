"""
Multi-monitor screenshot helper.

The tricky part is keeping the coordinate systems straight:

  1. PHYSICAL  — what mss.grab() returns. Real GPU pixels. e.g. 2560x1440.
  2. LOGICAL   — what the OS / Qt cursor uses after DPI scaling. e.g. 1707x960
                 on a 150%-DPI 2560x1440 display.

The overlay plots in LOGICAL coordinates. The screenshot capture stores the
monitor's PHYSICAL geometry plus its DPI scale so we can map detections back
into LOGICAL screen space accurately on single- and multi-monitor setups.
"""

import base64
import ctypes
import io
from dataclasses import dataclass
from typing import Dict, List, Optional

import mss
from PIL import Image


MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 0x00000001
PROCESS_PER_MONITOR_DPI_AWARE = 2


@dataclass
class ScreenShot:
    index: int
    is_primary: bool

    # JPEG actually sent to the LLM / stored downstream
    width: int
    height: int
    base64_jpeg: str

    # Real (physical) monitor size and origin in mss virtual-screen coords
    physical_width: int
    physical_height: int
    physical_left: int
    physical_top: int

    # DPI scale (physical / logical). 1.0 on normal displays, 1.5 on 150% DPI.
    dpi_scale: float

    # Convenience: where this monitor's top-left sits in LOGICAL screen space
    logical_left: int
    logical_top: int


def enable_dpi_awareness() -> None:
    """Enable the highest DPI-awareness mode Windows offers for this process."""
    if getattr(enable_dpi_awareness, "_done", False):
        return
    try:
        user32 = ctypes.windll.user32
        awareness_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if awareness_context:
            # -4 is DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            if awareness_context(ctypes.c_void_p(-4)):
                enable_dpi_awareness._done = True
                print("[capture] DPI awareness enabled: per-monitor v2")
                return
    except Exception:
        pass
    try:
        shcore = ctypes.windll.shcore
        if shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            enable_dpi_awareness._done = True
            print("[capture] DPI awareness enabled: per-monitor")
            return
    except Exception:
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            print("[capture] DPI awareness enabled: system aware")
    except Exception:
        pass
    enable_dpi_awareness._done = True


def _get_monitor_handle(left: int, top: int, width: int, height: int):
    center_x = int(left + width / 2)
    center_y = int(top + height / 2)
    pt = ctypes.wintypes.POINT(center_x, center_y)
    return ctypes.windll.user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)


def _query_system_dpi_scale() -> float:
    """System-level DPI fallback when per-monitor lookup is unavailable."""
    try:
        user32 = ctypes.windll.user32
        get_dpi_for_system = getattr(user32, "GetDpiForSystem", None)
        if get_dpi_for_system:
            return max(1.0, get_dpi_for_system() / 96.0)
    except Exception:
        pass
    try:
        dc = ctypes.windll.user32.GetDC(0)
        if dc:
            dpi_x = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)
            ctypes.windll.user32.ReleaseDC(0, dc)
            if dpi_x:
                return max(1.0, dpi_x / 96.0)
    except Exception:
        pass
    return 1.0


def _query_monitor_dpi_scale(left: int, top: int, width: int, height: int) -> float:
    """Best-effort DPI scale for the monitor covering the given rectangle."""
    hmon = None
    try:
        hmon = _get_monitor_handle(left, top, width, height)
        shcore = ctypes.windll.shcore
        dx = ctypes.c_uint()
        dy = ctypes.c_uint()
        if hmon and shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
            return max(1.0, dx.value / 96.0)
    except Exception:
        pass
    return _query_system_dpi_scale()


def _is_primary_monitor(left: int, top: int, width: int, height: int) -> bool:
    try:
        hmon = _get_monitor_handle(left, top, width, height)
        if not hmon:
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.c_ulong),
            ]

        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return bool(info.dwFlags & MONITORINFOF_PRIMARY)
    except Exception:
        pass
    return left == 0 and top == 0


def detect_monitors() -> List[Dict[str, float]]:
    """Return physical geometry + DPI metadata for every detected monitor."""
    enable_dpi_awareness()
    monitors: List[Dict[str, float]] = []
    with mss.mss() as sct:
        for i, monitor in enumerate(sct.monitors[1:], start=1):
            width = int(monitor["width"])
            height = int(monitor["height"])
            left = int(monitor.get("left", 0))
            top = int(monitor.get("top", 0))
            dpi_scale = _query_monitor_dpi_scale(left, top, width, height)
            is_primary = _is_primary_monitor(left, top, width, height)
            info = {
                "index": i,
                "width": width,
                "height": height,
                "left": left,
                "top": top,
                "dpi_scale": dpi_scale,
                "is_primary": is_primary,
                "logical_left": int(round(left / dpi_scale)),
                "logical_top": int(round(top / dpi_scale)),
            }
            monitors.append(info)
            print(
                "[capture] detected monitor="
                f"{i} primary={is_primary} size={width}x{height} "
                f"dpi_scale={dpi_scale:.2f} origin=({left},{top})"
            )
    return monitors


def _encode_image(img: Image.Image, jpeg_quality: int) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def capture_all_screens(
    max_width: Optional[int] = None,
    jpeg_quality: int = 90,
) -> List[ScreenShot]:
    """Capture all monitors. Each ScreenShot carries everything needed
    to convert detection coords back into logical screen space."""
    enable_dpi_awareness()
    monitor_meta = {int(m["index"]): m for m in detect_monitors()}
    results = []
    with mss.mss() as sct:
        for i, monitor in enumerate(sct.monitors[1:], start=1):
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            meta = monitor_meta.get(i, {})

            phys_w, phys_h = img.width, img.height
            phys_left = int(monitor.get("left", 0))
            phys_top = int(monitor.get("top", 0))
            dpi = float(meta.get("dpi_scale", _query_system_dpi_scale()))
            logical_left = int(meta.get("logical_left", round(phys_left / dpi)))
            logical_top = int(meta.get("logical_top", round(phys_top / dpi)))
            is_primary = bool(meta.get("is_primary", False))

            encoded_img = img
            if max_width is not None and img.width > max_width:
                ratio = max_width / img.width
                encoded_img = img.resize(
                    (max_width, int(img.height * ratio)),
                    Image.Resampling.LANCZOS,
                )

            encoded = _encode_image(encoded_img, jpeg_quality=jpeg_quality)

            results.append(ScreenShot(
                index=i,
                is_primary=is_primary,
                width=encoded_img.width,
                height=encoded_img.height,
                base64_jpeg=encoded,
                physical_width=phys_w,
                physical_height=phys_h,
                physical_left=phys_left,
                physical_top=phys_top,
                dpi_scale=dpi,
                logical_left=logical_left,
                logical_top=logical_top,
            ))
            print(
                "[capture] captured monitor="
                f"{i} primary={is_primary} expected={phys_w}x{phys_h} "
                f"captured_shape={raw.height}x{raw.width} encoded={encoded_img.width}x{encoded_img.height} "
                f"dpi_scale={dpi:.2f} origin=({phys_left},{phys_top}) "
                f"logical_origin=({logical_left},{logical_top})"
            )

    return results


def capture_primary(
    max_width: Optional[int] = None,
    jpeg_quality: int = 90,
) -> Optional[ScreenShot]:
    """Capture the Windows primary monitor at native resolution by default."""
    screens = capture_all_screens(max_width=max_width, jpeg_quality=jpeg_quality)
    for screen in screens:
        if screen.is_primary:
            print(f"[capture] using primary monitor index={screen.index}")
            return screen
    if screens:
        print(f"[capture] primary monitor not flagged; falling back to index={screens[0].index}")
        return screens[0]
    return None


def capture_virtual_screen(
    max_width: Optional[int] = None,
    jpeg_quality: int = 90,
) -> Optional[ScreenShot]:
    """Capture the full virtual desktop spanning every monitor."""
    enable_dpi_awareness()
    with mss.mss() as sct:
        virtual = sct.monitors[0]
        raw = sct.grab(virtual)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        dpi = _query_system_dpi_scale()
        encoded_img = img
        if max_width is not None and img.width > max_width:
            ratio = max_width / img.width
            encoded_img = img.resize(
                (max_width, int(img.height * ratio)),
                Image.Resampling.LANCZOS,
            )
        encoded = _encode_image(encoded_img, jpeg_quality=jpeg_quality)
        shot = ScreenShot(
            index=0,
            is_primary=False,
            width=encoded_img.width,
            height=encoded_img.height,
            base64_jpeg=encoded,
            physical_width=img.width,
            physical_height=img.height,
            physical_left=int(virtual.get("left", 0)),
            physical_top=int(virtual.get("top", 0)),
            dpi_scale=dpi,
            logical_left=int(round(int(virtual.get("left", 0)) / dpi)),
            logical_top=int(round(int(virtual.get("top", 0)) / dpi)),
        )
        print(
            "[capture] captured virtual-screen expected="
            f"{img.width}x{img.height} captured_shape={raw.height}x{raw.width} "
            f"encoded={encoded_img.width}x{encoded_img.height} dpi_scale={dpi:.2f}"
        )
        return shot


def screen_count() -> int:
    enable_dpi_awareness()
    with mss.mss() as sct:
        return len(sct.monitors) - 1  # subtract virtual combined monitor

import base64
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


def _debug_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    out = Path(base) / "Kai Agent" / "debug_ai_view"
    out.mkdir(parents=True, exist_ok=True)
    return out


def save_model_visible_image(
    image_b64: str,
    *,
    context: str,
    index: int = 1,
    metadata: Optional[dict] = None,
) -> Path:
    """Persist the exact base64 image payload about to be sent to a model."""
    out_dir = _debug_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    jpeg_bytes = base64.b64decode(image_b64)

    latest_path = out_dir / f"latest-{context}-{index}.jpg"
    stamped_path = out_dir / f"{stamp}-{context}-{index}.jpg"
    latest_path.write_bytes(jpeg_bytes)
    stamped_path.write_bytes(jpeg_bytes)

    if metadata:
        meta_text = "\n".join(f"{k}={v}" for k, v in metadata.items()) + "\n"
        (out_dir / f"latest-{context}-{index}.txt").write_text(meta_text, encoding="utf-8")
        (out_dir / f"{stamp}-{context}-{index}.txt").write_text(meta_text, encoding="utf-8")

    print(f"[capture] saved model-visible image: {stamped_path}")
    return stamped_path

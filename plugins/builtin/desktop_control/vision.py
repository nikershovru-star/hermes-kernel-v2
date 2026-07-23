"""plugins/builtin/desktop_control/vision.py — DesktopVision CV layer (ADR-017).

Computer-vision layer for desktop automation: OCR + UI element detection.
All heavy deps (pytesseract / opencv / Pillow) are imported **lazily** so the
module imports without them installed; calling a method without the dep raises
a clear ``RuntimeError``. No kernel internals imported — pure CV (axis clean).

AXIS CONTRACT: depends on kernel.domain only (for UIElement typing). Never
imports kernel.bus / kernel.events / plugins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from kernel.domain import BaseEntity  # type: ignore[import-not-found]

logger = logging.getLogger("hermes.desktop.vision")


@dataclass
class UIElement:
    """A detected clickable UI element (bounding box + label)."""

    label: str | None
    bbox: tuple[int, int, int, int]  # x, y, w, h
    confidence: float
    type: str = "unknown"


def _require_pillow():
    try:
        from PIL import Image  # noqa: PLC0415  # type: ignore
    except ImportError as exc:  # pragma: no cover - host install dependent
        raise RuntimeError(
            "DesktopVision requires Pillow; install the 'desktop-vision' extra:\n"
            "  pip install 'hermes-kernel-v2[desktop-vision]'"
        ) from exc
    return Image


def _require_tesseract():
    try:
        import pytesseract  # noqa: PLC0415  # type: ignore
    except ImportError as exc:  # pragma: no cover - host install dependent
        raise RuntimeError(
            "DesktopVision.ocr requires pytesseract; install the 'desktop-vision' "
            "extra:\n  pip install 'hermes-kernel-v2[desktop-vision]'"
        ) from exc
    return pytesseract


def _require_cv2():
    try:
        import cv2  # noqa: PLC0415  # type: ignore
    except ImportError as exc:  # pragma: no cover - host install dependent
        raise RuntimeError(
            "DesktopVision.detect_elements requires opencv-python; install the "
            "'desktop-vision' extra:\n  pip install 'hermes-kernel-v2[desktop-vision]'"
        ) from exc
    return cv2


class DesktopVision:
    """OCR + UI element detection for desktop screenshots (ADR-017)."""

    async def ocr(self, image: Any) -> str:
        """Extract text from a screenshot (PIL.Image or raw bytes)."""
        Image = _require_pillow()
        pytesseract = _require_tesseract()
        if isinstance(image, bytes):
            import io

            img = Image.open(io.BytesIO(image))
        else:
            img = image
        return pytesseract.image_to_string(img).strip()

    async def detect_elements(self, image: Any) -> list[UIElement]:
        """Detect clickable UI elements.

        For v2.3.0 the lightweight path uses OCR: any text region is treated as
        a clickable element (bounding box from Tesseract). If opencv is installed
        a template/edge pass could refine boxes, but the v2.3.0 baseline is
        OCR-driven (documented honestly in ADR-017).
        """
        Image = _require_pillow()
        pytesseract = _require_tesseract()
        if isinstance(image, bytes):
            import io

            img = Image.open(io.BytesIO(image))
        else:
            img = image
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        elements: list[UIElement] = []
        n = len(data.get("text", []))
        for i in range(n):
            text = data["text"][i].strip()
            conf = int(data["conf"][i])
            if not text or conf < 0:
                continue
            x, y, w, h = (
                int(data["left"][i]),
                int(data["top"][i]),
                int(data["width"][i]),
                int(data["height"][i]),
            )
            elements.append(UIElement(label=text, bbox=(x, y, w, h), confidence=conf / 100.0))
        return elements

    async def find_element(self, image: Any, label: str) -> UIElement | None:
        """Find an element by fuzzy label match; returns best confidence hit."""
        import difflib

        elements = await self.detect_elements(image)
        if not elements:
            return None
        best: UIElement | None = None
        best_score = 0.0
        for el in elements:
            if not el.label:
                continue
            score = difflib.SequenceMatcher(None, el.label.lower(), label.lower()).ratio()
            if score > best_score:
                best_score = score
                best = el
        # require a reasonable match
        return best if (best is not None and best_score >= 0.6) else None

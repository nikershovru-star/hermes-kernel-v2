"""tests/test_desktop_vision.py — DesktopVision CV layer (ADR-017).

tesseract/opencv are optional and likely absent in CI, so we mock the heavy
imports and verify the vision logic (OCR string return, element detection from
mocked OCR dict, fuzzy find_element). When deps ARE present a synthetic image
path is exercised; otherwise the import-error path is asserted.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.builtin.desktop_control.vision import DesktopVision, UIElement


def _fake_vision_modules() -> dict:
    """Build fake pytesseract + PIL + cv2 modules for injection."""
    pytesseract = MagicMock()
    pytesseract.image_to_string.return_value = "Submit\nCancel"
    pytesseract.image_to_data.return_value = {
        "text": ["Submit", "Cancel"],
        "conf": [95, 88],
        "left": [10, 80],
        "top": [10, 80],
        "width": [50, 50],
        "height": [20, 20],
    }
    pytesseract.Output = MagicMock()
    pytesseract.Output.DICT = "dict"

    PIL = types.ModuleType("PIL")
    image_mod = MagicMock()
    image_mod.open.return_value = "IMG"
    PIL.Image = image_mod

    cv2 = MagicMock()
    return {"pytesseract": pytesseract, "PIL": PIL, "cv2": cv2}


@pytest.mark.asyncio
async def test_ocr_returns_text() -> None:
    fakes = _fake_vision_modules()
    with patch.dict(sys.modules, fakes):
        v = DesktopVision()
        text = await v.ocr("fake-bytes")
    assert text == "Submit\nCancel"


@pytest.mark.asyncio
async def test_detect_elements_from_ocr() -> None:
    fakes = _fake_vision_modules()
    with patch.dict(sys.modules, fakes):
        v = DesktopVision()
        els = await v.detect_elements("fake-bytes")
    assert len(els) == 2
    assert els[0].label == "Submit"
    assert els[0].bbox == (10, 10, 50, 20)
    assert els[0].confidence == 0.95


@pytest.mark.asyncio
async def test_find_element_fuzzy_match() -> None:
    fakes = _fake_vision_modules()
    with patch.dict(sys.modules, fakes):
        v = DesktopVision()
        el = await v.find_element("fake-bytes", "submit")
    assert el is not None
    assert el.label == "Submit"


@pytest.mark.asyncio
async def test_find_element_no_match_returns_none() -> None:
    fakes = _fake_vision_modules()
    with patch.dict(sys.modules, fakes):
        v = DesktopVision()
        el = await v.find_element("fake-bytes", "zzz-nonexistent")
    assert el is None


@pytest.mark.asyncio
async def test_missing_tesseract_raises_runtime_error() -> None:
    # remove pytesseract from sys.modules view for the import guard
    real = sys.modules.pop("pytesseract", None)
    try:
        with patch.dict(sys.modules, {"pytesseract": None}):
            v = DesktopVision()
            with pytest.raises(RuntimeError, match="pytesseract"):
                await v.ocr("x")
    finally:
        if real is not None:
            sys.modules["pytesseract"] = real


def test_uielement_dataclass() -> None:
    el = UIElement(label="OK", bbox=(1, 2, 3, 4), confidence=0.9, type="button")
    assert el.label == "OK" and el.bbox == (1, 2, 3, 4) and el.type == "button"


@pytest.mark.asyncio
async def test_detect_elements_empty_ocr() -> None:
    fakes = _fake_vision_modules()
    fakes["pytesseract"].image_to_data.return_value = {
        "text": [""], "conf": [-1], "left": [0], "top": [0], "width": [0], "height": [0]
    }
    with patch.dict(sys.modules, fakes):
        v = DesktopVision()
        els = await v.detect_elements("fake")
    assert els == []

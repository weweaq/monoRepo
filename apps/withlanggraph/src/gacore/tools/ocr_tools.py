"""OCR tools for gacore: local image-to-text via rapidocr-onnxruntime.

Port of GA's memory/ocr_utils.py into @tool form. The engine is lazy-loaded on first
call (~1s per image, Chinese+English, returns bbox + confidence).

Pitfalls learned from GA (preserved here so they don't bite again):
- rapid result[i][2] conf is str, not float — coerce with float()
- rapid returns None (not empty list) when no text is found
- enhance (upscale + contrast) harms clear text — default off
"""

from __future__ import annotations

import re
from typing import Any, Final

from langchain_core.tools import tool

from gacore.jsonl_logger import get_logger

logger = get_logger("tools.ocr")

_LANG: Final = "zh-Hans-CN"
_rapid_engine: object | None = None

_STRIP_CJK_RE: Final = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")


def _get_engine() -> object:
    global _rapid_engine
    if _rapid_engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _rapid_engine = RapidOCR()
    return _rapid_engine


def _strip_cjk_spaces(text: str) -> str:
    return _STRIP_CJK_RE.sub("", text)


def _ocr_rapid(image_path: str) -> dict[str, Any]:
    try:
        import numpy as np
        from PIL import Image

        engine = _get_engine()
        arr = np.array(Image.open(image_path).convert("RGB"))
        result, elapse = engine(arr)
        if not result:
            logger.info("ocr_image: no text found", path=image_path, lines=0)
            return {"text": "", "lines": [], "details": []}
        lines = [r[1] for r in result]
        details = [{"bbox": r[0], "text": r[1], "conf": float(r[2])} for r in result]
        text = _strip_cjk_spaces("\n".join(lines))
        logger.info(
            "ocr_image success",
            path=image_path,
            text_length=len(text),
            lines=len(lines),
            elapsed_ms=round(sum(elapse) * 1000, 1) if isinstance(elapse, (list, tuple)) else None,
        )
        return {"text": text, "lines": [_strip_cjk_spaces(line) for line in lines], "details": details}
    except Exception as exc:
        logger.error(
            "ocr_image failed",
            error_type=type(exc).__name__,
            stack_trace=str(exc),
            context={"path": image_path},
        )
        raise


@tool
def ocr_image(path: str) -> dict[str, Any]:
    """对本地图片文件做 OCR，返回识别文本和每个文本块的 bbox+置信度。

    支持中英文混排，纯本地运行（~1s/次），不需要网络。当需要识别图片中的文字、
    读取截图内容、提取图中文本时使用。

    Args:
        path: 本地图片文件路径（绝对或相对路径均可）。

    Returns:
        dict: {"text": 全文字符串, "lines": 按行拆分列表,
               "details": [{"bbox": [[x1,y1],[x2,y2],[x3,y3],[x4,y4]], "text": 单块文本, "conf": 置信度}]}
    """
    return _ocr_rapid(path)


@tool
def ocr_screen(x1: int, y1: int, x2: int, y2: int) -> dict[str, Any]:
    """截取屏幕指定矩形区域并 OCR。

    当用户要求识别屏幕上某区域内容、读取屏幕文字时使用。坐标单位为像素，
    (x1,y1) 为左上角，(x2,y2) 为右下角。

    Args:
        x1, y1: 左上角像素坐标。
        x2, y2: 右下角像素坐标。

    Returns:
        同 ocr_image。
    """
    from PIL import ImageGrab

    img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        img.save(tmp.name, "JPEG")
    return _ocr_rapid(tmp.name)

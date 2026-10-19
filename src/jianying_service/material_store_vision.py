"""Optional, window-scoped OCR for the Qt store opener, never material cards."""
from __future__ import annotations

import importlib.util
import time
from typing import Any


class StoreVisionError(RuntimeError):
    def __init__(self, reason: str, *, failure_type: str | None = None) -> None:
        self.reason = reason
        self.failure_type = failure_type
        self.labels: list | None = None
        super().__init__(reason)


def available() -> bool:
    return importlib.util.find_spec("rapidocr_onnxruntime") is not None


def opener_target(rows: list, width: int, height: int) -> dict:
    """Require the observed sticker-library layout, and one exact store label."""
    def labels(text: str) -> list[dict]:
        matches = []
        for box, value, score in rows:
            if value.strip() != text or float(score) < 0.96:
                continue
            xs, ys = [point[0] for point in box], [point[1] for point in box]
            matches.append({"text": value, "confidence": float(score),
                            "x": sum(xs) / len(xs), "y": sum(ys) / len(ys)})
        return matches

    stores, anchors = labels("商店"), labels("贴纸库")
    if len(stores) != 1 or len(anchors) != 1:
        raise StoreVisionError("store_opener_labels_not_unique")
    target, anchor = stores[0], anchors[0]
    if not (0 < anchor["x"] < target["x"] < width * 0.4
            and 0 < target["y"] < anchor["y"] < height * 0.4
            and 0 < anchor["y"] - target["y"] < height * 0.15):
        raise StoreVisionError("store_opener_layout_not_verified")
    return {"target": target, "anchor": anchor}


def sticker_tab_target(rows: list, width: int, height: int) -> dict:
    labels = {}
    for box, value, score in rows:
        center_x = sum(point[0] for point in box) / len(box)
        center_y = sum(point[1] for point in box) / len(box)
        if (value.strip() in {"素材", "文本", "贴纸"} and float(score) >= 0.96
                and 0 < center_x < width * 0.4 and 0 < center_y < height * 0.16):
            labels.setdefault(value.strip(), []).append({
                "text": value.strip(), "confidence": float(score),
                "x": center_x, "y": center_y,
            })
    if any(len(labels.get(name, [])) != 1 for name in ("素材", "文本", "贴纸")):
        raise StoreVisionError("store_sticker_tab_not_unique")
    media, text, sticker = (labels[name][0] for name in ("素材", "文本", "贴纸"))
    if not (0 < media["x"] < text["x"] < sticker["x"] < width * 0.4
            and 0 < sticker["y"] < height * 0.16
            and abs(media["y"] - sticker["y"]) < height * 0.02
            and abs(text["y"] - sticker["y"]) < height * 0.02):
        raise StoreVisionError("store_sticker_tab_layout_not_verified")
    return {"target": sticker, "anchor": text}


class StoreOpenerVision:
    def __init__(self) -> None:
        self._engine = None

    def _observe(self, window: Any, locator=opener_target) -> dict:
        from PIL import ImageGrab
        from rapidocr_onnxruntime import RapidOCR

        if self._engine is None:
            # The package ships its OCR models. No runtime model fetch is used.
            self._engine = RapidOCR()
        rectangle = window.BoundingRectangle
        # Qt/DirectComposition can return an old frame through PrintWindow.
        # Capture only the foreground editor's current bounds from the desktop.
        image = ImageGrab.grab(
            bbox=(rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
            all_screens=True, include_layered_windows=True,
        )
        rows, _ = self._engine(image)
        try:
            result = locator(rows or [], image.width, image.height)
        except StoreVisionError as exc:
            exc.labels = [{"text": text, "confidence": float(score)}
                          for _, text, score in rows or []
                          if text.strip() in {"素材", "文本", "贴纸", "商店", "贴纸库"}]
            raise
        scale_x = (rectangle.right - rectangle.left) / image.width
        scale_y = (rectangle.bottom - rectangle.top) / image.height
        if not (0.5 <= scale_x <= 2 and abs(scale_x - scale_y) < 0.02):
            raise StoreVisionError("store_opener_capture_scale_not_verified")
        result["screen_x"] = round(rectangle.left + result["target"]["x"] * scale_x)
        result["screen_y"] = round(rectangle.top + result["target"]["y"] * scale_y)
        result["window_handle"] = window.NativeWindowHandle
        return result

    def capture(self, window: Any) -> tuple[Any, list]:
        from PIL import ImageGrab
        from rapidocr_onnxruntime import RapidOCR

        if self._engine is None:
            self._engine = RapidOCR()
        rectangle = window.BoundingRectangle
        image = ImageGrab.grab(
            bbox=(rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
            all_screens=True, include_layered_windows=True,
        )
        rows, _ = self._engine(image)
        return image, rows or []

    def _click(self, automation: Any, window: Any, first: dict, second: dict) -> None:
        if (first["window_handle"] != second["window_handle"]
                or abs(first["screen_x"] - second["screen_x"]) > 2
                or abs(first["screen_y"] - second["screen_y"]) > 2):
            raise StoreVisionError("store_opener_layout_changed")
        hit = automation.ControlFromPoint(second["screen_x"], second["screen_y"])
        for _ in range(15):
            if hit is None or hit.NativeWindowHandle == window.NativeWindowHandle:
                break
            hit = hit.GetParentControl()
        if hit is None or hit.NativeWindowHandle != window.NativeWindowHandle:
            raise StoreVisionError("store_opener_hit_test_mismatch")
        automation.Click(second["screen_x"], second["screen_y"], waitTime=0.1)

    def open(self, automation: Any, window: Any) -> dict:
        if not available():
            raise StoreVisionError("store_opener_ocr_dependency_missing")
        if (not getattr(window, "NativeWindowHandle", 0)
                or window.Name != "剪映专业版"
                or not window.ClassName.startswith("MainWindow_")):
            raise StoreVisionError("store_opener_window_not_verified")
        window.SetActive(waitTime=0.1)
        if automation.GetForegroundControl().NativeWindowHandle != window.NativeWindowHandle:
            raise StoreVisionError("store_opener_window_not_foreground")
        prepared = False
        try:
            first = self._observe(window)
        except StoreVisionError as exc:
            if str(exc) != "store_opener_labels_not_unique":
                raise
            first = self._observe(window, sticker_tab_target)
            second = self._observe(window, sticker_tab_target)
            self._click(automation, window, first, second)
            prepared = True
            deadline = time.monotonic() + 6
            while True:
                try:
                    first = self._observe(window)
                    break
                except StoreVisionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
        second = self._observe(window)
        self._click(automation, window, first, second)
        return {"method": "window_ocr_with_layout_and_hit_test",
                "target": second["target"], "anchor": second["anchor"],
                "prepared_sticker_library": prepared}

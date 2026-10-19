from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import replace
from typing import Any

from .material_store import StoreObservation
from .material_store_vision import StoreOpenerVision, StoreVisionError, available, sticker_tab_target


def clean_label(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def search_field_query(text: str) -> str | None:
    value = " ".join(text.split())
    for prefix in ("Q ", "q ", "O ", "o ", "0 "):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    if any(placeholder in value for placeholder in ("搜索贴纸", "贴纸名称/元素")):
        return None
    return value or None


def sticker_library_anchor(rows: list, width: int, height: int) -> dict:
    """Locate the sticker-library sidebar without requiring the material store opener."""
    matches = []
    for box, text, score in rows:
        if text.strip() != "贴纸库" or float(score) < 0.96:
            continue
        x = sum(point[0] for point in box) / len(box)
        y = sum(point[1] for point in box) / len(box)
        if 0 < x < width * 0.3 and 0 < y < height * 0.4:
            matches.append({
                "text": "贴纸库", "confidence": float(score), "x": x, "y": y,
                "box": (min(point[0] for point in box), min(point[1] for point in box),
                        max(point[0] for point in box), max(point[1] for point in box)),
            })
    if len(matches) != 1:
        raise StoreVisionError("sticker_library_anchor_not_unique")
    return {"target": matches[0], "anchor": matches[0]}


def _row_center(box: list) -> tuple[float, float]:
    return (sum(point[0] for point in box) / len(box),
            sum(point[1] for point in box) / len(box))


def search_field_rectangles(
    rectangles: list[tuple[int, int, int, int]], anchor: dict, rows: list,
    width: int, height: int,
) -> list[tuple[int, int, int, int]]:
    """Select plausible outlines to the right of and above the sidebar anchor."""
    anchor_box = anchor.get("box")
    if anchor_box:
        anchor_right = anchor_box[2]
        anchor_top = anchor_box[1]
    else:
        anchor_right, anchor_top = anchor["x"], anchor["y"]

    candidates = []
    for rectangle in rectangles:
        x, y, right, bottom = rectangle
        field_width, field_height = right - x, bottom - y
        if not (anchor_right < x < right < width and y < bottom < anchor_top):
            continue
        if not (width * 0.04 < field_width < width * 0.55
                and height * 0.015 < field_height < height * 0.07):
            continue
        candidates.append(rectangle)

    # OCR confirms the search row; it is evidence rather than a fixed-coordinate
    # requirement because the placeholder can be split or omitted by OCR.
    matches = []
    for box, text, _ in rows:
        stripped = text.strip()
        cx, cy = _row_center(box)
        if not (anchor_right < cx < width and
                anchor_top - height * 0.08 < cy < anchor_top):
            continue
        if stripped == "贴纸库" or any(label in stripped for label in ("商店", "素材")):
            continue
        containing = [rectangle for rectangle in candidates
                      if rectangle[0] < cx < rectangle[2]
                      and rectangle[1] < cy < rectangle[3]]
        if stripped and containing:
            matches.extend(containing)
    return list(dict.fromkeys(matches)) or candidates


def select_search_field(
    rectangles: list[tuple[int, int, int, int]], anchor: dict, rows: list,
    width: int, height: int,
) -> tuple[int, int, int, int]:
    candidates = search_field_rectangles(rectangles, anchor, rows, width, height)
    if not candidates:
        raise StoreVisionError("sticker_search_field_outline_not_observed")
    return min(candidates, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))


def sticker_library_regions(
    field: tuple[int, int, int, int], rows: list,
    *, sidebar_top: int, category_bottom: int, panel_bottom: int,
) -> tuple[list[dict], list[str]]:
    """Read sidebar categories and the full result panel within verified bounds."""
    left, top, right, bottom = field
    if not (0 < sidebar_top < category_bottom < panel_bottom
            and top < bottom < panel_bottom):
        raise StoreVisionError("sticker_library_panel_bounds_not_verified")
    categories, visible = [], []
    for box, text, score in rows:
        stripped = text.strip()
        if not stripped or float(score) < 0.85:
            continue
        cx, cy = _row_center(box)
        sidebar = 0 < cx < left and sidebar_top < cy < category_bottom
        if sidebar:
            if stripped not in {"收藏", "AI生成", "图形库", "贴纸库"}:
                categories.append({"name": stripped, "type": "OCRText", "selected": None,
                                   "x": cx, "y": cy,
                                   "confidence": float(score)})
        elif not sidebar and left < cx < right and bottom < cy < panel_bottom:
            visible.append(stripped)
    return categories, visible


def sticker_grid_bounds(
    field: tuple[int, int, int, int], panel_bottom: int,
) -> tuple[int, int, int, int]:
    """Return a top-left/bottom-right crop wholly inside the results panel."""
    bounds = (field[0], field[3] + 20, field[2], panel_bottom - 12)
    if not (0 <= bounds[0] < bounds[2] and bounds[1] < bounds[3]):
        raise StoreVisionError("sticker_grid_boundary_not_observed")
    return bounds


class StickerLibraryVision(StoreOpenerVision):
    """Operate the editor's sticker library shown in the user's reference image."""

    def field_query(self, image: Any, field: tuple[int, int, int, int]) -> str | None:
        """Read only the field interior so its search and clear icons are excluded."""
        _, _, _, bottom = field
        padding = max(4, round((bottom - field[1]) * 0.35))
        crop = image.crop((field[0] + padding, field[1], field[2] - padding, bottom))
        rows, _ = self._engine(crop)
        return search_field_query(" ".join(text for _, text, _ in rows or []))

    @staticmethod
    def _foreground(automation: Any, window: Any) -> None:
        if (window.Name != "剪映专业版" or not window.ClassName.startswith("MainWindow_")
                or not window.NativeWindowHandle):
            raise StoreVisionError("sticker_library_requires_editor")
        window.SetActive(waitTime=0.1)
        if automation.GetForegroundControl().NativeWindowHandle != window.NativeWindowHandle:
            raise StoreVisionError("sticker_library_window_not_foreground")

    def prepare(self, automation: Any, window: Any) -> None:
        if not available():
            raise StoreVisionError("store_opener_ocr_dependency_missing")
        self._foreground(automation, window)
        try:
            self._snapshot(automation, window)
            return
        except StoreVisionError as exc:
            if exc.reason != "sticker_library_anchor_not_unique":
                raise
        first, second = self._observe(window, sticker_tab_target), self._observe(window, sticker_tab_target)
        self._click(automation, window, first, second)
        deadline = time.monotonic() + 8
        while True:
            try:
                self._snapshot(automation, window)
                return
            except StoreVisionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    @staticmethod
    def _hit(automation: Any, window: Any, x: int, y: int) -> None:
        if automation.GetForegroundControl().NativeWindowHandle != window.NativeWindowHandle:
            raise StoreVisionError("sticker_library_window_not_foreground")
        hit = automation.ControlFromPoint(x, y)
        for _ in range(15):
            if hit is None or hit.NativeWindowHandle == window.NativeWindowHandle:
                break
            hit = hit.GetParentControl()
        if hit is None or hit.NativeWindowHandle != window.NativeWindowHandle:
            raise StoreVisionError("sticker_library_hit_test_mismatch")

    def _snapshot(self, automation: Any, window: Any) -> tuple[StoreObservation, dict, Any]:
        import cv2
        import numpy as np

        self._foreground(automation, window)
        image, rows = self.capture(window)
        height, width = image.height, image.width
        anchor = sticker_library_anchor(rows, width, height)["anchor"]
        grayscale = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
        contours, _ = cv2.findContours(cv2.Canny(grayscale, 30, 80), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        rectangles = [tuple(cv2.boundingRect(contour)) for contour in contours]
        rectangles = [(x, y, x + w, y + h) for x, y, w, h in rectangles]
        field = select_search_field(rectangles, anchor, rows, width, height)
        sidebar_top = anchor["y"]
        category_bottom = None
        for box, text, score in rows:
            if text.strip() == "AI生成" and 0 < _row_center(box)[0] < field[0]:
                sidebar_top = _row_center(box)[1]
            if text.strip() == "图形库" and 0 < _row_center(box)[0] < field[0]:
                category_bottom = _row_center(box)[1]
        # Locate the editor's horizontal timeline boundary from the current image.
        lines = cv2.HoughLinesP(cv2.Canny(grayscale, 30, 80), 1, np.pi / 180,
                               threshold=100, minLineLength=int(width * 0.7), maxLineGap=20)
        dividers = (
            [int(y1) for x1, y1, x2, y2 in lines.reshape(-1, 4)
             if abs(int(y1) - int(y2)) < 3 and height * 0.4 < y1 < height * 0.8]
            if lines is not None else []
        )
        if not dividers:
            raise StoreVisionError("sticker_grid_boundary_not_observed")
        panel_bottom = min(dividers)
        if category_bottom is None:
            category_bottom = panel_bottom
        categories, visible = sticker_library_regions(
            field, rows, sidebar_top=round(sidebar_top),
            category_bottom=round(category_bottom), panel_bottom=round(panel_bottom),
        )
        grid_bounds = sticker_grid_bounds(field, round(panel_bottom))
        query = self.field_query(image, field)
        grid = image.crop(grid_bounds).convert("L").resize((32, 32))
        vector = np.asarray(grid, dtype=np.int16)
        fingerprint = hashlib.sha256(vector.tobytes()).hexdigest()
        rectangle = window.BoundingRectangle
        scale_x = (rectangle.right - rectangle.left) / width
        scale_y = (rectangle.bottom - rectangle.top) / height
        if not (0.5 <= scale_x <= 2 and 0.5 <= scale_y <= 2 and abs(scale_x - scale_y) < 0.02):
            raise StoreVisionError("sticker_capture_scale_not_verified")
        layout = {"field": field, "categories": categories, "panel_bottom": round(panel_bottom),
                  "window_handle": window.NativeWindowHandle,
                  "origin_x": rectangle.left, "origin_y": rectangle.top,
                  "scale_x": scale_x, "scale_y": scale_y}
        observation = StoreObservation(
            "available", "sticker_library_ocr_readback", False, current_query=query,
            categories=tuple({k: v for k, v in category.items() if k not in {"x", "y"}} for category in categories),
            loading=any("加载中" in text for text in visible),
            page_fingerprint=fingerprint, surface="sticker_library", visible_text=tuple(visible),
            evidence={"method": "window_ocr", "result_grid_fingerprint": fingerprint,
                      "result_count": None, "items_are_not_extracted": True,
                      "grid_bounds_in_capture": list(grid_bounds),
                      "search_field_bounds_in_capture": list(field),
                      "sticker_library_anchor_in_capture": [anchor["x"], anchor["y"]],
                      "capture_size": [width, height],
                      "capture_to_screen_scale": [scale_x, scale_y],
                      "search_field_text": query if query is not None else "placeholder"},
        )
        return observation, layout, vector

    def _ready(self, automation: Any, window: Any, before: Any, *, query: str | None = None,
               category: str | None = None) -> StoreObservation:
        import numpy as np

        deadline, previous = time.monotonic() + 16, None
        while time.monotonic() < deadline:
            observation, _, vector = self._snapshot(automation, window)
            change = float(np.mean(np.abs(vector - before))) / 255
            stable = previous is not None and float(np.mean(np.abs(vector - previous))) / 255 < 0.025
            query_matches = query is None or observation.current_query == query
            if query_matches and not observation.loading and change >= 0.035 and stable:
                return replace(observation, verification="sticker_query_and_grid_readback" if query is not None else "sticker_category_and_grid_readback",
                               requested_category=category,
                               evidence={**observation.evidence, "grid_changed": True, "grid_difference": round(change, 4),
                                         "grid_stable": True, "query_readback": query_matches})
            previous = vector
            time.sleep(0.25)
        raise StoreVisionError("sticker_library_result_change_not_confirmed")

    def run(self, automation: Any, window: Any, operation: str, value: str = "") -> StoreObservation:
        stage = "prepare" if operation != "state" else "snapshot"
        try:
            if operation != "state":
                self.prepare(automation, window)
            stage = "snapshot"
            before, layout, vector = self._snapshot(automation, window)
            if operation in {"state", "open"}:
                return before
            if operation == "search":
                if not value or len(value) > 40 or any(ord(char) < 32 for char in value):
                    raise StoreVisionError("sticker_library_query_invalid")
                stage = "search_focus"
                x = round(layout["origin_x"] + (layout["field"][0] + 30) * layout["scale_x"])
                y = round(layout["origin_y"] + ((layout["field"][1] + layout["field"][3]) / 2) * layout["scale_y"])
                self._hit(automation, window, x, y)
                automation.Click(x, y, waitTime=0.1)
                self._hit(automation, window, x, y)
                automation.SendKeys("{Ctrl}a", waitTime=0.05)
                data = value.encode("utf-16-le")
                for offset in range(0, len(data), 2):
                    automation.SendUnicodeChar(chr(int.from_bytes(data[offset:offset + 2], "little")))
                stage = "search_readback"
                typed, _, _ = self._snapshot(automation, window)
                if typed.current_query != value:
                    raise StoreVisionError("sticker_library_query_readback_mismatch")
                stage = "search_submit"
                automation.SendKeys("{Enter}", waitTime=0.1)
                stage = "search_result"
                return self._ready(automation, window, vector, query=value)
            if operation == "browse":
                if not layout["categories"]:
                    first = self._observe(window, sticker_library_anchor)
                    second = self._observe(window, sticker_library_anchor)
                    self._click(automation, window, first, second)
                    deadline = time.monotonic() + 8
                    while not layout["categories"]:
                        if time.monotonic() >= deadline:
                            raise StoreVisionError("sticker_library_categories_not_read")
                        time.sleep(0.2)
                        before, layout, vector = self._snapshot(automation, window)
                matches = [row for row in layout["categories"] if clean_label(row["name"]) == clean_label(value)]
                if len(matches) != 1:
                    raise StoreVisionError("sticker_library_category_not_unique")
                target = matches[0]
                second, second_layout, _ = self._snapshot(automation, window)
                fresh = [row for row in second_layout["categories"] if row["name"] == target["name"]]
                if len(fresh) != 1 or abs(fresh[0]["x"] - target["x"]) > 2 or abs(fresh[0]["y"] - target["y"]) > 2:
                    raise StoreVisionError("sticker_library_category_moved")
                x = round(layout["origin_x"] + target["x"] * layout["scale_x"])
                y = round(layout["origin_y"] + target["y"] * layout["scale_y"])
                self._hit(automation, window, x, y)
                automation.Click(x, y, waitTime=0.1)
                return self._ready(automation, window, vector, category=value)
            raise StoreVisionError("sticker_library_operation_unknown")
        except StoreVisionError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).exception("Sticker library %s failed", stage)
            raise StoreVisionError(f"sticker_library_{stage}_failed", failure_type=type(exc).__name__) from exc

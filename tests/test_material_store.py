from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from jianying_service.config import Settings
from jianying_service.material_store import (
    MaterialStoreService,
    StoreObservation,
    UnverifiedMaterialStoreAdapter,
)
from jianying_service.material_store_uia import (
    UiaMaterialStoreAdapter,
    UiaProbeError,
    UiaWindowProbe,
    jianying_windows,
)
from jianying_service.material_store_vision import (
    StoreVisionError,
    opener_target,
    sticker_tab_target,
)
from jianying_service.sticker_library import (
    StickerLibraryVision,
    search_field_query,
    select_search_field,
    sticker_library_anchor,
    sticker_grid_bounds,
    sticker_library_regions,
)
from jianying_service.mcp_server import create_server

from conftest import FakeMaterialStoreAdapter


def decode_result(result: object) -> dict:
    return json.loads(getattr(result, "content")[0].text)


class FakePattern:
    def __init__(self, value: str = "", read_only: bool = False) -> None:
        self.Value = value
        self.IsReadOnly = read_only
        self.changes: list[str] = []
        self.ignore_set_value = False

    def SetValue(self, value: str) -> None:
        self.changes.append(value)
        if not self.ignore_set_value:
            self.Value = value


class FakeSelection:
    def __init__(self, control: FakeControl) -> None:
        self.control = control
        self.clicked = False

    def Select(self) -> None:
        self.clicked = True
        self.control.selected = True
        for sibling in self.control.parent.children:
            if (
                sibling is not self.control
                and sibling.ControlTypeName == "TabItemControl"
            ):
                sibling.selected = False

    @property
    def IsSelected(self) -> bool:
        return self.control.selected


class FakeInvoke:
    def Invoke(self) -> None:
        self.control.invoked += 1
        if self.control.on_invoke is not None:
            self.control.on_invoke()


class FakeControl:
    def __init__(
        self,
        name: str,
        control_type: str,
        *,
        automation_id: str = "",
        class_name: str = "",
        offscreen: bool = False,
    ) -> None:
        self.Name = name
        self.ControlTypeName = control_type
        self.AutomationId = automation_id
        self.ClassName = class_name
        self.IsOffscreen = offscreen
        self.parent: FakeControl | None = None
        self.children: list[FakeControl] = []
        self.value = FakePattern(read_only=True)
        self.selected = False
        self.invoked = 0
        self.on_invoke = None

    def GetChildren(self) -> list["FakeControl"]:
        return self.children

    def GetValuePattern(self) -> FakePattern:
        return self.value

    def GetInvokePattern(self) -> Any:
        pattern = FakeInvoke()
        pattern.control = self
        return pattern

    def GetSelectionItemPattern(self) -> FakeSelection:
        return FakeSelection(self)


class FakeAutomation:
    def __init__(self, window: FakeControl) -> None:
        self.root = FakeControl("Desktop", "PaneControl")
        self.window = window

    def GetRootControl(self) -> FakeControl:
        return self.root

    def WindowControl(self, searchFromControl: FakeControl) -> FakeControl:
        return searchFromControl

    @staticmethod
    def WalkControl(
        root: FakeControl, maxDepth: int = 12
    ) -> list[tuple[FakeControl, int]]:
        output: list[tuple[FakeControl, int]] = []

        def visit(node: FakeControl, depth: int) -> None:
            if depth > maxDepth:
                return
            output.append((node, depth))
            for child in node.children:
                visit(child, depth + 1)

        visit(root, 0)
        return output


class FakeProbe:
    def __init__(self, status: str = "available") -> None:
        self.status = status
        self.probe_count = 0
        self.reason = (
            None if status == "available" else "desktop_window_tree_empty"
        )

    def probe(self) -> dict:
        self.probe_count += 1
        return {
            "status": self.status,
            "desktop_children": 1,
            "jianying_windows": [],
            "reason": self.reason,
        }


def test_jianying_window_probe_ignores_tooltip_and_selects_main_window() -> None:
    main = FakeControl("剪映专业版", "WindowControl", class_name="JianyingProMain")
    tooltip = FakeControl(
        "JianyingPro", "WindowControl", class_name="LVToolTipWindow_QMLTYPE_1740"
    )
    automation = FakeAutomation(main)
    automation.root.children.extend((tooltip, main))

    assert jianying_windows(automation) == [main]


def ocr_row(text: str, x: int, y: int, confidence: float = 0.99) -> tuple:
    return ([[x - 4, y - 4], [x + 4, y - 4], [x + 4, y + 4], [x - 4, y + 4]], text, confidence)


def test_visual_store_opener_requires_exact_labels_and_relative_layout() -> None:
    rows = [ocr_row("商店", 220, 100), ocr_row("贴纸库", 60, 160)]
    result = opener_target(rows, 1000, 600)
    assert result["target"]["text"] == "商店"
    assert result["anchor"]["text"] == "贴纸库"

    with pytest.raises(StoreVisionError, match="store_opener_labels_not_unique"):
        opener_target(rows + [ocr_row("商店", 880, 100)], 1000, 600)
    with pytest.raises(StoreVisionError, match="store_opener_layout_not_verified"):
        opener_target([ocr_row("商店", 60, 100), ocr_row("贴纸库", 220, 160)], 1000, 600)


def test_visual_sticker_tab_locator_is_scoped_to_toolbar() -> None:
    rows = [ocr_row("素材", 35, 50), ocr_row("文本", 95, 50), ocr_row("贴纸", 155, 50),
            ocr_row("素材", 35, 400)]
    result = sticker_tab_target(rows, 1000, 600)
    assert result["target"]["text"] == "贴纸"

    with pytest.raises(StoreVisionError, match="store_sticker_tab_not_unique"):
        sticker_tab_target(rows + [ocr_row("贴纸", 215, 50)], 1000, 600)


def test_sticker_search_field_readback_ignores_placeholder_and_icon() -> None:
    assert search_field_query("Q 搜索贴纸名称/元素") is None
    assert search_field_query("Q 花朵") == "花朵"
    assert search_field_query("风景") == "风景"
    assert search_field_query("Q版") == "Q版"
    assert search_field_query("OpenAI") == "OpenAI"
    assert search_field_query("0号") == "0号"


def test_sticker_field_query_ocrs_only_field_interior() -> None:
    class Image:
        def crop(self, bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
            return bounds

    class Engine:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def __call__(self, image: object) -> tuple[list, None]:
            self.calls.append(image)
            return [ocr_row("花朵", 250, 103)], None

    vision = StickerLibraryVision()
    vision._engine = Engine()
    field = (172, 91, 620, 114)
    assert vision.field_query(Image(), field) == "花朵"
    padding = round((field[3] - field[1]) * 0.35)
    assert vision._engine.calls == [(field[0] + padding, field[1], field[2] - padding, field[3])]


def test_sticker_library_snapshot_uses_sidebar_anchor_without_store_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [ocr_row("商店", 220, 100), ocr_row("贴纸库", 60, 160),
            ocr_row("商店", 880, 100)]
    anchor = sticker_library_anchor(rows, 1000, 600)["anchor"]
    assert anchor["text"] == "贴纸库"
    assert anchor["x"] == 60

    with pytest.raises(StoreVisionError, match="sticker_library_anchor_not_unique"):
        sticker_library_anchor(rows + [ocr_row("贴纸库", 50, 200)], 1000, 600)


def test_sticker_search_field_is_right_of_sidebar_anchor_and_above_it() -> None:
    rows = [ocr_row("贴纸库", 60, 160), ocr_row("搜索贴纸名称/元素", 250, 100)]
    anchor = sticker_library_anchor(rows, 1000, 600)["anchor"]
    wrong_side = (10, 90, 300, 115)
    below_anchor = (180, 190, 600, 215)
    outline = (72, 91, 520, 114)
    assert select_search_field([wrong_side, below_anchor, outline], anchor, rows, 1000, 600) == outline

    unrelated = ocr_row("热门分类", 250, 64)
    assert select_search_field(
        [wrong_side, below_anchor, outline], anchor, [*rows, unrelated], 1000, 600
    ) == outline

    with pytest.raises(StoreVisionError, match="sticker_search_field_outline_not_observed"):
        select_search_field([wrong_side, below_anchor], anchor, rows, 1000, 600)


def test_sticker_regions_use_sidebar_and_result_panel_bounds() -> None:
    field = (172, 91, 620, 114)
    rows = [
        ocr_row("热门", 95, 200),
        ocr_row("旅行", 160, 350),
        ocr_row("文本", 220, 50),
        ocr_row("分享", 900, 20),
        ocr_row("图形库", 90, 250),
        ocr_row("收藏", 90, 200),
        ocr_row("AI生成", 90, 200),
        ocr_row("贴纸库", 90, 200),
        ocr_row("搜索贴纸名称/元素", 250, 102),
        ocr_row("旅行", 1000, 200),
        ocr_row("旅行结果", 250, 180),
    ]
    categories, visible = sticker_library_regions(
        field, rows, sidebar_top=160, category_bottom=300, panel_bottom=450,
    )
    # The sidebar ends at the search field, so the duplicate at x=160 is not a panel category.
    assert [row["name"] for row in categories] == ["热门"]
    assert visible == ["旅行结果"]
    categories, visible = sticker_library_regions(
        field, rows, sidebar_top=160, category_bottom=210, panel_bottom=450,
    )
    assert [row["name"] for row in categories] == ["热门"]
    assert visible == ["旅行结果"]


def test_sticker_grid_bounds_keep_left_right_and_top_bottom_separate() -> None:
    field = (172, 91, 620, 114)
    bounds = sticker_grid_bounds(field, 450)
    left, top, right, bottom = bounds
    assert (left, right) == (field[0], field[2])
    assert left < right
    assert field[3] < top < bottom <= 450

    with pytest.raises(StoreVisionError, match="sticker_grid_boundary_not_observed"):
        sticker_grid_bounds(field, field[3] + 20)


def test_prepare_recognizes_sticker_library_before_switching_tabs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vision = StickerLibraryVision()
    snapshot = [replace(
        StoreObservation("available", "sticker_library_ocr_readback", False,
                         current_query="花朵", surface="sticker_library"),
        evidence={"anchor": "贴纸库"},
    ), {}, None]
    observes, clicks = [], []
    monkeypatch.setattr("jianying_service.sticker_library.available", lambda: True)
    monkeypatch.setattr(vision, "_foreground", lambda automation, window: None)
    monkeypatch.setattr(vision, "_snapshot", lambda automation, window: (
        observes.append("snapshot"), snapshot)[1])
    monkeypatch.setattr(vision, "_observe", lambda *args, **kwargs: observes.append("tab"))
    monkeypatch.setattr(vision, "_click", lambda *args: clicks.append("click"))

    vision.prepare(object(), FakeControl("剪映专业版", "WindowControl", class_name="MainWindow_1"))

    assert observes == ["snapshot"]
    assert clicks == []


def test_prepare_does_not_switch_sticker_tab_when_anchor_was_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vision = StickerLibraryVision()
    failure = StoreVisionError("sticker_grid_boundary_not_observed")
    observes, clicks = [], []
    monkeypatch.setattr("jianying_service.sticker_library.available", lambda: True)
    monkeypatch.setattr(vision, "_foreground", lambda automation, window: None)
    def failed_snapshot(automation: object, window: object) -> None:
        observes.append("snapshot")
        raise failure

    monkeypatch.setattr(vision, "_snapshot", failed_snapshot)
    monkeypatch.setattr(vision, "_observe", lambda *args, **kwargs: observes.append("tab"))
    monkeypatch.setattr(vision, "_click", lambda *args: clicks.append("click"))

    with pytest.raises(StoreVisionError, match="sticker_grid_boundary_not_observed"):
        vision.prepare(object(), FakeControl("剪映专业版", "WindowControl", class_name="MainWindow_1"))

    assert observes == ["snapshot"]
    assert clicks == []


def test_prepare_switches_only_without_anchor_and_requires_full_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vision = StickerLibraryVision()
    missing = StoreVisionError("sticker_library_anchor_not_unique")
    incomplete = StoreVisionError("sticker_search_field_outline_not_observed")
    complete = replace(
        StoreObservation("available", "sticker_library_ocr_readback", False,
                         surface="sticker_library"),
        evidence={"search_field_bounds_in_capture": [70, 20, 400, 40]},
    )
    states = iter([missing, incomplete, complete])
    observes, clicks = [], []
    observations: list[str] = []
    monkeypatch.setattr("jianying_service.sticker_library.available", lambda: True)
    monkeypatch.setattr(vision, "_foreground", lambda automation, window: None)
    def snapshot(automation: object, window: object) -> tuple:
        observations.append("snapshot")
        state = next(states)
        if isinstance(state, StoreVisionError):
            raise state
        return state, {}, None

    monkeypatch.setattr(vision, "_snapshot", snapshot)
    def observe(window: object, locator: object) -> dict:
        observes.append("tab")
        return {"window_handle": 1, "screen_x": 100, "screen_y": 20}

    monkeypatch.setattr(vision, "_observe", observe)
    monkeypatch.setattr(vision, "_click", lambda *args: clicks.append("click"))

    vision.prepare(object(), FakeControl("剪映专业版", "WindowControl", class_name="MainWindow_1"))

    assert observations == ["snapshot"] * 3
    assert observes == ["tab", "tab"]
    assert clicks == ["click"]


def test_sticker_snapshot_keeps_timeline_panel_when_graphic_library_moves_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Automation:
        @staticmethod
        def GetForegroundControl() -> SimpleNamespace:
            return SimpleNamespace(NativeWindowHandle=1)

    class Image:
        width, height = 1000, 600

        @staticmethod
        def crop(bounds: tuple[int, int, int, int]) -> Image:
            assert bounds == (172, 134, 620, 438)
            return Image()

        @staticmethod
        def convert(mode: str) -> Image:
            return Image()

        @staticmethod
        def resize(size: tuple[int, int]) -> Image:
            return Image()

        def __array__(self, dtype: object = None) -> np.ndarray:
            return np.full((32, 32), 128, dtype=np.uint8)

    # This stub exposes only the fixed outline and timeline for the boundary logic.
    cv2 = types.SimpleNamespace(
        RETR_LIST=0, CHAIN_APPROX_SIMPLE=0,
        COLOR_RGB2GRAY=0, BORDER_CONSTANT=0,
        cvtColor=lambda image, code: np.zeros((32, 32), dtype=np.uint8),
        Canny=lambda image, low, high: image,
        findContours=lambda image, mode, method: ([object()], None),
        boundingRect=lambda contour: (172, 91, 448, 23),
        HoughLinesP=lambda *args, **kwargs: np.array([[[0, 450, 1000, 450]]]),
    )
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    vision = StickerLibraryVision()
    vision._foreground = lambda automation, window: None
    rows = [ocr_row("贴纸库", 60, 160), ocr_row("AI生成", 90, 185),
            ocr_row("图形库", 90, 210), ocr_row("花朵", 300, 200)]
    vision.capture = lambda window: (Image(), rows)
    vision.field_query = lambda image, field: "花朵"
    window = FakeControl("剪映专业版", "MainWindow_1", class_name="MainWindow_1")
    window.NativeWindowHandle = 1
    window.BoundingRectangle = SimpleNamespace(left=0, top=0, right=1000, bottom=600)

    observation, layout, _ = vision._snapshot(Automation(), window)

    assert observation.evidence["grid_bounds_in_capture"] == [172, 134, 620, 438]
    assert observation.visible_text == ("花朵",)
    assert layout["categories"] == []
    assert layout["panel_bottom"] == 450


def test_sticker_browse_does_not_click_when_categories_are_already_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vision = StickerLibraryVision()
    monkeypatch.setattr(vision, "prepare", lambda automation, window: None)
    observations, clicks = [], []
    category = {"name": "热门", "type": "OCRText", "selected": None,
                "x": 90.0, "y": 250.0, "confidence": 0.99}
    initial = StoreObservation("available", "sticker_library_ocr_readback", False,
                               surface="sticker_library")
    layout = {"categories": [category], "origin_x": 0, "origin_y": 0,
              "scale_x": 1, "scale_y": 1}
    snapshots = iter([(initial, layout, None), (initial, layout, None)])
    monkeypatch.setattr(vision, "_snapshot", lambda automation, window: next(snapshots))
    monkeypatch.setattr(vision, "_observe", lambda *args: observations.append("observe"))
    monkeypatch.setattr(vision, "_click", lambda *args: clicks.append("anchor"))
    monkeypatch.setattr(vision, "_hit", lambda *args: clicks.append("hit"))

    class Automation:
        @staticmethod
        def Click(x: int, y: int, waitTime: float) -> None:
            clicks.append("category")

    expected = replace(initial, requested_category="热门")
    monkeypatch.setattr(vision, "_ready", lambda *args, **kwargs: expected)

    assert vision.run(Automation(), object(), "browse", "热门") == expected
    assert observations == []
    assert clicks == ["hit", "category"]


def test_sticker_browse_expands_collapsed_sidebar_once_and_then_rereads_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vision = StickerLibraryVision()
    observations = []
    clicks, snapshots = [], []
    monkeypatch.setattr(vision, "prepare", lambda automation, window: None)

    def observe(window: object, locator: object) -> dict:
        assert locator is sticker_library_anchor
        observations.append("observe")
        return {"window_handle": 1, "screen_x": 80, "screen_y": 220}

    def click(automation: object, window: object, first: dict, second: dict) -> None:
        clicks.append((first, second))

    def hit(automation: object, window: object, x: int, y: int) -> None:
        clicks.append(("hit", x, y))

    initial = StoreObservation("available", "sticker_library_ocr_readback", False,
                               surface="sticker_library")
    expanded = StoreObservation("available", "sticker_library_ocr_readback", False,
                                surface="sticker_library")
    populated = replace(initial, categories=({"name": "热门", "type": "OCRText"},))
    layout = {"categories": [], "origin_x": 0, "origin_y": 0, "scale_x": 1, "scale_y": 1}
    loaded_layout = {**layout, "categories": [{"name": "热门", "type": "OCRText",
                                              "selected": None, "x": 90, "y": 250,
                                              "confidence": 0.99}]}
    states = iter([(initial, layout, np.zeros((32, 32))),
                   (expanded, layout, np.zeros((32, 32))),
                   (populated, loaded_layout, np.zeros((32, 32))),
                   (populated, loaded_layout, np.zeros((32, 32)))])
    observations[:] = ["snapshot"]
    monkeypatch.setattr(vision, "_snapshot", lambda automation, window: (snapshots.append("snapshot"), next(states))[1])
    monkeypatch.setattr(vision, "_observe", observe)
    monkeypatch.setattr(vision, "_click", click)
    monkeypatch.setattr(vision, "_hit", hit)
    class Automation:
        @staticmethod
        def Click(x: int, y: int, waitTime: float) -> None:
            clicks.append(("category_click", x, y))

    result = replace(populated, requested_category="热门")
    monkeypatch.setattr(vision, "_ready", lambda *args, **kwargs: result)
    monkeypatch.setattr("jianying_service.sticker_library.time.sleep", lambda seconds: None)

    assert vision.run(Automation(), object(), "browse", "热门") == result
    assert observations == ["snapshot", "observe", "observe"]
    assert clicks == [({"window_handle": 1, "screen_x": 80, "screen_y": 220},
                       {"window_handle": 1, "screen_x": 80, "screen_y": 220}),
                      ("hit", 90, 250), ("category_click", 90, 250)]
    assert snapshots == ["snapshot"] * 4


def fake_uia_scene() -> tuple[FakeControl, FakeControl, FakeControl, FakeControl]:
    window = FakeControl("JianyingPro", "WindowControl")
    store_button = FakeControl("素材商店", "ButtonControl")
    store = FakeControl("素材商店", "PaneControl")
    search = FakeControl(
        "搜索素材",
        "EditControl",
        automation_id="materials.search",
    )
    store.children.append(search)
    window.children.extend((store_button, store))
    search.parent = store
    store.parent = window
    store_button.parent = window
    return window, store_button, store, search


def use_fake_uia(
    monkeypatch: pytest.MonkeyPatch, window: FakeControl
) -> None:
    automation = FakeAutomation(window)
    monkeypatch.setattr(
        "jianying_service.material_store_uia.jianying_windows",
        lambda ignored: [automation.window],
    )
    monkeypatch.setattr(
        "jianying_service.material_store_uia.automation_client",
        lambda: contextlib.nullcontext(automation),
    )
    monkeypatch.setattr(
        "jianying_service.material_store_uia.UiaMaterialStoreAdapter._walk",
        lambda self, root, max_depth=20: FakeAutomation.WalkControl(
            root, maxDepth=max_depth
        ),
        raising=False,
    )


def test_uia_open_is_idempotent_and_reading_does_not_require_an_opener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, button, store, search = fake_uia_scene()
    use_fake_uia(monkeypatch, window)
    adapter = UiaMaterialStoreAdapter(FakeProbe())

    assert adapter.status() == "available"
    assert adapter.last_diagnostic["store"]["verification"] == "store_document_readback"
    assert button.invoked == 0
    assert adapter.last_diagnostic["operations"]["open"]["status"] == "already_open"
    window.children.remove(button)
    assert adapter.open_store().store_open is True
    assert adapter.observe().status == "available"
    assert button.invoked == 0

    store.children.remove(search)
    assert adapter.status() == "needs_gui"
    assert adapter.last_diagnostic["reason"] == "store_content_not_observed"
    assert adapter.last_diagnostic["store"]["store_open"] is True


def test_uia_search_requires_submission_and_changed_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, button, store, search = fake_uia_scene()
    search.value.IsReadOnly = False
    use_fake_uia(monkeypatch, window)
    adapter = UiaMaterialStoreAdapter(FakeProbe())
    # Merely changing the query value is not a successful search.
    with pytest.raises(Exception) as error:
        adapter.search("风景")
    assert error.value.details["reason"] == "store_search_submit_not_observed"
    assert search.value.changes == []

    submit = FakeControl("搜索", "ButtonControl")
    submit.on_invoke = lambda: store.children.append(FakeControl("风景结果", "ListItemControl"))
    store.children.append(submit)
    result = adapter.search("风景")
    assert result.status == "available"
    assert search.value.changes == ["风景"]
    assert result.current_query == "风景"
    assert submit.invoked == 1
    assert [item["name"] for item in result.items] == ["风景结果"]
    assert button.invoked == 0

    search.value.ignore_set_value = True
    with pytest.raises(Exception) as error:
        adapter.search("城市")
    assert error.value.details["reason"] == "store_search_readback_mismatch"
    assert adapter.diagnostic()["status"] == "available"
    assert adapter.diagnostic()["last_action"]["ok"] is False


def test_uia_category_uses_unique_selection_and_confirms_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, button, store, search = fake_uia_scene()
    video = FakeControl("视频", "TabItemControl")
    audio = FakeControl("音频", "TabItemControl")
    audio.selected = True
    audio.value.selected = True
    download = FakeControl("下载", "TabItemControl")
    store.children.extend((video, audio, download))
    video.parent = store
    audio.parent = store
    download.parent = store
    use_fake_uia(monkeypatch, window)
    adapter = UiaMaterialStoreAdapter(FakeProbe())
    result = adapter.browse("视频")
    assert result.status == "available"
    assert result.current_category == "视频"
    assert video.selected is True
    assert download.selected is False
    assert button.invoked == 0

    with pytest.raises(Exception) as error:
        adapter.browse("下载")
    assert error.value.details["reason"] == "store_category_not_unique"
    assert download.selected is False


def test_uia_items_filter_actions_and_probe_does_not_accept_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, button, store, search = fake_uia_scene()
    store.children.extend(
        (
            FakeControl("风景素材", "ListItemControl"),
            FakeControl("下载风景素材", "ListItemControl"),
            FakeControl("添加到时间线", "ListItemControl"),
            FakeControl("", "ListItemControl", class_name="btn-使用"),
        )
    )
    use_fake_uia(monkeypatch, window)
    adapter = UiaMaterialStoreAdapter(FakeProbe())
    items = adapter.observe().items
    assert [item["name"] for item in items] == ["风景素材"]
    assert items[0]["read_only"] is True

    class UnsupportedAdapter(UnverifiedMaterialStoreAdapter):
        def status(self) -> str:
            return "unsupported"

        def diagnostic(self) -> dict:
            return {"status": "unsupported", "reason": self.reason}

    failed_probe = MaterialStoreService(UnsupportedAdapter())
    assert failed_probe.status()["unsupported"] is True
    assert failed_probe.status()["needs_gui"] is True

    class BrokenRoot:
        def GetRootControl(self) -> None:
            return None

    monkeypatch.setattr(
        "jianying_service.material_store_uia.automation_client",
        lambda: contextlib.nullcontext(BrokenRoot()),
    )
    assert UiaWindowProbe().probe()["reason"] == "uia_desktop_root_unavailable"
    assert issubclass(UiaProbeError, RuntimeError)


def independent_store_scene() -> tuple[FakeControl, FakeControl, FakeControl]:
    window = FakeControl("剪映专业版", "WindowControl")
    store = FakeControl("商店", "WindowControl", class_name="MaterialStoreWindow_QMLTYPE_1899")
    document = FakeControl("", "DocumentControl", class_name="Chrome_RenderWidgetHostHWND")
    window.children.append(store)
    store.children.append(document)
    header = FakeControl("", "GroupControl", automation_id="root")
    document.children.append(header)
    header.children.extend(FakeControl(name, "TextControl") for name in ("贴纸", "文字模板", "滤镜", "精选", "🌸季节"))
    content = header
    for _ in range(12):
        wrapper = FakeControl("", "GroupControl")
        content.children.append(wrapper)
        content = wrapper
    content.children.extend((FakeControl("秋日物语", "TextControl"), FakeControl("秋日可爱拼贴", "GroupControl")))
    return window, store, document


def test_independent_cef_store_is_readable_without_search_or_main_store_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, store, document = independent_store_scene()
    # A search box outside the store must not be mistaken for a store search.
    outside = FakeControl("搜索素材", "EditControl")
    outside.value.IsReadOnly = False
    window.children.append(outside)
    use_fake_uia(monkeypatch, window)
    adapter = UiaMaterialStoreAdapter(FakeProbe())
    service = MaterialStoreService(adapter)

    observed = service.observe()
    assert observed["ok"] is True
    assert [item["name"] for item in observed["items"]] == ["秋日物语", "秋日可爱拼贴"]
    assert observed["current_category"] is None
    assert [item["name"] for item in observed["categories"]] == ["精选", "🌸季节"]
    assert service.open_store()["store_open"] is True
    assert store.invoked == 0

    with pytest.raises(Exception) as error:
        service.search("花朵")
    assert error.value.status == 409
    assert error.value.details["unsupported"] is True
    assert outside.value.changes == []
    status = service.status()
    assert status["verified"] is True
    assert status["verification_scope"] == "page_state"
    assert status["all_operations_available"] is False
    assert status["operations"]["state"] == "available"
    assert status["operations"]["search"] == "unsupported"
    assert status["operations"]["open"] == "already_open"


def test_navigation_never_treats_a_material_title_as_a_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, store, document = independent_store_scene()
    use_fake_uia(monkeypatch, window)
    adapter = UiaMaterialStoreAdapter(FakeProbe())
    with pytest.raises(Exception) as error:
        adapter.browse("秋日物语")
    assert error.value.details["reason"] == "store_category_not_unique"
    assert store.invoked == 0
    assert all(control.invoked == 0 for control, _ in FakeAutomation.WalkControl(document, maxDepth=20))


def test_cef_category_uses_fresh_bounds_only_after_window_and_hit_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, store, document = independent_store_scene()
    use_fake_uia(monkeypatch, window)
    store.NativeWindowHandle = 123
    store.BoundingRectangle = SimpleNamespace(left=0, top=0, right=800, bottom=600)
    store.SetActive = lambda **kwargs: True
    target = document.children[0].children[4]
    target.BoundingRectangle = SimpleNamespace(left=160, top=40, right=220, bottom=60)
    target.GetSelectionItemPattern = lambda: None
    target.GetInvokePattern = lambda: None
    target.GetRuntimeId = lambda: [1, 5]
    clicks: list[tuple[int, int]] = []
    automation = SimpleNamespace(
        GetForegroundControl=lambda: store,
        ControlFromPoint=lambda x, y: target,
        Click=lambda x, y, **kwargs: clicks.append((x, y)),
    )
    adapter = UiaMaterialStoreAdapter(FakeProbe())
    assert adapter._click_navigation(automation, window, target) == "uia_bounds_with_hit_test"
    assert clicks == [(190, 50)]


def test_cef_category_hit_test_blocks_overlapping_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, store, document = independent_store_scene()
    use_fake_uia(monkeypatch, window)
    store.NativeWindowHandle = 123
    store.BoundingRectangle = SimpleNamespace(left=0, top=0, right=800, bottom=600)
    store.SetActive = lambda **kwargs: True
    target = document.children[0].children[4]
    target.BoundingRectangle = SimpleNamespace(left=160, top=40, right=220, bottom=60)
    target.GetSelectionItemPattern = lambda: None
    target.GetInvokePattern = lambda: None
    target.GetRuntimeId = lambda: [1, 5]
    obstacle = FakeControl("遮挡窗口", "WindowControl")
    obstacle.GetRuntimeId = lambda: [2, 9]
    obstacle.GetParentControl = lambda: None
    clicks: list[tuple[int, int]] = []
    automation = SimpleNamespace(
        GetForegroundControl=lambda: store,
        ControlFromPoint=lambda x, y: obstacle,
        Click=lambda x, y, **kwargs: clicks.append((x, y)),
    )
    adapter = UiaMaterialStoreAdapter(FakeProbe())
    with pytest.raises(Exception) as error:
        adapter._click_navigation(automation, window, target)
    assert error.value.details["reason"] == "store_navigation_hit_test_mismatch"
    assert clicks == []


def test_category_without_uia_pattern_is_unsupported_and_not_clicked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, store, document = independent_store_scene()
    use_fake_uia(monkeypatch, window)
    adapter = UiaMaterialStoreAdapter(FakeProbe())
    header = document.children[0]
    for child in header.children:
        child.GetSelectionItemPattern = lambda: None
        child.GetInvokePattern = lambda: None
    assert adapter.observe().status == "available"
    assert adapter.status() == "available"
    assert adapter.last_diagnostic["operations"]["browse"]["status"] == "available"
    with pytest.raises(Exception) as error:
        adapter.browse("🌸季节")
    assert error.value.status == 503
    assert error.value.details["reason"] == "store_navigation_window_not_verified"
    assert error.value.details["needs_gui"] is True


def test_loading_or_unchanged_page_cannot_confirm_a_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = UiaMaterialStoreAdapter(FakeProbe())
    before = StoreObservation("available", "store_document_readback", True,
                              items=({"name": "旧内容"},), page_fingerprint="old")
    loading = replace(before, loading=True, items=(), page_fingerprint="placeholder")
    reads = iter((loading, before))
    ticks = iter((0, 1, 2, 11))
    monkeypatch.setattr(adapter, "_read", lambda _: next(reads))
    monkeypatch.setattr("jianying_service.material_store_uia.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("jianying_service.material_store_uia.time.sleep", lambda _: None)
    with pytest.raises(Exception) as error:
        adapter._wait_change(None, before, "季节")
    assert error.value.details["reason"] == "store_view_change_not_confirmed"


def test_default_status_is_honest_needs_gui_without_ui_window() -> None:
    service = MaterialStoreService(UnverifiedMaterialStoreAdapter())
    status = service.status()
    assert status["ok"] is True
    assert status["verified"] is False
    assert status["needs_gui"] is True
    assert status["operations"]["search"] == "needs_gui"


def test_http_unverified_operations_never_return_success(
    client: TestClient,
) -> None:
    status = client.get("/api/material-store/status")
    assert status.status_code == 200
    body = status.json()
    assert body["verified"] is False
    assert body["needs_gui"] is True
    assert body["operations"]["open"] == "needs_gui"
    assert body["diagnostic"]["adapter"] == "uiautomation"

    unopened = client.post("/api/material-store/open")
    assert unopened.status_code == 503
    assert unopened.json()["ok"] is False
    assert unopened.json()["error"]["code"] == "MATERIAL_STORE_UI_UNVERIFIED"
    assert unopened.json()["error"]["details"]["needs_gui"] is True

    search = client.post(
        "/api/material-store/search", json={"query": "风景"}
    )
    assert search.status_code == 503
    assert search.json()["ok"] is False
    browse = client.post(
        "/api/material-store/browse", json={"category": "视频"}
    )
    assert browse.status_code == 503
    assert browse.json()["ok"] is False
    observed = client.get("/api/material-store/state")
    assert observed.status_code == 503
    assert observed.json()["ok"] is False


def test_http_search_uses_typed_body_and_rejects_bulk_or_destructive_routes(
    store_client: TestClient, material_store: FakeMaterialStoreAdapter
) -> None:
    response = store_client.post(
        "/api/material-store/search", json={"query": "风景"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["current_query"] == "风景"
    assert material_store.calls[-1] == ("search", "风景")
    assert store_client.post(
        "/api/material-store/search", json={"query": "风景", "download": True}
    ).status_code == 422
    for path in (
        "/api/material-store/download",
        "/api/material-store/purchase",
        "/api/material-store/add",
        "/api/material-store/downloads",
    ):
        assert store_client.post(path).status_code == 404


def test_http_sticker_library_has_separate_search_surface(
    store_client: TestClient, material_store: FakeMaterialStoreAdapter
) -> None:
    state = store_client.get("/api/sticker-library/state")
    assert state.status_code == 200
    assert state.json()["surface"] == "sticker_library"
    assert material_store.calls[-1] == ("library", "state", "")

    search = store_client.post(
        "/api/sticker-library/search", json={"query": "花朵"}
    )
    assert search.status_code == 200
    assert search.json()["surface"] == "sticker_library"
    assert search.json()["current_query"] == "花朵"
    assert material_store.calls[-1] == ("library", "search", "花朵")

    assert store_client.post(
        "/api/sticker-library/search", json={"query": "x" * 41}
    ).status_code == 422
    assert store_client.post(
        "/api/sticker-library/search", json={"query": "花朵", "download": True}
    ).status_code == 422
    assert store_client.post(
        "/api/sticker-library/browse", json={"category": "热门"}
    ).json()["surface"] == "sticker_library"


def test_http_open_observes_after_action(
    store_client: TestClient, material_store: FakeMaterialStoreAdapter
) -> None:
    result = store_client.post("/api/material-store/open")
    assert result.status_code == 200, result.text
    assert result.json()["store_open"] is True
    assert material_store.calls.count(("open",)) == 1

    browse = store_client.post(
        "/api/material-store/browse", json={"category": "视频"}
    )
    assert browse.status_code == 200
    assert browse.json()["current_category"] == "视频"
    state = store_client.get("/api/material-store/state")
    assert state.status_code == 200
    assert state.json()["items"][0]["name"] == "只读预览条目"


def test_failed_open_and_non_available_observation_are_not_false_successes(
    material_store: FakeMaterialStoreAdapter, settings: Settings
) -> None:
    material_store.open_fails = True
    service = MaterialStoreService(material_store)
    for call in (lambda: service.open_store(),):
        try:
            call()
        except Exception as exc:
            assert getattr(exc, "code", "") == "MATERIAL_STORE_UI_UNVERIFIED"
        else:
            raise AssertionError("An unverified UI action was reported as successful")
    material_store.open = True
    material_store.state = "needs_gui"
    try:
        service.observe()
    except Exception as exc:
        assert getattr(exc, "code", "") == "MATERIAL_STORE_UI_UNVERIFIED"
    else:
        raise AssertionError("An unverified UI observation was reported as successful")


def test_mcp_store_capabilities_and_errors(
    settings: Settings, material_store: FakeMaterialStoreAdapter
) -> None:
    service = MaterialStoreService(material_store)
    server = create_server(settings=settings, material_store=service)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert {
        "get_material_store_status",
        "diagnose_material_store",
        "open_material_store",
        "get_material_store_state",
        "search_material_store",
        "browse_material_store",
    } <= names
    result = decode_result(
        asyncio.run(
            server.call_tool("search_material_store", {"query": "模板"})
        )
    )
    assert result["ok"] is True
    assert result["current_query"] == "模板"

    unavailable = MaterialStoreService(UnverifiedMaterialStoreAdapter())
    unavailable_server = create_server(
        settings=settings, material_store=unavailable
    )
    failure = decode_result(
        asyncio.run(
            unavailable_server.call_tool(
                "open_material_store", {}
            )
        )
    )
    assert failure["ok"] is False
    assert failure["error"]["details"]["needs_gui"] is True

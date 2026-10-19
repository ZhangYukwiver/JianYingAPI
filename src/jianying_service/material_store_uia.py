from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Iterator

from .errors import ServiceError
from .material_store import CapabilityState, StoreObservation


STORE_NAMES = ("素材商店", "商店")
SEARCH_NAMES = ("搜索素材", "搜索", "搜索贴纸名称/标签", "搜索贴纸名称/元素")
# Observed navigation labels in Jianying 11.6's independent store window.
# Text controls outside this header are never actionable through this adapter.
TAB_NAMES = ("贴纸", "文字模板", "滤镜")
CATEGORY_NAMES = (
    "精选", "🎉节日", "🌸季节", "🕺🏻互动引导", "☀️出游", "💗浪漫",
    "🛍️种草带货", "🤣情绪", "🍪美食", "🐾萌宠", "🕶️潮酷",
    "✍️手写字", "🧨国潮", "🎂生日快乐",
)
SECTION_NAMES = ("🎉最近上新", "👑会员精选", "🔥热门推荐")
EMPTY_NAMES = ("暂无素材", "暂无内容", "暂无结果", "没有找到相关素材")
DANGEROUS_KEYS = ("下载", "购买", "添加到时间线", "添加", "使用")
MAX_TEXT = 160


@contextmanager
def automation_client() -> Iterator[Any]:
    import uiautomation as automation

    with automation.UIAutomationInitializerInThread():
        yield automation


def normalize_name(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def label_key(value: str) -> str:
    return "".join(char for char in normalize_name(value) if char.isalnum())


def safe_text(value: str | None) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "..."


def control_snapshot(control: Any) -> dict:
    return {
        "name": safe_text(control.Name),
        "type": control.ControlTypeName,
        "automation_id": safe_text(getattr(control, "AutomationId", "")),
        "class_name": safe_text(control.ClassName),
    }


def pattern(control: Any, name: str) -> Any | None:
    try:
        return getattr(control, name)()
    except Exception:
        return None


def readable_value(control: Any) -> str | None:
    try:
        return safe_text(control.GetValuePattern().Value)
    except Exception:
        return None


def visible(control: Any) -> bool:
    try:
        return not control.IsOffscreen
    except Exception:
        return False


def jianying_windows(automation: Any) -> list[Any]:
    root = automation.GetRootControl()
    if root is None:
        return []
    return [
        control for control in root.GetChildren()
        if control.ControlTypeName == "WindowControl"
        and "tooltip" not in normalize_name(control.ClassName)
        and (
            normalize_name(control.Name) == "剪映专业版"
            or normalize_name(control.Name).startswith("jianyingpro")
        )
    ]


class UiaProbeError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class UiaWindowProbe:
    def probe(self) -> dict:
        try:
            with automation_client() as automation:
                root = automation.GetRootControl()
                if root is None:
                    raise UiaProbeError("uia_desktop_root_unavailable")
                children = root.GetChildren()
                windows = [control_snapshot(control) for control in jianying_windows(automation)]
                return {
                    "status": "available" if windows else "needs_gui",
                    "desktop_children": len(children),
                    "jianying_windows": windows,
                    "reason": None if windows else (
                        "desktop_window_tree_empty" if not children else "jianying_window_not_observed"
                    ),
                }
        except Exception as exc:
            return {
                "status": "needs_gui", "desktop_children": None, "jianying_windows": [],
                "reason": exc.reason if isinstance(exc, UiaProbeError) else "uia_desktop_access_failed",
            }


class UiaMaterialStoreAdapter:
    def __init__(self, window_probe: UiaWindowProbe | None = None) -> None:
        self.window_probe = window_probe or UiaWindowProbe()
        self._last_diagnostic: dict = {"status": "needs_gui", "reason": "not_probed"}
        self._last_action: dict | None = None
        self._opener_vision = None
        self._library_vision = None

    @property
    def last_diagnostic(self) -> dict:
        return dict(self._last_diagnostic)

    def _fail(self, reason: str, *, unsupported: bool = False, action: bool = False) -> ServiceError:
        state = "unsupported" if unsupported else "needs_gui"
        self._last_action = {"ok": False, "status": state, "reason": reason}
        return ServiceError(
            "MATERIAL_STORE_ACTION_FAILED" if action else "MATERIAL_STORE_UI_UNVERIFIED",
            "The store operation is not supported by the observed UI" if unsupported else
            "The store operation could not be verified from the observed UI",
            status=409 if unsupported else 503,
            details={"status": state, "needs_gui": not unsupported,
                     "unsupported": unsupported, "reason": reason},
        )

    @contextmanager
    def _session(self) -> Iterator[tuple[Any, Any, dict]]:
        probe = self.window_probe.probe()
        if probe.get("status") != "available":
            raise self._fail(str(probe.get("reason") or "jianying_window_not_observed"))
        with automation_client() as automation:
            windows = [window for window in jianying_windows(automation) if visible(window)]
            if len(windows) != 1:
                raise self._fail("jianying_window_not_unique" if windows else "jianying_window_not_observed")
            yield automation, windows[0], probe

    @staticmethod
    def _walk(window: Any, max_depth: int = 20) -> Iterator[tuple[Any, int]]:
        import uiautomation as automation

        for index, entry in enumerate(automation.WalkControl(window, maxDepth=max_depth)):
            if index >= 2500:
                break
            yield entry

    def _find_store_candidates(self, window: Any) -> list[Any]:
        return [
            control for control, _ in self._walk(window, max_depth=5)
            if visible(control) and (
                control.ControlTypeName == "WindowControl"
                and control.Name in STORE_NAMES
                and control.ClassName.startswith("MaterialStoreWindow_")
                or control.ControlTypeName == "PaneControl" and control.Name == "素材商店"
            )
        ]

    def _page(self, window: Any) -> tuple[Any | None, list[tuple[Any, int]]]:
        candidates = self._find_store_candidates(window)
        if len(candidates) != 1:
            return None, []
        region = candidates[0]
        entries = list(self._walk(region))
        if region.ControlTypeName == "WindowControl":
            documents = [control for control, _ in entries
                         if control.ControlTypeName == "DocumentControl" and visible(control)]
            if len(documents) != 1:
                return region, []
            entries = list(self._walk(documents[0]))
        return region, entries

    @staticmethod
    def _find_search(entries: list[tuple[Any, int]]) -> Any | None:
        matches = [control for control, _ in entries
                   if control.ControlTypeName == "EditControl" and visible(control)
                   and (control.Name in SEARCH_NAMES
                        or getattr(control, "AutomationId", "") == "materials.search")]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _selected(control: Any) -> bool | None:
        try:
            return bool(control.GetSelectionItemPattern().IsSelected)
        except Exception:
            return None

    def _navigation(self, entries: list[tuple[Any, int]]) -> list[Any]:
        controls = []
        header = True
        for control, _ in entries:
            if not visible(control) or not control.Name:
                continue
            if control.ControlTypeName == "TabItemControl":
                if not any(key in control.Name for key in DANGEROUS_KEYS):
                    controls.append(control)
            elif control.ControlTypeName in {"TextControl", "GroupControl"}:
                if header and control.ControlTypeName == "TextControl" and control.Name in TAB_NAMES + CATEGORY_NAMES:
                    controls.append(control)
                else:
                    header = False
        return controls

    def _read(self, window: Any) -> StoreObservation:
        region, entries = self._page(window)
        if region is None:
            return StoreObservation("needs_gui", "unverified", False, reason="store_window_not_unique_or_not_open")
        navigation = self._navigation(entries)
        tabs, categories = [], []
        for control in navigation:
            snapshot = {**control_snapshot(control), "selected": self._selected(control)}
            (tabs if control.Name in TAB_NAMES else categories).append(snapshot)
        texts = [safe_text(control.Name) for control, _ in entries
                 if control.ControlTypeName in {"TextControl", "GroupControl", "ListItemControl"}
                 and control.Name and visible(control)]
        sections = tuple(dict.fromkeys(name for name in texts if name in SECTION_NAMES))
        excluded = set(TAB_NAMES + CATEGORY_NAMES + SECTION_NAMES + EMPTY_NAMES + ("全部显示", "加载中...", "加载中…"))
        items, seen = [], set()
        navigation_names = {control.Name for control in navigation}
        for control, _ in entries:
            name = safe_text(control.Name)
            if (not name or name in excluded or name in navigation_names or name in seen
                    or not visible(control) or "加载中" in name
                    or control.ControlTypeName not in {"TextControl", "GroupControl", "ListItemControl"}
                    or any(key in name for key in DANGEROUS_KEYS)):
                continue
            seen.add(name)
            items.append({**control_snapshot(control), "read_only": True})
            if len(items) >= 50:
                break
        search = self._find_search(entries)
        # An independently identified store document with its navigation is enough
        # to read a page. A missing search box does not invalidate that evidence.
        verified = bool(entries and (navigation or items or search is not None))
        loading = any("加载中" in name for name in texts)
        fingerprint = hashlib.sha256(json.dumps(
            {"items": [item["name"] for item in items], "sections": sections,
             "empty": [name for name in texts if name in EMPTY_NAMES]},
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")).hexdigest() if verified else None
        selected_tabs = [tab["name"] for tab in tabs if tab["selected"]]
        selected_categories = [category["name"] for category in categories if category["selected"]]
        return StoreObservation(
            "available" if verified else "needs_gui", "store_document_readback" if verified else "unverified", True,
            current_query=readable_value(search) if search is not None else None,
            current_category=selected_categories[0] if len(selected_categories) == 1 else None,
            items=tuple(items), tabs=tuple(tabs), categories=tuple(categories), sections=sections,
            loading=loading, page_fingerprint=fingerprint,
            reason=None if verified else "store_content_not_observed",
            evidence={"store_window": control_snapshot(region),
                      "current_tab": selected_tabs[0] if len(selected_tabs) == 1 else None,
                      "selection_exposed": bool(selected_categories or selected_tabs)},
        )

    def _find_store_button(self, window: Any) -> Any | None:
        matches = [control for control, _ in self._walk(window, max_depth=5)
                   if control.ControlTypeName == "ButtonControl" and control.Name in STORE_NAMES
                   and visible(control)]
        return matches[0] if len(matches) == 1 else None

    def diagnostic(self) -> dict:
        try:
            with self._session() as (_, window, probe):
                observation = self._read(window)
                _, entries = self._page(window)
                navigation = self._navigation(entries)
                search = self._find_search(entries)
                submit = self._search_submit(entries)
                effective = self._effective_store(window)
                operations = {
                    "state": {"status": observation.status, "verification": observation.verification},
                    "open": {
                        "status": "already_open" if observation.status == "available"
                        else "available" if self._open_button(effective) is not None
                        else "needs_gui",
                        "reason": "store_already_open" if observation.status == "available"
                        else None if self._open_button(effective) is not None
                        else "store_open_control_not_verified",
                    },
                    "browse": {"status": "available" if observation.status == "available" and navigation else "needs_gui",
                               "reason": None if navigation else "store_navigation_not_observed",
                               "verification": "navigation_target_and_page_readback_per_call"},
                    "search": {"status": "available" if search is not None and submit is not None else "unsupported" if observation.status == "available" else "needs_gui",
                               "reason": None if search is not None and submit is not None else "store_search_control_not_present" if search is None else "store_search_submit_not_observed"},
                }
                result = {
                    "status": observation.status, "adapter": "uiautomation", "lane": "local_desktop",
                    "verification": observation.verification, "reason": observation.reason,
                    "window": control_snapshot(window), "window_probe": probe,
                    "store": observation.as_dict(), "operations": operations,
                }
        except ServiceError as exc:
            result = {"status": "needs_gui", "adapter": "uiautomation", "lane": "local_desktop",
                      "verification": "unverified", "reason": exc.details["reason"]}
        if self._last_action is not None:
            result["last_action"] = {**self._last_action, "is_history": True}
        self._last_diagnostic = result
        return dict(result)

    def status(self) -> CapabilityState:
        return self.diagnostic()["status"]

    def observe(self) -> StoreObservation:
        with self._session() as (_, window, _):
            return self._read(window)

    def open_store(self) -> StoreObservation:
        with self._session() as (automation, window, _):
            existing = self._read(window)
            if existing.status == "available":
                return replace(existing, verification="store_already_open_readback")
            button = self._open_button(self._effective_store(window))
            invoke = pattern(button, "GetInvokePattern") if button is not None else None
            if invoke is None:
                from .material_store_vision import StoreOpenerVision, StoreVisionError

                if self._opener_vision is None:
                    self._opener_vision = StoreOpenerVision()
                try:
                    evidence = self._opener_vision.open(automation, window)
                except StoreVisionError as exc:
                    error = self._fail(str(exc))
                    if exc.labels is not None:
                        error.details["observed_navigation_labels"] = exc.labels
                    raise error from exc
            else:
                invoke.Invoke()
                evidence = {"method": "uia_invoke", "target": control_snapshot(button)}
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                after = self._read(window)
                if after.status == "available" and not after.loading and (after.items or after.sections):
                    result = replace(after, verification="store_open_and_document_readback",
                                     evidence={**(after.evidence or {}), "opener": evidence})
                    self._last_action = {"ok": True, "operation": "open",
                                         "verification": result.verification, "evidence": result.evidence}
                    return result
                time.sleep(0.15)
            raise self._fail("store_open_not_confirmed", action=True)

    def _navigation_target(
        self, entries: list[tuple[Any, int]], category: str
    ) -> Any:
        matches = [control for control in self._navigation(entries)
                   if label_key(control.Name) == label_key(category)]
        if len(matches) != 1:
            raise self._fail("store_category_not_unique")
        return matches[0]

    @staticmethod
    def _semantic_navigation_action(control: Any) -> str | None:
        if pattern(control, "GetSelectionItemPattern") is not None:
            return "uia_selection"
        if pattern(control, "GetInvokePattern") is not None:
            return "uia_invoke"
        return None

    @staticmethod
    def _same_control(left: Any, right: Any) -> bool:
        if left is right:
            return True
        try:
            return tuple(left.GetRuntimeId()) == tuple(right.GetRuntimeId())
        except Exception:
            return False

    def _click_navigation(self, automation: Any, window: Any, target: Any) -> str:
        selection = pattern(target, "GetSelectionItemPattern")
        if selection is not None:
            selection.Select()
            return "uia_selection"
        invoke = pattern(target, "GetInvokePattern")
        if invoke is not None:
            invoke.Invoke()
            return "uia_invoke"
        region, _ = self._page(window)
        if region is None or not getattr(region, "NativeWindowHandle", 0):
            raise self._fail("store_navigation_window_not_verified")
        region.SetActive(waitTime=0.1)
        if automation.GetForegroundControl().NativeWindowHandle != region.NativeWindowHandle:
            raise self._fail("store_navigation_window_not_foreground")
        _, entries = self._page(window)
        fresh = self._navigation_target(entries, target.Name)
        if not self._same_control(target, fresh):
            raise self._fail("store_navigation_target_changed")
        rectangle, outer = fresh.BoundingRectangle, region.BoundingRectangle
        x, y = (rectangle.left + rectangle.right) // 2, (rectangle.top + rectangle.bottom) // 2
        if not (rectangle.right > rectangle.left and rectangle.bottom > rectangle.top
                and outer.left <= x < outer.right and outer.top <= y < outer.bottom):
            raise self._fail("store_navigation_bounds_not_verified")
        hit = automation.ControlFromPoint(x, y)
        for _ in range(6):
            if hit is None or self._same_control(hit, fresh):
                break
            hit = hit.GetParentControl()
        if hit is None or not self._same_control(hit, fresh):
            raise self._fail("store_navigation_hit_test_mismatch")
        automation.Click(x, y, waitTime=0.1)
        return "uia_bounds_with_hit_test"

    def _wait_change(self, window: Any, before: StoreObservation, category: str | None = None) -> StoreObservation:
        deadline = time.monotonic() + 10
        stable_fingerprint, stable_since = None, 0.0
        while time.monotonic() < deadline:
            after = self._read(window)
            if after.status != "available":
                raise self._fail("store_view_lost_during_navigation", action=True)
            selected = category is not None and after.current_category is not None and label_key(after.current_category) == label_key(category)
            changed = after.page_fingerprint != before.page_fingerprint
            # A loading placeholder is not the target page. Require settled content
            # or an explicit UIA selection; two readbacks avoid transient empty grids.
            settled = not after.loading and bool(after.items or after.sections)
            if not after.loading and (selected or changed and settled):
                if after.page_fingerprint != stable_fingerprint:
                    stable_fingerprint, stable_since = after.page_fingerprint, time.monotonic()
                elif time.monotonic() - stable_since >= 0.2:
                    return after
            else:
                stable_fingerprint = None
            time.sleep(0.15)
        raise self._fail("store_view_change_not_confirmed", action=True)

    def browse(self, category: str) -> StoreObservation:
        with self._session() as (automation, window, _):
            before = self._read(window)
            if before.status != "available" or before.loading:
                raise self._fail("store_page_not_ready")
            _, entries = self._page(window)
            target = self._navigation_target(entries, category)
            if self._selected(target):
                selected = replace(
                    before,
                    verification="selected_navigation_readback",
                    requested_category=category,
                )
                self._last_action = {
                    "ok": True,
                    "operation": "browse",
                    "requested_category": category,
                    "verification": selected.verification,
                }
                return selected
            method = self._click_navigation(automation, window, target)
            after = self._wait_change(window, before, category)
            evidence = {**(after.evidence or {}), "target": control_snapshot(target), "method": method,
                        "before_fingerprint": before.page_fingerprint,
                        "after_fingerprint": after.page_fingerprint,
                        "page_changed": after.page_fingerprint != before.page_fingerprint,
                        "observed_item_count": len(after.items)}
            result = replace(after, verification="navigation_and_page_readback",
                             requested_category=category, evidence=evidence)
            self._last_action = {"ok": True, "operation": "browse", "requested_category": category,
                                 "verification": result.verification, "evidence": evidence}
            return result

    def _effective_store(self, window: Any) -> Any:
        candidates = self._find_store_candidates(window)
        return candidates[0] if len(candidates) == 1 else window

    def _open_button(self, root: Any) -> Any | None:
        matches = [control for control, _ in self._walk(root, max_depth=5)
                   if control.ControlTypeName == "ButtonControl" and control.Name in STORE_NAMES
                   and visible(control) and pattern(control, "GetInvokePattern") is not None]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _search_submit(entries: list[tuple[Any, int]]) -> Any | None:
        matches = [control for control, _ in entries
                   if control.ControlTypeName == "ButtonControl" and control.Name == "搜索"
                   and visible(control) and pattern(control, "GetInvokePattern") is not None]
        return matches[0] if len(matches) == 1 else None

    def search(self, query: str) -> StoreObservation:
        with self._session() as (_, window, _):
            before = self._read(window)
            if before.status != "available" or before.loading:
                raise self._fail("store_page_not_ready")
            _, entries = self._page(window)
            search = self._find_search(entries)
            if search is None:
                raise self._fail("store_search_control_not_present", unsupported=True)
            submit = self._search_submit(entries)
            if submit is None:
                raise self._fail("store_search_submit_not_observed", unsupported=True)
            value = pattern(search, "GetValuePattern")
            if value is None or value.IsReadOnly:
                raise self._fail("store_search_value_not_writable", unsupported=True)
            value.SetValue(query)
            if readable_value(search) != query:
                raise self._fail("store_search_readback_mismatch", action=True)
            submit.GetInvokePattern().Invoke()
            after = self._wait_change(window, before)
            if after.current_query != query:
                raise self._fail("store_search_readback_mismatch", action=True)
            return replace(after, verification="search_submit_and_page_readback")

    def library(self, operation: str, value: str = "") -> StoreObservation:
        from .material_store_vision import StoreVisionError
        from .sticker_library import StickerLibraryVision

        with self._session() as (automation, window, _):
            stores = self._find_store_candidates(window)
            if stores:
                if operation == "state":
                    raise self._fail("store_window_overlays_sticker_library")
                if len(stores) != 1:
                    raise self._fail("store_window_not_unique")
                store = stores[0]
                if (not store.ClassName.startswith("MaterialStoreWindow_")
                        or not store.NativeWindowHandle or store.ProcessId != window.ProcessId):
                    raise self._fail("store_close_window_not_verified")
                # Alt+F4 is forwarded by this Qt app to the editor. Address only
                # the independently verified store HWND with the native close message.
                import ctypes
                from ctypes import wintypes

                close = ctypes.windll.user32.PostMessageW
                close.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
                close.restype = wintypes.BOOL
                if not close(store.NativeWindowHandle, 0x0010, 0, 0):
                    raise self._fail("store_close_failed", action=True)
                deadline = time.monotonic() + 5
                while self._find_store_candidates(window):
                    if time.monotonic() >= deadline:
                        raise self._fail("store_close_not_confirmed", action=True)
                    time.sleep(0.15)
            if self._library_vision is None:
                self._library_vision = StickerLibraryVision()
            try:
                result = self._library_vision.run(automation, window, operation, value)
            except StoreVisionError as exc:
                error = self._fail(str(exc), action=True)
                error.details["surface"] = "sticker_library"
                if exc.failure_type:
                    error.details["failure_type"] = exc.failure_type
                raise error from exc
            if operation != "state":
                self._last_action = {"ok": True, "surface": result.surface,
                                     "operation": operation, "verification": result.verification}
            return result

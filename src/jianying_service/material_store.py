from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from .errors import ServiceError


CapabilityState = str
Action = Callable[[], "StoreObservation"]


class MaterialStoreAdapter(Protocol):
    def status(self) -> CapabilityState: ...

    def diagnostic(self) -> dict: ...

    def observe(self) -> "StoreObservation": ...

    def open_store(self) -> "StoreObservation": ...

    def search(self, query: str) -> "StoreObservation": ...

    def browse(self, category: str) -> "StoreObservation": ...

    def library(self, operation: str, value: str = "") -> "StoreObservation": ...


@dataclass(frozen=True)
class StoreObservation:
    status: CapabilityState
    verification: str
    store_open: bool
    current_query: str | None = None
    current_category: str | None = None
    items: tuple[dict, ...] = ()
    reason: str | None = None
    tabs: tuple[dict, ...] = ()
    categories: tuple[dict, ...] = ()
    sections: tuple[str, ...] = ()
    loading: bool = False
    page_fingerprint: str | None = None
    requested_category: str | None = None
    evidence: dict | None = None
    surface: str = "material_store"
    visible_text: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        payload = {
            "status": self.status,
            "surface": self.surface,
            "verification": self.verification,
            "store_open": self.store_open,
            "current_query": self.current_query,
            "current_category": self.current_category,
            "items": list(self.items),
            "tabs": list(self.tabs),
            "categories": list(self.categories),
            "sections": list(self.sections),
            "loading": self.loading,
            "page_fingerprint": self.page_fingerprint,
            "visible_text": list(self.visible_text),
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.requested_category is not None:
            payload["requested_category"] = self.requested_category
        if self.evidence is not None:
            payload["evidence"] = self.evidence
        return payload


class WindowProbe(Protocol):
    def probe(self) -> dict: ...


class MaterialStoreService:
    """Expose store reading and navigation, with evidence scoped to each operation."""

    def __init__(
        self,
        adapter: MaterialStoreAdapter | None = None,
        window_probe: WindowProbe | None = None,
    ) -> None:
        if adapter is None:
            from .material_store_uia import UiaMaterialStoreAdapter

            adapter = UiaMaterialStoreAdapter(window_probe)  # type: ignore[arg-type]
            if window_probe is None:
                from .material_store_uia import UiaWindowProbe

                window_probe = UiaWindowProbe()
        self.adapter: MaterialStoreAdapter = adapter
        self.window_probe: WindowProbe | None = window_probe
        self._lock = threading.Lock()

    def _run(self, action: Action) -> dict:
        with self._lock:
            try:
                observation = action()
            except ServiceError:
                raise
            except Exception as exc:
                raise ServiceError(
                    "MATERIAL_STORE_PROBE_FAILED",
                    "The local store UI probe failed",
                    status=503,
                    details={
                        "status": "needs_gui",
                        "needs_gui": True,
                        "unsupported": False,
                        "reason": "ui_adapter_error",
                    },
                ) from exc
        if observation.status != "available":
            raise ServiceError(
                "MATERIAL_STORE_UI_UNVERIFIED",
                "Material store controls have not been verified",
                status=503,
                details={
                    "status": observation.status,
                    "needs_gui": True,
                    "unsupported": observation.status == "unsupported",
                    "reason": observation.reason or "ui_state_not_verified",
                    "observation": observation.as_dict(),
                },
            )
        return {"ok": True, **observation.as_dict()}

    def status(self) -> dict:
        try:
            with self._lock:
                diagnostic = self.adapter.diagnostic()
            state = diagnostic.get("status")
        except Exception as exc:
            raise ServiceError(
                "MATERIAL_STORE_PROBE_FAILED",
                "The local store diagnostic probe failed",
                status=503,
                details={
                    "status": "needs_gui",
                    "needs_gui": True,
                    "unsupported": False,
                    "reason": "ui_adapter_error",
                },
            ) from exc
        if (
            state not in {"available", "needs_gui", "unsupported"}
            or (
                state == "available"
                and diagnostic.get("store", {}).get("status") != "available"
            )
        ):
            raise ServiceError(
                "MATERIAL_STORE_PROBE_FAILED",
                "The local store capability probe returned inconsistent evidence",
                status=503,
                details={
                    "status": "needs_gui",
                    "needs_gui": True,
                    "unsupported": False,
                    "reason": "inconsistent_ui_evidence",
                },
            )
        envelope = status_envelope(state, diagnostic)
        if "window_probe" in diagnostic:
            envelope["window_probe"] = diagnostic["window_probe"]
        elif self.window_probe is not None:
            try:
                envelope["window_probe"] = self.window_probe.probe()
            except Exception:
                envelope["window_probe"] = {
                    "status": "needs_gui",
                    "desktop_children": None,
                    "jianying_windows": [],
                    "reason": "desktop_probe_error",
                }
        return envelope

    def diagnostic(self) -> dict:
        return self.status()

    def capabilities(self) -> dict:
        return self.status()

    def observe(self) -> dict:
        return self._run(self.adapter.observe)

    def open_store(self) -> dict:
        return self._run(self.adapter.open_store)

    def search(self, query: str) -> dict:
        return self._run(lambda: self.adapter.search(query))

    def browse(self, category: str) -> dict:
        return self._run(lambda: self.adapter.browse(category))

    def library(self, operation: str, value: str = "") -> dict:
        return self._run(lambda: self.adapter.library(operation, value))


class UnverifiedMaterialStoreAdapter:
    def __init__(self, reason: str = "ui_adapter_not_configured") -> None:
        self.reason = reason

    def status(self) -> CapabilityState:
        return "needs_gui"

    def diagnostic(self) -> dict:
        return {
            "status": "needs_gui",
            "adapter": None,
            "reason": self.reason,
        }

    def observe(self) -> StoreObservation:
        return StoreObservation("needs_gui", "unverified", False, reason=self.reason)

    open_store = observe

    def search(self, query: str) -> StoreObservation:
        return self.observe()

    def browse(self, category: str) -> StoreObservation:
        return self.observe()

    def library(self, operation: str, value: str = "") -> StoreObservation:
        return self.observe()


def status_envelope(state: CapabilityState, diagnostic: dict) -> dict:
    details = diagnostic.get("operations", {})
    # An available page reader is not evidence that search or navigation works.
    operations = {
        "status": "available",
        "diagnose": "available",
        "state": state,
        "open": "needs_gui",
        "search": "needs_gui",
        "browse": "needs_gui",
        "sticker_library_state": "needs_gui",
        "sticker_library_open": "needs_gui",
        "sticker_library_search": "needs_gui",
        "sticker_library_browse": "needs_gui",
    }
    for name in operations:
        if name in details:
            operations[name] = details[name]["status"]
    return {
        "ok": True,
        "status": state,
        "verified": state == "available",
        "verification_scope": "page_state",
        "all_operations_available": all(
            value in {"available", "already_open"} for value in operations.values()
        ),
        "needs_gui": state != "available",
        "unsupported": state == "unsupported",
        "operations": operations,
        "operation_details": details,
        "diagnostic": diagnostic,
    }

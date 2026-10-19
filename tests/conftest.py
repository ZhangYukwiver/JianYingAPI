from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jianying_service.app import create_app
from jianying_service.config import Settings
from jianying_service.material_store import MaterialStoreService, StoreObservation


class LocalTestClient(TestClient):
    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("base_url", "http://127.0.0.1:8765")
        super().__init__(*args, **kwargs)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "runtime",
        media_roots=(tmp_path / "media",),
        host="127.0.0.1",
        port=8765,
    )


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        "jianying_service.material_store_uia.jianying_windows",
        lambda _: [],
    )
    return LocalTestClient(create_app(settings=settings))


class FakeMaterialStoreAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.open = False
        self.query = ""
        self.category: str | None = None
        self.open_fails = False
        self.state = "available"

    def status(self) -> str:
        self.calls.append(("status",))
        return self.state

    def diagnostic(self) -> dict:
        return {
            "status": self.state,
            "verification": (
                "controls_and_readback"
                if self.state == "available"
                else "unverified"
            ),
            "adapter": "fake-uia",
            "reason": None if self.state == "available" else "fake_limit",
            "store": {
                "status": self.state,
                "verification": "controls_and_readback",
            },
        }

    def _observation(self) -> StoreObservation:
        return StoreObservation(
            status=self.state,
            verification="fake:invoke-pattern/selection/value/readback",
            store_open=self.open,
            current_query=self.query,
            current_category=self.category,
            items=(
                {"name": "只读预览条目", "type": "ListItemControl", "read_only": True},
            )
            if self.open
            else (),
        )

    def observe(self) -> StoreObservation:
        self.calls.append(("observe",))
        return self._observation()

    def open_store(self) -> StoreObservation:
        self.calls.append(("open",))
        if self.open_fails:
            return StoreObservation(
                "needs_gui",
                "unverified",
                False,
                reason="store_open_not_confirmed",
            )
        self.open = True
        return self._observation()

    def search(self, query: str) -> StoreObservation:
        self.calls.append(("search", query))
        self.query = query
        return self._observation()

    def browse(self, category: str) -> StoreObservation:
        self.calls.append(("browse", category))
        self.category = category
        return self._observation()

    def library(self, operation: str, value: str = "") -> StoreObservation:
        self.calls.append(("library", operation, value))
        return StoreObservation(
            "available",
            "fake:sticker_library_readback",
            False,
            current_query=value if operation == "search" else None,
            surface="sticker_library",
            evidence={"operation": operation},
        )


@pytest.fixture
def material_store() -> FakeMaterialStoreAdapter:
    return FakeMaterialStoreAdapter()


@pytest.fixture
def store_client(
    settings: Settings, material_store: FakeMaterialStoreAdapter
) -> TestClient:
    return LocalTestClient(
        create_app(
            settings=settings,
            material_store=MaterialStoreService(material_store),
        )
    )


def create_project(client: TestClient, name: str = "Test project") -> dict:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["project"]


def create_track(
    client: TestClient, project: dict, track_type: str
) -> dict:
    response = client.post(
        f"/api/projects/{project['id']}/tracks",
        json={"track_type": track_type},
    )
    assert response.status_code == 200, response.text
    return response.json()["project"]

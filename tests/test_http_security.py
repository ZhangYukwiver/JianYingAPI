from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jianying_service.app import create_app
from jianying_service.config import Settings


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_local_clients_and_same_origin_docs_work(settings: Settings, host: str) -> None:
    with TestClient(create_app(settings=settings), base_url=f"http://{host}") as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert client.post(
            "/api/projects",
            json={"name": "Local browser"},
            headers={"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"},
        ).status_code == 200
        assert client.get("/api/health").json()["status"] == "ok"


@pytest.mark.parametrize(
    "headers,code,status",
    [
        ({"Host": "rebind.example:8765"}, "HTTP_HOST_REJECTED", 400),
        ({"Host": "127.0.0.1.example:8765"}, "HTTP_HOST_REJECTED", 400),
        ({"Host": "localhost@rebind.example:8765"}, "HTTP_HOST_REJECTED", 400),
        ({"Host": "localhost:0"}, "HTTP_HOST_REJECTED", 400),
        ({"Origin": "https://untrusted.example"}, "HTTP_ORIGIN_REJECTED", 403),
        ({"Origin": "http://127.0.0.1:9000"}, "HTTP_ORIGIN_REJECTED", 403),
        ({"Origin": "http://localhost:8765"}, "HTTP_ORIGIN_REJECTED", 403),
        ({"Origin": "null"}, "HTTP_ORIGIN_REJECTED", 403),
        ({"Sec-Fetch-Site": "cross-site"}, "HTTP_ORIGIN_REJECTED", 403),
        ({"Sec-Fetch-Site": "same-site"}, "HTTP_ORIGIN_REJECTED", 403),
    ],
)
def test_untrusted_browser_requests_cannot_read_or_write(
    settings: Settings, headers: dict[str, str], code: str, status: int
) -> None:
    with TestClient(
        create_app(settings=settings), base_url="http://127.0.0.1:8765"
    ) as client:
        for response in (
            client.get("/api/projects", headers=headers),
            client.post("/api/projects", json={"name": "Rejected"}, headers=headers),
        ):
            assert response.status_code == status
            assert response.json()["error"]["code"] == code
        assert client.get("/api/projects").json()["projects"] == []


def test_duplicate_hosts_and_origins_are_rejected(settings: Settings) -> None:
    with TestClient(
        create_app(settings=settings), base_url="http://127.0.0.1:8765"
    ) as client:
        assert client.get(
            "/api/health", headers=[("Host", "127.0.0.1"), ("Host", "untrusted.example")]
        ).status_code == 400
        assert client.get(
            "/api/health",
            headers=[("Origin", "http://127.0.0.1:8765"), ("Origin", "null")],
        ).status_code == 403

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from jianying_service import membership as mod
from jianying_service.membership import (
    MembershipReader,
    parse_expiry,
    read_membership,
    tier_of,
)

from conftest import LocalTestClient
from jianying_service.app import create_app
from jianying_service.config import Settings
from jianying_service.mcp_server import create_server
from jianying_service.user_info import JianyingUserReader

# Geometry mirrors the crop taken from Jianying 11.6's home window.
WIDTH = 1261
HEIGHT = 491
NAME_Y = 107
HOME_Y = 163
TIER_Y = 271
EXPIRY_Y = 329


def row(text: str, y: int, x: int = 200, score: float = 0.97) -> tuple:
    half_w, half_h = 70, 15
    return (
        [
            [x - half_w, y - half_h],
            [x + half_w, y - half_h],
            [x + half_w, y + half_h],
            [x - half_w, y + half_h],
        ],
        text,
        score,
    )


def badge(*extra: tuple, name: str | None = "zzzz张") -> list[tuple]:
    rows = list(extra)
    if name is not None:
        rows.append(row(name, NAME_Y, x=120))
    rows.append(row("抖音主页>", HOME_Y, x=200))
    return rows


class FakeRect:
    def __init__(self) -> None:
        self.left = 100
        self.top = 50
        self.right = 100 + WIDTH
        self.bottom = 50 + HEIGHT


class FakeWindow:
    def __init__(self, *, name: str = "剪映专业版", class_name: str = "HomePage_QMLTYPE_214") -> None:
        self.Name = name
        self.ClassName = class_name
        self.BoundingRectangle = FakeRect()
        self.NativeWindowHandle = 4242
        self.active = False

    def SetActive(self, waitTime: float = 0.1) -> None:
        self.active = True


class FakeControl:
    def __init__(self, handle: int) -> None:
        self.NativeWindowHandle = handle


class FakeAutomation:
    def __init__(self, windows: list[FakeWindow], foreground: int | None = 4242) -> None:
        self._windows = windows
        self._foreground = foreground

    def GetRootControl(self) -> object:
        windows = self._windows
        return type("Root", (), {"GetChildren": lambda self: list(windows)})()

    def GetForegroundControl(self) -> FakeControl:
        return FakeControl(self._foreground or 0)


class FakeImage:
    width = WIDTH
    height = HEIGHT


class FakeEngine:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.calls = 0

    def __call__(self, image: object) -> tuple:
        self.calls += 1
        return self._rows, 0.01


def reader(rows: list[tuple], *, windows=None, foreground=4242, today=None) -> MembershipReader:
    return MembershipReader(
        automation=FakeAutomation(
            windows if windows is not None else [FakeWindow()], foreground
        ),
        engine=FakeEngine(rows),
        capture=lambda window: FakeImage(),
        today=today,
    )


@pytest.fixture(autouse=True)
def ocr_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "ocr_available", lambda: True)


def test_reads_svip_tier_and_expiry() -> None:
    result = reader(
        badge(row("SVIP", TIER_Y, x=90, score=0.98), row("2026.11.05 到期 >", EXPIRY_Y, x=160, score=0.97)),
        today=date(2026, 10, 23),
    ).read()

    assert result["available"] is True
    assert result["reason"] is None
    assert result["needs_gui"] is True
    assert result["read_only"] is True
    assert result["data_source"] == "jianying_account_badge"
    membership = result["membership"]
    assert membership["status"] == "svip"
    assert membership["label"] == "SVIP"
    assert membership["expires_on"] == "2026-11-05"
    assert membership["expired"] is False
    assert membership["days_left"] == 13
    assert membership["display_name_hint"] == "zzzz张"
    assert membership["confidence"] == pytest.approx(0.98)
    assert "SVIP" in result["evidence"]


def test_vip_is_not_mistaken_for_svip() -> None:
    result = read_membership(
        badge(row("VIP", TIER_Y, score=0.95)), WIDTH, HEIGHT
    )
    assert result["membership"]["status"] == "vip"
    assert result["membership"]["expires_on"] is None


def test_expired_membership_reports_negative_days() -> None:
    result = reader(
        badge(row("SVIP", TIER_Y), row("2026.11.05 到期", EXPIRY_Y)),
        today=date(2026, 12, 1),
    ).read()

    assert result["membership"]["expired"] is True
    assert result["membership"]["days_left"] == -26


def test_free_account_uses_upsell_label() -> None:
    result = read_membership(
        badge(row("开通会员", TIER_Y, score=0.93)), WIDTH, HEIGHT
    )

    assert result["membership"]["status"] == "free"
    assert result["membership"]["expires_on"] is None
    assert result["membership"]["label"] == "开通会员"


def test_unreadable_badge_is_a_reason_not_a_guess() -> None:
    result = read_membership(badge(), WIDTH, HEIGHT)

    assert "membership" not in result
    assert result["reason"] == "badge_not_readable"


def test_two_tiers_in_the_block_are_rejected() -> None:
    result = read_membership(
        badge(row("SVIP", TIER_Y), row("VIP", TIER_Y + 40, x=600)), WIDTH, HEIGHT
    )

    assert result["reason"] == "badge_ambiguous"


def test_expiry_above_the_tier_is_not_used() -> None:
    result = read_membership(
        badge(row("SVIP", TIER_Y), row("2025.01.01 到期", EXPIRY_Y - 120)), WIDTH, HEIGHT
    )

    assert result["membership"]["status"] == "svip"
    assert result["membership"]["expires_on"] is None


def test_expiry_far_below_the_tier_is_not_used() -> None:
    result = read_membership(
        badge(row("SVIP", TIER_Y), row("2025.01.01 到期", HEIGHT - 5)), WIDTH, HEIGHT
    )

    assert result["membership"]["expires_on"] is None


def test_missing_window_reports_needs_gui() -> None:
    result = reader([], windows=[]).read()

    assert result["available"] is False
    assert result["reason"] == "window_not_found"
    assert result["membership"] is None


def test_background_window_is_refused_without_stealing_focus() -> None:
    window = FakeWindow()
    result = reader(
        badge(row("SVIP", TIER_Y)), windows=[window], foreground=999
    ).read()

    assert result["available"] is False
    assert result["reason"] == "window_not_foreground"
    assert "focus=true" in result["hint"]
    assert window.active is False


def test_focus_flag_raises_the_window_first() -> None:
    window = FakeWindow()
    automation = FakeAutomation([window], foreground=999)
    fake = MembershipReader(
        automation=automation,
        engine=FakeEngine(badge(row("SVIP", TIER_Y), row("2026.11.05 到期", EXPIRY_Y))),
        capture=lambda w: FakeImage(),
        today=date(2026, 10, 23),
    )
    # The fake window becomes foreground once activated, as Win32 would.
    original = window.SetActive

    def activate(waitTime: float = 0.1) -> None:
        original(waitTime)
        automation._foreground = window.NativeWindowHandle

    window.SetActive = activate  # type: ignore[method-assign]
    result = fake.read(focus=True)

    assert window.active is True
    assert result["available"] is True
    assert result["membership"]["status"] == "svip"


def test_tray_window_name_is_ignored() -> None:
    tray = FakeWindow(name="JianyingProTray", class_name="TrayWindow")
    result = reader(badge(row("SVIP", TIER_Y)), windows=[tray]).read()

    assert result["reason"] == "window_not_found"


def test_missing_ocr_dependency_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "ocr_available", lambda: False)
    result = reader(badge(row("SVIP", TIER_Y))).read()

    assert result["available"] is False
    assert result["reason"] == "ocr_dependency_missing"
    assert reader([]).status()["reason"] == "ocr_dependency_missing"


def test_status_declares_gui_requirement() -> None:
    status = reader([]).status()

    assert status["available"] is True
    assert status["needs_gui"] is True
    assert status["read_only"] is True
    assert status["verification"] == "window_ocr_with_geometry_check"


def test_helpers_parse_tiers_and_dates() -> None:
    assert tier_of("SVIP") == "svip"
    assert tier_of("svip 会员") == "svip"
    assert tier_of("VIP") == "vip"
    assert tier_of("个人版") is None
    assert parse_expiry("2026.11.05 到期 >") == "2026-11-05"
    assert parse_expiry("2026 。 1 1 。 05") == "2026-11-05"
    assert parse_expiry("2026.13.40 到期") is None
    assert parse_expiry("到期") is None


def test_http_route_and_capabilities(settings: Settings, tmp_path: Path) -> None:
    client = LocalTestClient(
        create_app(
            settings=settings,
            user_info=JianyingUserReader(user_data_root=tmp_path / "absent"),
            membership=reader(
                badge(row("SVIP", TIER_Y), row("2026.11.05 到期", EXPIRY_Y)),
                today=date(2026, 10, 23),
            ),
        )
    )

    body = client.get("/api/jianying/membership").json()
    assert body["ok"] is True
    assert body["membership"]["status"] == "svip"
    assert client.get("/api/jianying/membership/status").json()["needs_gui"] is True
    capabilities = client.get("/api/capabilities").json()
    assert capabilities["jianying_membership"]["source"] == "account_badge_ocr"


def test_mcp_tools_expose_the_reader(settings: Settings) -> None:
    import asyncio
    import json

    server = create_server(
        settings=settings,
        membership=reader(
            badge(row("SVIP", TIER_Y), row("2026.11.05 到期", EXPIRY_Y)),
            today=date(2026, 10, 23),
        ),
    )
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"jianying_membership", "jianying_membership_status"} <= names

    result = asyncio.run(server.call_tool("jianying_membership", {}))
    payload = json.loads(result.content[0].text)
    assert payload["membership"]["status"] == "svip"

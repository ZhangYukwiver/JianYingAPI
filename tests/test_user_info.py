from __future__ import annotations

import json
from pathlib import Path

import pytest

from jianying_service.app import create_app
from jianying_service.config import Settings
from jianying_service.user_info import (
    JianyingUserReader,
    discover_user_data_root,
    mask_value,
    pick_uid,
    read_setting_file,
    UidCandidate,
)

from conftest import LocalTestClient

UID = "810812062057833"


def write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def build_user_data(root: Path, *, uid: str = UID) -> Path:
    user_data = root / "JianyingPro" / "User Data"
    write(
        user_data / "Config" / "globalSetting",
        "\n".join(
            [
                "[General]",
                "currentCustomDraftPath=D:\\\\Jianying Drafts",
                "customMaterialPath=D:\\\\Jianying Materials",
                "lastLoginPlatform=tiktok",
                "needWaterMark=false",
                "hardwareEncodeEnable=true",
                "firstStartupStamp=1782315281",
                f"removeAigcWatermark_{uid}=false",
                "timeCodeStyle=HH:MM:SS + 帧",
                "",
            ]
        ),
    )
    write(
        user_data / "Config" / "commonSetting.ini",
        "\n".join(
            [
                "[General]",
                "notification_client_version=11.6.0",
                "notification_client_app_user_mode_id=Bytedance.JianyingPro",
                "web_cover_login_id=a133e87e54229814e6ff48beb8083032",
                "showVipRes=false",
                "vipIconViewed=true",
                f"hub.plaza.last_tab.{uid}=template",
                "%U6325%U58A8%U4F53=D:/fonts/hui.otf",
                "",
            ]
        ),
    )
    write(
        user_data / "Config" / "vipRemindViewedInfo.ini",
        "\n".join(["[General]", f"{uid}_1783143689=true", f"{uid}_1785931760=true", ""]),
    )
    (user_data / "Projects" / f"com.lveditor.cloud.draft_{uid}").mkdir(parents=True)
    write(
        user_data / "Projects" / "com.lveditor.draft" / "root_meta_info.json",
        json.dumps(
            {
                "all_draft_store": [
                    {"draft_id": "a", "tm_draft_modified": 1792414852129668},
                    {"draft_id": "b", "tm_draft_modified": 1792375992000000},
                    {"draft_id": "c"},
                ]
            }
        ),
    )
    return user_data


@pytest.fixture
def user_data(tmp_path: Path) -> Path:
    return build_user_data(tmp_path)


def test_reads_account_identity_and_preferences(user_data: Path) -> None:
    result = JianyingUserReader(user_data_root=user_data).read()

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["network_access"] == "none"
    assert result["available"] is True
    account = result["account"]
    assert account["uid"] == UID
    assert account["uid_masked"] is False
    assert account["uid_confidence"] == "high"
    assert account["login_platform"] == "tiktok"
    assert account["cloud_draft_space"] is True
    assert account["signed_in_hint"] is True
    assert account["nickname"] is None
    assert account["avatar_url"] is None
    assert len(account["uid_sources"]) == 3
    assert account["uid_sources"][0].startswith("Projects/com.lveditor.cloud.draft_")
    assert account["local_web_cover_login_id"].startswith("a1")
    assert account["local_web_cover_login_id"].endswith("32")
    assert "a133e87e54229814e6ff48beb8083032" not in json.dumps(result)


def test_preferences_unescape_paths_and_decoded_keys(user_data: Path) -> None:
    result = JianyingUserReader(user_data_root=user_data).read()
    preferences = result["preferences"]

    assert preferences["draft_root"] == "D:\\Jianying Drafts"
    assert preferences["material_root"] == "D:\\Jianying Materials"
    assert preferences["export_watermark"] is False
    assert preferences["hardware_encode"] is True
    assert preferences["first_startup_at"].startswith("2026-06-")
    assert result["client"]["version"] == "11.6.0"
    assert result["client"]["app_user_mode_id"] == "Bytedance.JianyingPro"

    values = read_setting_file(
        user_data / "Config" / "commonSetting.ini", "common_setting"
    ).values
    assert values["挥墨体"] == "D:/fonts/hui.otf"


def test_draft_summary_counts_without_trusting_entries(user_data: Path) -> None:
    drafts = JianyingUserReader(user_data_root=user_data).read()["drafts"]

    assert drafts["index_readable"] is True
    assert drafts["local_count"] == 3
    assert drafts["last_modified_at"].startswith("2026-10-")
    assert drafts["cloud_space_present"] is True


def test_missing_profile_fields_are_reported_not_faked(user_data: Path) -> None:
    result = JianyingUserReader(user_data_root=user_data).read()
    fields = {item["field"]: item for item in result["unavailable"]}

    assert fields["account.nickname"]["reason"] == "remote_only"
    assert fields["account.avatar_url"]["reason"] == "remote_only"
    assert fields["account.membership"]["reason"] == "remote_only"
    assert "account.uid" not in fields
    assert result["entitlement_hints"]["not_membership_status"] is True
    assert result["entitlement_hints"]["vip_icon_viewed"] is True


def test_mask_uid_removes_every_plaintext_copy(user_data: Path) -> None:
    result = JianyingUserReader(user_data_root=user_data).read(mask_uid=True)
    encoded = json.dumps(result, ensure_ascii=False)

    assert UID not in encoded
    assert result["account"]["uid"] == mask_value(UID)
    assert result["account"]["uid_masked"] is True
    assert all(UID not in source for source in result["account"]["uid_sources"])
    assert all(
        UID not in name for name in result["drafts"]["cloud_space_directories"]
    )


def test_include_paths_false_drops_absolute_locations(user_data: Path) -> None:
    result = JianyingUserReader(user_data_root=user_data).read(include_paths=False)
    encoded = json.dumps(result, ensure_ascii=False)

    assert str(user_data) not in encoded
    assert result["user_data_root"] == "User Data"
    assert result["preferences"]["draft_root"] == "Jianying Drafts"
    assert result["account"]["uid_sources"] == 3
    assert result["drafts"]["cloud_space_directories"] == 1


def test_locked_files_degrade_instead_of_failing(
    user_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.read_bytes
    locked = {
        user_data / "Config" / "globalSetting",
        user_data / "Projects" / "com.lveditor.draft" / "root_meta_info.json",
    }

    def fake_read_bytes(self: Path) -> bytes:
        if self in locked:
            raise PermissionError("in use by another process")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    result = JianyingUserReader(user_data_root=user_data).read()

    assert result["available"] is True
    sources = {item["purpose"]: item for item in result["sources"]}
    assert sources["preferences and login platform"]["readable"] is False
    assert sources["preferences and login platform"]["reason"] == "file_locked"
    assert sources["local draft count"]["reason"] == "file_locked"
    assert result["drafts"]["local_count"] is None
    assert result["preferences"] == {}
    # The uid still comes from the directory name and the other setting file.
    assert result["account"]["uid"] == UID


def test_corrupt_draft_index_is_reported(user_data: Path) -> None:
    write(
        user_data / "Projects" / "com.lveditor.draft" / "root_meta_info.json", "{oops"
    )
    result = JianyingUserReader(user_data_root=user_data).read()

    assert result["drafts"]["index_readable"] is False
    assert result["drafts"]["reason"] == "invalid_json"
    assert result["drafts"]["local_count"] is None


def test_missing_user_data_returns_unsupported_payload(tmp_path: Path) -> None:
    reader = JianyingUserReader(user_data_root=tmp_path / "absent")
    result = reader.read()
    status = reader.status()

    assert result["available"] is False
    assert result["reason"] == "jianying_user_data_unavailable"
    assert result["account"] is None
    assert result["unavailable"][0]["field"] == "account"
    assert status["available"] is False
    assert status["read_only"] is True


def test_user_data_discovery_uses_local_app_data(tmp_path: Path) -> None:
    build_user_data(tmp_path)
    environ = {"LOCALAPPDATA": str(tmp_path)}

    assert discover_user_data_root(environ) == tmp_path / "JianyingPro" / "User Data"
    assert discover_user_data_root({}) is None
    assert JianyingUserReader(environ=environ).read()["account"]["uid"] == UID


def test_uid_voting_prefers_corroborated_candidate() -> None:
    uid, sources, confidence = pick_uid(
        [
            UidCandidate("111", "a:x_111"),
            UidCandidate("111", "a:y_111"),
            UidCandidate("222", "b:z_222"),
        ]
    )

    assert (uid, confidence) == ("111", "high")
    assert sources == ["a:x_111 (+1 more keys)"]
    assert pick_uid([]) == (None, [], None)


def test_without_uid_the_gap_is_named(tmp_path: Path) -> None:
    user_data = build_user_data(tmp_path)
    for path in user_data.rglob("*"):
        if path.is_file() and path.name != "root_meta_info.json":
            path.write_text("[General]\n", encoding="utf-8")
    for directory in (user_data / "Projects").iterdir():
        if directory.name.startswith("com.lveditor.cloud.draft_"):
            directory.rmdir()

    result = JianyingUserReader(user_data_root=user_data).read()
    fields = {item["field"]: item for item in result["unavailable"]}

    assert result["account"]["uid"] is None
    assert result["account"]["signed_in_hint"] is False
    assert fields["account.uid"]["reason"] == "not_present_locally"


def test_http_and_status_routes_expose_the_reader(
    settings: Settings, user_data: Path
) -> None:
    client = LocalTestClient(
        create_app(
            settings=settings,
            user_info=JianyingUserReader(user_data_root=user_data),
        )
    )

    response = client.get("/api/jianying/user")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["account"]["uid"] == UID

    masked = client.get("/api/jianying/user", params={"mask_uid": "true"}).json()
    assert masked["account"]["uid_masked"] is True

    status = client.get("/api/jianying/user/status").json()
    assert status["available"] is True
    assert status["read_only"] is True

    capabilities = client.get("/api/capabilities").json()
    assert capabilities["jianying_user"]["network_access"] == "none"
    assert capabilities["jianying_user"]["nickname"] == "remote_only"

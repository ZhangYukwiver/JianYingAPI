"""Read-only profile of the Jianying client that is installed on this machine.

Every value comes from files the Jianying desktop client already writes under
its own user data directory. Nothing here talks to a network endpoint, decrypts
a credential store, or writes a file. Fields that Jianying only holds remotely
(the display name and avatar of the signed-in account) are reported as
unavailable with a reason instead of being guessed.
"""

from __future__ import annotations

import json
import os
import re
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

USER_DATA_RELATIVE_PATH = Path("JianyingPro") / "User Data"
CONFIG_DIRECTORY = "Config"
PROJECTS_DIRECTORY = "Projects"
GLOBAL_SETTING = Path(CONFIG_DIRECTORY) / "globalSetting"
COMMON_SETTING = Path(CONFIG_DIRECTORY) / "commonSetting.ini"
VIP_REMIND = Path(CONFIG_DIRECTORY) / "vipRemindViewedInfo.ini"
LOCAL_DRAFT_INDEX = (
    Path(PROJECTS_DIRECTORY) / "com.lveditor.draft" / "root_meta_info.json"
)
CLOUD_DRAFT_PREFIX = "com.lveditor.cloud.draft_"
MAX_SETTING_BYTES = 4 * 1024 * 1024
MAX_INDEX_BYTES = 32 * 1024 * 1024
LOCAL_TIMEZONE = timezone(timedelta(hours=8))

CLOUD_DIRECTORY_UID = re.compile(r"^com\.lveditor\.cloud\.draft_(\d{6,25})$")
UID_KEY_SUFFIX = re.compile(r"[._](\d{10,25})$")
SETTING_LINE = re.compile(r"^(?P<key>[^=\r\n]+)=(?P<value>.*)$")

# Preference keys worth surfacing, mapped to the public name used in responses.
PREFERENCE_KEYS = {
    "lastLoginPlatform": "login_platform",
    "currentCustomDraftPath": "draft_root",
    "customMaterialPath": "material_root",
    "currentCachePath": "cache_root",
    "needWaterMark": "export_watermark",
    "hardwareEncodeEnable": "hardware_encode",
    "hardwareDecodeEnable": "hardware_decode",
    "enableAutoUpdate": "auto_update",
    "firstStartupStamp": "first_startup_at",
}

# Client-level values that describe the installed build rather than the account.
CLIENT_KEYS = {
    "notification_client_version": "version",
    "notification_client_app_user_mode_id": "app_user_mode_id",
    "notification_client_app_name": "app_name",
}


@dataclass(frozen=True)
class SettingFile:
    """One locally written setting file and what happened while reading it."""

    path: Path
    purpose: str
    values: dict[str, str] = field(default_factory=dict)
    reason: str | None = None

    @property
    def readable(self) -> bool:
        return self.reason is None


@dataclass(frozen=True)
class UidCandidate:
    uid: str
    source: str


def discover_user_data_root(
    environ: dict[str, str] | None = None,
) -> Path | None:
    """Return the Jianying user data directory when this machine has one."""
    environ = dict(os.environ if environ is None else environ)
    local_app_data = environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    root = Path(local_app_data) / USER_DATA_RELATIVE_PATH
    return root if root.is_dir() else None


def _decode_qsettings(text: str) -> str:
    """Undo the escaping Qt applies to special characters."""
    if "%" not in text and "\\\\" not in text:
        return text
    text = text.replace("\\\\", "\\")
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "%" and index + 1 < length:
            marker = text[index + 1]
            if marker in "uU" and index + 6 <= length:
                chunk = text[index + 2 : index + 6]
                if all(item in string.hexdigits for item in chunk):
                    out.append(chr(int(chunk, 16)))
                    index += 6
                    continue
            if index + 3 <= length:
                chunk = text[index + 1 : index + 3]
                if all(item in string.hexdigits for item in chunk):
                    out.append(chr(int(chunk, 16)))
                    index += 3
                    continue
        out.append(char)
        index += 1
    return "".join(out)


def read_setting_file(path: Path, purpose: str) -> SettingFile:
    """Parse a Qt QSettings style file without raising on local I/O problems."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return SettingFile(path=path, purpose=purpose, reason="missing")
    except OSError:
        return SettingFile(path=path, purpose=purpose, reason="unreadable")
    if size > MAX_SETTING_BYTES:
        return SettingFile(path=path, purpose=purpose, reason="too_large")
    try:
        raw = path.read_bytes()
    except PermissionError:
        # Jianying keeps several of these files open while it runs.
        return SettingFile(path=path, purpose=purpose, reason="file_locked")
    except OSError:
        return SettingFile(path=path, purpose=purpose, reason="unreadable")
    text = raw.decode("utf-8", errors="replace")
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("[", ";", "#")):
            continue
        match = SETTING_LINE.match(stripped)
        if not match:
            continue
        key = _decode_qsettings(match.group("key").strip())
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        values.setdefault(key, _decode_qsettings(value))
    return SettingFile(path=path, purpose=purpose, values=values)


def collect_uid_candidates(
    root: Path, settings: list[SettingFile]
) -> list[UidCandidate]:
    """Find the signed-in account id the client embeds in its own paths and keys."""
    candidates: list[UidCandidate] = []
    projects = root / PROJECTS_DIRECTORY
    if projects.is_dir():
        try:
            entries = sorted(projects.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            match = CLOUD_DIRECTORY_UID.match(entry.name)
            if match:
                candidates.append(
                    UidCandidate(match.group(1), f"{PROJECTS_DIRECTORY}/{entry.name}")
                )
    for setting in settings:
        for key in setting.values:
            match = UID_KEY_SUFFIX.search(key)
            if match:
                candidates.append(
                    UidCandidate(match.group(1), f"{setting.purpose}:{key}")
                )
    return candidates


def pick_uid(candidates: list[UidCandidate]) -> tuple[str | None, list[str], str | None]:
    """Choose the most corroborated uid and describe how strong that evidence is."""
    if not candidates:
        return None, [], None
    scores: dict[str, int] = {}
    sources: dict[str, list[str]] = {}
    for candidate in candidates:
        scores[candidate.uid] = scores.get(candidate.uid, 0) + 1
        bucket = sources.setdefault(candidate.uid, [])
        if candidate.source not in bucket:
            bucket.append(candidate.source)
    best = max(scores, key=lambda uid: (scores[uid], len(sources[uid]), uid))
    distinct_sources = len(sources[best])
    confidence = "high" if distinct_sources >= 2 else "single_source"
    return best, _summarize_sources(sources[best]), confidence


def _summarize_sources(sources: list[str]) -> list[str]:
    """Collapse repeated account-scoped keys of one file into a single entry."""
    seen: dict[str, str] = {}
    counts: dict[str, int] = {}
    order: list[str] = []
    for source in sources:
        prefix, _, _ = source.partition(":")
        if prefix not in order:
            order.append(prefix)
            seen[prefix] = source
            counts[prefix] = 1
        else:
            counts[prefix] += 1
    summary: list[str] = []
    for prefix in order:
        extra = counts[prefix] - 1
        summary.append(f"{seen[prefix]} (+{extra} more keys)" if extra else seen[prefix])
    return summary


def _local_draft_summary(path: Path) -> dict[str, Any]:
    """Count the drafts in Jianying's local index without trusting its contents."""
    summary: dict[str, Any] = {
        "local_count": None,
        "last_modified_at": None,
        "index_readable": False,
        "reason": None,
    }
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        summary["reason"] = "missing"
        return summary
    except OSError:
        summary["reason"] = "unreadable"
        return summary
    if size > MAX_INDEX_BYTES:
        summary["reason"] = "too_large"
        return summary
    try:
        raw = path.read_bytes()
    except PermissionError:
        summary["reason"] = "file_locked"
        return summary
    except OSError:
        summary["reason"] = "unreadable"
        return summary
    try:
        document = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        summary["reason"] = "invalid_json"
        return summary
    if not isinstance(document, dict):
        summary["reason"] = "unexpected_structure"
        return summary
    store = document.get("all_draft_store")
    if not isinstance(store, list):
        summary["reason"] = "unexpected_structure"
        return summary
    summary["index_readable"] = True
    summary["local_count"] = len(store)
    latest: str | None = None
    latest_value = 0
    for item in store:
        if not isinstance(item, dict):
            continue
        stamp = item.get("tm_draft_modified")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            continue
        if stamp > latest_value:
            latest_value = int(stamp)
    if latest_value:
        latest = _format_timestamp(latest_value)
    summary["last_modified_at"] = latest
    return summary


def _format_timestamp(value: int) -> str | None:
    if value <= 0 or value >= 2**63:
        return None
    divisor = 1_000_000 if value >= 10**14 else 1000
    try:
        return datetime.fromtimestamp(value / divisor, tz=LOCAL_TIMEZONE).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _format_unix_seconds(value: str | None) -> str | None:
    if not value:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    return _format_timestamp(seconds * 1000)


def mask_value(value: str) -> str:
    """Keep enough of an opaque identifier to recognise it, not to replay it."""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def _flag(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    return None


def _cloud_directory_label(
    name: str, *, mask_uid: bool, masking: Callable[[str], str]
) -> str:
    """Keep the account out of a cloud draft folder name when masking is on."""
    if not mask_uid or not name.startswith(CLOUD_DRAFT_PREFIX):
        return name
    return f"{CLOUD_DRAFT_PREFIX}{masking(name[len(CLOUD_DRAFT_PREFIX):])}"


class JianyingUserReader:
    """Assemble a read-only account picture for the local Jianying client."""

    def __init__(
        self,
        *,
        user_data_root: Path | None = None,
        environ: dict[str, str] | None = None,
        root_locator: Callable[[], Path | None] | None = None,
    ) -> None:
        if user_data_root is not None:
            self._root = Path(user_data_root)
        elif root_locator is not None:
            self._root = root_locator()
        else:
            self._root = discover_user_data_root(environ)

    @property
    def user_data_root(self) -> Path | None:
        return self._root

    def status(self) -> dict[str, Any]:
        """Report whether the read-only account surface can work here at all."""
        root = self._root
        return {
            "ok": True,
            "read_only": True,
            "available": bool(root and root.is_dir()),
            "reason": None if root and root.is_dir() else "jianying_user_data_unavailable",
            "user_data_root": str(root) if root else None,
            "network_access": "none",
        }

    def read(
        self,
        *,
        mask_uid: bool = False,
        include_paths: bool = True,
    ) -> dict[str, Any]:
        root = self._root
        generated_at = datetime.now(tz=LOCAL_TIMEZONE).isoformat()
        if root is None or not root.is_dir():
            return {
                "ok": True,
                "read_only": True,
                "network_access": "none",
                "generated_at": generated_at,
                "available": False,
                "reason": "jianying_user_data_unavailable",
                "user_data_root": None,
                "account": None,
                "client": None,
                "preferences": None,
                "drafts": None,
                "entitlement_hints": None,
                "sources": [],
                "unavailable": [
                    {
                        "field": "account",
                        "reason": "jianying_user_data_unavailable",
                        "detail": "JianyingPro\\User Data was not found for this user",
                    }
                ],
            }

        settings = [
            read_setting_file(root / GLOBAL_SETTING, "global_setting"),
            read_setting_file(root / COMMON_SETTING, "common_setting"),
            read_setting_file(root / VIP_REMIND, "vip_remind"),
        ]
        global_values = settings[0].values
        common_values = settings[1].values
        vip_values = settings[2].values

        uid, uid_sources, uid_confidence = pick_uid(
            collect_uid_candidates(root, settings)
        )
        cloud_space = root / PROJECTS_DIRECTORY
        cloud_dirs: list[str] = []
        if cloud_space.is_dir():
            try:
                cloud_dirs = sorted(
                    entry.name
                    for entry in cloud_space.iterdir()
                    if CLOUD_DIRECTORY_UID.match(entry.name)
                )
            except OSError:
                cloud_dirs = []

        login_platform = global_values.get("lastLoginPlatform")
        setting_paths = {setting.purpose: setting for setting in settings}
        local_web_id = common_values.get("web_cover_login_id")
        drafts = _local_draft_summary(root / LOCAL_DRAFT_INDEX)

        preferences: dict[str, Any] = {}
        for key, public_name in PREFERENCE_KEYS.items():
            if key not in global_values:
                continue
            value: Any = global_values[key]
            if key in {"needWaterMark", "hardwareEncodeEnable", "hardwareDecodeEnable", "enableAutoUpdate"}:
                value = _flag(value)
            elif key == "firstStartupStamp":
                value = _format_unix_seconds(value)
            if key.endswith(("Path",)) and not include_paths and isinstance(value, str):
                value = Path(value).name or value
            preferences[public_name] = value

        client: dict[str, Any] = {}
        for key, public_name in CLIENT_KEYS.items():
            if key in common_values:
                client[public_name] = common_values[key]

        unavailable: list[dict[str, Any]] = [
            {
                "field": "account.nickname",
                "reason": "not_in_local_files",
                "detail": (
                    "Jianying keeps the display name on its servers and never "
                    "caches it on disk. It is printed in the account badge, so "
                    "/api/jianying/membership reads it from the window via OCR."
                ),
            },
            {
                "field": "account.avatar_url",
                "reason": "remote_only",
                "detail": (
                    "The avatar is a remote image the client renders; no local "
                    "record of its URL exists."
                ),
            },
            {
                "field": "account.membership",
                "reason": "not_in_local_files",
                "detail": (
                    "VIP state is resolved by the client online and only lands in "
                    "encrypted storage. The badge shows it as text, so "
                    "/api/jianying/membership reads tier and expiry via OCR."
                ),
            },
        ]
        if uid is None:
            unavailable.append(
                {
                    "field": "account.uid",
                    "reason": "not_present_locally",
                    "detail": (
                        "No cloud draft folder or account-scoped setting key was "
                        "found; the client may never have been signed in here"
                    ),
                }
            )

        sources = [
            {
                "path": str(root / GLOBAL_SETTING) if include_paths else GLOBAL_SETTING.as_posix(),
                "purpose": "preferences and login platform",
                "readable": setting_paths["global_setting"].readable,
                "reason": setting_paths["global_setting"].reason,
            },
            {
                "path": str(root / COMMON_SETTING) if include_paths else COMMON_SETTING.as_posix(),
                "purpose": "client build and local UI switches",
                "readable": setting_paths["common_setting"].readable,
                "reason": setting_paths["common_setting"].reason,
            },
            {
                "path": str(root / VIP_REMIND) if include_paths else VIP_REMIND.as_posix(),
                "purpose": "account-scoped reminder keys",
                "readable": setting_paths["vip_remind"].readable,
                "reason": setting_paths["vip_remind"].reason,
            },
            {
                "path": str(root / LOCAL_DRAFT_INDEX) if include_paths else LOCAL_DRAFT_INDEX.as_posix(),
                "purpose": "local draft count",
                "readable": drafts["index_readable"],
                "reason": drafts["reason"],
            },
        ]

        drafts["cloud_space_present"] = bool(cloud_dirs)
        if include_paths:
            drafts["cloud_space_directories"] = [
                _cloud_directory_label(name, mask_uid=mask_uid, masking=mask_value)
                for name in cloud_dirs
            ]
        else:
            drafts["cloud_space_directories"] = len(cloud_dirs)

        entitlement_hint_keys = {
            "showVipRes": "vip_resources_visible",
            "showCaptionVipResource": "caption_vip_resources_visible",
            "retouchShowVipRes": "retouch_vip_resources_visible",
            "vipIconViewed": "vip_icon_viewed",
        }
        entitlement_hints = {
            public: _flag(common_values[key])
            for key, public in entitlement_hint_keys.items()
            if key in common_values
        }
        account_scoped_keys = sorted(
            key for key in vip_values if UID_KEY_SUFFIX.search(key)
        )
        entitlement_hints["account_scoped_key_count"] = len(account_scoped_keys)
        entitlement_hints["not_membership_status"] = True

        account = {
            "uid": mask_value(uid) if (uid and mask_uid) else uid,
            "uid_masked": bool(uid and mask_uid),
            "uid_sources": (
                [
                    source.replace(uid, mask_value(uid)) if uid else source
                    for source in uid_sources
                ]
                if include_paths
                else len(uid_sources)
            ),
            "uid_confidence": uid_confidence,
            "nickname": None,
            "avatar_url": None,
            "login_platform": login_platform,
            "signed_in_hint": bool(uid or cloud_dirs),
            "cloud_draft_space": bool(cloud_dirs),
            "local_web_cover_login_id": (
                mask_value(local_web_id) if local_web_id else None
            ),
        }

        return {
            "ok": True,
            "read_only": True,
            "network_access": "none",
            "generated_at": generated_at,
            "available": True,
            "reason": None,
            "user_data_root": str(root) if include_paths else root.name,
            "account": account,
            "client": client,
            "preferences": preferences,
            "drafts": drafts,
            "entitlement_hints": entitlement_hints,
            "sources": sources,
            "unavailable": unavailable,
        }

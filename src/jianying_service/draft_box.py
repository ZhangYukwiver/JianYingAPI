from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import ServiceError
from .storage import atomic_write


INDEX_RELATIVE_PATH = (
    Path("JianyingPro")
    / "User Data"
    / "Projects"
    / "com.lveditor.draft"
    / "root_meta_info.json"
)
REGISTRY_KEY = r"Software\ByteDance\JianyingPro\GlobalSettings\History"
REGISTRY_VALUE = "currentCustomDraftPath"
MAX_INDEX_BYTES = 16 * 1024 * 1024
GENERATED_DRAFT_FILES = {
    "draft_content.json",
    "draft_meta_info.json",
    "project.json",
    "draft_cover.jpg",
    ".owned",
    ".jianying_ai_owned.json",
}
DRAFT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
DRAFT_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z"
)


@dataclass(frozen=True)
class DraftBoxDiscovery:
    index_path: Path | None
    drafts_root: Path | None
    drafts_root_source: str | None
    index_source: str
    reason: str | None = None


@dataclass(frozen=True)
class DraftRecord:
    draft_id: str
    name: str
    path: Path
    created_at: str | None
    modified_at: str | None
    duration_us: int | None
    material_size_bytes: int | None
    files_present: dict[str, bool]

    def public_dict(self) -> dict:
        return {
            "id": self.draft_id,
            "name": self.name,
            "path": str(self.path),
            "created_at": self.created_at,
            "modified_at": self.modified_at,
            "duration_us": self.duration_us,
            "material_size_bytes": self.material_size_bytes,
            "files_present": dict(self.files_present),
            "source": "jianying_index",
            "read_only": True,
        }


def discover_draft_box(
    *,
    environ: dict[str, str] | None = None,
    registry_reader: Callable[[], str | None] | None = None,
) -> DraftBoxDiscovery:
    environ = dict(os.environ if environ is None else environ)
    local_app_data = environ.get("LOCALAPPDATA")
    index_path = (
        Path(local_app_data) / INDEX_RELATIVE_PATH if local_app_data else None
    )
    explicit_root = environ.get("JY_JIANYING_DRAFTS_ROOT", "").strip()
    if explicit_root:
        drafts_root = Path(explicit_root)
        root_source = "environment"
    elif os.name == "nt":
        reader = registry_reader or _read_registry_draft_root
        try:
            value = reader()
        except Exception:
            value = None
        drafts_root = Path(value) if value else None
        root_source = "registry" if value else None
    else:
        drafts_root = None
        root_source = None
    reason = None
    if index_path is None:
        reason = "local_app_data_unavailable"
    elif drafts_root is None:
        reason = "drafts_root_unavailable"
    return DraftBoxDiscovery(
        index_path=index_path,
        drafts_root=drafts_root,
        drafts_root_source=root_source,
        index_source="local_app_data",
        reason=reason,
    )


def _read_registry_draft_root() -> str | None:
    if os.name != "nt":
        return None
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_READ
    ) as key:
        value, kind = winreg.QueryValueEx(key, REGISTRY_VALUE)
    if kind != winreg.REG_SZ or not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


class DraftBoxService:
    """Read the Jianying local draft index and write service-owned drafts."""

    def __init__(
        self,
        *,
        index_path: Path | None = None,
        drafts_root: Path | None = None,
        drafts_root_source: str | None = None,
        app_running_check: Callable[[], bool] | None = None,
        project_builder: Callable[
            [str, Path, str], tuple[Any, Path, bytes, dict[str, Any]]
        ]
        | None = None,
    ) -> None:
        discovery = discover_draft_box()
        self.index_path = index_path if index_path is not None else discovery.index_path
        self.drafts_root = (
            drafts_root if drafts_root is not None else discovery.drafts_root
        )
        self.drafts_root_source = (
            drafts_root_source
            if drafts_root_source is not None
            else discovery.drafts_root_source
        )
        self._lock = threading.Lock()
        self._app_running_check = app_running_check or jianying_app_running
        self._project_builder = project_builder

    def status(self) -> dict:
        return self._status()

    def list_drafts(
        self, *, limit: int = 50, offset: int = 0, query: str | None = None
    ) -> dict:
        if not 1 <= limit <= 200 or not 0 <= offset <= 10_000:
            raise ServiceError(
                "DRAFT_BOX_PAGINATION_INVALID",
                "Draft box pagination is out of range",
                status=422,
            )
        normalized_query = (query or "").casefold()
        if len(normalized_query) > 200:
            raise ServiceError(
                "DRAFT_BOX_QUERY_INVALID",
                "Draft box query is too long",
                status=422,
            )
        records, source = self._records()
        selected = [
            record
            for record in records
            if normalized_query in record.name.casefold()
            or normalized_query in record.draft_id.casefold()
        ]
        items = selected[offset : offset + limit]
        return {
            "ok": True,
            "items": [record.public_dict() for record in items],
            "total": len(selected),
            "limit": limit,
            "offset": offset,
            "query": query or "",
            "index": {
                "source": "jianying_index",
                "draft_count": len(records),
                "skipped_count": source["skipped_count"],
            },
        }

    def get_draft(self, draft_id: str) -> dict:
        if not isinstance(draft_id, str) or not DRAFT_ID.fullmatch(draft_id):
            raise ServiceError(
                "DRAFT_BOX_INVALID_ID",
                "Draft ID is invalid",
                status=422,
            )
        records, source = self._records()
        record = next(
            (item for item in records if item.draft_id.casefold() == draft_id.casefold()),
            None,
        )
        if record is None:
            raise ServiceError(
                "DRAFT_BOX_NOT_FOUND",
                "Draft was not found",
                status=404,
            )
        return {
            "ok": True,
            "draft": {
                **record.public_dict(),
                "index": {
                    "source": "jianying_index",
                    "draft_count": len(records),
                    "skipped_count": source["skipped_count"],
                },
                "timeline_content": {
                    "status": "unsupported",
                    "reason": "encrypted",
                },
            },
        }

    def create_draft(
        self,
        project_id: str,
        *,
        project_builder: Callable[
            [str, Path, str], tuple[Any, Path, bytes, dict[str, Any]]
        ]
        | None = None,
    ) -> dict:
        """Publish a new draft folder and append it to Jianying's local index."""
        builder = project_builder or self._project_builder
        if builder is None:
            raise ServiceError(
                "DRAFT_BOX_WRITE_UNAVAILABLE",
                "Native draft creation is unavailable",
                status=503,
            )
        if not _windows_write_supported():
            raise ServiceError(
                "DRAFT_BOX_WRITE_UNAVAILABLE",
                "Native draft creation is only available on Windows",
                status=503,
                details={"reason": "platform_unsupported"},
            )
        try:
            running = self._app_running_check()
        except Exception as exc:
            raise ServiceError(
                "DRAFT_BOX_PROCESS_CHECK_FAILED",
                "Could not determine whether Jianying is running",
                status=503,
            ) from exc
        if not isinstance(running, bool):
            raise ServiceError(
                "DRAFT_BOX_PROCESS_CHECK_FAILED",
                "Could not determine whether Jianying is running",
                status=503,
            )
        if running:
            raise ServiceError(
                "DRAFT_BOX_APP_RUNNING",
                "Close Jianying before adding a draft",
                status=409,
                details={"action": "close_jianying_and_retry"},
            )
        with self._lock:
            root, index_path, index_directory, _, _, _ = (
                self._writable_index()
            )
            lock_path = index_path.parent / ".jianying_ai_draft_box.lock"
            with _index_file_lock(lock_path):
                _, _, index = _read_index_snapshot(
                    index_path, index_directory
                )
                self._check_app_stopped()
                draft_id = str(uuid.uuid4()).upper()
                folder_id = draft_id.replace("-", "")
                target = root / folder_id
                if target.exists() or target.is_symlink():
                    raise ServiceError(
                        "DRAFT_BOX_COLLISION",
                        "New draft directory already exists",
                        status=409,
                    )
                self._check_app_stopped()
                if _find_index_record(index, draft_id) is not None:
                    raise ServiceError(
                        "DRAFT_BOX_COLLISION",
                        "Draft identity is already in the index",
                        status=409,
                    )
                project_identity: str | None = None
                try:
                    project, directory, _content, content = builder(
                        project_id, target, draft_id
                    )
                except Exception:
                    marker = _read_owner(target)
                    if not (
                        marker is not None
                        and marker.get("project_id") == project_id
                    ):
                        raise
                    _remove_owned_directory(
                        target, draft_id, project_id=project_id
                    )
                    raise
                project_identity = getattr(project, "id", None)
                created_directory = _is_owned_draft_directory(
                    target,
                    draft_id,
                    project_identity
                    if isinstance(project_identity, str)
                    else None,
                )
                try:
                    if not target.is_dir() or target.is_symlink():
                        raise ServiceError(
                            "DRAFT_BOX_INVALID_DRAFT",
                            "Drafted directory identity does not match its request",
                            status=500,
                        )
                    target.resolve(strict=True)
                    if not _same_path(directory, target):
                        raise ServiceError(
                            "DRAFT_BOX_INVALID_DRAFT",
                            "Drafted directory is outside its reserved path",
                            status=500,
                        )
                    _validate_built_draft(directory, draft_id, content, project)
                    record = _new_index_record(
                        draft_id,
                        project.name,
                        directory,
                        root,
                        content,
                    )
                    updated = dict(index)
                    updated["all_draft_store"] = [
                        *index["all_draft_store"],
                        record,
                    ]
                    encoded = json.dumps(
                        updated,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                    if len(encoded) > MAX_INDEX_BYTES:
                        raise ServiceError(
                            "DRAFT_BOX_INDEX_INVALID",
                            "Updated Jianying draft index exceeds its limit",
                            status=500,
                        )
                    self._check_app_stopped()
                    _, _, current = _read_index_snapshot(
                        index_path, index_directory
                    )
                    if current != index:
                        raise ServiceError(
                            "DRAFT_BOX_INDEX_CONFLICT",
                            "Jianying draft index changed during the write",
                            status=409,
                            details={"action": "retry"},
                        )
                    _atomic_replace_index(index_path, encoded)
                except ServiceError as error:
                    if created_directory:
                        _remove_owned_directory(
                            target,
                            draft_id,
                            project_id=project_identity
                            if isinstance(project_identity, str)
                            else None,
                        )
                    raise error
                except Exception:
                    if created_directory:
                        _remove_owned_directory(
                            target,
                            draft_id,
                            project_id=project_identity
                            if isinstance(project_identity, str)
                            else None,
                        )
                    raise
                if _regular_file(index_path) is None:
                    raise ServiceError(
                        "DRAFT_BOX_INDEX_WRITE_FAILED",
                        "Draft was published but its index entry could not be verified",
                        status=500,
                    )
                _read_index_snapshot(index_path, index_directory)
                public_record = record.copy()
                return {
                    "ok": True,
                    "status": "written",
                    "draft": public_record,
                    "record": public_record,
                    "id": draft_id,
                    "name": record["draft_name"],
                    "path": str(directory),
                    "source": "jianying_index",
                    "read_only": False,
                    "jianying_ui_verified": False,
                    "verification": {
                        "files_and_index_written": True,
                        "jianying_ui_verified": False,
                        "note": "Generated JSON files and the index entry were written; Jianying UI compatibility was not verified.",
                    },
                }

    def repair_draft(
        self,
        project_id: str,
        draft_id: str,
        *,
        project_builder: Callable[
            [str, Path, str], tuple[Any, Path, bytes, dict[str, Any]]
        ]
        | None = None,
    ) -> dict:
        """Rebuild one service-owned draft in place from its source project."""
        if not isinstance(draft_id, str) or not DRAFT_UUID.fullmatch(draft_id):
            raise ServiceError(
                "DRAFT_BOX_INVALID_ID",
                "Only UUID drafts can be repaired",
                status=422,
            )
        builder = project_builder or self._project_builder
        if builder is None:
            raise ServiceError(
                "DRAFT_BOX_WRITE_UNAVAILABLE",
                "Native draft repair is unavailable",
                status=503,
            )
        if not _windows_write_supported():
            raise ServiceError(
                "DRAFT_BOX_WRITE_UNAVAILABLE",
                "Native draft repair is only available on Windows",
                status=503,
                details={"reason": "platform_unsupported"},
            )
        self._check_app_stopped()

        with self._lock:
            root, index_path, index_directory, _, _, _ = self._writable_index()
            lock_path = index_path.parent / ".jianying_ai_draft_box.lock"
            with _index_file_lock(lock_path):
                original_identity, original_raw, index = _read_index_snapshot(
                    index_path, index_directory
                )
                self._check_app_stopped()
                indexed_record = _find_index_record(index, draft_id)
                if indexed_record is None:
                    raise ServiceError(
                        "DRAFT_BOX_NOT_FOUND",
                        "Draft was not found",
                        status=404,
                    )
                matching_records = [
                    item
                    for item in index["all_draft_store"]
                    if isinstance(item, dict)
                    and isinstance(item.get("draft_id"), str)
                    and item["draft_id"].casefold() == draft_id.casefold()
                ]
                if len(matching_records) != 1:
                    raise ServiceError(
                        "DRAFT_BOX_INDEX_INVALID",
                        "Draft identity is ambiguous in the Jianying index",
                        status=500,
                    )

                normalized_id = indexed_record["draft_id"]
                folder_id = normalized_id.replace("-", "")
                target = root / folder_id
                indexed_path = indexed_record.get("draft_fold_path")
                if (
                    not isinstance(indexed_path, str)
                    or not _same_path(Path(indexed_path), target)
                    or target.is_symlink()
                    or not target.is_dir()
                ):
                    raise ServiceError(
                        "DRAFT_BOX_NOT_OWNED",
                        "Draft path is not a repairable service-owned directory",
                        status=409,
                    )
                try:
                    if not _same_path(target.resolve(strict=True).parent, root):
                        raise OSError("draft path is outside its root")
                except (OSError, RuntimeError) as exc:
                    raise ServiceError(
                        "DRAFT_BOX_NOT_OWNED",
                        "Draft path is not a repairable service-owned directory",
                        status=409,
                    ) from exc
                owner = _read_owner(target)
                if not _is_owned_draft_directory(
                    target,
                    normalized_id,
                    project_id,
                    owner_required=True,
                ):
                    code = (
                        "DRAFT_BOX_PROJECT_MISMATCH"
                        if owner is not None
                        and owner.get("project_id") != project_id
                        else "DRAFT_BOX_NOT_OWNED"
                    )
                    raise ServiceError(
                        code,
                        "Draft is not owned by the requested service project",
                        status=409,
                    )

                token = secrets.token_hex(8)
                # Keep staging paths short for Windows' legacy path length limit.
                staging_root = root / f".{token}.repair"
                backup = root / f".{folder_id}.{token}.backup"
                preserved_entries = [
                    entry.name
                    for entry in target.iterdir()
                    if entry.name not in GENERATED_DRAFT_FILES
                ]
                try:
                    staging_root.mkdir(mode=0o700)
                except FileExistsError as exc:
                    raise ServiceError(
                        "DRAFT_BOX_REPAIR_CONFLICT",
                        "Draft repair staging path already exists",
                        status=409,
                    ) from exc

                staged = staging_root / folder_id
                original_moved = False
                index_write_started = False
                retain_staging = False
                main_timeline_path = None
                try:
                    project, built_directory, _content, content = builder(
                        project_id, staged, normalized_id
                    )
                    staged_owned = _is_owned_draft_directory(
                        staged, normalized_id, project_id, owner_required=True
                    )
                    if (
                        not staged_owned
                        or not _same_path(built_directory, staged)
                        or staged.is_symlink()
                        or not staged.is_dir()
                    ):
                        raise ServiceError(
                            "DRAFT_BOX_INVALID_DRAFT",
                            "Generated repair draft does not match its reserved path",
                            status=500,
                        )
                    staged.resolve(strict=True)
                    _validate_built_draft(staged, normalized_id, content, project)
                    metadata_path = staged / "draft_meta_info.json"
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    metadata["draft_fold_path"] = str(target)
                    metadata["draft_root_path"] = str(root)
                    atomic_write(
                        metadata_path,
                        json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"),
                        replace=True,
                    )
                    main_timeline_path = _stage_main_timeline_repair(
                        target, staged, content, token
                    )
                    if main_timeline_path is not None:
                        preserved_entries = [
                            name for name in preserved_entries
                            if name.casefold() != "timelines"
                        ]
                    fresh_record = _new_index_record(
                        normalized_id,
                        project.name,
                        target,
                        root,
                        content,
                    )
                    updated_record = {**indexed_record, **fresh_record}
                    if "tm_draft_create" in indexed_record:
                        updated_record["tm_draft_create"] = indexed_record[
                            "tm_draft_create"
                        ]
                    updated = dict(index)
                    updated["all_draft_store"] = [
                        updated_record if item is indexed_record else item
                        for item in index["all_draft_store"]
                    ]
                    encoded = json.dumps(
                        updated,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                    if len(encoded) > MAX_INDEX_BYTES:
                        raise ServiceError(
                            "DRAFT_BOX_INDEX_INVALID",
                            "Updated Jianying draft index exceeds its limit",
                            status=500,
                        )

                    self._check_app_stopped()
                    current_identity, current_raw, _ = _read_index_snapshot(
                        index_path, index_directory
                    )
                    if (
                        current_identity != original_identity
                        or current_raw != original_raw
                    ):
                        raise ServiceError(
                            "DRAFT_BOX_INDEX_CONFLICT",
                            "Jianying draft index changed during the repair",
                            status=409,
                            details={"action": "retry"},
                        )
                    if not _is_owned_draft_directory(
                        target,
                        normalized_id,
                        project_id,
                        owner_required=True,
                    ):
                        raise ServiceError(
                            "DRAFT_BOX_NOT_OWNED",
                            "Draft ownership changed during the repair",
                            status=409,
                        )
                    self._check_app_stopped()
                    os.replace(target, backup)
                    original_moved = True
                    for name in preserved_entries:
                        old_entry = backup / name
                        staged_entry = staged / name
                        if staged_entry.exists() or staged_entry.is_symlink():
                            raise ServiceError(
                                "DRAFT_BOX_REPAIR_CONFLICT",
                                "Generated draft conflicts with an existing auxiliary file",
                                status=409,
                            )
                        os.replace(old_entry, staged_entry)
                    os.replace(staged, target)
                    self._check_app_stopped()
                    index_write_started = True
                    _atomic_replace_index(index_path, encoded)
                    _, verified_raw, verified_index = _read_index_snapshot(
                        index_path, index_directory
                    )
                    if verified_raw != encoded or verified_index != updated:
                        raise ServiceError(
                            "DRAFT_BOX_INDEX_WRITE_FAILED",
                            "Repaired draft index entry could not be verified",
                            status=500,
                        )
                except Exception as exc:
                    index_rollback_failed = False
                    if index_write_started:
                        try:
                            _, current_raw, _ = _read_index_snapshot(
                                index_path, index_directory
                            )
                            if current_raw == encoded:
                                _atomic_replace_index(index_path, original_raw)
                            elif current_raw != original_raw:
                                index_rollback_failed = True
                        except Exception:
                            index_rollback_failed = True
                    directory_rollback_failed = False
                    if original_moved:
                        if _is_owned_draft_directory(
                            target,
                            normalized_id,
                            project_id,
                            owner_required=True,
                        ):
                            try:
                                os.replace(target, staged)
                            except OSError:
                                pass
                        for name in preserved_entries:
                            staged_entry = staged / name
                            backup_entry = backup / name
                            if (
                                (staged_entry.exists() or staged_entry.is_symlink())
                                and not backup_entry.exists()
                                and not backup_entry.is_symlink()
                            ):
                                try:
                                    os.replace(staged_entry, backup_entry)
                                except OSError:
                                    directory_rollback_failed = True
                            if (
                                (staged_entry.exists() or staged_entry.is_symlink())
                                and not backup_entry.exists()
                                and not backup_entry.is_symlink()
                            ):
                                directory_rollback_failed = True
                        try:
                            if not target.exists() and not target.is_symlink():
                                if backup.exists() and not backup.is_symlink():
                                    os.replace(backup, target)
                                    original_moved = False
                                else:
                                    directory_rollback_failed = True
                            else:
                                directory_rollback_failed = True
                        except OSError:
                            directory_rollback_failed = True
                    if index_rollback_failed or directory_rollback_failed:
                        retain_staging = directory_rollback_failed
                        details: dict[str, Any] = {
                            "index_restore_required": index_rollback_failed,
                            "draft_restore_required": directory_rollback_failed,
                        }
                        if index_rollback_failed:
                            details["index_path"] = str(index_path)
                        if backup.exists() or backup.is_symlink():
                            details["backup_path"] = str(backup)
                        if staging_root.exists() or staging_root.is_symlink():
                            details["staging_path"] = str(staging_root)
                        raise ServiceError(
                            "DRAFT_BOX_REPAIR_ROLLBACK_FAILED",
                            "Repair failed and automatic recovery was incomplete",
                            status=500,
                            details=details,
                        ) from exc
                    if isinstance(exc, OSError):
                        raise ServiceError(
                            "DRAFT_BOX_REPAIR_FAILED",
                            "Could not replace the owned Jianying draft",
                            status=500,
                        ) from exc
                    raise
                else:
                    try:
                        shutil.rmtree(backup)
                    except OSError:
                        pass
                    return {
                        "ok": True,
                        "status": "repaired",
                        "draft": updated_record.copy(),
                        "record": updated_record.copy(),
                        "id": normalized_id,
                        "name": updated_record["draft_name"],
                        "path": str(target),
                        "source": "jianying_index",
                        "read_only": False,
                        "jianying_ui_verified": False,
                        "verification": {
                            "files_and_index_written": True,
                            "draft_id_preserved": True,
                            "draft_path_preserved": True,
                            "main_timeline_updated": main_timeline_path is not None,
                            "main_timeline_path": main_timeline_path,
                            "jianying_ui_verified": False,
                            "note": "The service-owned draft was rebuilt and replaced in place; Jianying UI compatibility was not verified.",
                        },
                    }
                finally:
                    if not retain_staging:
                        try:
                            if _same_path(
                                staging_root.resolve(strict=True), staging_root
                            ):
                                shutil.rmtree(staging_root)
                        except (OSError, RuntimeError):
                            pass

    def _check_app_stopped(self) -> None:
        try:
            running = self._app_running_check()
        except Exception as exc:
            raise ServiceError(
                "DRAFT_BOX_PROCESS_CHECK_FAILED",
                "Could not determine whether Jianying is running",
                status=503,
            ) from exc
        if not isinstance(running, bool):
            raise ServiceError(
                "DRAFT_BOX_PROCESS_CHECK_FAILED",
                "Could not determine whether Jianying is running",
                status=503,
            )
        if running:
            raise ServiceError(
                "DRAFT_BOX_APP_RUNNING",
                "Close Jianying before changing a draft",
                status=409,
                details={"action": "close_jianying_and_retry"},
            )

    def _writable_index(
        self,
    ) -> tuple[Path, Path, Path, tuple[int, ...], bytes, dict[str, Any]]:
        index_path = self.index_path
        if index_path is None or self.drafts_root is None:
            raise ServiceError(
                "DRAFT_BOX_WRITE_UNAVAILABLE",
                "Jianying draft box paths are unavailable",
                status=503,
            )
        index_path = _regular_file(index_path)
        if index_path is None:
            raise ServiceError(
                "DRAFT_BOX_WRITE_UNAVAILABLE",
                "Jianying draft index is unavailable",
                status=503,
            )
        try:
            index_directory = index_path.parent.resolve(strict=True)
            root = self.drafts_root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ServiceError(
                "DRAFT_BOX_WRITE_UNAVAILABLE",
                "Jianying draft paths are unavailable",
                status=503,
            )
        if (
            not index_directory.is_dir()
            or index_directory.is_symlink()
            or not root.is_dir()
            or root.is_symlink()
        ):
            raise ServiceError(
                "DRAFT_BOX_WRITE_UNAVAILABLE",
                "Jianying draft paths are unavailable",
                status=503,
            )
        identity, raw, document = _read_index_snapshot(index_path, index_directory)
        try:
            if not os.access(index_path, os.R_OK | os.W_OK):
                raise PermissionError
            probe = index_path.parent / f".jianying-write-probe-{secrets.token_hex(8)}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY  # type: ignore[attr-defined]
            fd = os.open(probe, flags, 0o600)
            os.close(fd)
            probe.unlink()
        except OSError as exc:
            raise ServiceError(
                "DRAFT_BOX_WRITE_UNAVAILABLE",
                "Jianying draft index is not writable",
                status=503,
            ) from exc
        return root, index_path, index_directory, identity, raw, document

    def _status(self) -> dict:
        reason, error = self._availability()
        if reason:
            return {
                "ok": True,
                "status": "unavailable",
                "reason": reason,
                "read_only": True,
                "timeline_content": "unsupported_encrypted",
            }
        if error:
            raise error
        records, source = self._records(error)
        return {
            "ok": True,
            "status": "available",
            "read_only": False,
            "draft_count": len(records),
            "skipped_count": source["skipped_count"],
            "index_source": "jianying_index",
            "drafts_root_source": self.drafts_root_source,
            "timeline_content": "unsupported_encrypted",
            "operations": {
                "status": "available",
                "list": "available",
                "get": "available",
                "create": "available",
                "repair": "owned_draft_only",
                "edit": "unsupported_encrypted",
                "delete": "unsupported",
                "write_support": "create_and_repair_owned",
                "open": "unsupported",
                "export_video": "unsupported",
            },
        }

    def _records(
        self, read_error: ServiceError | None = None
    ) -> tuple[list[DraftRecord], dict]:
        with self._lock:
            return self._load_records(read_error)

    def _availability(self) -> tuple[str | None, ServiceError | None]:
        index_path = self.index_path
        if index_path is None:
            return "local_app_data_unavailable", None
        if self.drafts_root is None:
            return "drafts_root_unavailable", None
        if _regular_file(index_path) is None:
            return "index_not_found", None
        try:
            with index_path.open("rb") as stream:
                stream.read(1)
        except OSError as exc:
            return "index_permission_denied", ServiceError(
                "DRAFT_BOX_INDEX_UNREADABLE",
                "Jianying draft index cannot be read",
                status=503,
            )
        return None, None

    def _load_records(
        self, read_error: ServiceError | None = None
    ) -> tuple[list[DraftRecord], dict]:
        index_path = self.index_path
        if index_path is None:
            raise ServiceError(
                "DRAFT_BOX_UNAVAILABLE", "Jianying draft index is unavailable", status=404
            )
        if self.drafts_root is None:
            raise ServiceError(
                "DRAFT_BOX_UNAVAILABLE", "Jianying draft root is unavailable", status=404
            )
        index = _regular_file(index_path)
        if index is None:
            raise ServiceError(
                "DRAFT_BOX_UNAVAILABLE", "Jianying draft index is unavailable", status=404
            )
        try:
            with index.open("rb") as stream:
                raw_bytes = stream.read(MAX_INDEX_BYTES + 1)
        except (OSError, UnicodeError) as exc:
            if read_error is not None:
                raise read_error
            raise ServiceError(
                "DRAFT_BOX_INDEX_UNREADABLE",
                "Jianying draft index cannot be read",
                status=503,
            ) from exc
        if len(raw_bytes) > MAX_INDEX_BYTES:
            raise ServiceError(
                "DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", status=500
            )
        try:
            document = json.loads(raw_bytes.decode("utf-8-sig"))
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ServiceError(
                "DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", status=500
            ) from exc
        if not isinstance(document, dict):
            raise ServiceError(
                "DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", status=500
            )
        index_root_value: Any = document.get("root_path")
        store = document.get("all_draft_store")
        if (
            not isinstance(index_root_value, str)
            or not index_root_value.strip()
            or not isinstance(store, list)
        ):
            raise ServiceError(
                "DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", status=500
            )
        index_root = self._normalize_root(index_root_value)
        index_directory = self._normalize_root(str(index.parent))
        draft_root = self._normalize_root(str(self.drafts_root))
        if (
            index_root is None
            or index_directory is None
            or draft_root is None
            or not _same_path(index_root, index_directory)
        ):
            raise ServiceError(
                "DRAFT_BOX_INDEX_INVALID",
                "Jianying draft index root is invalid",
                status=500,
            )
        records: dict[str, DraftRecord] = {}
        skipped = 0
        for item in store:
            record = self._record(item, draft_root)
            if record is None or record.draft_id.casefold() in records:
                skipped += 1
                continue
            records[record.draft_id.casefold()] = record
        ordered = sorted(
            records.values(),
            key=lambda record: (
                record.modified_at is None,
                -_sort_timestamp(record.modified_at),
                -_sort_timestamp(record.created_at),
                record.name.casefold(),
                record.draft_id.casefold(),
            ),
        )
        return ordered, {"skipped_count": skipped}

    @staticmethod
    def _normalize_root(raw: str) -> Path | None:
        try:
            return Path(os.path.abspath(os.path.expandvars(raw))).resolve()
        except (OSError, RuntimeError, ValueError):
            return None

    def _record(self, item: object, draft_root: Path) -> DraftRecord | None:
        if not isinstance(item, dict):
            return None
        draft_id = item.get("draft_id")
        name = item.get("draft_name")
        folder = item.get("draft_fold_path")
        if (
            not isinstance(draft_id, str)
            or not DRAFT_ID.fullmatch(draft_id)
            or not isinstance(name, str)
            or not name
            or len(name) > 200
            or not isinstance(folder, str)
            or not folder.strip()
        ):
            return None
        item_root = item.get("draft_root_path")
        if item_root is not None and not isinstance(item_root, str):
            return None
        try:
            candidate = Path(os.path.abspath(folder))
            if candidate.is_symlink():
                return None
            path = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        if not path.is_relative_to(draft_root) or path == draft_root:
            return None
        if item_root is not None:
            normalized_item_root = self._normalize_root(item_root)
            if normalized_item_root is None or not _same_path(
                normalized_item_root, draft_root
            ):
                return None
        try:
            if path.is_symlink() or not path.is_dir():
                return None
        except OSError:
            return None
        if isinstance(item.get("draft_is_invisible"), bool) and item["draft_is_invisible"]:
            return None
        files = {}
        for filename in ("draft_content.json", "draft_meta_info.json"):
            candidate = path / filename
            try:
                files[filename] = candidate.is_file() and not candidate.is_symlink()
            except OSError:
                files[filename] = False
        if item.get("draft_is_cloud_temp_draft") is True:
            return None
        duration = item.get("tm_duration")
        material_size = item.get("draft_timeline_materials_size")
        return DraftRecord(
            draft_id=draft_id,
            name=name,
            path=path,
            created_at=_utc_timestamp(item.get("tm_draft_create")),
            modified_at=_utc_timestamp(item.get("tm_draft_modified")),
            duration_us=_nonnegative_int(duration),
            material_size_bytes=_nonnegative_int(material_size),
            files_present=files,
        )


def _regular_file(path: Path) -> Path | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _read_index_document(
    path: Path, index_directory: Path
) -> tuple[bytes, dict[str, Any]]:
    _, raw, document = _read_index_snapshot(path, index_directory)
    return raw, document


def _read_index_snapshot(
    path: Path, index_directory: Path
) -> tuple[tuple[int, ...], bytes, dict[str, Any]]:
    try:
        stat = path.stat()
        with path.open("rb") as stream:
            raw = stream.read(MAX_INDEX_BYTES + 1)
    except OSError as exc:
        raise ServiceError(
            "DRAFT_BOX_INDEX_UNREADABLE",
            "Jianying draft index cannot be read",
            status=503,
        ) from exc
    if len(raw) > MAX_INDEX_BYTES:
        raise ServiceError(
            "DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", status=500
        )
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ServiceError(
            "DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", status=500
        ) from exc
    if not isinstance(document, dict):
        raise ServiceError(
            "DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", status=500
        )
    root_value = document.get("root_path")
    if (
        not isinstance(root_value, str)
        or not root_value.strip()
        or not isinstance(document.get("all_draft_store"), list)
    ):
        raise ServiceError(
            "DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", status=500
        )
    root = DraftBoxService._normalize_root(root_value)
    directory = DraftBoxService._normalize_root(str(index_directory))
    if root is None or directory is None or not _same_path(root, directory):
        raise ServiceError(
            "DRAFT_BOX_INDEX_INVALID",
            "Jianying draft index root is invalid",
            status=500,
        )
    identity = (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        getattr(stat, "st_ctime_ns", 0),
    )
    return identity, raw, document


def _find_index_record(document: dict[str, Any], draft_id: str) -> dict | None:
    for item in document["all_draft_store"]:
        if isinstance(item, dict) and isinstance(item.get("draft_id"), str):
            if item["draft_id"].casefold() == draft_id.casefold():
                return item
    return None


def _validate_built_draft(
    directory: Path, draft_id: str, content: dict[str, Any], project: Any
) -> None:
    if (
        not isinstance(content, dict)
        or not isinstance(content.get("id"), str)
        or not DRAFT_UUID.fullmatch(content["id"])
        or content["id"].casefold() != draft_id.casefold()
        or not isinstance(getattr(project, "name", None), str)
        or not project.name.strip()
        or len(project.name) > 200
        or content.get("name") != project.name
    ):
        raise ServiceError(
            "DRAFT_BOX_INVALID_DRAFT",
            "Generated draft content has an invalid identity",
            status=500,
        )
    for filename in (
        "draft_content.json",
        "draft_meta_info.json",
        "project.json",
    ):
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise ServiceError(
                "DRAFT_BOX_INVALID_DRAFT",
                "Generated draft is incomplete",
                status=500,
            )


def _new_index_record(
    draft_id: str,
    name: str,
    directory: Path,
    root: Path,
    content: dict[str, Any],
) -> dict[str, Any]:
    created = content.get("create_time")
    modified = content.get("update_time")
    duration = content.get("duration")
    if (
        not isinstance(created, int)
        or isinstance(created, bool)
        or created < 0
        or not isinstance(modified, int)
        or isinstance(modified, bool)
        or modified < 0
        or not isinstance(duration, int)
        or isinstance(duration, bool)
        or duration < 0
    ):
        raise ServiceError(
            "DRAFT_BOX_INVALID_DRAFT",
            "Generated draft timestamps or duration are invalid",
            status=500,
        )
    try:
        material_size = sum(
            material.get("material_size", 0)
            for material in content.get("materials", {}).get("videos", [])
            if isinstance(material, dict)
        ) + sum(
            material.get("material_size", 0)
            for material in content.get("materials", {}).get("audios", [])
            if isinstance(material, dict)
        )
        material_size = (
            material_size
            if isinstance(material_size, int) and material_size >= 0
            else 0
        )
    except (AttributeError, TypeError):
        material_size = 0
    modified = max(modified, created)
    return {
        "cloud_draft_cover": False,
        "cloud_draft_sync": False,
        "draft_cloud_last_action_download": False,
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "",
        "draft_fold_path": str(directory),
        "draft_id": draft_id,
        "draft_is_ai_shorts": False,
        "draft_is_cloud_temp_draft": False,
        "draft_is_infinite_canvas_draft": False,
        "draft_is_invisible": False,
        "draft_is_pippit_draft": False,
        "draft_is_web_article_video": False,
        "draft_json_file": str(directory / "draft_content.json"),
        "draft_name": name,
        "draft_new_version": "",
        "draft_root_path": str(root),
        "draft_timeline_materials_size": material_size,
        "draft_type": "",
        "draft_web_article_video_enter_from": "",
        "pippit_avatar_url": "",
        "pippit_extra_info": "",
        "pippit_id": "",
        "pippit_user_name": "",
        "streaming_edit_draft_ready": True,
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1,
        "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_space_id": -1,
        "tm_draft_cloud_user_id": -1,
        "tm_draft_create": created * 1_000_000,
        "tm_draft_modified": modified * 1_000_000,
        "tm_draft_parent_entry_id": -1,
        "tm_draft_removed": 0,
        "tm_duration": duration,
    }


def _atomic_replace_index(path: Path, encoded: bytes) -> None:
    if _windows_write_supported():
        return _atomic_replace_index_windows(path, encoded)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(temporary, _exclusive_create_flags(), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ServiceError(
            "DRAFT_BOX_INDEX_WRITE_FAILED",
            "Jianying draft index could not be replaced",
            status=500,
        ) from exc
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _atomic_replace_index_windows(path: Path, encoded: bytes) -> None:
    import ctypes
    import ctypes.wintypes

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    create_file.restype = ctypes.wintypes.HANDLE
    write_file = kernel32.WriteFile
    write_file.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.DWORD),
        ctypes.c_void_p,
    ]
    write_file.restype = ctypes.wintypes.BOOL
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [ctypes.wintypes.HANDLE]
    flush_file_buffers.restype = ctypes.wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.wintypes.HANDLE]
    close_handle.restype = ctypes.wintypes.BOOL
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
    ]
    move_file_ex.restype = ctypes.wintypes.BOOL
    invalid_handle = ctypes.c_void_p(-1).value
    handle: int | None = None
    try:
        handle = create_file(
            str(temporary),
            0x40000000,  # GENERIC_WRITE
            0,
            None,
            2,  # CREATE_ALWAYS; name is freshly random.
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle in (None, invalid_handle):
            raise OSError(ctypes.get_last_error(), "temporary index open failed")
        if len(encoded) > 0xFFFFFFFF:
            raise OSError("draft index is too large")
        written = ctypes.wintypes.DWORD()
        payload = ctypes.create_string_buffer(encoded, len(encoded))
        if not write_file(
            handle,
            ctypes.cast(payload, ctypes.c_void_p),
            len(encoded),
            ctypes.byref(written),
            None,
        ) or written.value != len(encoded):
            raise OSError(ctypes.get_last_error(), "temporary index write failed")
        if not flush_file_buffers(handle):
            raise OSError(ctypes.get_last_error(), "temporary index flush failed")
        close_handle(handle)
        handle = None
        if not move_file_ex(
            str(temporary), str(path), 0x00000001 | 0x00000008
        ):
            raise OSError(ctypes.get_last_error(), "index replace failed")
        _fsync_windows_directory(path.parent)
    except OSError as exc:
        raise ServiceError(
            "DRAFT_BOX_INDEX_WRITE_FAILED",
            "Jianying draft index could not be replaced",
            status=500,
        ) from exc
    finally:
        if handle not in (None, invalid_handle):
            close_handle(handle)
        try:
            if not temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass


def _fsync_windows_directory(directory: Path) -> None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.CreateFileW(
            str(directory),
            0x80000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000,
            None,
        )
        if handle in (None, -1, 0xFFFFFFFFFFFFFFFF):
            return
        try:
            kernel32.FlushFileBuffers(handle)
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return


class _index_file_lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: Any = None

    def __enter__(self) -> "_index_file_lock":
        buffer = self.path.parent / f".{self.path.name}.{secrets.token_hex(8)}.lock"
        try:
            if not os.path.lexists(self.path):
                fd = os.open(buffer, _exclusive_create_flags(), 0o600)
                try:
                    try:
                        os.link(buffer, self.path, follow_symlinks=False)
                    except FileExistsError:
                        pass
                finally:
                    os.close(fd)
                    try:
                        os.unlink(buffer)
                    except OSError:
                        pass
            if self.path.is_symlink() or not self.path.is_file():
                raise OSError("draft index lock path is invalid")
            self.stream = self.path.open("r+b")
            if _windows_write_supported():
                self.stream.seek(0, os.SEEK_END)
                if self.stream.tell() == 0:
                    self.stream.seek(0)
                    self.stream.write(b"\0")
                    self.stream.flush()
                self.stream.seek(0)
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            try:
                buffer.unlink()
            except OSError:
                pass
            raise ServiceError(
                "DRAFT_BOX_LOCK_FAILED",
                "Could not lock the Jianying draft index",
                status=503,
            ) from exc
        return self

    def __exit__(self, *args: Any) -> None:
        self._release()

    def _release(self) -> None:
        if self.stream is None:
            return
        if _windows_write_supported():
            import msvcrt

            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()
        self.stream = None


def _stage_main_timeline_repair(
    source: Path, staged: Path, content: dict[str, Any], token: str
) -> str | None:
    """Refresh the native main timeline without decoding its old draft content.

    Jianying 11.6 reads Timelines/<main_timeline_id>/draft_content.json
    after its first save. Updating only the draft root leaves that timeline stale.
    Stage a copy so publication failures can still restore the untouched original.
    """
    timelines = source / "Timelines"
    if not timelines.exists() and not timelines.is_symlink():
        return None
    try:
        if timelines.is_symlink() or timelines.is_junction() or not timelines.is_dir():
            raise ValueError("Unsafe timeline directory")
        for directory, directories, files in os.walk(timelines, followlinks=False):
            for name in [*directories, *files]:
                entry = Path(directory) / name
                if entry.is_symlink() or entry.is_junction():
                    raise ValueError("Linked timeline entry")
        manifest_path = timelines / "project.json"
        if manifest_path.stat().st_size > 1024 * 1024:
            raise ValueError("Oversized timeline manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Invalid timeline manifest")
        main_id = manifest.get("main_timeline_id")
        entries = manifest.get("timelines")
        if (
            not isinstance(main_id, str)
            or not DRAFT_UUID.fullmatch(main_id)
            or not isinstance(entries, list)
            or sum(
                isinstance(entry, dict) and entry.get("id") == main_id
                for entry in entries
            ) != 1
            or not (timelines / main_id / "draft_content.json").is_file()
        ):
            raise ValueError("Invalid main timeline")
        destination = staged / "Timelines"
        shutil.copytree(timelines, destination)
        snapshot = destination / ".jianying_ai_repair_backups" / token
        snapshot.mkdir(parents=True)
        shutil.copy2(manifest_path, snapshot / "project.json")
        for name in ("draft_content.json", "draft_content.json.bak"):
            previous = timelines / main_id / name
            if previous.is_file():
                shutil.copy2(previous, snapshot / name)
        replacement = {**content, "id": main_id}
        encoded = json.dumps(
            replacement, ensure_ascii=False, indent=4, allow_nan=False
        ).encode("utf-8")
        main_file = destination / main_id / "draft_content.json"
        atomic_write(main_file, encoded, replace=True)
        fallback = main_file.with_name("draft_content.json.bak")
        if fallback.exists():
            atomic_write(fallback, encoded, replace=True)
        if main_file.read_bytes() != encoded:
            raise OSError("Main timeline write could not be verified")
        return str(Path("Timelines") / main_id / "draft_content.json")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ServiceError(
            "DRAFT_BOX_TIMELINE_INVALID",
            "Could not safely rebuild the owned draft's main timeline",
            status=409,
        ) from exc


def _remove_owned_directory(
    directory: Path, draft_id: str, *, project_id: str | None = None
) -> None:
    if not _is_owned_draft_directory(
        directory, draft_id, project_id, owner_required=True
    ):
        return
    try:
        shutil.rmtree(directory)
    except (OSError, TypeError, ValueError):
        return


def jianying_app_running() -> bool:
    """Check the fixed Jianying process name without starting or stopping it."""
    if not _windows_write_supported():
        return False
    import ctypes
    import ctypes.wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("cntUsage", ctypes.wintypes.DWORD),
            ("th32ProcessID", ctypes.wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.wintypes.DWORD),
            ("cntThreads", ctypes.wintypes.DWORD),
            ("th32ParentProcessID", ctypes.wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szExeFile", ctypes.wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateToolhelp32Snapshot.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
    ]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32FirstW.restype = ctypes.wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(PROCESSENTRY32W),
    ]
    kernel32.Process32NextW.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot in (None, invalid_handle):
        raise OSError("process snapshot unavailable")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        found = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while found:
            if str(entry.szExeFile).casefold() == "jianyingpro.exe":
                return True
            found = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return False


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first)) == os.path.normcase(str(second))


def _windows_write_supported() -> bool:
    return os.name == "nt"


def _exclusive_create_flags() -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY  # type: ignore[attr-defined]
    return flags


def _is_owned_draft_directory(
    directory: Path,
    draft_id: str,
    project_id: str | None,
    *,
    owner_required: bool = False,
) -> bool:
    if (
        directory.name.casefold() != draft_id.replace("-", "").casefold()
        or directory.is_symlink()
        or not directory.is_dir()
    ):
        return False
    try:
        marker = directory / ".jianying_ai_owned.json"
        if marker.is_symlink() or not marker.is_file():
            return not owner_required
        owner = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(owner, dict):
            return False
        owner_id = owner.get("draft_id")
        return (
            isinstance(owner_id, str)
            and owner_id.strip().replace("-", "").casefold()
            == draft_id.strip().replace("-", "").casefold()
            and owner.get("generator") == "jianying-ai-service"
            and (
                project_id is None
                or owner.get("project_id") == project_id
            )
        )
    except (OSError, TypeError, ValueError):
        return False


def _read_owner(directory: Path) -> dict[str, Any] | None:
    try:
        marker = directory / ".jianying_ai_owned.json"
        if marker.is_symlink() or not marker.is_file():
            return None
        value = json.loads(marker.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, TypeError, ValueError):
        return None


def _timestamp(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and 0 < value < 2**63:
        return int(value)
    return 0


def _sort_timestamp(value: str | None) -> int:
    if value is None:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1_000_000)
    except (OverflowError, OSError, ValueError):
        return 0


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or not value.is_integer():
        return None
    return int(value)


def _utc_timestamp(value: object) -> str | None:
    raw = value
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
            return parsed.astimezone(timezone(timedelta(hours=8))).isoformat()
        except (OverflowError, ValueError):
            return None
    timestamp = _timestamp(raw)
    if timestamp == 0:
        return None
    try:
        return datetime.fromtimestamp(
            timestamp / (1_000_000 if timestamp >= 10**14 else 1000),
            tz=timezone(timedelta(hours=8)),
        ).isoformat()
    except (OverflowError, OSError, ValueError):
        return None

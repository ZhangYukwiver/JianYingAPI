from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Callable

from .errors import ServiceError
from .models import Project


class ProjectStore:
    """One immutable project file per revision, with a single current pointer."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, project_id: str, revision: int) -> Path:
        if len(project_id) != 32 or any(c not in "0123456789abcdef" for c in project_id):
            raise ServiceError("PROJECT_NOT_FOUND", "Project not found", status=404)
        return self.root / project_id / f"revision-{revision:08d}.json"

    def _current(self, project_id: str) -> Path:
        self._path(project_id, 1)  # Validate before reading the revision pointer.
        return self.root / project_id / "current.json"

    def new_id(self) -> str:
        while True:
            candidate = secrets.token_hex(16)
            if not (self.root / candidate).exists():
                return candidate

    def create(self, project: Project) -> None:
        directory = self.root / project.id
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if directory.is_dir() and any(directory.iterdir()):
                raise ServiceError("PROJECT_EXISTS", "Project already exists", status=409)
        self._save(project, expect_revision=0)

    def get(self, project_id: str) -> Project:
        pointer = self._current(project_id)
        try:
            pointer_data = json.loads(pointer.read_text(encoding="utf-8"))
            revision = pointer_data["revision"]
        except (OSError, ValueError, KeyError, TypeError):
            raise ServiceError("PROJECT_NOT_FOUND", "Project not found", status=404)
        path = self._path(project_id, revision)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            project = Project.model_validate(data)
        except (OSError, ValueError):
            raise ServiceError("STORE_CORRUPT", "Stored project cannot be read", status=500)
        if project.id != project_id or project.revision != revision:
            raise ServiceError("STORE_CORRUPT", "Stored project identity mismatch", status=500)
        return project

    def save(self, project: Project, *, expect_revision: int) -> Project:
        current = self.get(project.id)
        if current.revision != expect_revision:
            raise ServiceError(
                "PROJECT_CONFLICT",
                "Project changed; read it again before saving",
                status=409,
                details={"current_revision": current.revision},
            )
        next_project = project.model_copy(update={"revision": expect_revision + 1})
        self._save(next_project, expect_revision=expect_revision)
        return next_project

    def _save(self, project: Project, *, expect_revision: int) -> None:
        if project.revision != expect_revision + 1:
            raise ValueError("save requires exactly one new revision")
        target = self._path(project.id, project.revision)
        if target.exists():
            raise ServiceError("PROJECT_CONFLICT", "Revision already exists", status=409)
        encoded = json.dumps(
            project.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ServiceError("PROJECT_TOO_LARGE", "Project exceeds size limit", status=413)
        atomic_write(target, encoded, replace=False)
        pointer_data = json.dumps({"revision": project.revision}, separators=(",", ":")).encode()
        atomic_write(self._current(project.id), pointer_data, replace=True)

    def list(self, *, limit: int = 100, offset: int = 0) -> list[Project]:
        items: list[Project] = []
        if not self.root.exists():
            return items
        for directory in self.root.iterdir():
            if directory.is_dir():
                try:
                    items.append(self.get(directory.name))
                except ServiceError:
                    continue
        items.sort(key=lambda x: x.updated_at, reverse=True)
        return items[offset : offset + limit]

    def remove_draft_files(self, project_id: str) -> None:
        """No delete API is public; used only by tests inside a temp directory."""
        path = self.root / project_id
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("outside store")


def atomic_write(target: Path, data: bytes, *, replace: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temp, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, target)
        else:
            try:
                os.link(temp, target)
            except FileExistsError:
                raise ServiceError("PROJECT_CONFLICT", "Target already exists", status=409)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

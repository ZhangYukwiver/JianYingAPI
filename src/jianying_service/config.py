from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ServiceError


@dataclass(frozen=True)
class Settings:
    data_root: Path
    media_roots: tuple[Path, ...]
    host: str = "127.0.0.1"
    port: int = 8765
    jianying_exe: Path | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    max_project_bytes: int = 32 * 1024 * 1024
    max_media_bytes: int = 8 * 1024 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        data_root = Path(os.environ.get("JY_DATA_ROOT", "runtime")).resolve()
        root_value = os.environ.get("JY_MEDIA_ROOT", "samples")
        roots = tuple(Path(x).resolve() for x in root_value.split(os.pathsep) if x.strip())
        if not roots:
            raise ValueError("JY_MEDIA_ROOT must contain at least one path")
        exe_value = os.environ.get("JY_JIANYING_EXE")
        ffmpeg = os.environ.get("JY_FFMPEG", "ffmpeg")
        ffprobe = os.environ.get("JY_FFPROBE", "ffprobe")
        host = os.environ.get("JY_HOST", "127.0.0.1")
        port = int(os.environ.get("JY_PORT", "8765"))
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("JY_HOST must be a loopback address")
        if not 1 <= port <= 65535:
            raise ValueError("JY_PORT must be in 1..65535")
        return cls(
            data_root=data_root,
            media_roots=roots,
            host=host,
            port=port,
            jianying_exe=Path(exe_value).resolve() if exe_value else None,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )

    @property
    def projects_root(self) -> Path:
        return self.data_root / "projects"

    @property
    def drafts_root(self) -> Path:
        return self.data_root / "drafts"

    @property
    def previews_root(self) -> Path:
        return self.data_root / "previews"

    def ensure_roots(self) -> None:
        data = self.data_root.resolve()
        for path in (data, self.projects_root, self.drafts_root, self.previews_root):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path.resolve().is_relative_to(data):
                raise ServiceError("PATH_UNSAFE", "Service data path left its root", status=500)

    def public_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "data_root": str(self.data_root),
            "projects_root": str(self.projects_root),
            "drafts_root": str(self.drafts_root),
            "media_roots": [str(x) for x in self.media_roots],
            "jianying_exe": str(self.jianying_exe) if self.jianying_exe else None,
            "ffmpeg": self.ffmpeg,
            "ffprobe": self.ffprobe,
        }


def read_version(path: Path) -> str | None:
    try:
        metadata = path.parent / "data" / "version.json"
        if not metadata.exists():
            return None
        with metadata.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        return str(value.get("version")) if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None

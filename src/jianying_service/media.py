from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationFailure
from .models import MAX_TIME_US


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    kind: str
    duration_us: int
    width: int | None
    height: int | None
    audio: bool
    video: bool


def media_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    raise ValidationFailure("MEDIA_TYPE_UNSUPPORTED", "Unsupported local media type")


class MediaResolver:
    def __init__(
        self,
        roots: tuple[Path, ...],
        ffprobe: str,
        max_bytes: int,
    ) -> None:
        self.roots = tuple(path.resolve() for path in roots)
        self.ffprobe = ffprobe
        self.max_bytes = max_bytes

    def resolve(self, raw: str) -> Path:
        if "\x00" in raw:
            raise ValidationFailure("PATH_INVALID", "Path contains a null byte")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValidationFailure("PATH_NOT_ABSOLUTE", "Media paths must be absolute")
        try:
            normalized = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValidationFailure("MEDIA_NOT_FOUND", "Media file does not exist")
        if not any(normalized.is_relative_to(root) for root in self.roots):
            raise ValidationFailure(
                "PATH_OUTSIDE_WHITELIST",
                "Media path is outside configured roots",
            )
        kind = media_kind(normalized)
        if not normalized.is_file():
            raise ValidationFailure("MEDIA_NOT_FILE", "Media path is not a regular file")
        try:
            stat = normalized.stat()
        except OSError:
            raise ValidationFailure("MEDIA_NOT_FOUND", "Media file cannot be read")
        if stat.st_size == 0:
            raise ValidationFailure("MEDIA_EMPTY", "Media file is empty")
        if stat.st_size > self.max_bytes:
            raise ValidationFailure("MEDIA_TOO_LARGE", "Media file exceeds size limit")
        return normalized

    def probe(self, raw: str) -> MediaInfo:
        path = self.resolve(raw)
        expected = media_kind(path)
        tool = shutil.which(self.ffprobe)
        if not tool:
            raise ValidationFailure("FFPROBE_UNAVAILABLE", "ffprobe is not available")
        try:
            result = subprocess.run(
                [
                    tool,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type,width,height,duration",
                    "-of",
                    "json",
                    path,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValidationFailure("MEDIA_PROBE_FAILED", "Media probing failed")
        if result.returncode != 0:
            raise ValidationFailure("MEDIA_INVALID", "Media file could not be parsed")
        try:
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
            has_video = any(x.get("codec_type") == "video" for x in streams)
            has_audio = any(x.get("codec_type") == "audio" for x in streams)
            durations = []
            for value in (data.get("format", {}).get("duration"), *(x.get("duration") for x in streams)):
                if value not in (None, "N/A"):
                    durations.append(float(value))
            duration = max(durations) if durations else 0.0
            if not math.isfinite(duration) or duration < 0:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValidationFailure("MEDIA_INVALID", "Media metadata is invalid")
        if expected == "image":
            if not has_video:
                raise ValidationFailure("MEDIA_KIND_MISMATCH", "Image has no picture stream")
            duration_us = MAX_TIME_US
            dims = next((x for x in streams if x.get("codec_type") == "video"), {})
        elif expected == "video":
            if not has_video:
                raise ValidationFailure("MEDIA_KIND_MISMATCH", "Video has no picture stream")
            if duration <= 0:
                raise ValidationFailure("MEDIA_INVALID", "Video duration is missing")
            duration_us = min(MAX_TIME_US, math.floor(duration * 1_000_000))
            dims = next((x for x in streams if x.get("codec_type") == "video"), {})
        elif expected == "audio":
            if has_video:
                raise ValidationFailure("MEDIA_KIND_MISMATCH", "Audio must not contain video")
            if not has_audio or duration <= 0:
                raise ValidationFailure("MEDIA_INVALID", "Audio duration is missing")
            duration_us = min(MAX_TIME_US, math.floor(duration * 1_000_000))
            dims = {}
        else:
            raise ValidationFailure("MEDIA_TYPE_UNSUPPORTED", "Unsupported media type")
        if expected != "image" and duration_us == 0:
            raise ValidationFailure("MEDIA_INVALID", "Media has no usable duration")
        return MediaInfo(
            path=path,
            kind=expected,
            duration_us=duration_us,
            width=_safe_dimension(dims.get("width")),
            height=_safe_dimension(dims.get("height")),
            audio=has_audio,
            video=has_video,
        )


def _safe_dimension(value: object) -> int | None:
    try:
        converted = int(value)  # type: ignore[arg-type]
        return converted if 1 <= converted <= 100000 else None
    except (TypeError, ValueError):
        return None

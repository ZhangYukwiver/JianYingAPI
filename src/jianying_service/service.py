from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError
from pyJianYingDraft import (
    AudioSceneEffectType,
    EffectSegment,
    FilterType,
    FilterSegment,
    GroupAnimationType,
    IntroType,
    MaskType,
    MixModeType,
    OutroType,
    SpeechToSongType,
    TextIntro,
    TextLoopAnim,
    TextOutro,
    ToneEffectType,
    TransitionType,
    VideoCharacterEffectType,
    VideoSceneEffectType,
)

from .config import Settings
from .draft import _enum_member, create_draft_folder
from .errors import ServiceError, ValidationFailure
from .media import MediaInfo, MediaResolver
from .models import (
    AnimationCreate,
    BackgroundFillCreate,
    Canvas,
    ChromaCreate,
    DraftInfo,
    EffectCreate,
    FadeCreate,
    FilterCreate,
    KeyframeBatch,
    KeyframeCreate,
    MarkerCreate,
    MaskCreate,
    MediaSegmentCreate,
    MixModeCreate,
    Project,
    ProjectCreate,
    ProjectUpdate,
    SegmentUpdate,
    StickerSegmentCreate,
    SubtitleEntry,
    TextSegmentCreate,
    TimeRange,
    TrackOrder,
    TrackEffectCreate,
    TrackFilterCreate,
    TrackCreate,
    TrackUpdate,
    TransitionCreate,
)
from .storage import ProjectStore, atomic_write, sha256_file


MAX_SRT_BYTES = 4 * 1024 * 1024


class ProjectService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: ProjectStore | None = None,
        media: MediaResolver | None = None,
    ) -> None:
        settings.ensure_roots()
        self.settings = settings
        self._lock = threading.RLock()
        self.store = store or ProjectStore(settings.projects_root, settings.max_project_bytes)
        self.media = media or MediaResolver(
            settings.media_roots, settings.ffprobe, settings.max_media_bytes
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProjectService":
        return cls(settings)

    def create_project(self, request: ProjectCreate) -> Project:
        now = datetime.now(timezone.utc)
        project = Project(
            id=self.store.new_id(),
            name=request.name,
            canvas=request.canvas,
            created_at=now,
            updated_at=now,
            revision=1,
        )
        with self._lock:
            self.store.create(project)
        return project

    def list_projects(self, *, limit: int = 100, offset: int = 0) -> list[Project]:
        if not 1 <= limit <= 1000 or not 0 <= offset <= 1_000_000:
            raise ValidationFailure("PAGINATION_INVALID", "Pagination is out of range")
        return self.store.list(limit=limit, offset=offset)

    def projects(self, *, limit: int = 100, offset: int = 0) -> list[Project]:
        return self.list_projects(limit=limit, offset=offset)

    def list(self, *, limit: int = 100, offset: int = 0) -> list[Project]:
        return self.list_projects(limit=limit, offset=offset)

    def get_project(self, project_id: str) -> Project:
        return self.store.get(project_id)

    def get(self, project_id: str) -> Project:
        return self.get_project(project_id)

    def read_project(self, project_id: str) -> Project:
        return self.get_project(project_id)

    def create(self, request: ProjectCreate) -> Project:
        return self.create_project(request)

    def create_track(
        self,
        project_id: str,
        request: TrackCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        return self.add_track(project_id, request, expect_revision=expect_revision)

    def add_track_to_project(
        self,
        project_id: str,
        track: TrackCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        return self.add_track(project_id, track, expect_revision=expect_revision)

    def set_canvas(
        self,
        project_id: str,
        canvas: Canvas,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            updated = project.model_copy(
                update={
                    "canvas": canvas,
                    "updated_at": datetime.now(timezone.utc),
                    "draft": None,
                }
            )
            return self.store.save(updated, expect_revision=project.revision)

    def update_project(
        self,
        project_id: str,
        request: ProjectUpdate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            updates = request.model_dump(exclude_unset=True)
            if any(value is None for value in updates.values()):
                raise ValidationFailure("PROJECT_UPDATE_INVALID", "Project name cannot be null")
            if not updates:
                return project
            return self.store.save(
                project.model_copy(
                    update={
                        **updates,
                        "updated_at": datetime.now(timezone.utc),
                        "draft": None,
                    }
                ),
                expect_revision=project.revision,
            )

    def update_track(
        self,
        project_id: str,
        track_id: str,
        request: TrackUpdate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            self._track_any(project, track_id)
            changes = request.model_dump(exclude_unset=True)
            if any(value is None for value in changes.values()):
                raise ValidationFailure("TRACK_UPDATE_INVALID", "Track fields cannot be null")
            if not changes:
                return project
            tracks = [
                {**track, **changes} if track.get("id") == track_id else track
                for track in project.tracks
            ]
            return self._save_tracks(project, tracks)

    def delete_track(
        self,
        project_id: str,
        track_id: str,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            self._track_any(project, track_id)
            tracks = [track for track in project.tracks if track.get("id") != track_id]
            return self._save_tracks(project, tracks)

    def reorder_tracks(
        self,
        project_id: str,
        request: TrackOrder,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            current = [str(track.get("id")) for track in project.tracks]
            requested = list(request.track_ids)
            if len(set(requested)) != len(requested) or set(requested) != set(current):
                raise ValidationFailure(
                    "TRACK_ORDER_INVALID",
                    "track_ids must contain every track exactly once",
                )
            by_id = {str(track.get("id")): track for track in project.tracks}
            return self._save_tracks(project, [by_id[item] for item in requested])

    def add_track(
        self,
        project_id: str,
        request: TrackCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track = {
                "id": self._track_id(project, request.track_type),
                "track_type": request.track_type,
                "name": request.name or request.track_type.capitalize(),
                "mute": request.mute,
                "segments": [],
            }
            updated = project.model_copy(
                update={
                    "tracks": [*project.tracks, track],
                    "updated_at": datetime.now(timezone.utc),
                    "draft": None,
                }
            )
            return self.store.save(updated, expect_revision=project.revision)

    def update_segment(
        self,
        project_id: str,
        segment_id: str,
        request: SegmentUpdate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track, current = self._segment_location(project, segment_id)
            # Keep nested Pydantic settings as models until they are normalized below.
            changes = {
                field: getattr(request, field)
                for field in request.model_fields_set
            }
            if not changes:
                return project
            kind = current.get("kind")
            nullable_fields = {"transform", "bubble", "text_effect_id"}
            if any(value is None for key, value in changes.items() if key not in nullable_fields):
                raise ValidationFailure("SEGMENT_UPDATE_INVALID", "Only transform may be cleared with null")
            if kind == "text":
                forbidden = {"source", "volume", "speed", "change_pitch", "fit", "path", "material_name"}
                if forbidden.intersection(changes):
                    raise ValidationFailure(
                        "SEGMENT_FIELD_MISMATCH",
                        "Text clips only accept timeline, text style and transform fields",
                    )
            elif kind in {"video", "image"} and {
                "font_family", "bold", "italic", "underline", "opacity", "vertical",
                "letter_spacing", "line_spacing", "auto_wrapping", "max_line_width",
                "border", "background", "shadow", "bubble", "text_effect_id",
            }.intersection(changes):
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Text style fields require a text clip")
            elif kind == "audio" and {"transform", "fit"}.intersection(changes):
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Audio clips do not support visual transforms")
            elif kind == "audio" and "crop" in changes:
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Audio clips do not support cropping")
            elif kind != "sticker" and {
                "text", "font_size", "color", "alignment", "bubble", "text_effect_id"
            }.intersection(changes):
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Text style fields require a text clip")
            elif kind == "sticker" and {
                "text", "font_size", "color", "alignment", "bubble", "text_effect_id"
            }.intersection(changes):
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Text style fields require a text clip")
            elif kind in {"track_effect", "track_filter"} and set(changes) - {"timeline"}:
                raise ValidationFailure(
                    "SEGMENT_FIELD_MISMATCH",
                    "Track effect clips only accept timeline updates",
                )
            elif kind not in {
                "video", "image", "audio", "sticker", "track_effect", "track_filter"
            }:
                raise ValidationFailure("SEGMENT_KIND_INVALID", "Segment cannot be edited")
            if kind == "sticker":
                forbidden = {"source", "volume", "speed", "change_pitch", "fit", "path", "material_name", "crop"}
                if forbidden.intersection(changes):
                    raise ValidationFailure(
                        "SEGMENT_FIELD_MISMATCH",
                        "Sticker clips only accept timeline and transform fields",
                    )

            timeline = changes.get("timeline") or TimeRange(**current["timeline"])
            if isinstance(timeline, dict):
                timeline = TimeRange(**timeline)
            source_data = current.get("source")
            speed = float(changes.get("speed", current.get("speed", 1.0)))
            media_info = None
            if kind in {"video", "image", "audio"}:
                path = changes.get("path", current.get("path"))
                media_info = self.media.probe(path)
                if (track["track_type"] == "audio") != (media_info.kind == "audio"):
                    raise ValidationFailure("MEDIA_TRACK_MISMATCH", "Clip media type does not match track")
                if media_info.kind == "image" and speed != 1.0:
                    raise ValidationFailure("SPEED_INVALID", "Still images do not support speed changes")
                if "path" in changes:
                    changes["path"] = str(media_info.path)
                    changes["kind"] = media_info.kind
                    source_data = None
                if "source" in changes:
                    raw_source = changes["source"]
                    if raw_source is None:
                        raise ValidationFailure("SOURCE_RANGE_INVALID", "A media source range is required")
                    source = raw_source if isinstance(raw_source, TimeRange) else TimeRange(**raw_source)
                    source_data = source.model_dump(mode="json")
                elif source_data:
                    source = TimeRange(**source_data)
                else:
                    source = TimeRange(
                        duration_us=round(timeline.duration_us * speed)
                    )
                if "timeline" in changes and "source" not in changes and "path" not in changes:
                    source_data = {
                        **source_data,
                        "duration_us": round(timeline.duration_us * speed),
                    }
                    source = TimeRange(**source_data)
                elif "timeline" not in changes and ("source" in changes or "speed" in changes):
                    timeline = TimeRange(
                        start_us=timeline.start_us,
                        duration_us=round(source.duration_us / speed),
                    )
                    changes["timeline"] = timeline
                if round(source.duration_us / speed) != timeline.duration_us:
                    raise ValidationFailure(
                        "MEDIA_DURATION_MISMATCH",
                        "Timeline duration must equal source duration divided by speed",
                    )
                if source.end_us > media_info.duration_us:
                    raise ValidationFailure("SOURCE_RANGE_INVALID", "Source range exceeds media duration")
                changes["source"] = source.model_dump(mode="json")
                changes["speed"] = speed
            if "timeline" in changes:
                self._validate_placement(
                    project, track["id"], timeline, ignore_segment_id=segment_id
                )
                changes["timeline"] = timeline.model_dump(mode="json")
            if "volume" in changes and kind not in {"audio", "video", "image"}:
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Volume is only valid for media clips")
            if "transform" in changes:
                transform = changes["transform"]
                changes["transform"] = (
                    transform.model_dump(mode="json") if transform is not None else None
                )
            if "crop" in changes:
                crop = changes["crop"]
                if kind not in {"video", "image"}:
                    raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Cropping requires a video or image clip")
                changes["crop"] = crop.model_dump(mode="json") if crop is not None else None
            for field in ("border", "background", "shadow", "bubble"):
                if field in changes and changes[field] is not None:
                    changes[field] = changes[field].model_dump(mode="json")
            if "color" in changes and changes["color"] is not None:
                changes["color"] = str(changes["color"]).upper()
            updated_segment = {**current, **changes}
            for keyframe in updated_segment.get("keyframes", []):
                if keyframe["offset_us"] > updated_segment["timeline"]["duration_us"]:
                    raise ValidationFailure(
                        "KEYFRAME_OFFSET_INVALID",
                        "Updated clip duration would move a keyframe outside the clip",
                    )
            fade = updated_segment.get("fade")
            if fade and fade["in_duration_us"] + fade["out_duration_us"] > updated_segment["timeline"]["duration_us"]:
                raise ValidationFailure("FADE_INVALID", "Fade duration exceeds clip duration")
            return self._replace_segment(project, track["id"], segment_id, updated_segment)

    def move_segment(
        self,
        project_id: str,
        segment_id: str,
        start_us: int,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        segment = self.get_segment(project_id, segment_id)
        timeline = TimeRange(
            start_us=start_us, duration_us=segment["timeline"]["duration_us"]
        )
        return self.update_segment(
            project_id,
            segment_id,
            SegmentUpdate(timeline=timeline),
            expect_revision=expect_revision,
        )

    def delete_segment(
        self,
        project_id: str,
        segment_id: str,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track, _ = self._segment_location(project, segment_id)
            tracks = [
                {
                    **item,
                    "segments": [
                        segment
                        for segment in item.get("segments", [])
                        if segment.get("id") != segment_id
                    ],
                }
                if item.get("id") == track["id"]
                else item
                for item in project.tracks
            ]
            return self._save_tracks(project, tracks)

    def split_segment(
        self,
        project_id: str,
        segment_id: str,
        offset_us: int,
        *,
        expect_revision: int | None = None,
    ) -> tuple[Project, list[dict[str, Any]]]:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track, current = self._segment_location(project, segment_id)
            duration = int(current["timeline"]["duration_us"])
            if not 0 < offset_us < duration:
                raise ValidationFailure(
                    "SPLIT_OFFSET_INVALID",
                    "offset_us must be inside the segment",
                )
            first_duration = offset_us
            second_duration = duration - offset_us
            first = dict(current)
            second = dict(current)
            first["id"], second["id"] = self._segment_id(), self._segment_id()
            first["timeline"] = {
                **current["timeline"],
                "duration_us": first_duration,
            }
            second["timeline"] = {
                **current["timeline"],
                "start_us": current["timeline"]["start_us"] + offset_us,
                "duration_us": second_duration,
            }
            if current.get("source"):
                source = current["source"]
                first_source = round(source["duration_us"] * offset_us / duration)
                if first_source <= 0 or first_source >= source["duration_us"]:
                    raise ValidationFailure("SPLIT_OFFSET_INVALID", "Split would create an empty source range")
                first["source"] = {
                    **source,
                    "duration_us": first_source,
                }
                second["source"] = {
                    **source,
                    "start_us": source["start_us"] + first_source,
                    "duration_us": source["duration_us"] - first_source,
                }
            first_keys, second_keys = self._split_keyframes(
                current.get("keyframes", []), offset_us
            )
            first["keyframes"], second["keyframes"] = first_keys, second_keys
            segments = []
            for item in track.get("segments", []):
                if item.get("id") == segment_id:
                    segments.extend((first, second))
                else:
                    segments.append(item)
            tracks = [
                {**item, "segments": segments}
                if item.get("id") == track["id"]
                else item
                for item in project.tracks
            ]
            saved = self._save_tracks(project, tracks)
            return saved, [first, second]

    def get_segment(self, project_id: str, segment_id: str) -> dict[str, Any]:
        with self._lock:
            project = self.store.get(project_id)
            _, segment = self._segment_location(project, segment_id)
            return dict(segment)

    def add_keyframes(
        self,
        project_id: str,
        segment_id: str,
        request: KeyframeBatch,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track, current = self._segment_location(project, segment_id)
            keyframes = [
                dict(item)
                for item in current.get("keyframes", [])
            ]
            duration = int(current["timeline"]["duration_us"])
            for item in request.keyframes:
                if item.offset_us > duration:
                    raise ValidationFailure(
                        "KEYFRAME_OFFSET_INVALID",
                        "Keyframe offset must be within the segment",
                    )
                self._validate_keyframe_value(item)
                prop = self._canonical_keyframe_property(item.property)
                kind = current.get("kind")
                if prop == "volume" and kind not in {"audio", "video", "image"}:
                    raise ValidationFailure("KEYFRAME_PROPERTY_INVALID", "Volume keyframes require a media clip")
                if prop != "volume" and kind == "audio":
                    raise ValidationFailure("KEYFRAME_PROPERTY_INVALID", "Audio clips support volume keyframes only")
                if prop in {"brightness", "contrast", "saturation"} and kind not in {"video", "image"}:
                    raise ValidationFailure("KEYFRAME_PROPERTY_INVALID", "Color keyframes require a video or image clip")
                keyframes = [
                    existing
                    for existing in keyframes
                    if not (
                        existing.get("property") == prop
                        and existing.get("offset_us") == item.offset_us
                    )
                ]
                keyframes.append(
                    {"property": prop, "offset_us": item.offset_us, "value": item.value}
                )
            keyframes.sort(key=lambda item: (item["property"], item["offset_us"]))
            scale_properties = {item["property"] for item in keyframes}
            if "uniform_scale" in scale_properties and scale_properties.intersection(
                {"scale_x", "scale_y"}
            ):
                raise ValidationFailure(
                    "KEYFRAME_PROPERTY_CONFLICT",
                    "Uniform scale keyframes cannot be combined with axis scale keyframes",
                )
            return self._replace_segment(
                project, track["id"], segment_id, {**current, "keyframes": keyframes}
            )

    def list_keyframes(self, project_id: str, segment_id: str) -> dict[str, Any]:
        segment = self.get_segment(project_id, segment_id)
        return {"segment_id": segment_id, "keyframes": segment.get("keyframes", [])}

    def remove_keyframe(
        self,
        project_id: str,
        segment_id: str,
        property_name: str,
        offset_us: int,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        prop = self._canonical_keyframe_property(property_name)
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track, current = self._segment_location(project, segment_id)
            keyframes = [
                item
                for item in current.get("keyframes", [])
                if not (
                    item.get("property") == prop and item.get("offset_us") == offset_us
                )
            ]
            if len(keyframes) == len(current.get("keyframes", [])):
                raise ServiceError("KEYFRAME_NOT_FOUND", "Keyframe does not exist", status=404)
            return self._replace_segment(
                project, track["id"], segment_id, {**current, "keyframes": keyframes}
            )

    def add_effect(
        self,
        project_id: str,
        segment_id: str,
        request: EffectCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        return self._append_feature(project_id, segment_id, "effects", request.model_dump(), expect_revision)

    def add_filter(
        self,
        project_id: str,
        segment_id: str,
        request: FilterCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        return self._append_feature(project_id, segment_id, "filters", request.model_dump(), expect_revision)

    def add_animation(
        self,
        project_id: str,
        segment_id: str,
        request: AnimationCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        return self._append_feature(project_id, segment_id, "animations", request.model_dump(), expect_revision)

    def add_transition(
        self,
        project_id: str,
        segment_id: str,
        request: TransitionCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track, current = self._segment_location(project, segment_id)
            if current.get("kind") not in {"video", "image"}:
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Transitions require a video or image clip")
            _enum_member(TransitionType, request.transition_type)
            duration = request.duration_us or min(500_000, current["timeline"]["duration_us"])
            if duration > current["timeline"]["duration_us"]:
                raise ValidationFailure("TRANSITION_INVALID", "Transition is longer than the clip")
            return self._replace_segment(
                project,
                track["id"],
                segment_id,
                {
                    **current,
                    "transition": {
                        "transition_type": request.transition_type,
                        "duration_us": duration,
                    },
                },
            )

    def add_fade(
        self,
        project_id: str,
        segment_id: str,
        request: FadeCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track, current = self._segment_location(project, segment_id)
            if current.get("kind") not in {"video", "image", "audio"}:
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Fades require a media clip")
            if request.in_duration_us + request.out_duration_us > current["timeline"]["duration_us"]:
                raise ValidationFailure("FADE_INVALID", "Fade duration exceeds clip duration")
            return self._replace_segment(
                project,
                track["id"],
                segment_id,
                {**current, "fade": request.model_dump(mode="json")},
            )

    def clear_feature(
        self,
        project_id: str,
        segment_id: str,
        feature: str,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        allowed = {
            "effects", "filters", "animations", "transition", "fade", "mask",
            "chroma", "background_filling", "mix_mode", "crop", "border",
            "background", "shadow", "bubble", "text_effect",
        }
        if feature not in allowed:
            raise ValidationFailure("FEATURE_INVALID", "Unknown segment feature")
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track, current = self._segment_location(project, segment_id)
            if feature in {"bubble", "text_effect"} and current.get("kind") != "text":
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Text features require a text clip")
            value: Any = [] if feature in {"effects", "filters", "animations"} else None
            field = "text_effect_id" if feature == "text_effect" else feature
            return self._replace_segment(project, track["id"], segment_id, {**current, field: value})

    def add_mask(
        self, project_id: str, segment_id: str, request: MaskCreate,
        *, expect_revision: int | None = None,
    ) -> Project:
        _enum_member(MaskType, request.mask_type)
        return self._set_visual_feature(project_id, segment_id, "mask", request.model_dump(), expect_revision)

    def add_chroma(
        self, project_id: str, segment_id: str, request: ChromaCreate,
        *, expect_revision: int | None = None,
    ) -> Project:
        return self._set_visual_feature(project_id, segment_id, "chroma", request.model_dump(), expect_revision)

    def add_background_filling(
        self, project_id: str, segment_id: str, request: BackgroundFillCreate,
        *, expect_revision: int | None = None,
    ) -> Project:
        return self._set_visual_feature(
            project_id, segment_id, "background_filling", request.model_dump(), expect_revision
        )

    def set_mix_mode(
        self, project_id: str, segment_id: str, request: MixModeCreate,
        *, expect_revision: int | None = None,
    ) -> Project:
        _enum_member(MixModeType, request.mode)
        return self._set_visual_feature(project_id, segment_id, "mix_mode", request.model_dump(), expect_revision)

    def _set_visual_feature(
        self, project_id: str, segment_id: str, feature: str, value: dict[str, Any],
        expect_revision: int | None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track, current = self._segment_location(project, segment_id)
            if current.get("kind") not in {"video", "image"}:
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Feature requires a video or image clip")
            if current.get(feature) is not None:
                raise ValidationFailure("FEATURE_EXISTS", f"Clip already has {feature}")
            return self._replace_segment(
                project, track["id"], segment_id, {**current, feature: value}
            )

    def add_sticker_segment(
        self,
        project_id: str,
        track_id: str,
        request: StickerSegmentCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track = self._track(project, track_id)
            if track["track_type"] != "sticker":
                raise ValidationFailure("TRACK_TYPE_MISMATCH", "Stickers need a sticker track")
            self._validate_placement(project, track_id, request.timeline)
            segment = {
                "id": self._segment_id(),
                "kind": "sticker",
                "resource_id": request.resource_id,
                "timeline": request.timeline.model_dump(mode="json"),
                "transform": request.transform.model_dump(mode="json") if request.transform else None,
                "keyframes": [],
            }
            return self._append_segment(project, track, segment)

    def add_track_effect_segment(
        self,
        project_id: str,
        track_id: str,
        request: TrackEffectCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track = self._track(project, track_id)
            if track["track_type"] != "effect":
                raise ValidationFailure("TRACK_TYPE_MISMATCH", "Effects need an effect track")
            effect_type = None
            for enum_type in (VideoSceneEffectType, VideoCharacterEffectType):
                try:
                    effect_type = _enum_member(enum_type, request.effect_type)
                    break
                except ValidationFailure:
                    continue
            if effect_type is None:
                raise ValidationFailure("EFFECT_INVALID", "Unknown video effect")
            if request.params is not None and len(request.params) > len(effect_type.value.params):
                raise ValidationFailure("EFFECT_PARAMS_INVALID", "Too many effect parameters")
            self._validate_placement(project, track_id, request.timeline)
            segment = {
                "id": self._segment_id(),
                "kind": "track_effect",
                "effect_type": request.effect_type,
                "params": request.params,
                "timeline": request.timeline.model_dump(mode="json"),
            }
            return self._append_segment(project, track, segment)

    def add_track_filter_segment(
        self,
        project_id: str,
        track_id: str,
        request: TrackFilterCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track = self._track(project, track_id)
            if track["track_type"] != "filter":
                raise ValidationFailure("TRACK_TYPE_MISMATCH", "Filters need a filter track")
            _enum_member(FilterType, request.filter_type)
            self._validate_placement(project, track_id, request.timeline)
            segment = {
                "id": self._segment_id(),
                "kind": "track_filter",
                "filter_type": request.filter_type,
                "intensity": request.intensity,
                "timeline": request.timeline.model_dump(mode="json"),
            }
            return self._append_segment(project, track, segment)

    def add_marker(
        self,
        project_id: str,
        request: MarkerCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            markers = [marker for marker in project.markers if marker.get("time_us") != request.time_us]
            markers.append(request.model_dump(mode="json"))
            markers.sort(key=lambda marker: marker["time_us"])
            updated = project.model_copy(
                update={
                    "markers": markers,
                    "updated_at": datetime.now(timezone.utc),
                    "draft": None,
                }
            )
            return self.store.save(updated, expect_revision=project.revision)

    def list_markers(self, project_id: str) -> list[dict[str, Any]]:
        return list(self.store.get(project_id).markers)

    def remove_marker(
        self,
        project_id: str,
        time_us: int,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            markers = [marker for marker in project.markers if marker.get("time_us") != time_us]
            if len(markers) == len(project.markers):
                raise ServiceError("MARKER_NOT_FOUND", "Timeline marker does not exist", status=404)
            updated = project.model_copy(
                update={
                    "markers": markers,
                    "updated_at": datetime.now(timezone.utc),
                    "draft": None,
                }
            )
            return self.store.save(updated, expect_revision=project.revision)

    def remove_subtitle_group(
        self,
        project_id: str,
        track_id: str,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track = next(
                (item for item in project.tracks if item.get("id") == track_id and item.get("kind") == "subtitle"),
                None,
            )
            if track is None:
                raise ValidationFailure("SUBTITLE_NOT_FOUND", "Subtitle group does not exist")
            return self._save_tracks(
                project, [item for item in project.tracks if item.get("id") != track_id]
            )

    def add_media_segment(
        self,
        project_id: str,
        track_id: str,
        request: MediaSegmentCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track = self._track(project, track_id)
        info = self.media.probe(request.path)
        if info.kind == "audio" and track["track_type"] != "audio":
            raise ValidationFailure("MEDIA_TRACK_MISMATCH", "Audio needs an audio track")
        if info.kind != "audio" and track["track_type"] != "video":
            raise ValidationFailure("MEDIA_TRACK_MISMATCH", "Picture needs a video track")
        source = request.source or TimeRange(
            duration_us=(
                round(request.timeline.duration_us * request.speed)
                if request.speed is not None
                else request.timeline.duration_us
            )
        )
        speed = request.speed or (source.duration_us / request.timeline.duration_us)
        if source.start_us + source.duration_us > info.duration_us:
            raise ValidationFailure("SOURCE_RANGE_INVALID", "Source range exceeds media duration")
        if info.kind == "image" and speed != 1.0:
            raise ValidationFailure("SPEED_INVALID", "Still images do not support speed changes")
        if info.kind == "audio" and (request.transform is not None or request.fit != "contain"):
            raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Audio clips do not support visual transforms")
        if info.kind == "audio" and request.crop is not None:
            raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Audio clips do not support cropping")
        if request.speed is not None and round(source.duration_us / speed) != request.timeline.duration_us:
            raise ValidationFailure(
                "MEDIA_DURATION_MISMATCH",
                "Timeline duration must equal source duration divided by speed",
            )
        with self._lock:
            return self._add_media(
                project_id,
                track_id,
                request,
                info,
                source,
                expect_revision,
            )

    def _add_media(
        self,
        project_id: str,
        track_id: str,
        request: MediaSegmentCreate,
        info: MediaInfo,
        source: TimeRange,
        expect_revision: int | None,
    ) -> Project:
        project = self._expected(project_id, expect_revision)
        track = self._track(project, track_id)
        if info.kind == "audio" and track["track_type"] != "audio":
            raise ValidationFailure("MEDIA_TRACK_MISMATCH", "Audio needs an audio track")
        if info.kind != "audio" and track["track_type"] != "video":
            raise ValidationFailure("MEDIA_TRACK_MISMATCH", "Picture needs a video track")
        self._validate_placement(project, track_id, request.timeline)
        speed = request.speed or (source.duration_us / request.timeline.duration_us)
        if round(source.duration_us / speed) != request.timeline.duration_us:
            raise ValidationFailure(
                "MEDIA_DURATION_MISMATCH",
                "Timeline duration must equal source duration divided by speed",
            )
        segment = {
            "id": self._segment_id(),
            "kind": info.kind,
            "path": str(info.path),
            "timeline": request.timeline.model_dump(mode="json"),
            "source": source.model_dump(mode="json"),
            "volume": request.volume,
            "speed": speed,
            "change_pitch": request.change_pitch,
            "fit": request.fit,
            "transform": request.transform.model_dump(mode="json")
            if request.transform
            else None,
            "crop": request.crop.model_dump(mode="json") if request.crop else None,
            "material_name": request.material_name or info.path.name,
            "keyframes": [],
            "effects": [],
            "filters": [],
            "animations": [],
            "transition": None,
            "fade": None,
        }
        return self._append_segment(project, track, segment)

    def add_text_segment(
        self,
        project_id: str,
        track_id: str,
        request: TextSegmentCreate,
        *,
        expect_revision: int | None = None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track = self._track(project, track_id)
            if track["track_type"] != "text":
                raise ValidationFailure("TRACK_TYPE_MISMATCH", "Text needs a text track")
            self._validate_placement(project, track_id, request.timeline)
            segment = {
                "id": self._segment_id(),
                "kind": "text",
                "text": request.text,
                "timeline": request.timeline.model_dump(mode="json"),
                "transform": request.transform.model_dump(mode="json")
                if request.transform
                else None,
                "font_size": request.font_size,
                "color": request.color.upper(),
                "alignment": request.alignment,
                "font_family": request.font_family,
                "bold": request.bold,
                "italic": request.italic,
                "underline": request.underline,
                "opacity": request.opacity,
                "vertical": request.vertical,
                "letter_spacing": request.letter_spacing,
                "line_spacing": request.line_spacing,
                "auto_wrapping": request.auto_wrapping,
                "max_line_width": request.max_line_width,
                "border": request.border.model_dump(mode="json") if request.border else None,
                "background": request.background.model_dump(mode="json") if request.background else None,
                "shadow": request.shadow.model_dump(mode="json") if request.shadow else None,
                "bubble": request.bubble.model_dump(mode="json") if request.bubble else None,
                "text_effect_id": request.text_effect_id,
                "keyframes": [],
                "animations": [],
            }
            return self._append_segment(project, track, segment)

    def add_srt_subtitles(
        self,
        project_id: str,
        srt: str,
        *,
        group_id: str | None = None,
        expect_revision: int | None = None,
    ) -> Project:
        if not isinstance(srt, str) or not srt.strip():
            raise ValidationFailure("SRT_EMPTY", "SRT content is empty")
        if len(srt.encode("utf-8")) > MAX_SRT_BYTES:
            raise ValidationFailure("SRT_TOO_LARGE", "SRT content exceeds size limit")
        entries = parse_srt(srt)
        group = group_id or secrets.token_hex(16)
        if len(group) != 32 or any(c not in "0123456789abcdef" for c in group):
            raise ValidationFailure("SUBTITLE_GROUP_INVALID", "Subtitle group is invalid")
        with self._lock:
            project = self._expected(project_id, expect_revision)
            if any(
                track.get("kind") == "subtitle" and track.get("id") == group
                for track in project.tracks
            ):
                raise ServiceError(
                    "SUBTITLE_EXISTS", "Subtitle group already exists", status=409
                )
            track = {
                "id": group,
                "kind": "subtitle",
                "track_type": "text",
                "name": "Subtitles",
                "mute": False,
                "segments": [
                    {
                        "id": self._segment_id(),
                        "kind": "text",
                        "text": entry.text,
                        "timeline": TimeRange(
                            start_us=entry.start_us,
                            duration_us=entry.end_us - entry.start_us,
                        ).model_dump(mode="json"),
                        "transform": None,
                        "font_size": 8.0,
                        "color": "#FFFFFF",
                        "alignment": "center",
                    }
                    for entry in entries
                ],
            }
            updated = project.model_copy(
                update={
                    "tracks": [*project.tracks, track],
                    "updated_at": datetime.now(timezone.utc),
                    "draft": None,
                }
            )
            return self.store.save(updated, expect_revision=project.revision)

    def save_draft(self, project_id: str) -> tuple[Project, DraftInfo]:
        with self._lock:
            project = self.store.get(project_id)
            infos = self._media_infos(project)
            subtitle_tracks = self._subtitle_segments(project)
            material_names: dict[str, str] = {}
            for track in project.tracks:
                if track.get("kind") == "subtitle":
                    continue
                for segment in track.get("segments", []):
                    if segment.get("kind") in {"text", "sticker"} or not segment.get("material_name"):
                        continue
                    key = str(Path(segment["path"]).resolve()).casefold()
                    material_name = segment["material_name"]
                    path = Path(material_name)
                    if path.suffix.casefold() == Path(segment["path"]).suffix.casefold():
                        material_name = path.stem
                    material_names.setdefault(key, material_name)
            base_draft_id = hashlib.sha256(
                f"draft\0{project.id}".encode()
            ).hexdigest()[:32].upper()
            draft_id = base_draft_id
            target = self.settings.drafts_root / draft_id
            if target.exists() or target.is_symlink():
                self._validate_existing_draft(project, target)
                draft_info = self._draft_info(project, target)
                if (
                    project.draft is not None
                    and project.draft.content_sha256 == draft_info.content_sha256
                    and target.joinpath(".owned").read_bytes() == project.id.encode("ascii")
                ):
                    return project, draft_info
                # A project edit invalidates the previous immutable draft. Create a
                # revision-scoped sibling instead of replacing existing files.
                draft_id = hashlib.sha256(
                    f"draft\0{project.id}\0{project.revision}".encode()
                ).hexdigest()[:32].upper()
                target = self.settings.drafts_root / draft_id
                while target.exists() or target.is_symlink():
                    draft_id = secrets.token_hex(16).upper()
                    target = self.settings.drafts_root / draft_id

            self.settings.drafts_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            result = create_draft_folder(
                project,
                self.settings.drafts_root,
                draft_id,
                infos,
                subtitle_tracks,
                material_names,
                animation_resource_roots=self.settings.media_roots,
            )
            draft_info = DraftInfo(
                path=str(result.directory),
                content_sha256=result.content_hash,
                bytes=result.size,
                modified_at=datetime.fromtimestamp(
                    result.content_path.stat().st_mtime, timezone.utc
                ),
            )
            saved = project.model_copy(
                update={"draft": draft_info, "updated_at": datetime.now(timezone.utc)}
            )
            try:
                saved = self.store.save(saved, expect_revision=project.revision)
            except ServiceError:
                if project.revision == 1:
                    self._remove_orphan_draft(draft_id)
                raise
            return saved, draft_info

    def build_draft(
        self,
        project_id: str,
        directory: Path,
        draft_id: str,
        *,
        owned: bool = True,
    ) -> tuple[Project, Path, bytes, dict[str, Any]]:
        """Create a new draft directory from the project without changing its state."""
        with self._lock:
            project = self.store.get(project_id)
            infos = self._media_infos(project)
            subtitle_tracks = self._subtitle_segments(project)
            material_names: dict[str, str] = {}
            for track in project.tracks:
                if track.get("kind") == "subtitle":
                    continue
                for segment in track.get("segments", []):
                    if segment.get("kind") in {"text", "sticker"} or not segment.get("material_name"):
                        continue
                    key = str(Path(segment["path"]).resolve()).casefold()
                    material_name = segment["material_name"]
                    path = Path(material_name)
                    if path.suffix.casefold() == Path(segment["path"]).suffix.casefold():
                        material_name = path.stem
                    material_names.setdefault(key, material_name)
            result = create_draft_folder(
                project=project,
                root=directory.parent,
                draft_id=draft_id,
                media=infos,
                subtitle_tracks=subtitle_tracks,
                material_names=material_names,
                owned=owned,
                animation_resource_roots=self.settings.media_roots,
            )
            content = json.loads(result.content)
            if owned:
                marker = result.directory / ".jianying_ai_owned.json"
                if marker.exists() or marker.is_symlink():
                    raise ServiceError(
                        "DRAFT_EXISTS",
                        "Draft ownership marker already exists",
                        status=409,
                    )
                atomic_write(
                    marker,
                    json.dumps(
                        {
                            "draft_id": draft_id.upper(),
                            "project_id": project.id,
                            "generator": "jianying-ai-service",
                        },
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    replace=False,
                )
            return project, result.directory, result.content, content

    def _expected(self, project_id: str, expect_revision: int | None) -> Project:
        project = self.store.get(project_id)
        if expect_revision is not None and project.revision != expect_revision:
            raise ServiceError(
                "PROJECT_CONFLICT",
                "Project changed; read it again before saving",
                status=409,
                details={"current_revision": project.revision},
            )
        return project

    def _save_tracks(self, project: Project, tracks: list[dict]) -> Project:
        updated = project.model_copy(
            update={
                "tracks": tracks,
                "updated_at": datetime.now(timezone.utc),
                "draft": None,
            }
        )
        return self.store.save(updated, expect_revision=project.revision)

    @staticmethod
    def _track_any(project: Project, track_id: str) -> dict:
        for track in project.tracks:
            if track.get("id") == track_id:
                return track
        raise ValidationFailure("TRACK_NOT_FOUND", "Track does not exist")

    @classmethod
    def _segment_location(cls, project: Project, segment_id: str) -> tuple[dict, dict]:
        for track in project.tracks:
            for segment in track.get("segments", []):
                if segment.get("id") == segment_id:
                    return track, segment
        raise ValidationFailure("SEGMENT_NOT_FOUND", "Segment does not exist")

    def _replace_segment(
        self,
        project: Project,
        track_id: str,
        segment_id: str,
        replacement: dict[str, Any],
    ) -> Project:
        tracks = [
            {
                **track,
                "segments": [
                    replacement if item.get("id") == segment_id else item
                    for item in track.get("segments", [])
                ],
            }
            if track.get("id") == track_id
            else track
            for track in project.tracks
        ]
        return self._save_tracks(project, tracks)

    def _append_feature(
        self,
        project_id: str,
        segment_id: str,
        feature: str,
        value: dict[str, Any],
        expect_revision: int | None,
    ) -> Project:
        with self._lock:
            project = self._expected(project_id, expect_revision)
            track, current = self._segment_location(project, segment_id)
            if current.get("kind") not in {"video", "image", "audio", "text"}:
                raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Feature requires a media or text clip")
            if feature == "effects":
                if current.get("kind") not in {"video", "image", "audio"}:
                    raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Effects require a media clip")
                effect_types = (
                    (AudioSceneEffectType, ToneEffectType, SpeechToSongType)
                    if current.get("kind") == "audio"
                    else (VideoSceneEffectType, VideoCharacterEffectType)
                )
                for enum_type in effect_types:
                    try:
                        effect_type = _enum_member(enum_type, value["effect_type"])
                        break
                    except ValidationFailure:
                        continue
                else:
                    raise ValidationFailure("EFFECT_INVALID", "Unknown effect for this clip type")
                params = value.get("params")
                if params is not None and len(params) > len(effect_type.value.params):
                    raise ValidationFailure("EFFECT_PARAMS_INVALID", "Too many effect parameters")
                item = {"effect_type": value["effect_type"], "params": value.get("params")}
            elif feature == "filters":
                if current.get("kind") not in {"video", "image"}:
                    raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Filters require a video or image clip")
                _enum_member(FilterType, value["filter_type"])
                item = {"filter_type": value["filter_type"], "intensity": value["intensity"]}
            elif feature == "animations":
                if current.get("kind") not in {"video", "image", "text"}:
                    raise ValidationFailure("SEGMENT_FIELD_MISMATCH", "Animations require a visual clip")
                animation_types = (
                    (TextIntro, TextOutro, TextLoopAnim)
                    if current.get("kind") == "text"
                    else (IntroType, OutroType, GroupAnimationType)
                )
                for enum_type in animation_types:
                    try:
                        _enum_member(enum_type, value["animation_type"])
                        break
                    except ValidationFailure:
                        continue
                else:
                    raise ValidationFailure("ANIMATION_INVALID", "Unknown animation for this clip type")
                duration = value.get("duration_us")
                if duration is not None and duration > current["timeline"]["duration_us"]:
                    raise ValidationFailure("ANIMATION_INVALID", "Animation exceeds clip duration")
                item = {"animation_type": value["animation_type"], "duration_us": value.get("duration_us")}
            else:
                raise ValidationFailure("FEATURE_INVALID", "Unknown segment feature")
            return self._replace_segment(
                project,
                track["id"],
                segment_id,
                {**current, feature: [*current.get(feature, []), item]},
            )

    @staticmethod
    def _canonical_keyframe_property(value: str) -> str:
        aliases = {"position_x": "transform_x", "position_y": "transform_y"}
        allowed = {
            "transform_x",
            "transform_y",
            "scale_x",
            "scale_y",
            "uniform_scale",
            "rotation",
            "alpha",
            "volume",
            "brightness",
            "contrast",
            "saturation",
        }
        canonical = aliases.get(value, value)
        if canonical not in allowed:
            raise ValidationFailure("KEYFRAME_PROPERTY_INVALID", "Unsupported keyframe property")
        return canonical

    @staticmethod
    def _validate_keyframe_value(item: KeyframeCreate) -> None:
        prop = ProjectService._canonical_keyframe_property(item.property)
        value = float(item.value)
        ranges = {
            "alpha": (0.0, 1.0),
            "volume": (0.0, 2.0),
            "scale_x": (0.01, 100.0),
            "scale_y": (0.01, 100.0),
            "uniform_scale": (0.01, 100.0),
            "transform_x": (-1.0, 1.0),
            "transform_y": (-1.0, 1.0),
            "rotation": (-360.0, 360.0),
            "brightness": (-1.0, 1.0),
            "contrast": (-1.0, 1.0),
            "saturation": (-1.0, 1.0),
        }
        low, high = ranges[prop]
        if not low <= value <= high:
            raise ValidationFailure(
                "KEYFRAME_VALUE_INVALID",
                f"Value for {prop} must be between {low} and {high}",
            )

    @staticmethod
    def _split_keyframes(
        keyframes: list[dict[str, Any]], offset_us: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        properties = {item["property"] for item in keyframes}
        left: list[dict[str, Any]] = []
        right: list[dict[str, Any]] = []
        for prop in properties:
            points = sorted(
                (item for item in keyframes if item["property"] == prop),
                key=lambda item: item["offset_us"],
            )
            before = [item for item in points if item["offset_us"] < offset_us]
            exact = [item for item in points if item["offset_us"] == offset_us]
            after = [item for item in points if item["offset_us"] > offset_us]
            left.extend(dict(item) for item in before)
            right.extend(
                {**item, "offset_us": item["offset_us"] - offset_us}
                for item in after
            )
            if exact:
                boundary = dict(exact[0])
                left.append(boundary)
                right.append({**boundary, "offset_us": 0})
            elif before and after:
                first, last = before[-1], after[0]
                ratio = (offset_us - first["offset_us"]) / (
                    last["offset_us"] - first["offset_us"]
                )
                value = first["value"] + (last["value"] - first["value"]) * ratio
                boundary = {"property": prop, "offset_us": offset_us, "value": value}
                left.append(boundary)
                right.append({**boundary, "offset_us": 0})
        key = lambda item: (item["property"], item["offset_us"])
        return sorted(left, key=key), sorted(right, key=key)

    @staticmethod
    def _track(project: Project, track_id: str) -> dict:
        for track in project.tracks:
            if track.get("id") == track_id and track.get("kind") != "subtitle":
                return track
        raise ValidationFailure("TRACK_NOT_FOUND", "Track does not exist")

    @staticmethod
    def _track_id(project: Project, track_type: str) -> str:
        existing = {track.get("id") for track in project.tracks}
        while True:
            candidate = secrets.token_hex(16)
            if candidate not in existing:
                return candidate

    @staticmethod
    def _segment_id() -> str:
        return secrets.token_hex(16)

    @staticmethod
    def _validate_placement(
        project: Project,
        track_id: str | None,
        timeline: TimeRange,
        *,
        ignore_segment_id: str | None = None,
    ) -> None:
        for track in project.tracks:
            if track.get("kind") == "subtitle" or (
                track_id is not None and track.get("id") != track_id
            ):
                continue
            for segment in track.get("segments", []):
                if ignore_segment_id is not None and segment.get("id") == ignore_segment_id:
                    continue
                existing = segment["timeline"]
                if timeline.start_us < existing["start_us"] + existing["duration_us"] and (
                    existing["start_us"] < timeline.start_us + timeline.duration_us
                ):
                    raise ValidationFailure("TIMELINE_OVERLAP", "Clips overlap on the track")

    def _append_segment(self, project: Project, track: dict, segment: dict) -> Project:
        tracks = [
            {**item, "segments": [*item["segments"], segment]}
            if item.get("id") == track["id"]
            else item
            for item in project.tracks
        ]
        updated = project.model_copy(
            update={
                "tracks": tracks,
                "updated_at": datetime.now(timezone.utc),
                "draft": None,
            }
        )
        return self.store.save(updated, expect_revision=project.revision)

    def _media_infos(self, project: Project) -> list[MediaInfo]:
        paths: dict[str, str] = {}
        media_kinds = {"video", "image", "audio"}
        for track in project.tracks:
            if track.get("kind") == "subtitle":
                continue
            for segment in track.get("segments", []):
                if segment.get("kind") in media_kinds:
                    paths[str(Path(segment["path"]).resolve()).casefold()] = segment["path"]
        infos: list[MediaInfo] = []
        for path in paths.values():
            info = self.media.probe(path)
            key = str(Path(path).resolve()).casefold()
            for track in project.tracks:
                for segment in track.get("segments", []):
                    if segment.get("kind") not in media_kinds:
                        continue
                    if str(Path(segment["path"]).resolve()).casefold() != key:
                        continue
                    source = segment["source"]
                    if source["start_us"] + source["duration_us"] > info.duration_us:
                        raise ValidationFailure(
                            "SOURCE_RANGE_INVALID", "Media changed since it was added"
                        )
            infos.append(info)
        return infos

    @staticmethod
    def _subtitle_segments(project: Project) -> list[tuple[dict, list[dict]]]:
        return [
            (track, track["segments"])
            for track in project.tracks
            if track.get("kind") == "subtitle"
        ]

    def _validate_existing_draft(self, project: Project, target: Path) -> None:
        if not target.is_dir() or target.is_symlink():
            raise ServiceError("DRAFT_EXISTS", "Draft folder already exists", status=409)
        for name in (
            "draft_content.json",
            "draft_meta_info.json",
            "project.json",
            ".owned",
        ):
            path = target / name
            if not path.is_file() or path.is_symlink():
                raise ServiceError("DRAFT_EXISTS", "Draft folder is incomplete", status=409)
        try:
            owner = (target / ".owned").read_bytes()
            metadata = json.loads(
                (target / "draft_meta_info.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ServiceError("DRAFT_UNREADABLE", "Draft cannot be read", status=500) from exc
        if owner != project.id.encode("ascii") or metadata.get("draft_name") != project.name:
            raise ServiceError("DRAFT_EXISTS", "Draft folder is not owned by this project", status=409)
        expected_id = hashlib.sha256(f"draft\0{project.id}".encode()).hexdigest()[:32].upper()
        if target.name != expected_id:
            raise ServiceError("DRAFT_EXISTS", "Draft folder is not owned by this project", status=409)

    def _draft_info(self, project: Project, target: Path) -> DraftInfo:
        content = target / "draft_content.json"
        try:
            return DraftInfo(
                path=str(target),
                content_sha256=sha256_file(content),
                bytes=content.stat().st_size,
                modified_at=datetime.fromtimestamp(
                    content.stat().st_mtime, timezone.utc
                ),
            )
        except OSError as exc:
            raise ServiceError("DRAFT_UNREADABLE", "Draft cannot be read", status=500) from exc

    def _remove_orphan_draft(self, draft_id: str) -> None:
        target = self.settings.drafts_root / draft_id
        if target.is_dir() and not target.is_symlink() and target.is_relative_to(
            self.settings.drafts_root
        ):
            shutil.rmtree(target, ignore_errors=True)


def parse_srt(content: str) -> list[SubtitleEntry]:
    text = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re_split_blocks(text)
    entries: list[SubtitleEntry] = []
    for block in blocks:
        lines = block.split("\n")
        if not lines or not lines[0].strip():
            continue
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            raise ValidationFailure("SRT_INVALID", "Expected an SRT timing line")
        start, end = parse_srt_interval(lines[0])
        caption = "\n".join(lines[1:]).strip()
        if not caption:
            raise ValidationFailure("SRT_INVALID", "SRT caption is empty")
        try:
            entries.append(SubtitleEntry(start_us=start, end_us=end, text=caption))
        except ValidationError as exc:
            raise ValidationFailure("SRT_INVALID", "SRT times exceed the 24-hour limit") from exc
    if not entries:
        raise ValidationFailure("SRT_INVALID", "SRT contains no captions")
    return entries


def re_split_blocks(content: str) -> Sequence[str]:
    return [block for block in content.split("\n\n") if block.strip()]


def parse_srt_interval(line: str) -> tuple[int, int]:
    match = re.fullmatch(
        r"\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
        r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})(?:\s+.*)?",
        line,
    )
    if match is None:
        raise ValidationFailure("SRT_INVALID", "SRT timing is invalid")
    start = srt_timestamp(match.group(1), match.group(2), match.group(3), match.group(4))
    end = srt_timestamp(match.group(5), match.group(6), match.group(7), match.group(8))
    if end <= start:
        raise ValidationFailure("SRT_INVALID", "SRT interval must be positive")
    return start, end


def srt_timestamp(hours: str, minutes: str, seconds: str, millis: str) -> int:
    minute, second = int(minutes), int(seconds)
    if minute > 59 or second > 59:
        raise ValidationFailure("SRT_INVALID", "SRT time component is invalid")
    return ((int(hours) * 60 + minute) * 60 + second) * 1_000_000 + int(millis) * 1000

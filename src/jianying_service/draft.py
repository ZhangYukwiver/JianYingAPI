from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyJianYingDraft as draft_lib
from pyJianYingDraft import (
    AudioSceneEffectType,
    AudioMaterial,
    AudioSegment,
    ClipSettings,
    CropSettings,
    FilterType,
    FontType,
    GroupAnimationType,
    IntroType,
    KeyframeProperty,
    MaskType,
    MixModeType,
    OutroType,
    SpeechToSongType,
    StickerSegment,
    TextSegment,
    TextBackground,
    TextBorder,
    TextShadow,
    TextStyle,
    Timerange,
    TrackSpec,
    TrackType,
    ToneEffectType,
    TransitionType,
    TextIntro,
    TextLoopAnim,
    TextOutro,
    VideoCharacterEffectType,
    VideoMaterial,
    VideoSceneEffectType,
    VideoSegment,
)

from .errors import ServiceError, ValidationFailure
from .media import MediaInfo
from .models import Canvas, ClipTransform, Project, TextSegmentCreate
from .storage import atomic_write, sha256_file


DRAFT_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z"
)


@dataclass(frozen=True)
class DraftResult:
    directory: Path
    content_path: Path
    content: bytes
    content_hash: str
    draft_hash: str
    size: int
    modifications: list[str]


def _id(label: str, scope: str) -> str:
    digest = hashlib.sha256(f"{scope}\0{label}".encode("utf-8")).hexdigest()
    return digest[:32]


def _timerange(start: int, duration: int) -> Timerange:
    return Timerange(start, duration)


def _transform(transform: ClipTransform | None, fit: str = "contain", media: MediaInfo | None = None, canvas: Canvas | None = None) -> ClipSettings | None:
    if transform is None and fit == "contain":
        return None
    values = (transform or ClipTransform()).model_dump()
    if fit != "contain" and media and canvas and media.width and media.height:
        source_ratio = media.width / media.height
        canvas_ratio = canvas.width / canvas.height
        factor = canvas_ratio / source_ratio
        if fit == "cover":
            uniform = max(1.0, factor)
            values["scale_x"] *= uniform
            values["scale_y"] *= uniform
        elif fit == "stretch":
            values["scale_x"] *= factor
        else:
            values["scale_y"] *= max(1.0, factor)
    return ClipSettings(**values)


def _rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def _text_style(segment: dict[str, Any]) -> TextStyle:
    alignment = segment.get("alignment", "center")
    alignments = {"left": 0, "center": 1, "right": 2}
    return TextStyle(
        size=float(segment.get("font_size", 8.0)),
        bold=bool(segment.get("bold", False)),
        italic=bool(segment.get("italic", False)),
        underline=bool(segment.get("underline", False)),
        color=_rgb(segment.get("color", "#FFFFFF")),
        alpha=float(segment.get("opacity", 1.0)),
        align=alignments[alignment],
        vertical=bool(segment.get("vertical", False)),
        letter_spacing=int(segment.get("letter_spacing", 0)),
        line_spacing=int(segment.get("line_spacing", 0)),
        auto_wrapping=bool(segment.get("auto_wrapping", True)),
        max_line_width=float(segment.get("max_line_width", 0.82)),
    )


def _enum_member(enum_type: Any, raw: str) -> Any:
    """Resolve stable enum names while accepting the display value from the library."""
    try:
        return enum_type[raw]
    except (KeyError, TypeError):
        for member in enum_type:
            if raw == getattr(member, "value", None) or raw == getattr(member, "name", None):
                return member
    raise ValidationFailure("DRAFT_FEATURE_INVALID", f"Unknown {enum_type.__name__}: {raw}")


def _apply_keyframes(instance: Any, segment: dict[str, Any]) -> None:
    property_map = {
        "transform_x": KeyframeProperty.position_x,
        "transform_y": KeyframeProperty.position_y,
        "scale_x": KeyframeProperty.scale_x,
        "scale_y": KeyframeProperty.scale_y,
        "uniform_scale": KeyframeProperty.uniform_scale,
        "rotation": KeyframeProperty.rotation,
        "alpha": KeyframeProperty.alpha,
        "volume": KeyframeProperty.volume,
        "brightness": KeyframeProperty.brightness,
        "contrast": KeyframeProperty.contrast,
        "saturation": KeyframeProperty.saturation,
    }
    for item in segment.get("keyframes", []):
        prop = property_map.get(item.get("property"))
        if prop is None:
            raise ValidationFailure("DRAFT_FEATURE_INVALID", "Unknown keyframe property")
        if isinstance(instance, AudioSegment):
            if prop is not KeyframeProperty.volume:
                raise ValidationFailure("DRAFT_FEATURE_INVALID", "Audio supports volume keyframes only")
            instance.add_keyframe(int(item["offset_us"]), float(item["value"]))
        else:
            instance.add_keyframe(prop, int(item["offset_us"]), float(item["value"]))


def _apply_features(instance: Any, segment: dict[str, Any]) -> None:
    _apply_keyframes(instance, segment)
    if isinstance(instance, VideoSegment):
        for item in segment.get("effects", []):
            raw = item["effect_type"]
            try:
                effect_type = _enum_member(VideoSceneEffectType, raw)
            except ValidationFailure:
                effect_type = _enum_member(VideoCharacterEffectType, raw)
            instance.add_effect(effect_type, item.get("params"))
        for item in segment.get("filters", []):
            instance.add_filter(
                _enum_member(FilterType, item["filter_type"]),
                float(item.get("intensity", 100.0)),
            )
        transition = segment.get("transition")
        if transition:
            instance.add_transition(
                _enum_member(TransitionType, transition["transition_type"]),
                duration=int(transition["duration_us"]),
            )
        fade = segment.get("fade")
        if fade:
            instance.add_fade(int(fade["in_duration_us"]), int(fade["out_duration_us"]))
        mask = segment.get("mask")
        if mask:
            instance.add_mask(
                _enum_member(MaskType, mask["mask_type"]),
                center_x=float(mask.get("center_x", 0.0)),
                center_y=float(mask.get("center_y", 0.0)),
                size=float(mask.get("size", 0.5)),
                rotation=float(mask.get("rotation", 0.0)),
                feather=float(mask.get("feather", 0.0)),
                invert=bool(mask.get("invert", False)),
                rect_width=mask.get("rect_width"),
                round_corner=mask.get("round_corner"),
            )
        chroma = segment.get("chroma")
        if chroma:
            instance.add_chroma(
                chroma["color"],
                intensity=float(chroma.get("intensity", 20.0)),
                shadow=float(chroma.get("shadow", 0.0)),
                edge_smooth=float(chroma.get("edge_smooth", 0.0)),
                spill=float(chroma.get("spill", 0.0)),
            )
        background = segment.get("background_filling")
        if background:
            instance.add_background_filling(
                background["fill_type"],
                blur=float(background.get("blur", 0.0625)),
                color=background.get("color", "#00000000"),
            )
        mix_mode = segment.get("mix_mode")
        if mix_mode:
            instance.set_mix_mode(_enum_member(MixModeType, mix_mode["mode"]))
        for item in segment.get("animations", []):
            raw = item["animation_type"]
            for enum_type in (IntroType, OutroType, GroupAnimationType):
                try:
                    animation = _enum_member(enum_type, raw)
                    instance.add_animation(animation, duration=item.get("duration_us"))
                    break
                except ValidationFailure:
                    continue
            else:
                raise ValidationFailure("DRAFT_FEATURE_INVALID", f"Unknown animation: {raw}")
    elif isinstance(instance, AudioSegment):
        fade = segment.get("fade")
        if fade:
            instance.add_fade(int(fade["in_duration_us"]), int(fade["out_duration_us"]))
        for item in segment.get("effects", []):
            raw = item["effect_type"]
            try:
                effect_type = _enum_member(AudioSceneEffectType, raw)
            except ValidationFailure:
                try:
                    effect_type = _enum_member(ToneEffectType, raw)
                except ValidationFailure:
                    effect_type = _enum_member(SpeechToSongType, raw)
            instance.add_effect(effect_type, item.get("params"))
    elif isinstance(instance, TextSegment):
        bubble = segment.get("bubble")
        if bubble:
            instance.add_bubble(bubble["effect_id"], bubble["resource_id"])
        if segment.get("text_effect_id"):
            instance.add_effect(segment["text_effect_id"])
        for item in segment.get("animations", []):
            raw = item["animation_type"]
            for enum_type in (TextIntro, TextOutro, TextLoopAnim):
                try:
                    animation = _enum_member(enum_type, raw)
                    instance.add_animation(animation, duration=item.get("duration_us"))
                    break
                except ValidationFailure:
                    continue
            else:
                raise ValidationFailure("DRAFT_FEATURE_INVALID", f"Unknown text animation: {raw}")


def _new_script(canvas: Canvas, title: str) -> tuple[Any, dict[str, Any]]:
    script = draft_lib.ScriptFile(canvas.width, canvas.height, canvas.fps, True)
    script.content["name"] = title[:200]
    now = int(datetime.now(timezone.utc).timestamp())
    script.content["create_time"] = now
    script.content["update_time"] = now
    refs: dict[str, Any] = {}
    for track in script.content["tracks"]:
        raise AssertionError("fresh template unexpectedly contains tracks")
    return script, refs


def editing_options() -> dict[str, list[str]]:
    return {
        "video_effects": list(VideoSceneEffectType.__members__),
        "character_effects": list(VideoCharacterEffectType.__members__),
        "audio_effects": list(AudioSceneEffectType.__members__),
        "tone_effects": list(ToneEffectType.__members__),
        "filters": list(FilterType.__members__),
        "mask_types": list(MaskType.__members__),
        "mix_modes": list(MixModeType.__members__),
        "text_font_families": list(FontType.__members__),
        "transitions": list(TransitionType.__members__),
        "video_intro_animations": list(IntroType.__members__),
        "video_outro_animations": list(OutroType.__members__),
        "video_group_animations": list(GroupAnimationType.__members__),
        "text_intro_animations": list(TextIntro.__members__),
        "text_outro_animations": list(TextOutro.__members__),
        "text_loop_animations": list(TextLoopAnim.__members__),
        "text_addons": ["bubble", "effect"],
        "keyframe_properties": [
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
        ],
        "marker_support": ["service_metadata_only"],
    }


_ANIMATION_CATEGORIES = (
    ("video_intro", "video", "in", IntroType),
    ("video_outro", "video", "out", OutroType),
    ("video_group", "video", "group", GroupAnimationType),
    ("text_intro", "text", "in", TextIntro),
    ("text_outro", "text", "out", TextOutro),
    ("text_loop", "text", "loop", TextLoopAnim),
)


def animation_catalog(
    *,
    query: str = "",
    kind: str | None = None,
    target: str | None = None,
    vip_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Search the animation metadata supported by pyJianYingDraft."""
    query_key = re.sub(r"[\s_]+", "", query).casefold()
    items: list[dict[str, Any]] = []
    for category, animation_target, animation_type, enum_type in _ANIMATION_CATEGORIES:
        if kind is not None and category != kind:
            continue
        if target is not None and animation_target != target:
            continue
        for member_name, member in enum_type.__members__.items():
            metadata = member.value
            item = {
                "kind": category,
                "target": animation_target,
                "animation_type": member_name,
                "animation_mode": animation_type,
                "name": metadata.title,
                "resource_id": metadata.resource_id,
                "effect_id": metadata.effect_id,
                "md5": metadata.md5,
                "default_duration_us": metadata.duration,
                "is_vip": metadata.is_vip,
            }
            if vip_only and not metadata.is_vip:
                continue
            if query_key:
                searchable = (category, member_name, metadata.title, metadata.resource_id, metadata.effect_id)
                if not any(
                    query_key in re.sub(r"[\s_]+", "", value).casefold()
                    for value in searchable
                ):
                    continue
            items.append(item)
    return {
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "items": items[offset : offset + limit],
    }


def animation_resource_handling() -> dict[str, str]:
    return {
        "catalog_search": "available",
        "download": "jianying_client_attempts_on_draft_open",
        "local_path": "auto_resolved_from_media_roots",
        "local_cache_layout": "animation_resources/<effect_id>/<md5>",
    }


def _attach_local_animation_paths(content: dict[str, Any], roots: tuple[Path, ...]) -> None:
    """Bind installed animation packages so drafts can load them without a download."""
    if not roots:
        return
    metadata = {
        (member.value.effect_id, member.value.resource_id): (category, member.value)
        for category, _target, _mode, enum_type in _ANIMATION_CATEGORIES
        for member in enum_type
    }
    for group in content["materials"].get("material_animations", []):
        for animation in group.get("animations", []):
            entry = metadata.get((animation.get("id"), animation.get("resource_id")))
            if entry is None:
                continue
            category, item = entry
            for configured_root in roots:
                root = configured_root.resolve()
                candidate = root / "animation_resources" / item.effect_id / item.md5
                if not candidate.exists() and not candidate.is_symlink():
                    continue
                try:
                    resource = candidate.resolve(strict=True)
                    if (
                        not resource.is_relative_to(root)
                        or not resource.is_dir()
                        or candidate.is_symlink()
                        or candidate.is_junction()
                    ):
                        raise ValueError("Invalid animation package directory")
                    config = resource / "config.json"
                    if (
                        not config.resolve(strict=True).is_relative_to(resource)
                        or not config.is_file()
                        or config.stat().st_size > 1024 * 1024
                    ):
                        raise ValueError("Invalid animation package configuration")
                    package = json.loads(config.read_text(encoding="utf-8"))
                    if (
                        not isinstance(package, dict)
                        or not isinstance(package.get("effect"), dict)
                        or not isinstance(package["effect"].get("Link"), list)
                        or not package["effect"]["Link"]
                    ):
                        raise ValueError("Invalid animation package configuration")
                except (OSError, ValueError, TypeError, RuntimeError) as exc:
                    raise ValidationFailure(
                        "ANIMATION_RESOURCE_INVALID",
                        "An installed animation package is incomplete or outside configured media roots",
                    ) from exc
                animation["path"] = resource.as_posix()
                if category.startswith("text_"):
                    animation["panel"] = "text"
                break


def serialize_draft(
    project: Project,
    directory: Path,
    media: list[MediaInfo],
    subtitle_tracks: list[tuple[dict[str, Any], list[dict[str, Any]]]] | None = None,
    material_names: dict[str, str] | None = None,
    draft_id: str | None = None,
    animation_resource_roots: tuple[Path, ...] = (),
) -> DraftResult:
    """Translate service state into pyJianYingDraft's documented 10.8-compatible layout."""
    script, refs = _new_script(project.canvas, project.name)
    track_names: dict[str, str] = {}
    media_by_path = {str(info.path).casefold(): info for info in media}
    materials: dict[str, VideoMaterial | AudioMaterial] = {}
    tracks = [track for track in project.tracks if track.get("kind") != "subtitle"]
    subtitle_by_id = {track["id"]: (track, segments) for track, segments in (subtitle_tracks or [])}
    all_tracks = project.tracks
    for track in all_tracks:
        track_type = track["track_type"]
        if track_type not in {"video", "audio", "text", "sticker", "effect", "filter"}:
            if track.get("kind") != "subtitle":
                raise ValidationFailure("TRACK_TYPE_INVALID", "Unsupported track type")
            track_type = "text"
        selected = {
            "video": TrackType.video,
            "audio": TrackType.audio,
            "text": TrackType.text,
            "sticker": TrackType.sticker,
            "effect": TrackType.effect,
            "filter": TrackType.filter,
        }[track_type]
        ref = script.append_track(
            TrackSpec(selected, name=track["name"], mute=track["mute"])
        )
        refs[track["id"]] = ref
        track_names[track["id"]] = track["name"]

    segments: list[tuple[str, dict[str, Any]]] = []
    for track in tracks:
        segments.extend((track["id"], segment) for segment in track["segments"])
    for track in all_tracks:
        if track.get("kind") == "subtitle":
            track_segments = subtitle_by_id.get(track["id"], (track, track["segments"]))[1]
            segments.extend((track["id"], segment) for segment in track_segments)

    for track_id, segment in segments:
        ref = refs[track_id]
        timeline = segment["timeline"]
        segment_id = segment["id"]
        if segment["kind"] == "track_effect":
            effect_type = None
            for enum_type in (VideoSceneEffectType, VideoCharacterEffectType):
                try:
                    effect_type = _enum_member(enum_type, segment["effect_type"])
                    break
                except ValidationFailure:
                    continue
            if effect_type is None:
                raise ValidationFailure("DRAFT_FEATURE_INVALID", "Unknown video effect")
            script.add_effect(
                effect_type,
                _timerange(timeline["start_us"], timeline["duration_us"]),
                track_name=track_names[track_id],
                params=segment.get("params"),
            )
            script.tracks[track_names[track_id]].segments[-1].segment_id = _id(
                segment_id, "segment"
            )
            continue
        if segment["kind"] == "track_filter":
            script.add_filter(
                _enum_member(FilterType, segment["filter_type"]),
                _timerange(timeline["start_us"], timeline["duration_us"]),
                track_name=track_names[track_id],
                intensity=float(segment["intensity"]),
            )
            script.tracks[track_names[track_id]].segments[-1].segment_id = _id(
                segment_id, "segment"
            )
            continue
        if segment["kind"] == "sticker":
            instance = StickerSegment(
                segment["resource_id"],
                _timerange(timeline["start_us"], timeline["duration_us"]),
                clip_settings=_transform(
                    ClipTransform(**segment["transform"])
                    if segment.get("transform")
                    else None
                ),
            )
            instance.segment_id = _id(segment_id, "segment")
            _apply_features(instance, segment)
            script.add_segment(instance, ref)
            continue
        if segment["kind"] == "text":
            instance = TextSegment(
                segment["text"],
                _timerange(timeline["start_us"], timeline["duration_us"]),
                font=_enum_member(FontType, segment["font_family"])
                if segment.get("font_family")
                else None,
                style=_text_style(segment),
                clip_settings=_transform(
                    ClipTransform(**segment["transform"])
                    if segment["transform"]
                    else None
                ),
                border=(
                    TextBorder(
                        alpha=float(segment["border"].get("alpha", 1.0)),
                        color=_rgb(segment["border"].get("color", "#000000")),
                        width=float(segment["border"].get("width", 40.0)),
                    )
                    if segment.get("border")
                    else None
                ),
                background=TextBackground(**segment["background"])
                if segment.get("background")
                else None,
                shadow=(
                    TextShadow(
                        alpha=float(segment["shadow"].get("alpha", 1.0)),
                        color=_rgb(segment["shadow"].get("color", "#000000")),
                        diffuse=float(segment["shadow"].get("diffuse", 15.0)),
                        distance=float(segment["shadow"].get("distance", 5.0)),
                        angle=float(segment["shadow"].get("angle", -45.0)),
                    )
                    if segment.get("shadow")
                    else None
                ),
            )
            instance.segment_id = _id(segment_id, "segment")
            instance.material_id = _id(segment_id, "text-material")
            _apply_features(instance, segment)
            script.add_segment(instance, ref)
            continue
        info = media_by_path.get(str(Path(segment["path"]).resolve()).casefold())
        if info is None:
            raise ValidationFailure("MEDIA_NOT_FOUND", "Clip material is unavailable")
        key = str(info.path).casefold()
        crop = segment.get("crop") if info.kind != "audio" else None
        material_key = (
            key
            if crop is None
            else f"{key}\0crop\0{json.dumps(crop, sort_keys=True, separators=(',', ':'))}"
        )
        if material_key not in materials:
            material_name = (material_names or {}).get(key, info.path.name)
            if info.kind == "audio":
                materials[material_key] = AudioMaterial(str(info.path), material_name)
            else:
                materials[material_key] = VideoMaterial(
                    str(info.path),
                    material_name,
                    crop_settings=CropSettings(**crop) if crop else CropSettings(),
                )
        material = materials[material_key]
        source = segment["source"]
        common = {
            "source_timerange": _timerange(
                source["start_us"], source["duration_us"]
            ),
            "speed": None,
            "volume": segment["volume"],
            "change_pitch": bool(segment.get("change_pitch", False)),
        }
        if info.kind == "audio":
            instance = AudioSegment(
                material,
                _timerange(timeline["start_us"], timeline["duration_us"]),
                **common,
            )
        else:
            instance = VideoSegment(
                material,
                _timerange(timeline["start_us"], timeline["duration_us"]),
                clip_settings=_transform(
                    ClipTransform(**segment["transform"])
                    if segment["transform"]
                    else None,
                    segment.get("fit", "contain"),
                    info,
                    project.canvas,
                ),
                **common,
            )
        if segment.get("speed") is not None:
            # Construct with the source range first so the requested timeline stays stable;
            # then replace the derived speed in the exported segment.
            instance.speed.speed = float(segment["speed"])
        # Keep repeated saves deterministic and usable with the fixed template.
        instance.segment_id = _id(segment_id, "segment")
        material.material_id = _id(material_key, "material")
        _apply_features(instance, segment)
        script.add_segment(instance, ref)

    content = script.dumps().encode("utf-8")
    try:
        content_json = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ValidationFailure("DRAFT_SERIALIZE_FAILED", "Draft content is invalid") from exc
    generated_id = draft_id or _id(project.id, "draft").upper()
    content_json["id"] = (
        f"{generated_id[:8]}-{generated_id[8:12]}-{generated_id[12:16]}-"
        f"{generated_id[16:20]}-{generated_id[20:32]}"
    )
    content_json["name"] = project.name
    content_json["create_time"] = int(project.created_at.timestamp())
    content_json["update_time"] = int(project.updated_at.timestamp())
    content_json["last_modified_platform"]["app_version"] = "11.6.0.14552"
    content_json["platform"]["app_version"] = "11.6.0.14552"
    _attach_local_animation_paths(content_json, animation_resource_roots)
    content = json.dumps(content_json, ensure_ascii=False, indent=4, allow_nan=False).encode("utf-8")
    meta = _metadata(project, directory, content_json, media)
    metadata = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
    project_data = json.dumps(
        project.model_dump(mode="json"), ensure_ascii=False, indent=2, allow_nan=False
    ).encode("utf-8")
    digest = hashlib.sha256()
    for name, payload in (
        ("draft_content.json", content),
        ("draft_meta_info.json", metadata),
        ("project.json", project_data),
    ):
        digest.update(name.encode("ascii"))
        digest.update(payload)
    return DraftResult(
        directory=directory,
        content_path=directory / "draft_content.json",
        content=content,
        content_hash=hashlib.sha256(content).hexdigest(),
        draft_hash=digest.hexdigest(),
        size=len(content) + len(metadata),
        modifications=[],
    )


def create_draft_folder(
    project: Project,
    root: Path,
    draft_id: str,
    media: list[MediaInfo],
    subtitle_tracks: list[tuple[dict[str, Any], list[dict[str, Any]]]] | None = None,
    material_names: dict[str, str] | None = None,
    *,
    owned: bool = True,
    animation_resource_roots: tuple[Path, ...] = (),
) -> DraftResult:
    """Publish a complete draft folder without replacing an existing one."""
    generated_id = _id(project.id, "draft").upper()
    if DRAFT_UUID.fullmatch(draft_id):
        draft_id = draft_id.replace("-", "")
        generated_id = draft_id.upper()
    draft_id = draft_id.upper()
    root = root.resolve()
    if len(draft_id) != 32 or any(c not in "0123456789ABCDEF" for c in draft_id):
        raise ValidationFailure("DRAFT_ID_INVALID", "Draft identifier is invalid")
    target = root / draft_id
    if not target.is_relative_to(root):
        raise ValidationFailure("PATH_UNSAFE", "Draft path left its root")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists() or target.is_symlink():
        raise ServiceError("DRAFT_EXISTS", "Draft folder already exists", status=409)

    temporary = root / f".{draft_id}.{secrets.token_hex(8)}.tmp"
    claim = root / f".{draft_id}.{secrets.token_hex(8)}.claim"
    try:
        try:
            claim.mkdir(mode=0o700)
            if owned:
                atomic_write(claim / "owned", project.id.encode("ascii"), replace=False)
            temporary.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ServiceError(
                "DRAFT_EXISTS", "Draft folder already exists", status=409
            ) from exc
        content = serialize_draft(
            project,
            target,
            media,
            subtitle_tracks,
            material_names,
            generated_id,
            animation_resource_roots,
        ).content
        project_data = json.dumps(
            project.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        meta = _metadata(project, target, json.loads(content), media)
        metadata = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
        atomic_write(temporary / "draft_content.json", content, replace=False)
        atomic_write(temporary / "draft_meta_info.json", metadata, replace=False)
        atomic_write(temporary / "project.json", project_data, replace=False)
        if owned:
            os.link(claim / "owned", temporary / ".owned", follow_symlinks=False)
        publish_directory(temporary, target)
        return DraftResult(
            directory=target,
            content_path=target / "draft_content.json",
            content=content,
            content_hash=hashlib.sha256(content).hexdigest(),
            draft_hash=_manifest_hash(content, metadata, project_data),
            size=len(content) + len(metadata),
            modifications=[],
        )
    finally:
        if os.path.lexists(temporary):
            shutil.rmtree(temporary, ignore_errors=True)
        if os.path.lexists(claim):
            shutil.rmtree(claim, ignore_errors=True)


def publish_directory(temporary: Path, target: Path) -> None:
    """Publish a staged directory without replacing an existing target."""
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file_ex = kernel32.MoveFileExW
        move_file_ex.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        ]
        move_file_ex.restype = ctypes.c_int
        moved = move_file_ex(str(temporary), str(target), 0x00000008)
        if moved:
            return
        error = ctypes.get_last_error()
        if error in (80, 183):
            raise ServiceError(
                "DRAFT_EXISTS", "Draft folder already exists", status=409
            )
        raise OSError(error, "Staged draft directory could not be published")
    try:
        temporary.rename(target)
    except FileExistsError as exc:
        raise ServiceError(
            "DRAFT_EXISTS", "Draft folder already exists", status=409
        ) from exc


def _manifest_hash(
    content: bytes, metadata: bytes, project_data: bytes
) -> str:
    digest = hashlib.sha256()
    for name, payload in (
        ("draft_content.json", content),
        ("draft_meta_info.json", metadata),
        ("project.json", project_data),
    ):
        digest.update(name.encode("ascii"))
        digest.update(payload)
    return digest.hexdigest()


def _metadata(
    project: Project,
    directory: Path,
    content: dict[str, Any],
    media: list[MediaInfo],
) -> dict[str, Any]:
    return {
        "draft_id": content["id"],
        "draft_name": project.name,
        "draft_fold_path": str(directory),
        "draft_root_path": str(directory.parent),
        "draft_timeline_materials_size_": sum(
            info.path.stat().st_size for info in media if info.path.exists()
        ),
        "tm_draft_create": content["create_time"] * 1_000_000,
        "tm_draft_modified": content["update_time"] * 1_000_000,
        "tm_duration": content["duration"],
    }


def decode_embedded_png(data_uri: str) -> bytes:
    match = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)", data_uri)
    if not match:
        raise ValidationFailure("MEDIA_DATA_INVALID", "Expected a PNG data URI")
    try:
        payload = base64.b64decode(match.group(1), validate=True)
    except ValueError:
        raise ValidationFailure("MEDIA_DATA_INVALID", "Invalid base64 image")
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValidationFailure("MEDIA_DATA_INVALID", "Data is not a PNG")
    return payload


def service_manifest_hash(path: Path) -> str:
    return sha256_file(path)

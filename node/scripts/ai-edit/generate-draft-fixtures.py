"""Generate oracle fixtures for node/aiEdit/draft.mjs from the original JianYingAPI.

node/aiEdit/draft.mjs is a Node port of JianYingAPI's draft.py (plus the parts of
pyJianYingDraft 0.3.1 it uses). This script builds projects through the original
ProjectService, serializes them with the original code and stores the exact output, so
the Node port can be compared byte for byte (see node/aiEdit/draft.test.mjs).

Run from anywhere with the JianYingAPI virtualenv (needs ffprobe on PATH):

    <venv>/bin/python -I node/scripts/ai-edit/generate-draft-fixtures.py --jianying-api <JianYingAPI checkout>

Output: node/fixtures/ai-edit/drafts/index.json plus one <scenario>.draft_content.json
(and <scenario>.original.json where the port intentionally differs) per scenario.

What is patched on the Python side, and why:
- uuid.uuid4 / secrets.token_hex / service.datetime are made deterministic. Random ids come
  out as face0000…<n>; the test renumbers them by first appearance on both sides.
- pyJianYingDraft's VideoMaterial/AudioMaterial read duration/size with pymediainfo. The port
  uses the ffprobe-equivalent probe (node/fixtures/ai-edit/media-info.json) instead, so the
  patched constructors take those values (still images keep pyJianYingDraft's 3 h duration).
- Two confirmed bugs are fixed the same way as in the port ("fixed" output = what the port must
  produce); scenarios that hit them also store the unpatched "original" output:
  1. draft._transform: fit cover/stretch only scaled one direction (wrong for sources wider
     than the canvas).
  2. draft.serialize_draft: the material id was replaced after the first clip was built, so
     materials.videos listed the same file twice.
- Media paths are rewritten to the virtual root /AI-Edit/Media before serializing so material
  ids (sha256 of the path) do not depend on where the repo lives. Fixtures spell it ${MEDIA}.
"""

from __future__ import annotations

import __future__ as future_flags
import argparse
import inspect
import itertools
import json
import os
import secrets
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MEDIA_DIR = (REPO / "fixtures" / "ai-edit" / "media").resolve()
MEDIA_INFO = json.loads((REPO / "fixtures" / "ai-edit" / "media-info.json").read_text("utf-8"))
OUT = REPO / "fixtures" / "ai-edit" / "drafts"
VIRTUAL_MEDIA = "/AI-Edit/Media"

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--jianying-api", required=True, type=Path, help="JianYingAPI checkout (read only)")
args = parser.parse_args()
sys.path.insert(0, str(args.jianying_api.resolve() / "src"))

# ---- determinism -----------------------------------------------------------------------
_uuid_counter = itertools.count(1)
uuid.uuid4 = lambda: uuid.UUID(int=(0xFACE << 112) | next(_uuid_counter))
_token_counter = itertools.count(1)
secrets.token_hex = lambda nbytes=32: f"{next(_token_counter):0{nbytes * 2}x}"

import pyJianYingDraft  # noqa: E402
from pyJianYingDraft import ClipSettings, CropSettings  # noqa: E402
from jianying_service import draft as draft_module  # noqa: E402
from jianying_service import service as service_module  # noqa: E402
from jianying_service.config import Settings  # noqa: E402
from jianying_service.media import MediaInfo  # noqa: E402
from jianying_service.models import (  # noqa: E402
    AnimationCreate, BackgroundFillCreate, Canvas, ChromaCreate, ClipTransform, EffectCreate,
    FadeCreate, FilterCreate, KeyframeBatch, KeyframeCreate, MarkerCreate, MaskCreate,
    MediaSegmentCreate, MixModeCreate, Project, ProjectCreate, SegmentUpdate, StickerSegmentCreate,
    TextSegmentCreate, TimeRange, TrackCreate, TrackEffectCreate, TrackFilterCreate, TrackOrder,
    TrackUpdate, TransitionCreate,
)

_clock = itertools.count()


class FakeClock(datetime):
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 10, 19, 8, 0, 0, 123456, tzinfo=timezone.utc)
        return base + timedelta(seconds=next(_clock))


service_module.datetime = FakeClock


def _video_material_init(self, path, material_name=None, crop_settings=CropSettings()):
    path = os.path.abspath(path)
    info = MEDIA_INFO[os.path.basename(path)]
    self.material_name = material_name if material_name else os.path.basename(path)
    self.material_id = uuid.uuid4().hex
    self.path = path
    self.crop_settings = crop_settings
    self.local_material_id = ""
    if info["kind"] == "image":
        self.material_type = "photo"
        self.duration = 10800000000
    else:
        self.material_type = "video"
        self.duration = info["duration_us"]
    self.width = info["width"]
    self.height = info["height"]


def _audio_material_init(self, path, material_name=None):
    path = os.path.abspath(path)
    self.material_name = material_name if material_name else os.path.basename(path)
    self.material_id = uuid.uuid4().hex
    self.path = path
    self.duration = MEDIA_INFO[os.path.basename(path)]["duration_us"]


pyJianYingDraft.VideoMaterial.__init__ = _video_material_init
pyJianYingDraft.AudioMaterial.__init__ = _audio_material_init

# ---- bug fixes mirrored from the port ----------------------------------------------------
ORIGINAL_TRANSFORM = draft_module._transform
ORIGINAL_SERIALIZE = draft_module.serialize_draft


def fixed_transform(transform, fit="contain", media=None, canvas=None):
    if transform is None and fit == "contain":
        return None
    values = (transform or ClipTransform()).model_dump()
    if fit != "contain" and media and canvas and media.width and media.height:
        source_ratio = media.width / media.height
        canvas_ratio = canvas.width / canvas.height
        factor = canvas_ratio / source_ratio
        if fit == "cover":
            uniform = max(factor, 1 / factor)
            values["scale_x"] *= uniform
            values["scale_y"] *= uniform
        elif fit == "stretch":
            if factor >= 1:
                values["scale_x"] *= factor
            else:
                values["scale_y"] *= 1 / factor
        else:
            values["scale_y"] *= max(1.0, factor)
    return ClipSettings(**values)


def _patched_serialize():
    source = inspect.getsource(ORIGINAL_SERIALIZE)
    assign = '        material.material_id = _id(material_key, "material")\n'
    anchor = "        material = materials[material_key]\n"
    assert source.count(assign) == 1 and source.count(anchor) == 1, "draft.py changed; update the patch"
    source = source.replace(assign, "").replace(anchor, anchor + assign)
    namespace = draft_module.__dict__
    code = compile(source, draft_module.__file__, "exec", flags=future_flags.annotations.compiler_flag, dont_inherit=True)
    exec(code, namespace)
    patched = namespace["serialize_draft"]
    namespace["serialize_draft"] = ORIGINAL_SERIALIZE
    return patched


FIXED_SERIALIZE = _patched_serialize()


def use_fixed(fixed: bool) -> None:
    draft_module._transform = fixed_transform if fixed else ORIGINAL_TRANSFORM
    draft_module.serialize_draft = FIXED_SERIALIZE if fixed else ORIGINAL_SERIALIZE


# ---- service + helpers -------------------------------------------------------------------
WORK = Path(tempfile.mkdtemp(prefix="draft-fixtures-")).resolve()
ANIM_ROOT = WORK / "anim-root"
ANIM_MISSING = WORK / "anim-missing"
settings = Settings(data_root=WORK / "data", media_roots=(MEDIA_DIR,))
svc = service_module.ProjectService(settings)


def tr(start_us: int, duration_us: int) -> dict:
    return {"start_us": start_us, "duration_us": duration_us}


class Builder:
    def __init__(self, name: str, **canvas):
        self.id = svc.create_project(ProjectCreate(name=name, canvas=Canvas(**canvas))).id

    def track(self, track_type: str, name: str | None = None, mute: bool = False) -> str:
        return svc.add_track(self.id, TrackCreate(track_type=track_type, name=name, mute=mute)).tracks[-1]["id"]

    @staticmethod
    def _last(project, track_id):
        return next(t for t in project.tracks if t["id"] == track_id)["segments"][-1]["id"]

    def media(self, track_id, file, start, duration, **kw) -> str:
        request = MediaSegmentCreate(path=str(MEDIA_DIR / file), timeline=tr(start, duration), **kw)
        return self._last(svc.add_media_segment(self.id, track_id, request), track_id)

    def text(self, track_id, text, start, duration, **kw) -> str:
        request = TextSegmentCreate(text=text, timeline=tr(start, duration), **kw)
        return self._last(svc.add_text_segment(self.id, track_id, request), track_id)

    def sticker(self, track_id, resource_id, start, duration, **kw) -> str:
        request = StickerSegmentCreate(resource_id=resource_id, timeline=tr(start, duration), **kw)
        return self._last(svc.add_sticker_segment(self.id, track_id, request), track_id)

    def track_effect(self, track_id, effect_type, start, duration, params=None) -> str:
        request = TrackEffectCreate(effect_type=effect_type, params=params, timeline=tr(start, duration))
        return self._last(svc.add_track_effect_segment(self.id, track_id, request), track_id)

    def track_filter(self, track_id, filter_type, start, duration, intensity=100.0) -> str:
        request = TrackFilterCreate(filter_type=filter_type, intensity=intensity, timeline=tr(start, duration))
        return self._last(svc.add_track_filter_segment(self.id, track_id, request), track_id)

    def keyframes(self, segment_id, *items) -> None:
        batch = KeyframeBatch(keyframes=[KeyframeCreate(property=p, offset_us=o, value=v) for p, o, v in items])
        svc.add_keyframes(self.id, segment_id, batch)

    def project(self) -> Project:
        return svc.get_project(self.id)


def first_member(enum_type, exclude=()):
    return next(name for name in enum_type.__members__ if name not in exclude)


VIDEO_OUTRO = first_member(pyJianYingDraft.OutroType, pyJianYingDraft.IntroType.__members__)
TEXT_OUTRO = first_member(pyJianYingDraft.TextOutro, pyJianYingDraft.TextIntro.__members__)


class Captured(Exception):
    pass


def capture_inputs(project_id: str) -> dict:
    """Run the real build_draft but stop at create_draft_folder to record its arguments."""
    box: dict = {}

    def fake_create(**kwargs):
        box.update(kwargs)
        raise Captured

    real = service_module.create_draft_folder
    service_module.create_draft_folder = fake_create
    try:
        svc.build_draft(project_id, WORK / "capture" / ("0" * 32), "0" * 32)
    except Captured:
        pass
    finally:
        service_module.create_draft_folder = real
    return box


def virt(value):
    if isinstance(value, str):
        return value.replace(str(MEDIA_DIR), VIRTUAL_MEDIA)
    if isinstance(value, list):
        return [virt(item) for item in value]
    if isinstance(value, dict):
        return {key: virt(item) for key, item in value.items()}
    return value


def placeholder_text(text: str) -> str:
    return text.replace(VIRTUAL_MEDIA, "${MEDIA}").replace(str(WORK), "${WORK}")


def placeholder(value):
    return json.loads(placeholder_text(json.dumps(value, ensure_ascii=False)))


def virtual_inputs(project: Project):
    captured = capture_inputs(project.id)
    for info in captured["media"]:
        expected = MEDIA_INFO[info.path.name]
        assert {k: getattr(info, k) for k in expected} == expected, f"ffprobe drifted for {info.path.name}"
    media = [
        MediaInfo(Path(VIRTUAL_MEDIA) / info.path.name, info.kind, info.duration_us, info.width, info.height,
                  info.audio, info.video)
        for info in captured["media"]
    ]
    names = {
        key.replace(str(MEDIA_DIR).casefold(), VIRTUAL_MEDIA.casefold()): value
        for key, value in captured["material_names"].items()
    }
    vproject = Project.model_validate(virt(project.model_dump(mode="json")))
    return vproject, media, names


def media_json(media):
    return [
        {"path": str(info.path), "kind": info.kind, "duration_us": info.duration_us, "width": info.width,
         "height": info.height, "audio": info.audio, "video": info.video}
        for info in media
    ]


def serialize(project, media, names, *, fixed=True, draft_id=None, roots=()):
    use_fixed(fixed)
    subtitles = service_module.ProjectService._subtitle_segments(project)
    result = draft_module.serialize_draft(project, Path("/DRAFTS/X"), media, subtitles, names, draft_id, tuple(roots))
    return result.content.decode("utf-8")


SCENARIOS: list[dict] = []
FILES: dict[str, str] = {}


def scenario(name, description, builder_or_project, *, draft_id=None, roots=(), original=False, packages=None, mutate=None):
    project = builder_or_project.project() if isinstance(builder_or_project, Builder) else builder_or_project
    vproject, media, names = virtual_inputs(project)
    if mutate:
        data = vproject.model_dump(mode="json")
        media_list = mutate(data, media)
        if media_list is not None:
            media = media_list
        vproject = Project.model_validate(data)
    record = {
        "name": name,
        "description": description,
        "project": placeholder(vproject.model_dump(mode="json")),
        "media": placeholder(media_json(media)),
        "inputs": {
            "subtitle_track_ids": [t["id"] for t, _ in service_module.ProjectService._subtitle_segments(vproject)],
            "material_names": placeholder(names),
        },
        "draft_id": draft_id,
        "animation_roots": [placeholder_text(str(root)) for root in roots],
        "packages": packages or [],
    }
    try:
        content = serialize(vproject, media, names, draft_id=draft_id, roots=roots)
    except Exception as exc:  # noqa: BLE001 - record what the original raises
        record["error"] = {"type": type(exc).__name__, "code": getattr(exc, "code", None), "message": str(exc)}
    else:
        FILES[f"{name}.draft_content.json"] = placeholder_text(content)
        record["expected"] = f"{name}.draft_content.json"
        if original:
            FILES[f"{name}.original.json"] = placeholder_text(
                serialize(vproject, media, names, fixed=False, draft_id=draft_id, roots=roots)
            )
            record["original"] = f"{name}.original.json"
    SCENARIOS.append(record)
    return vproject, media, names


# ---- scenarios -------------------------------------------------------------------------
def build_basic():
    b = Builder("基础工程 Basic")
    video = b.track("video", "主轨")
    audio = b.track("audio")
    text = b.track("text", "标题")
    b.media(video, "clip-720p.mp4", 0, 2_000_000)
    b.media(video, "still.jpg", 2_000_000, 1_000_000)
    b.media(audio, "tone.wav", 0, 3_000_000)
    b.text(text, "你好，剪映 👋", 0, 1_500_000)
    svc.add_marker(b.id, MarkerCreate(time_us=500_000, label="开头"))
    return b


def build_fit_landscape():
    b = Builder("横屏画布适配", width=1920, height=1080, fps=30)
    video = b.track("video", "画面")
    b.media(video, "clip-portrait.mov", 0, 1_000_000, fit="cover")
    b.media(video, "clip-portrait.mov", 1_000_000, 1_000_000, fit="stretch")
    b.media(video, "still.png", 2_000_000, 1_000_000, fit="cover")
    b.media(video, "still.png", 3_000_000, 1_000_000, fit="stretch")
    b.media(video, "clip-720p.mp4", 4_000_000, 1_000_000, fit="cover")
    b.media(video, "clip-portrait.mov", 5_000_000, 1_000_000, fit="cover",
            transform={"scale_x": 1.2, "scale_y": 0.9, "transform_x": 0.25, "rotation": 30})
    return b


def build_fit_portrait():
    b = Builder("竖屏画布适配", width=1080, height=1920, fps=25)
    video = b.track("video", "画面")
    b.media(video, "clip-720p.mp4", 0, 1_000_000, fit="cover")
    b.media(video, "clip-720p.mp4", 1_000_000, 1_000_000, fit="stretch")
    b.media(video, "still.jpg", 2_000_000, 1_000_000, fit="cover", transform={"transform_y": -0.3, "alpha": 0.75})
    b.media(video, "still.png", 3_000_000, 1_000_000, fit="stretch")
    b.media(video, "hevc.mp4", 4_000_000, 1_000_000, fit="cover")
    b.media(video, "clip.avi", 5_000_000, 1_000_000, fit="contain", transform={"flip_horizontal": True})
    return b


def build_same_material():
    b = Builder("同一素材多次使用")
    video = b.track("video", "主轨")
    b.media(video, "clip-720p.mp4", 0, 1_000_000, material_name="镜头A.mp4")
    b.media(video, "clip-720p.mp4", 1_000_000, 1_000_000, source=tr(1_000_000, 1_000_000), material_name="别的名字")
    b.media(video, "clip-720p.mp4", 2_000_000, 500_000, source=tr(2_000_000, 500_000))
    crop = {"upper_left_x": 0.1, "upper_left_y": 0.2, "upper_right_x": 0.9, "upper_right_y": 0.2,
            "lower_left_x": 0.1, "lower_left_y": 0.8, "lower_right_x": 0.9, "lower_right_y": 0.8}
    b.media(video, "clip-720p.mp4", 2_500_000, 500_000, crop=crop)
    b.media(video, "clip-720p.mp4", 3_000_000, 500_000, crop=crop)
    b.media(video, "still.png", 3_500_000, 1_000_000, material_name="封面图")
    b.media(video, "still.png", 4_500_000, 1_000_000, crop={"upper_left_x": 0.0, "lower_right_x": 0.5})
    audio = b.track("audio", "配乐")
    b.media(audio, "tone.mp3", 0, 1_000_000, material_name="tone.MP3")
    b.media(audio, "tone.mp3", 1_000_000, 1_000_000, source=tr(1_000_000, 1_000_000))
    return b


def build_video_features():
    b = Builder("视频特效全家桶", width=1080, height=1920, fps=60)
    main = b.track("video", "主轨")
    a = b.media(main, "clip-720p.mp4", 0, 3_000_000, volume=0.8)
    svc.add_effect(b.id, a, EffectCreate(effect_type="_1998", params=[25.0, 80.0]))
    svc.add_effect(b.id, a, EffectCreate(effect_type="X", params=[10.0]))
    svc.add_effect(b.id, a, EffectCreate(effect_type="CCD闪光"))
    svc.add_filter(b.id, a, FilterCreate(filter_type="偏振镜", intensity=60.0))
    svc.add_filter(b.id, a, FilterCreate(filter_type=first_member(pyJianYingDraft.FilterType)))
    svc.add_transition(b.id, a, TransitionCreate(transition_type="上移"))
    svc.add_fade(b.id, a, FadeCreate(in_duration_us=300_000, out_duration_us=200_000))
    svc.set_mix_mode(b.id, a, MixModeCreate(mode="滤色"))
    svc.add_animation(b.id, a, AnimationCreate(animation_type="动感放大"))
    svc.add_animation(b.id, a, AnimationCreate(animation_type=VIDEO_OUTRO, duration_us=700_000))
    b.keyframes(
        a,
        ("transform_x", 0, -0.5), ("transform_x", 1_500_000, 0.5), ("position_y", 0, 0.1),
        ("rotation", 0, 0.0), ("rotation", 3_000_000, 90.0), ("alpha", 2_000_000, 0.3),
        ("brightness", 0, -0.2), ("contrast", 0, 0.4), ("saturation", 1_000_000, -1.0),
        ("volume", 0, 1.0), ("volume", 3_000_000, 0.0), ("scale_x", 0, 1.0), ("scale_y", 0, 1.5),
    )
    bpng = b.media(main, "still.png", 3_000_000, 2_000_000)
    svc.add_mask(b.id, bpng, MaskCreate(mask_type="矩形", center_x=100.0, center_y=-50.0, size=0.7, rotation=15.0,
                                        feather=20.0, invert=True, rect_width=0.6, round_corner=30.0))
    svc.add_chroma(b.id, bpng, ChromaCreate(color="#00FF00FF", intensity=35.0, shadow=10.0, edge_smooth=5.0, spill=2.0))
    svc.add_background_filling(b.id, bpng, BackgroundFillCreate(fill_type="blur", blur=0.375))
    svc.add_animation(b.id, bpng, AnimationCreate(animation_type="三分割"))
    b.keyframes(bpng, ("uniform_scale", 0, 1.0), ("uniform_scale", 2_000_000, 1.25))
    c = b.media(main, "clip-portrait.mov", 5_000_000, 1_000_000, speed=2.0, change_pitch=True, volume=0.5)
    svc.add_mask(b.id, c, MaskCreate(mask_type="圆形", size=0.4, center_x=-30.5))
    svc.add_background_filling(b.id, c, BackgroundFillCreate(fill_type="color", color="#FF336699"))
    svc.set_mix_mode(b.id, c, MixModeCreate(mode="正片叠底"))
    d = b.media(main, "still.jpg", 6_000_000, 1_500_000)
    svc.add_mask(b.id, d, MaskCreate(mask_type="爱心"))
    svc.add_transition(b.id, d, TransitionCreate(transition_type="_3D空间", duration_us=900_000))
    overlay = b.track("video", "叠加层", mute=True)
    for index, mask in enumerate(("线性", "镜面", "星形")):
        seg = b.media(overlay, "clip.m4v", index * 600_000, 600_000, source=tr(index * 600_000, 600_000),
                      transform={"scale_x": 0.5, "scale_y": 0.5, "transform_x": -0.5, "transform_y": 0.5})
        svc.add_mask(b.id, seg, MaskCreate(mask_type=mask, feather=float(index * 10), rotation=float(-index * 45)))
    return b


def build_audio_features():
    b = Builder("音频处理")
    music = b.track("audio", "音乐")
    a = b.media(music, "tone.mp3", 0, 3_000_000, volume=1.2)
    svc.add_fade(b.id, a, FadeCreate(in_duration_us=500_000, out_duration_us=1_000_000))
    svc.add_effect(b.id, a, EffectCreate(effect_type="回音", params=[30.0, 70.0]))
    svc.add_effect(b.id, a, EffectCreate(effect_type="大叔", params=[20.0]))
    svc.add_effect(b.id, a, EffectCreate(effect_type="Lofi"))
    b.keyframes(a, ("volume", 1_000_000, 1.0), ("volume", 0, 0.2), ("volume", 3_000_000, 0.0))
    b.media(music, "tone.flac", 3_000_000, 1_000_000, speed=1.5, change_pitch=True)
    b.media(music, "tone.m4a", 4_000_000, 2_000_000, volume=1.8, source=tr(200_000, 2_000_000))
    voice = b.track("audio", "人声", mute=True)
    b.media(voice, "tone.ogg", 0, 3_000_000, source=tr(100_000, 3_000_000))
    b.media(voice, "tone.aac", 3_000_000, 2_000_000)
    b.media(voice, "tone.wav", 5_000_000, 1_000_000, source=tr(2_500_000, 1_500_000))
    video = b.track("video", "画面")
    v = b.media(video, "clip-720p.mp4", 0, 3_000_000)
    svc.add_fade(b.id, v, FadeCreate(in_duration_us=0, out_duration_us=400_000))
    b.keyframes(v, ("volume", 0, 0.0), ("volume", 1_000_000, 1.0))
    return b


def build_text_features():
    b = Builder("文字样式全集", width=1080, height=1920, fps=30)
    text = b.track("text", "花字")
    b.text(
        text, '第一行\n第二行 "引号" \\ 反斜杠 😀', 0, 2_000_000,
        font_family="A1明朝", bold=True, italic=True, underline=True, color="#ff8800", opacity=0.8,
        alignment="left", letter_spacing=3, line_spacing=-2, auto_wrapping=False, max_line_width=0.6,
        font_size=12.5,
        border={"color": "#112233", "alpha": 0.9, "width": 55.0},
        background={"color": "#00000080", "style": 2, "alpha": 0.7, "round_radius": 0.3, "height": 0.2,
                    "width": 0.25, "horizontal_offset": 0.4, "vertical_offset": 0.6},
        shadow={"color": "#000000", "alpha": 0.5, "diffuse": 30.0, "distance": 8.0, "angle": -60.0},
        transform={"transform_y": -0.7, "scale_x": 1.3, "scale_y": 1.3, "rotation": -8.0},
    )
    t2 = b.text(text, "竖排气泡花字", 2_000_000, 3_000_000, vertical=True, alignment="right",
                bubble={"effect_id": "7084185632221236516", "resource_id": "7084185632221236516"},
                text_effect_id="7351319129124506930", border={}, shadow={"distance": 2.0})
    svc.add_animation(b.id, t2, AnimationCreate(animation_type="复古打字机", duration_us=400_000))
    svc.add_animation(b.id, t2, AnimationCreate(animation_type=TEXT_OUTRO))
    svc.add_animation(b.id, t2, AnimationCreate(animation_type="VHS"))
    t3 = b.text(text, "关键帧文字", 5_000_000, 2_000_000, background={"color": "#FFFFFFFF"})
    b.keyframes(t3, ("transform_y", 0, -0.2), ("transform_y", 2_000_000, 0.2), ("rotation", 1_000_000, 45.0),
                ("uniform_scale", 0, 0.5), ("alpha", 0, 0.0), ("alpha", 500_000, 1.0))
    svc.add_animation(b.id, t3, AnimationCreate(animation_type="上弧"))
    t4 = b.text(text, "入场+循环", 7_000_000, 1_000_000, alignment="center")
    svc.add_animation(b.id, t4, AnimationCreate(animation_type="冲屏位移"))
    svc.add_animation(b.id, t4, AnimationCreate(animation_type="刷屏"))
    t5 = b.text(text, "出场+循环", 8_000_000, 300_000)
    svc.add_animation(b.id, t5, AnimationCreate(animation_type=TEXT_OUTRO, duration_us=300_000))
    svc.add_animation(b.id, t5, AnimationCreate(animation_type="VHS"))
    caption = b.track("text", "说明")
    b.text(caption, "Hello\tworld\u0001 control", 0, 1_000_000, auto_wrapping=True, letter_spacing=-5, line_spacing=7)
    return b


def build_layers():
    b = Builder("多轨道层级")
    base = b.track("video", "底层")
    top = b.track("video", "上层", mute=True)
    stickers = b.track("sticker", "贴纸")
    effects = b.track("effect", "特效")
    filters = b.track("filter", "滤镜")
    text = b.track("text")
    b.media(base, "clip-720p.mp4", 0, 3_000_000)
    s = b.media(top, "clip.m4v", 500_000, 1_500_000, transform={"scale_x": 0.4, "scale_y": 0.4, "transform_x": 0.5})
    b.keyframes(s, ("transform_x", 0, 0.5), ("transform_x", 1_000_000, -0.5))
    st = b.sticker(stickers, "7226264929999999999", 0, 2_000_000,
                   transform={"scale_x": 0.3, "scale_y": 0.3, "transform_y": 0.6, "rotation": 12.0})
    b.keyframes(st, ("rotation", 0, 0.0), ("rotation", 2_000_000, 360.0), ("alpha", 0, 0.5))
    b.sticker(stickers, "6835012345678901234", 2_000_000, 1_000_000)
    b.track_effect(effects, "_90s画质", 0, 1_000_000, params=[10.0, 20.0, 30.0, 40.0])
    b.track_effect(effects, "BOOM", 1_000_000, 1_000_000)
    b.track_effect(effects, "CCD闪光", 2_000_000, 1_000_000, params=[0.0, 100.0])
    b.track_filter(filters, "偏振镜", 0, 1_500_000, intensity=35.5)
    b.track_filter(filters, first_member(pyJianYingDraft.FilterType), 1_500_000, 1_500_000)
    long_text = b.text(text, "分割前", 0, 2_000_000)
    b.keyframes(long_text, ("transform_x", 0, 0.0), ("transform_x", 2_000_000, 1.0))
    svc.split_segment(b.id, long_text, 500_000)
    removed = b.media(base, "still.jpg", 3_000_000, 500_000)
    svc.delete_segment(b.id, removed)
    project = b.project()
    ids = [t["id"] for t in project.tracks]
    svc.reorder_tracks(b.id, TrackOrder(track_ids=[ids[0], ids[2], ids[1], ids[5], ids[3], ids[4]]))
    svc.update_track(b.id, ids[5], TrackUpdate(name="文字", mute=True))
    return b


def build_subtitles():
    b = Builder("字幕")
    video = b.track("video", "画面")
    b.media(video, "clip-720p.mp4", 0, 3_000_000)
    title = b.track("text", "标题")
    b.text(title, "片头标题", 0, 800_000)
    srt = "1\n00:00:00,000 --> 00:00:01,200\n第一句字幕\n\n2\n00:00:01,200 --> 00:00:02,000\n两行\n字幕\n\n3\n00:00:02,500 --> 00:00:03,000\nLast line\n"
    svc.add_srt_subtitles(b.id, srt)
    return b


def build_formats():
    b = Builder("各种格式", width=1280, height=720, fps=24)
    video = b.track("video", "画面")
    start = 0
    for file in ("clip.mkv", "clip.webm", "clip.avi", "clip.m4v", "fragmented.mp4", "hevc.mp4", "still.webp"):
        duration = min(MEDIA_INFO[file]["duration_us"], 1_000_000)
        b.media(video, file, start, duration, fit="cover")
        start += duration
    b.media(video, "clip.mkv", start, 2_508_000, source=tr(0, 2_508_000))
    audio = b.track("audio", "音频")
    b.media(audio, "tone.aac", 0, 2_729_781, source=tr(0, 2_729_781))
    b.media(audio, "tone.ogg", 2_729_781, 3_106_500, source=tr(0, 3_106_500))
    return b


def build_animations():
    b = Builder("动画资源")
    video = b.track("video", "画面")
    v = b.media(video, "clip-720p.mp4", 0, 3_000_000)
    svc.add_animation(b.id, v, AnimationCreate(animation_type="动感放大"))
    svc.add_animation(b.id, v, AnimationCreate(animation_type=VIDEO_OUTRO))
    text = b.track("text", "文字")
    t = b.text(text, "动画", 0, 3_000_000)
    svc.add_animation(b.id, t, AnimationCreate(animation_type="复古打字机"))
    svc.add_animation(b.id, t, AnimationCreate(animation_type="VHS"))
    return b


def write_package(root: Path, enum_type, member: str, config: dict) -> dict:
    meta = enum_type[member].value
    folder = root / "animation_resources" / meta.effect_id / meta.md5
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return {"root": placeholder_text(str(root)), "effect_id": meta.effect_id, "md5": meta.md5, "config": config}


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    basic = build_basic()
    vbasic, basic_media, basic_names = scenario(
        "basic", "最小工程：视频+图片+音频+文字+标记，指定草稿 id", basic, draft_id="0123456789ABCDEF0123456789ABCDEF"
    )
    scenario("fit-landscape", "横屏画布上的竖屏/4:3 素材 cover/stretch（素材比画布窄，原实现也对）", build_fit_landscape(), original=True)
    scenario("fit-portrait", "竖屏画布上的横屏素材 cover/stretch（素材比画布宽，修复 bug 1）", build_fit_portrait(), original=True)
    scenario("same-material", "同一素材多次使用 + 裁剪变体 + 素材名（修复 bug 2）", build_same_material(), original=True)
    scenario("video-features", "视频片段全部可选特效：特效/滤镜/转场/淡入淡出/蒙版/抠像/背景/混合/动画/关键帧/倍速", build_video_features())
    scenario("audio-features", "音频淡入淡出/场景音/音色/声音成曲/音量关键帧/倍速变调/多格式", build_audio_features())
    scenario("text-features", "文字字体/样式/描边/背景/阴影/气泡/花字/动画/关键帧/转义字符", build_text_features())
    scenario("layers", "贴纸/特效/滤镜轨道、多视频轨、静音、重排、改名、分割、删除", build_layers())
    scenario("subtitles", "SRT 字幕组 + 普通文字轨", build_subtitles())
    scenario("formats", "mkv/webm/avi/m4v/分片 mp4/HEVC/webp/aac/ogg（原库 pymediainfo 读不了 webp/aac）", build_formats())
    empty = Builder("空工程", width=720, height=1280, fps=50)
    scenario("empty", "没有轨道", empty)
    empty_tracks = Builder("只有空轨道")
    for kind in ("video", "audio", "text", "sticker", "effect", "filter"):
        empty_tracks.track(kind)
    scenario("empty-tracks", "六种轨道各一条、没有片段", empty_tracks)

    packages = [
        write_package(ANIM_ROOT, pyJianYingDraft.IntroType, "动感放大", {"effect": {"Link": [{"path": "intro"}]}}),
        write_package(ANIM_ROOT, pyJianYingDraft.TextIntro, "复古打字机", {"effect": {"Link": [{"path": "text"}]}}),
    ]
    animations = build_animations()
    scenario("animation-resources", "本机动画资源包：视频入场、文字入场绑定路径，其余不绑", animations,
             roots=(ANIM_MISSING, ANIM_ROOT), packages=packages)

    # ---- errors ----
    bad_root = WORK / "anim-bad"
    bad_packages = [write_package(bad_root, pyJianYingDraft.IntroType, "动感放大", {"effect": {"Link": []}})]
    scenario("error-animation-package", "动画资源包配置不完整", animations, roots=(bad_root,), packages=bad_packages)

    def seg(data, track_index, segment_index=0):
        return data["tracks"][track_index]["segments"][segment_index]

    def bad_keyframe(data, media):
        seg(data, 0)["keyframes"] = [{"property": "skew", "offset_us": 0, "value": 1.0}]

    def audio_keyframe(data, media):
        seg(data, 1)["keyframes"] = [{"property": "alpha", "offset_us": 0, "value": 1.0}]

    def missing_media(data, media):
        return [info for info in media if info.path.name != "tone.wav"]

    def bad_track(data, media):
        data["tracks"][2]["track_type"] = "lyrics"

    def bad_animation(data, media):
        seg(data, 0)["animations"] = [{"animation_type": "不存在的动画", "duration_us": None}]

    def bad_text_animation(data, media):
        seg(data, 2)["animations"] = [{"animation_type": "不存在的文字动画", "duration_us": None}]

    def duplicate_intro(data, media):
        seg(data, 0)["animations"] = [{"animation_type": "动感放大", "duration_us": None},
                                      {"animation_type": "上下抖动", "duration_us": None}]

    def long_image(data, media):
        image = seg(data, 0, 1)
        image["timeline"] = {"start_us": 2_000_000, "duration_us": 10_800_000_001}
        image["source"] = {"start_us": 0, "duration_us": 10_800_000_001}

    def same_names(data, media):
        data["tracks"][1]["name"] = data["tracks"][0]["name"]
        data["tracks"][1]["track_type"] = "video"
        data["tracks"][1]["segments"] = []

    def overlap(data, media):
        data["tracks"].append({"id": "f" * 32, "kind": "subtitle", "track_type": "text", "name": "Subtitles", "mute": False,
                               "segments": [
                                   {"id": "e" * 32, "kind": "text", "text": "一", "timeline": tr(0, 1_000_000), "transform": None},
                                   {"id": "d" * 32, "kind": "text", "text": "二", "timeline": tr(500_000, 1_000_000), "transform": None},
                               ]})

    for name, description, mutate in (
        ("error-keyframe-property", "未知关键帧属性", bad_keyframe),
        ("error-audio-keyframe", "音频片段用了非音量关键帧", audio_keyframe),
        ("error-missing-media", "片段素材不在探测结果里", missing_media),
        ("error-track-type", "未知轨道类型", bad_track),
        ("error-animation", "未知视频动画", bad_animation),
        ("error-text-animation", "文字片段用了视频动画名", bad_text_animation),
        ("error-duplicate-intro", "两个入场动画（原库 ValueError → 500）", duplicate_intro),
        ("error-image-too-long", "图片片段超过 3 小时（原库 ValueError → 500）", long_image),
        ("error-subtitle-overlap", "字幕片段重叠（原库 SegmentOverlap → 500）", overlap),
        ("error-duplicate-track-name", "两条同名轨道（原库 NameError → 500；移植版照常导出）", same_names),
    ):
        scenario(name, description, basic, mutate=mutate)

    # ---- create_draft_folder ----
    use_fixed(True)
    root = WORK / "drafts-root"
    folder_cases = []
    subtitles = service_module.ProjectService._subtitle_segments(vbasic)
    for case, draft_id, owned in (
        ("owned", "0123456789abcdef0123456789abcdef", True),
        ("dashed-id", "a1b2c3d4-0000-4000-8000-0123456789ab", True),
        ("not-owned", "FEDCBA9876543210FEDCBA9876543210", False),
    ):
        result = draft_module.create_draft_folder(
            vbasic, root, draft_id, basic_media, subtitles, basic_names, owned=owned
        )
        target = result.directory
        files = sorted(p.name for p in target.iterdir())
        drafts_root = str(root.resolve())
        folder_cases.append({
            "case": case,
            "draft_id": draft_id,
            "owned": owned,
            "directory": str(target).replace(drafts_root, "${DRAFTS}"),
            "files": files,
            "content_sha256": result.content_hash,
            "content": placeholder_text((target / "draft_content.json").read_text("utf-8")),
            "meta": placeholder_text((target / "draft_meta_info.json").read_text("utf-8").replace(drafts_root, "${DRAFTS}")),
            "project_json": placeholder_text((target / "project.json").read_text("utf-8")),
            "owned_marker": (target / ".owned").read_text("ascii") if owned else None,
        })
    folder_errors = []
    for case, draft_id in (("exists", "0123456789ABCDEF0123456789ABCDEF"), ("invalid-id", "not-a-draft-id")):
        try:
            draft_module.create_draft_folder(vbasic, root, draft_id, basic_media, subtitles, basic_names)
        except Exception as exc:  # noqa: BLE001
            folder_errors.append({"case": case, "draft_id": draft_id, "type": type(exc).__name__,
                                  "code": getattr(exc, "code", None), "status": getattr(exc, "status", None)})

    catalog_queries = [
        {},
        {"query": "向上"},
        {"query": " 滑_动 ", "target": "text"},
        {"kind": "text_loop", "limit": 5, "offset": 2},
        {"target": "video", "vip_only": True, "limit": 3},
        {"query": "KIRA"},
        {"query": "1644267"},
        {"kind": "video_group", "offset": 120, "limit": 10},
    ]
    floats = [0.0, -0.0, 1.0, -2.5, 0.1, 0.30000000000000004, 1 / 3, 2 / 3, 1e16, 1e15, 9999999999999998.0,
              1.5e-05, 0.0001, 0.00001, 123456789.123, 5e-324, 1.7976931348623157e308, 100.0, 1e22,
              12345678901234567890.0, 0.0625, 0.82, 0.002, 1.1, 1e-7, 0.5019607843137255]
    index = {
        "generator": "node/scripts/ai-edit/generate-draft-fixtures.py",
        "source": "JianYingAPI draft.py + pyJianYingDraft 0.3.1",
        "media_root": VIRTUAL_MEDIA,
        "scenarios": SCENARIOS,
        "folder": {"cases": folder_cases, "errors": folder_errors},
        "editing_options": draft_module.editing_options(),
        "animation_catalog": [
            {"args": q, "result": draft_module.animation_catalog(**q)} for q in catalog_queries
        ],
        "hash_ids": [
            {"label": label, "scope": scope, "id": draft_module._id(label, scope)}
            for label, scope in (("abc", "segment"), ("片段", "text-material"), ("/ai-edit/media/x.mp4", "material"),
                                 ("0" * 32, "draft"))
        ],
        "float_repr": [[value, repr(value)] for value in floats],
    }
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for name, text in FILES.items():
        (OUT / name).write_text(text, encoding="utf-8")
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"wrote {len(SCENARIOS)} scenarios, {len(FILES)} drafts to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()

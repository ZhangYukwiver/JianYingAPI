"""Record JianYingAPI ProjectService behaviour as replay fixtures for node/aiEdit/projectStore.mjs.

The Node port of the project editor must behave exactly like JianYingAPI's ProjectService
(service.py + the request models in models.py + storage.py revision semantics). This script
drives the original Python service through operation sequences and writes every step's
input and output to node/fixtures/ai-edit/operations/*.json; projectStore.test.mjs
replays them against the Node store.

Usage (needs a JianYingAPI checkout, a virtualenv with its dependencies and ffprobe):

    <venv>/bin/python -I node/scripts/ai-edit/generate-operation-fixtures.py \
        --jianying-api <JianYingAPI checkout> [--ffprobe /opt/homebrew/bin/ffprobe]

How a step maps onto the Python service:
- Store methods are camelCase (createProject, addMediaSegment, ...). Arguments follow the
  Python order; request bodies keep their snake_case fields; the last argument is an options
  object ({"expectRevision": n}, plus {"groupId": ...} for addSrtSubtitles and
  {"limit", "offset"} for listProjects).
- Arguments are validated like JianYingAPI's HTTP routes (FastAPI 0.143) do: path/query
  scalars first (expect_revision >= 1, lax ints), then the body model. Like FastAPI, a None
  parameter counts as not given (default, or "missing" when required), a None body is
  "missing", and everything else goes through pydantic in Python mode with
  from_attributes=True (fastapi/_compat/v2.py ModelField.validate), which fixes the error
  order (declared fields first, extra fields last) and wording. Any pydantic error,
  including ones raised while the service builds a TimeRange internally, is recorded as
  {"code": "VALIDATION_ERROR", "status": 422, "details": {"errors": [{path, type, message}]}}
  with paths relative to the request body (no "body."/"query." prefix).
- listProjects only checks that limit/offset are integers here; the 1..1000 / 0..1000000
  ranges are left to the service so the result is PAGINATION_INVALID.
- Ids from secrets.token_hex(16) become a counter ("000...001", "000...002", ...) and
  datetime.now() becomes 2026-01-01T00:00:00Z + 1.5 s per call, so the Node side can inject
  the same newId()/now() and compare whole projects.
- Media paths are written as ${MEDIA}/<file>; ${MEDIA} is node/fixtures/ai-edit/media.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import secrets
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

REPO = Path(__file__).resolve().parents[2]
MEDIA = (REPO / "fixtures" / "ai-edit" / "media").resolve()
OUTPUT = REPO / "fixtures" / "ai-edit" / "operations"
BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
MAX_TIME_US = 86_400_000_000
MISSING_PROJECT = "f" * 32

parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("--jianying-api", required=True, type=Path)
parser.add_argument("--ffprobe", default="/opt/homebrew/bin/ffprobe")
ARGS = parser.parse_args()
sys.path.insert(0, str((ARGS.jianying_api / "src").resolve()))

from pydantic import ConfigDict, Field, ValidationError, create_model  # noqa: E402

import jianying_service.service as service_module  # noqa: E402
import jianying_service.storage as storage_module  # noqa: E402
from jianying_service.config import Settings  # noqa: E402
from jianying_service.errors import ServiceError  # noqa: E402
from jianying_service.media import MediaInfo  # noqa: E402
from jianying_service.models import (  # noqa: E402
    AnimationCreate,
    BackgroundFillCreate,
    CanvasUpdate,
    ChromaCreate,
    EffectCreate,
    FadeCreate,
    FilterCreate,
    KeyframeBatch,
    MarkerCreate,
    MaskCreate,
    MediaSegmentCreate,
    MixModeCreate,
    Project,
    ProjectCreate,
    ProjectUpdate,
    SegmentMove,
    SegmentSplit,
    SegmentUpdate,
    StickerSegmentCreate,
    TextSegmentCreate,
    TrackCreate,
    TrackEffectCreate,
    TrackFilterCreate,
    TrackOrder,
    TrackUpdate,
    TransitionCreate,
)
from jianying_service.service import ProjectService  # noqa: E402


# ---------------------------------------------------------------- route-level validation

def params_model(**fields: Any) -> Any:
    return create_model("Params", __config__=ConfigDict(extra="forbid"), **fields)


REVISION = (Annotated[int, Field(ge=1)] | None, None)
REVISION_PARAMS = params_model(expect_revision=REVISION)
LIST_PARAMS = params_model(limit=(int, 100), offset=(int, 0))
KEYFRAME_PARAMS = params_model(property_name=(str, ...), offset_us=(int, ...), expect_revision=REVISION)
FEATURE_PARAMS = params_model(feature=(str, ...), expect_revision=REVISION)
MARKER_PARAMS = params_model(time_us=(int, ...), expect_revision=REVISION)
SRT_PARAMS = params_model(group_id=(str | None, None), expect_revision=REVISION)

BODY_METHODS = {
    "updateProject": ("update_project", 1, ProjectUpdate),
    "setCanvas": ("set_canvas", 1, CanvasUpdate),
    "createTrack": ("add_track", 1, TrackCreate),
    "updateTrack": ("update_track", 2, TrackUpdate),
    "reorderTracks": ("reorder_tracks", 1, TrackOrder),
    "addMediaSegment": ("add_media_segment", 2, MediaSegmentCreate),
    "addTextSegment": ("add_text_segment", 2, TextSegmentCreate),
    "addStickerSegment": ("add_sticker_segment", 2, StickerSegmentCreate),
    "addTrackEffectSegment": ("add_track_effect_segment", 2, TrackEffectCreate),
    "addTrackFilterSegment": ("add_track_filter_segment", 2, TrackFilterCreate),
    "updateSegment": ("update_segment", 2, SegmentUpdate),
    "addKeyframes": ("add_keyframes", 2, KeyframeBatch),
    "addEffect": ("add_effect", 2, EffectCreate),
    "addFilter": ("add_filter", 2, FilterCreate),
    "addAnimation": ("add_animation", 2, AnimationCreate),
    "addTransition": ("add_transition", 2, TransitionCreate),
    "addFade": ("add_fade", 2, FadeCreate),
    "addMask": ("add_mask", 2, MaskCreate),
    "addChroma": ("add_chroma", 2, ChromaCreate),
    "addBackgroundFilling": ("add_background_filling", 2, BackgroundFillCreate),
    "setMixMode": ("set_mix_mode", 2, MixModeCreate),
    "addMarker": ("add_marker", 1, MarkerCreate),
}
ID_ONLY_METHODS = {
    "deleteTrack": ("delete_track", 2),
    "deleteSegment": ("delete_segment", 2),
    "removeSubtitleGroup": ("remove_subtitle_group", 2),
}


class RouteValidation(Exception):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("route validation failed")
        self.errors = errors


PARAM_MODELS = {REVISION_PARAMS, LIST_PARAMS, KEYFRAME_PARAMS, FEATURE_PARAMS, MARKER_PARAMS, SRT_PARAMS}


def check(*pairs: tuple[Any, Any]) -> list[Any]:
    """Validate the way FastAPI's request_body_to_args / _validate_value_with_model_field do."""
    errors: list[dict[str, Any]] = []
    values: list[Any] = []
    for model, data in pairs:
        data = json.loads(json.dumps(data))  # what the route would see after parsing JSON
        if model in PARAM_MODELS:
            data = {key: value for key, value in data.items() if value is not None}
        if data is None:
            errors.append({"loc": (), "type": "missing", "msg": "Field required"})
            values.append(None)
            continue
        try:
            values.append(model.model_validate(data, from_attributes=True))
        except ValidationError as error:
            errors.extend(error.errors())
            values.append(None)
    if errors:
        raise RouteValidation(errors)
    return values


def options(args: list[Any], index: int) -> dict[str, Any]:
    return args[index] if len(args) > index and args[index] is not None else {}


def revision(opts: dict[str, Any]) -> dict[str, Any]:
    return {"expect_revision": opts["expectRevision"]} if "expectRevision" in opts else {}


def dispatch(service: ProjectService, method: str, args: list[Any]) -> Any:
    if method in BODY_METHODS:
        name, id_count, model = BODY_METHODS[method]
        ids = args[:id_count]
        params, body = check((REVISION_PARAMS, revision(options(args, id_count + 1))), (model, args[id_count]))
        if model is SegmentUpdate:
            # update_segment iterates model_fields_set, whose order follows the per-process string
            # hash; pin it to the request's key order so new keys land in a stable order.
            ordered = dict.fromkeys(key for key in args[id_count] if key in body.model_fields_set)
            object.__setattr__(body, "__pydantic_fields_set__", ordered.keys())
        return getattr(service, name)(*ids, body, expect_revision=params.expect_revision)
    if method in ID_ONLY_METHODS:
        name, id_count = ID_ONLY_METHODS[method]
        (params,) = check((REVISION_PARAMS, revision(options(args, id_count))))
        return getattr(service, name)(*args[:id_count], expect_revision=params.expect_revision)
    if method == "createProject":
        (body,) = check((ProjectCreate, args[0]))
        return service.create_project(body)
    if method == "listProjects":
        opts = options(args, 0)
        (params,) = check((LIST_PARAMS, {key: opts[key] for key in ("limit", "offset") if key in opts}))
        return service.list_projects(limit=params.limit, offset=params.offset)
    if method == "getProject":
        return service.get_project(args[0])
    if method == "getSegment":
        return service.get_segment(args[0], args[1])
    if method == "listKeyframes":
        return service.list_keyframes(args[0], args[1])
    if method == "listMarkers":
        return service.list_markers(args[0])
    if method == "mediaInfos":
        return service._media_infos(Project.model_validate(args[0]))
    if method in {"moveSegment", "splitSegment"}:
        model, field = (SegmentMove, "start_us") if method == "moveSegment" else (SegmentSplit, "offset_us")
        body = {field: args[2]} if len(args) > 2 else {}
        params, request = check((REVISION_PARAMS, revision(options(args, 3))), (model, body))
        call = service.move_segment if method == "moveSegment" else service.split_segment
        return call(args[0], args[1], getattr(request, field), expect_revision=params.expect_revision)
    if method == "removeKeyframe":
        data = dict(zip(("property_name", "offset_us"), args[2:4]))
        (params,) = check((KEYFRAME_PARAMS, {**data, **revision(options(args, 4))}))
        return service.remove_keyframe(
            args[0], args[1], params.property_name, params.offset_us, expect_revision=params.expect_revision
        )
    if method == "clearFeature":
        data = {"feature": args[2]} if len(args) > 2 else {}
        (params,) = check((FEATURE_PARAMS, {**data, **revision(options(args, 3))}))
        return service.clear_feature(args[0], args[1], params.feature, expect_revision=params.expect_revision)
    if method == "removeMarker":
        data = {"time_us": args[1]} if len(args) > 1 else {}
        (params,) = check((MARKER_PARAMS, {**data, **revision(options(args, 2))}))
        return service.remove_marker(args[0], params.time_us, expect_revision=params.expect_revision)
    if method == "addSrtSubtitles":
        opts = options(args, 2)
        data = {"group_id": opts["groupId"]} if "groupId" in opts else {}
        (params,) = check((SRT_PARAMS, {**data, **revision(opts)}))
        return service.add_srt_subtitles(
            args[0], args[1], group_id=params.group_id, expect_revision=params.expect_revision
        )
    raise KeyError(method)


# ---------------------------------------------------------------- deterministic ids / clock

class CounterSecrets:
    def __init__(self, scenario: "Scenario") -> None:
        self.scenario = scenario

    def token_hex(self, nbytes: int = 32) -> str:
        if nbytes != 16:  # temp-file names in storage.atomic_write stay random
            return secrets.token_hex(nbytes)
        self.scenario.id_counter += 1
        return f"{self.scenario.id_counter:032x}"


def clock_for(scenario: "Scenario") -> type:
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            value = BASE_TIME + timedelta(milliseconds=1500 * scenario.ticks)
            scenario.ticks += 1
            return value

    return Clock


# ---------------------------------------------------------------- normalisation

def plain(value: Any) -> Any:
    if isinstance(value, Project):
        return value.model_dump(mode="json")
    if isinstance(value, MediaInfo):
        return plain(dataclasses.asdict(value))
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], Project):
        return {"project": plain(value[0]), "segments": plain(value[1])}
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def collapse_media(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace(str(MEDIA), "${MEDIA}")
    if isinstance(value, dict):
        return {key: collapse_media(item) for key, item in value.items()}
    if isinstance(value, list):
        return [collapse_media(item) for item in value]
    return value


def expand_media(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("${MEDIA}", str(MEDIA))
    if isinstance(value, dict):
        return {key: expand_media(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_media(item) for item in value]
    return value


def assert_portable(value: Any, where: str) -> None:
    """JSON.parse cannot tell 2.0 from 2 or keep -0.0, so fixtures must not depend on that."""
    if isinstance(value, float) and (value.is_integer() and where == "args" or str(value) == "-0.0"):
        raise AssertionError(f"non-portable float {value!r} in {where}")
    if isinstance(value, dict):
        for item in value.values():
            assert_portable(item, where)
    if isinstance(value, list):
        for item in value:
            assert_portable(item, where)


def validation_failure(errors: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "code": "VALIDATION_ERROR",
        "status": 422,
        "message": "Request validation failed",
        "details": {
            "errors": [
                {"path": ".".join(str(part) for part in item["loc"]), "type": item["type"], "message": item["msg"]}
                for item in errors
            ]
        },
    }


# ---------------------------------------------------------------- scenario recorder

class Scenario:
    def __init__(self, name: str) -> None:
        self.name = name
        self.steps: list[dict[str, Any]] = []
        self.id_counter = 0
        self.ticks = 0
        self.directory = Path(tempfile.mkdtemp(prefix="jy-operations-"))
        service_module.secrets = CounterSecrets(self)
        storage_module.secrets = CounterSecrets(self)
        service_module.datetime = clock_for(self)
        settings = Settings(data_root=self.directory, media_roots=(MEDIA,), ffprobe=ARGS.ffprobe)
        self.service = ProjectService(settings)

    def run(self, method: str, *args: Any) -> dict[str, Any]:
        if method != "mediaInfos":  # mediaInfos takes a stored project back, floats included
            assert_portable(list(args), "args")
        try:
            outcome = {"result": collapse_media(json.loads(json.dumps(plain(dispatch(self.service, method, expand_media(list(args)))))))}
        except ServiceError as error:
            outcome = {"error": {"code": error.code, "status": error.status, "message": error.message, "details": plain(error.details)}}
        except ValidationError as error:
            outcome = {"error": validation_failure(error.errors())}
        except RouteValidation as error:
            outcome = {"error": validation_failure(error.errors)}
        assert_portable(outcome, "outcome")
        self.steps.append({"method": method, "args": list(args), **outcome})
        return outcome

    def ok(self, method: str, *args: Any) -> Any:
        outcome = self.run(method, *args)
        if "error" in outcome:
            raise AssertionError(f"[{self.name}] {method}{args!r} failed: {outcome['error']}")
        return outcome["result"]

    def fail(self, method: str, code: str, *args: Any) -> dict[str, Any]:
        outcome = self.run(method, *args)
        if "error" not in outcome or outcome["error"]["code"] != code:
            raise AssertionError(f"[{self.name}] {method}{args!r} expected {code}, got {outcome}")
        return outcome["error"]

    def revision(self, project_id: str) -> int:
        """Current revision, read without recording a step (keeps ids and the clock untouched)."""
        return self.service.get_project(project_id).revision

    def finish(self) -> dict[str, Any]:
        shutil.rmtree(self.directory, ignore_errors=True)
        return {"name": self.name, "steps": self.steps}


def media(name: str) -> str:
    return f"${{MEDIA}}/{name}"


def track_id(project: dict[str, Any], index: int = -1) -> str:
    return project["tracks"][index]["id"]


def segment_id(project: dict[str, Any], track: str, index: int = -1) -> str:
    for item in project["tracks"]:
        if item["id"] == track:
            return item["segments"][index]["id"]
    raise KeyError(track)


def tl(start: int, duration: int) -> dict[str, int]:
    return {"start_us": start, "duration_us": duration}


def new_project(s: Scenario, name: str, *track_types: str, canvas: dict[str, int] | None = None) -> tuple[str, list[str], dict[str, Any]]:
    project = s.ok("createProject", {"name": name, **({"canvas": canvas} if canvas else {})})
    for kind in track_types:
        project = s.ok("createTrack", project["id"], {"track_type": kind})
    return project["id"], [track["id"] for track in project["tracks"]], project


# ---------------------------------------------------------------- scenarios: projects

def projects_scenarios() -> list[dict[str, Any]]:
    out = []

    s = Scenario("create, read and list projects")
    a = s.ok("createProject", {"name": "  Morning vlog  "})
    s.ok("getProject", a["id"])
    s.ok("createProject", {"name": "Portrait", "canvas": {"width": 1080, "height": 1920, "fps": 60}})
    s.ok("createProject", {"name": "Small", "canvas": {"width": 16, "height": 7680, "fps": 1}})
    s.ok("createProject", {"name": "x" * 200, "canvas": {"width": 7680, "height": 16, "fps": 120}})
    s.ok("createProject", {"name": "默认画布", "canvas": {}})
    s.ok("listProjects")
    s.ok("listProjects", {})
    s.ok("listProjects", {"limit": 2})
    s.ok("listProjects", {"limit": 2, "offset": 1})
    s.ok("listProjects", {"limit": "1", "offset": " 3 "})
    s.ok("listProjects", {"offset": 10})
    s.ok("listProjects", {"limit": 1000, "offset": 1_000_000})
    s.fail("listProjects", "PAGINATION_INVALID", {"limit": 0})
    s.fail("listProjects", "PAGINATION_INVALID", {"limit": 1001})
    s.fail("listProjects", "PAGINATION_INVALID", {"offset": -1})
    s.fail("listProjects", "PAGINATION_INVALID", {"offset": 1_000_001})
    s.fail("listProjects", "VALIDATION_ERROR", {"limit": "many"})
    s.fail("listProjects", "VALIDATION_ERROR", {"limit": 1.5, "offset": None})
    s.ok("listProjects", {"limit": None, "offset": None})
    s.ok("updateProject", a["id"], {"name": " Renamed "})
    s.ok("listProjects", {"limit": 3})
    s.fail("getProject", "PROJECT_NOT_FOUND", MISSING_PROJECT)
    s.fail("getProject", "PROJECT_NOT_FOUND", "../" + "a" * 29)
    s.fail("getProject", "PROJECT_NOT_FOUND", "A" * 32)
    s.fail("getProject", "PROJECT_NOT_FOUND", "")
    out.append(s.finish())

    s = Scenario("project request validation")
    s.fail("createProject", "VALIDATION_ERROR", {})
    s.fail("createProject", "VALIDATION_ERROR", {"name": ""})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "   "})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "x" * 201})
    s.fail("createProject", "VALIDATION_ERROR", {"name": 5})
    s.fail("createProject", "VALIDATION_ERROR", {"name": None})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "ok", "title": "extra"})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "   ", "zz": 1, "canvas": {"width": 1.5, "yy": 2}})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "ok", "canvas": {"width": 15, "height": 7681, "fps": 0}})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "ok", "canvas": {"width": 16, "height": 16, "fps": 121}})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "ok", "canvas": {"width": "1920", "height": True, "fps": None}})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "ok", "canvas": None})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "ok", "canvas": [1920, 1080]})
    s.fail("createProject", "VALIDATION_ERROR", None)
    s.fail("createProject", "VALIDATION_ERROR", "name")
    s.fail("createProject", "VALIDATION_ERROR", [{"name": "x"}])
    s.fail("createProject", "VALIDATION_ERROR", {"zz": [], "name": 5})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "x", "canvas": "abc"})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "a" + " " * 1000 + "a"})
    s.fail("createProject", "VALIDATION_ERROR", {"name": " \t\n\u3000" * 500})
    s.ok("createProject", {"name": " \t\n" * 300 + "x  y" + "\u2028\u3000\u0085" * 300})
    s.ok("createProject", {"name": "\x1c\x1fname\x1d"})
    s.ok("createProject", {"name": "　全角空格\u0085"})
    s.ok("createProject", {"name": "﻿BOM 不算空白​"})
    s.ok("createProject", {"name": "é" * 200})
    s.ok("createProject", {"name": "😀" * 200})
    s.fail("createProject", "VALIDATION_ERROR", {"name": "😀" * 201})
    out.append(s.finish())

    s = Scenario("update project and canvas with revisions")
    p = s.ok("createProject", {"name": "Rev"})
    pid = p["id"]
    s.ok("updateProject", pid, {})
    s.ok("updateProject", pid, {}, {"expectRevision": 1})
    s.fail("updateProject", "PROJECT_UPDATE_INVALID", pid, {"name": None})
    s.fail("updateProject", "VALIDATION_ERROR", pid, {"name": ""})
    s.fail("updateProject", "VALIDATION_ERROR", pid, {"title": "x"})
    s.ok("updateProject", pid, {"name": "Rev 2"}, {"expectRevision": 1})
    s.fail("updateProject", "PROJECT_CONFLICT", pid, {"name": "Rev 3"}, {"expectRevision": 1})
    s.fail("updateProject", "PROJECT_CONFLICT", pid, {}, {"expectRevision": 7})
    s.fail("updateProject", "VALIDATION_ERROR", pid, {"name": "x"}, {"expectRevision": 0})
    s.fail("updateProject", "VALIDATION_ERROR", pid, {"name": "x"}, {"expectRevision": -1})
    s.fail("updateProject", "VALIDATION_ERROR", pid, {"name": "x"}, {"expectRevision": "two"})
    s.fail("updateProject", "VALIDATION_ERROR", pid, {"name": "x"}, {"expectRevision": 1.5})
    s.fail("updateProject", "VALIDATION_ERROR", pid, {"name": "x"}, {"expectRevision": [2]})
    s.fail("updateProject", "VALIDATION_ERROR", pid, {"name": "", "x": 1}, {"expectRevision": 0})
    s.fail("updateProject", "PROJECT_NOT_FOUND", MISSING_PROJECT, {"name": "x"})
    s.ok("updateProject", pid, {"name": "Rev 3"}, {"expectRevision": "2"})
    s.ok("updateProject", pid, {"name": "Rev 4"}, {"expectRevision": None})
    s.fail("updateProject", "PROJECT_CONFLICT", pid, {"name": "Rev 5"}, {"expectRevision": True})
    s.ok("setCanvas", pid, {"width": 1080, "height": 1920, "fps": 25}, {"expectRevision": s.revision(pid)})
    s.ok("setCanvas", pid, {})
    s.ok("setCanvas", pid, {"fps": 24})
    s.fail("setCanvas", "VALIDATION_ERROR", pid, {"width": 15, "height": 7681, "fps": 0})
    s.fail("setCanvas", "VALIDATION_ERROR", pid, {"width": 1.5, "height": "1080", "fps": True, "depth": 3})
    s.fail("setCanvas", "VALIDATION_ERROR", pid, None)
    s.fail("setCanvas", "PROJECT_CONFLICT", pid, {"width": 1280}, {"expectRevision": 1})
    s.fail("setCanvas", "PROJECT_NOT_FOUND", MISSING_PROJECT, {})
    s.ok("getProject", pid)
    out.append(s.finish())
    return out


# ---------------------------------------------------------------- scenarios: tracks

def tracks_scenarios() -> list[dict[str, Any]]:
    out = []
    s = Scenario("create, update, reorder and delete tracks")
    pid, _, _ = new_project(s, "Tracks")
    project = None
    for kind in ("video", "audio", "text", "sticker", "effect", "filter"):
        project = s.ok("createTrack", pid, {"track_type": kind})
    project = s.ok("createTrack", pid, {"track_type": "video", "name": "  B-roll  ", "mute": True})
    project = s.ok("createTrack", pid, {"track_type": "audio", "name": None, "mute": False}, {"expectRevision": s.revision(pid)})
    ids = [track["id"] for track in project["tracks"]]
    video = ids[0]
    s.fail("createTrack", "VALIDATION_ERROR", pid, {"track_type": "subtitle"})
    s.fail("createTrack", "VALIDATION_ERROR", pid, {"track_type": None, "name": "   ", "mute": "true"})
    s.fail("createTrack", "VALIDATION_ERROR", pid, {"name": "x"})
    s.fail("createTrack", "VALIDATION_ERROR", pid, {"track_type": "video", "mute": 1, "solo": True})
    s.fail("createTrack", "VALIDATION_ERROR", pid, {"track_type": "Video"})
    s.fail("createTrack", "PROJECT_NOT_FOUND", MISSING_PROJECT, {"track_type": "video"})
    s.fail("createTrack", "PROJECT_CONFLICT", pid, {"track_type": "video"}, {"expectRevision": 1})
    s.ok("updateTrack", pid, video, {"name": "Main", "mute": True})
    s.ok("updateTrack", pid, video, {"mute": False})
    s.ok("updateTrack", pid, video, {})
    s.fail("updateTrack", "TRACK_UPDATE_INVALID", pid, video, {"name": None})
    s.fail("updateTrack", "TRACK_UPDATE_INVALID", pid, video, {"mute": None})
    s.fail("updateTrack", "TRACK_NOT_FOUND", pid, "missing", {"name": "x"})
    s.fail("updateTrack", "TRACK_NOT_FOUND", pid, "missing", {"name": None})
    s.fail("updateTrack", "VALIDATION_ERROR", pid, video, {"mute": "no", "track_type": "audio"})
    s.fail("updateTrack", "PROJECT_CONFLICT", pid, video, {"name": "x"}, {"expectRevision": 2})
    s.ok("reorderTracks", pid, {"track_ids": list(reversed(ids))})
    s.fail("reorderTracks", "TRACK_ORDER_INVALID", pid, {"track_ids": ids[:-1]})
    s.fail("reorderTracks", "TRACK_ORDER_INVALID", pid, {"track_ids": ids[:-1] + [ids[0]]})
    s.fail("reorderTracks", "TRACK_ORDER_INVALID", pid, {"track_ids": ids[:-1] + ["unknown"]})
    s.fail("reorderTracks", "TRACK_ORDER_INVALID", pid, {"track_ids": ids + ["unknown"]})
    s.fail("reorderTracks", "VALIDATION_ERROR", pid, {"track_ids": []})
    s.fail("reorderTracks", "VALIDATION_ERROR", pid, {"track_ids": ["a"] * 101})
    s.fail("reorderTracks", "VALIDATION_ERROR", pid, {"track_ids": ["", "x" * 65, 3, None]})
    s.fail("reorderTracks", "VALIDATION_ERROR", pid, {"track_ids": "abc"})
    s.fail("reorderTracks", "VALIDATION_ERROR", pid, {})
    s.fail("reorderTracks", "VALIDATION_ERROR", pid, {"track_ids": ids, "extra": True})
    s.ok("reorderTracks", pid, {"track_ids": ids}, {"expectRevision": s.revision(pid)})
    s.ok("deleteTrack", pid, ids[2])
    s.fail("deleteTrack", "TRACK_NOT_FOUND", pid, ids[2])
    s.fail("deleteTrack", "PROJECT_CONFLICT", pid, ids[3], {"expectRevision": 2})
    s.ok("deleteTrack", pid, ids[3], {"expectRevision": s.revision(pid)})
    s.fail("deleteTrack", "VALIDATION_ERROR", pid, ids[4], {"expectRevision": 0})
    s.fail("deleteTrack", "PROJECT_NOT_FOUND", MISSING_PROJECT, ids[4])
    s.ok("getProject", pid)
    out.append(s.finish())
    return out


# ---------------------------------------------------------------- scenarios: media clips

ALL_VIDEO = ["clip-720p.mp4", "clip-portrait.mov", "clip.avi", "clip.m4v", "clip.mkv", "clip.webm", "fragmented.mp4", "hevc.mp4"]
ALL_IMAGE = ["still.jpg", "still.png", "still.webp"]
ALL_AUDIO = ["tone.aac", "tone.flac", "tone.m4a", "tone.mp3", "tone.ogg", "tone.wav"]


def media_scenarios() -> list[dict[str, Any]]:
    out = []

    s = Scenario("add every supported media file")
    pid, (video, audio), _ = new_project(s, "All media", "video", "audio")
    start = 0
    for name in ALL_VIDEO + ALL_IMAGE:
        s.ok("addMediaSegment", pid, video, {"path": media(name), "timeline": tl(start, 1_000_000)})
        start += 1_000_000
    start = 0
    for name in ALL_AUDIO:
        project = s.ok("addMediaSegment", pid, audio, {"path": media(name), "timeline": tl(start, 1_000_000)})
        start += 1_000_000
    s.ok("mediaInfos", project)
    out.append(s.finish())

    s = Scenario("media clip timing, speed and track rules")
    pid, (video, video2, audio, text), _ = new_project(s, "Timing", "video", "video", "audio", "text")
    s.ok("addMediaSegment", pid, video, {"path": media("clip-720p.mp4"), "timeline": tl(0, 2_000_000)})
    s.ok("addMediaSegment", pid, video, {"path": media("clip.mkv"), "timeline": tl(2_000_000, 1_000_000), "source": tl(500_000, 2_000_000)})
    s.ok("addMediaSegment", pid, video, {"path": media("clip.m4v"), "timeline": tl(3_000_000, 1), "speed": 2.5})
    s.ok("addMediaSegment", pid, video, {"path": media("hevc.mp4"), "timeline": tl(4_000_000, 500_000), "source": tl(0, 1_000_000), "speed": 2})
    s.fail("addMediaSegment", "MEDIA_DURATION_MISMATCH", pid, video, {"path": media("hevc.mp4"), "timeline": tl(5_000_000, 600_000), "source": tl(0, 1_000_000), "speed": 2})
    s.fail("addMediaSegment", "SOURCE_RANGE_INVALID", pid, video, {"path": media("clip.avi"), "timeline": tl(5_000_000, 1_000_000), "source": tl(1_500_000, 1_000_000)})
    s.fail("addMediaSegment", "SOURCE_RANGE_INVALID", pid, video, {"path": media("clip.avi"), "timeline": tl(5_000_000, 1_500_000), "speed": 2})
    s.fail("addMediaSegment", "SPEED_INVALID", pid, video, {"path": media("still.png"), "timeline": tl(5_000_000, 1_000_000), "speed": 2})
    s.fail("addMediaSegment", "SPEED_INVALID", pid, video, {"path": media("still.jpg"), "timeline": tl(5_000_000, 1_000_000), "source": tl(0, 2_000_000)})
    s.ok("addMediaSegment", pid, video, {"path": media("still.jpg"), "timeline": tl(6_000_000, 1_000_000), "source": tl(1000, 1_000_000)})
    s.ok("addMediaSegment", pid, video, {"path": media("still.webp"), "timeline": tl(7_000_000, 1_000_000), "speed": 1})
    s.ok("addMediaSegment", pid, video, {"path": media("clip.webm"), "timeline": tl(8_000_000, 3_000_000), "source": tl(0, 1_000_000)})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {"path": media("clip.webm"), "timeline": tl(12_000_000, 1), "speed": 0.4})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video2, {"path": media("still.png"), "timeline": tl(0, MAX_TIME_US), "speed": 2})
    s.ok("addMediaSegment", pid, video2, {"path": media("still.png"), "timeline": tl(0, MAX_TIME_US)})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video2, {"path": media("still.png"), "timeline": tl(1, MAX_TIME_US)})
    s.fail("addMediaSegment", "TIMELINE_OVERLAP", pid, video, {"path": media("clip.avi"), "timeline": tl(1_999_999, 10)})
    s.fail("addMediaSegment", "TIMELINE_OVERLAP", pid, video2, {"path": media("clip.avi"), "timeline": tl(MAX_TIME_US - 10, 10)})
    s.ok("addMediaSegment", pid, video, {"path": media("clip.avi"), "timeline": tl(11_000_000, 10)})
    s.fail("addMediaSegment", "MEDIA_TRACK_MISMATCH", pid, video, {"path": media("tone.mp3"), "timeline": tl(20_000_000, 1_000_000)})
    s.fail("addMediaSegment", "MEDIA_TRACK_MISMATCH", pid, audio, {"path": media("clip.avi"), "timeline": tl(0, 1_000_000)})
    s.fail("addMediaSegment", "MEDIA_TRACK_MISMATCH", pid, audio, {"path": media("still.png"), "timeline": tl(0, 1_000_000)})
    s.fail("addMediaSegment", "MEDIA_TRACK_MISMATCH", pid, text, {"path": media("still.png"), "timeline": tl(0, 1_000_000)})
    s.fail("addMediaSegment", "MEDIA_TRACK_MISMATCH", pid, text, {"path": media("tone.mp3"), "timeline": tl(0, 1_000_000)})
    s.fail("addMediaSegment", "SEGMENT_FIELD_MISMATCH", pid, audio, {"path": media("tone.wav"), "timeline": tl(0, 1_000_000), "transform": {}})
    s.fail("addMediaSegment", "SEGMENT_FIELD_MISMATCH", pid, audio, {"path": media("tone.wav"), "timeline": tl(0, 1_000_000), "fit": "cover"})
    s.fail("addMediaSegment", "SEGMENT_FIELD_MISMATCH", pid, audio, {"path": media("tone.wav"), "timeline": tl(0, 1_000_000), "crop": {}})
    s.ok("addMediaSegment", pid, audio, {
        "path": media("tone.wav"), "timeline": tl(0, 2_000_000), "fit": "contain", "transform": None, "crop": None,
        "volume": 0.5, "change_pitch": True, "material_name": "  Voice over  ", "speed": 1.25,
    })
    s.ok("addMediaSegment", pid, audio, {"path": media("tone.mp3"), "timeline": tl(2_000_000, 1_000_000), "volume": 2, "speed": 3.5})
    s.fail("addMediaSegment", "SOURCE_RANGE_INVALID", pid, audio, {"path": media("tone.mp3"), "timeline": tl(3_000_000, 1_000_000), "speed": 4})
    s.ok("addMediaSegment", pid, video, {
        "path": media("clip-portrait.mov"), "timeline": tl(12_000_000, 1_500_000), "fit": "cover", "volume": 0,
        "transform": {"scale_x": 1.5, "scale_y": "0.75", "transform_x": -0.25, "transform_y": 0.5, "rotation": 15.5,
                      "alpha": 0.8, "flip_horizontal": "yes", "flip_vertical": 0},
        "crop": {"upper_left_x": 0.1, "upper_left_y": 0.05, "lower_right_x": 0.9, "lower_right_y": 0.95},
        "material_name": "Portrait",
    })
    s.ok("addMediaSegment", pid, video, {"path": media("fragmented.mp4"), "timeline": tl(14_000_000, 1_000_000), "fit": "stretch", "transform": {}, "crop": {}})
    s.fail("addMediaSegment", "PATH_NOT_ABSOLUTE", pid, video, {"path": "clip.avi", "timeline": tl(20_000_000, 1_000_000)})
    s.fail("addMediaSegment", "MEDIA_NOT_FOUND", pid, video, {"path": media("missing.mp4"), "timeline": tl(20_000_000, 1_000_000)})
    s.fail("addMediaSegment", "PATH_OUTSIDE_WHITELIST", pid, video, {"path": media("../media-info.json"), "timeline": tl(20_000_000, 1_000_000)})
    s.fail("addMediaSegment", "MEDIA_TYPE_UNSUPPORTED", pid, video, {"path": "${MEDIA}", "timeline": tl(20_000_000, 1_000_000)})
    s.fail("addMediaSegment", "PATH_INVALID", pid, video, {"path": media("clip.avi\u0000"), "timeline": tl(20_000_000, 1_000_000)})
    s.fail("addMediaSegment", "TRACK_NOT_FOUND", pid, "missing", {"path": "relative.mp4", "timeline": tl(0, 1_000_000)})
    s.fail("addMediaSegment", "PROJECT_CONFLICT", pid, video, {"path": "relative.mp4", "timeline": tl(0, 1_000_000)}, {"expectRevision": 1})
    s.fail("addMediaSegment", "PROJECT_NOT_FOUND", MISSING_PROJECT, video, {"path": media("clip.avi"), "timeline": tl(0, 1_000_000)})
    project = s.ok("getProject", pid)
    s.ok("mediaInfos", project)
    out.append(s.finish())

    s = Scenario("media clip request validation")
    pid, (video,), _ = new_project(s, "Validation", "video")
    base = {"path": media("clip.avi"), "timeline": tl(0, 1_000_000)}
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {"path": "", "timeline": None, "source": [], "speed": 0.05, "fit": "fill"})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "volume": -0.1, "change_pitch": "yes", "material_name": "   ", "kind": "video"})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "volume": 2.1, "speed": 100.5})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "volume": None, "speed": "fast"})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "timeline": {"start_us": -1, "duration_us": 0}})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "timeline": {"start_us": 1.5, "duration_us": "1", "end_us": 3}})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "timeline": {"start_us": MAX_TIME_US + 1, "duration_us": MAX_TIME_US + 1}})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "timeline": {"start_us": 0}})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "source": {"start_us": MAX_TIME_US, "duration_us": 1}})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "transform": {"scale_x": 0, "scale_y": 100.5, "transform_x": -1.5, "transform_y": 1.0001, "rotation": 400, "alpha": -0.1, "zoom": 2}})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "transform": {"scale_x": "abc", "scale_y": "inf", "rotation": None, "flip_horizontal": "maybe", "flip_vertical": 2, "alpha": 0.5}})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "transform": {"flip_horizontal": 0.5, "flip_vertical": " true ", "scale_x": [1]}})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "transform": "big"})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "crop": {"upper_left_x": -0.1, "upper_left_y": 1.1, "upper_right_x": 2, "upper_right_y": -1, "lower_left_x": 1.5, "lower_left_y": "x", "lower_right_x": None, "lower_right_y": 1.01, "angle": 0}})
    s.fail("addMediaSegment", "VALIDATION_ERROR", pid, video, {**base, "material_name": "m" * 201, "path": "p" * 32768})
    s.ok("addMediaSegment", pid, video, {**base, "transform": {"scale_x": 100, "scale_y": "1e-3", "transform_x": -1, "transform_y": 1, "rotation": -360, "alpha": 0, "flip_horizontal": "ON", "flip_vertical": "f"}, "crop": {"upper_left_x": 0, "lower_right_y": 1}, "volume": 2, "speed": 1, "material_name": " " + "m" * 200 + " "})
    out.append(s.finish())

    s = Scenario("media infos for drafts")
    pid, (video, video2, audio), _ = new_project(s, "Infos", "video", "video", "audio")
    s.ok("addMediaSegment", pid, video, {"path": media("clip-720p.mp4"), "timeline": tl(0, 1_000_000)})
    s.ok("addMediaSegment", pid, video, {"path": media("still.png"), "timeline": tl(1_000_000, 1_000_000)})
    s.ok("addMediaSegment", pid, video2, {"path": media("clip-720p.mp4"), "timeline": tl(0, 2_000_000), "source": tl(1_000_000, 2_000_000)})
    s.ok("addMediaSegment", pid, audio, {"path": media("tone.wav"), "timeline": tl(0, 3_000_000)})
    s.ok("addSrtSubtitles", pid, "1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    project = s.ok("createTrack", pid, {"track_type": "text"})
    project = s.ok("addTextSegment", pid, track_id(project), {"text": "Title", "timeline": tl(0, 1_000_000)})
    s.ok("mediaInfos", project)
    empty = s.ok("createProject", {"name": "Empty"})
    s.ok("mediaInfos", empty)
    out.append(s.finish())
    return out


# ---------------------------------------------------------------- scenarios: text / sticker / effect / filter clips

def overlay_scenarios() -> list[dict[str, Any]]:
    out = []
    s = Scenario("text, sticker, track effect and track filter clips")
    pid, (text, sticker, effect, filt, video), _ = new_project(s, "Overlays", "text", "sticker", "effect", "filter", "video")
    s.ok("addTextSegment", pid, text, {"text": "Hello", "timeline": {"duration_us": 1_000_000}})
    s.ok("addTextSegment", pid, text, {
        "text": "完整样式\n第二行", "timeline": tl(1_000_000, 2_000_000), "fit": "cover",
        "transform": {"scale_x": 1.2, "scale_y": 1.2, "transform_y": -0.6},
        "font_size": 12.5, "color": "#ff8800", "alignment": "left", "font_family": " 思源黑体 ",
        "bold": True, "italic": True, "underline": True, "opacity": 0.8, "vertical": True,
        "letter_spacing": -20, "line_spacing": 30, "auto_wrapping": False, "max_line_width": 1,
        "border": {"color": "#112233", "alpha": 0.5, "width": 60},
        "background": {"color": "#000000AA", "style": 2, "alpha": 0.7, "round_radius": 0.3, "height": 0.2, "width": 0.4, "horizontal_offset": 0, "vertical_offset": 1},
        "shadow": {"color": "#000000", "alpha": 0.6, "diffuse": 20, "distance": 8, "angle": 135},
        "bubble": {"effect_id": " 123 ", "resource_id": "456"}, "text_effect_id": "789",
    })
    s.ok("addTextSegment", pid, text, {"text": "默认嵌套", "timeline": tl(3_000_000, 1_000_000), "border": {}, "shadow": {}, "background": {"color": "#FFFFFF00", "style": True}})
    s.fail("addTextSegment", "TIMELINE_OVERLAP", pid, text, {"text": "x", "timeline": tl(500_000, 1_000_000)})
    s.fail("addTextSegment", "TRACK_TYPE_MISMATCH", pid, sticker, {"text": "x", "timeline": tl(0, 1)})
    s.fail("addTextSegment", "TRACK_TYPE_MISMATCH", pid, video, {"text": "x", "timeline": tl(0, 1)})
    s.fail("addTextSegment", "TRACK_NOT_FOUND", pid, "nope", {"text": "x", "timeline": tl(0, 1)})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "", "timeline": tl(0, 1), "color": "#fff", "alignment": "justify", "font_size": 0.5})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a" * 10001, "timeline": tl(0, 1), "color": "#GGGGGG", "font_size": 100.5, "font_family": ""})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a", "timeline": tl(0, 1), "bold": "yes", "italic": 1, "underline": None, "vertical": "false", "auto_wrapping": 0})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a", "timeline": tl(0, 1), "opacity": 1.5, "letter_spacing": 1.5, "line_spacing": 1001, "max_line_width": 0})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a", "timeline": tl(0, 1), "letter_spacing": -1001, "line_spacing": "2", "max_line_width": 1.5, "color": "#FFFFFF\n"})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a", "timeline": tl(0, 1), "border": {"color": "red", "alpha": 2, "width": 101, "style": 1}})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a", "timeline": tl(0, 1), "background": {"style": 3, "round_radius": 1.5, "height": 0, "width": 1.5, "horizontal_offset": -0.1, "vertical_offset": 2}})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a", "timeline": tl(0, 1), "background": {"color": "#000000", "style": "1"}})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a", "timeline": tl(0, 1), "background": {"color": "#00000000", "style": None}})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a", "timeline": tl(0, 1), "shadow": {"color": "#0000", "alpha": -1, "diffuse": 101, "distance": -1, "angle": 361}})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a", "timeline": tl(0, 1), "bubble": {"effect_id": "  "}, "text_effect_id": ""})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"text": "a", "timeline": tl(0, 1), "bubble": "b", "border": [], "shadow": 1, "kind": "text"})
    s.fail("addTextSegment", "VALIDATION_ERROR", pid, text, {"timeline": tl(0, 1)})
    s.ok("addTextSegment", pid, text, {"text": "😀" * 10000, "timeline": tl(4_000_000, 1), "letter_spacing": 1000, "line_spacing": -1000, "opacity": 0, "font_size": 1, "max_line_width": 0.0001})
    s.ok("addStickerSegment", pid, sticker, {"resource_id": "  7123  ", "timeline": tl(0, 1_000_000), "transform": {"scale_x": 0.5, "rotation": -30}})
    s.ok("addStickerSegment", pid, sticker, {"resource_id": "7124", "timeline": tl(1_000_000, 500_000)})
    s.fail("addStickerSegment", "TRACK_TYPE_MISMATCH", pid, text, {"resource_id": "1", "timeline": tl(9_000_000, 1)})
    s.fail("addStickerSegment", "TIMELINE_OVERLAP", pid, sticker, {"resource_id": "1", "timeline": tl(1_499_999, 1)})
    s.fail("addStickerSegment", "VALIDATION_ERROR", pid, sticker, {"resource_id": "  ", "timeline": tl(0, 1), "kind": "sticker"})
    s.fail("addStickerSegment", "VALIDATION_ERROR", pid, sticker, {"resource_id": "r" * 201})
    s.fail("addStickerSegment", "VALIDATION_ERROR", pid, sticker, {"resource_id": 7123, "timeline": tl(0, 1), "transform": {"alpha": 2}})
    s.ok("addTrackEffectSegment", pid, effect, {"effect_type": "_1998", "params": [10, 55.5], "timeline": tl(0, 1_000_000)})
    s.ok("addTrackEffectSegment", pid, effect, {"effect_type": "BOOM", "timeline": tl(1_000_000, 1_000_000)})
    s.ok("addTrackEffectSegment", pid, effect, {"effect_type": " DV界面 ", "params": [], "timeline": tl(2_000_000, 1_000_000)})
    s.fail("addTrackEffectSegment", "EFFECT_PARAMS_INVALID", pid, effect, {"effect_type": "_70s", "params": [1, 2], "timeline": tl(5_000_000, 1)})
    s.ok("addTrackEffectSegment", pid, effect, {"effect_type": "_70s", "params": [0], "timeline": tl(3_000_000, 1)})
    s.fail("addTrackEffectSegment", "EFFECT_INVALID", pid, effect, {"effect_type": "nope", "timeline": tl(5_000_000, 1)})
    s.fail("addTrackEffectSegment", "EFFECT_INVALID", pid, effect, {"effect_type": "_8bit", "timeline": tl(5_000_000, 1)})
    s.fail("addTrackEffectSegment", "TRACK_TYPE_MISMATCH", pid, filt, {"effect_type": "nope", "timeline": tl(5_000_000, 1)})
    s.fail("addTrackEffectSegment", "TIMELINE_OVERLAP", pid, effect, {"effect_type": "_1998", "timeline": tl(500_000, 1)})
    s.fail("addTrackEffectSegment", "VALIDATION_ERROR", pid, effect, {"effect_type": "", "params": [101, -1, "3"], "timeline": tl(0, 1)})
    s.fail("addTrackEffectSegment", "VALIDATION_ERROR", pid, effect, {"effect_type": "_1998", "params": [1] * 65 + ["x"], "timeline": tl(0, 1)})
    s.fail("addTrackEffectSegment", "VALIDATION_ERROR", pid, effect, {"effect_type": "_1998", "params": "10"})
    s.ok("addTrackFilterSegment", pid, filt, {"filter_type": "_1980", "timeline": tl(0, 1_000_000)})
    s.ok("addTrackFilterSegment", pid, filt, {"filter_type": "ABG", "intensity": 35.5, "timeline": tl(1_000_000, 1_000_000)})
    s.fail("addTrackFilterSegment", "DRAFT_FEATURE_INVALID", pid, filt, {"filter_type": "nope", "timeline": tl(5_000_000, 1)})
    s.fail("addTrackFilterSegment", "TRACK_TYPE_MISMATCH", pid, effect, {"filter_type": "nope", "timeline": tl(5_000_000, 1)})
    s.fail("addTrackFilterSegment", "TIMELINE_OVERLAP", pid, filt, {"filter_type": "ABG", "timeline": tl(1_999_999, 5)})
    s.fail("addTrackFilterSegment", "VALIDATION_ERROR", pid, filt, {"filter_type": "ABG", "intensity": 101, "timeline": tl(0, 1)})
    s.fail("addTrackFilterSegment", "VALIDATION_ERROR", pid, filt, {"filter_type": "ABG", "intensity": -1})
    s.fail("addStickerSegment", "TRACK_TYPE_MISMATCH", pid, video, {"resource_id": "1", "timeline": tl(0, 1)})
    s.fail("addTrackEffectSegment", "TRACK_TYPE_MISMATCH", pid, video, {"effect_type": "_1998", "timeline": tl(0, 1)})
    s.fail("addTrackFilterSegment", "TRACK_TYPE_MISMATCH", pid, video, {"filter_type": "_1980", "timeline": tl(0, 1)})
    s.fail("addTrackFilterSegment", "PROJECT_CONFLICT", pid, filt, {"filter_type": "_1980", "timeline": tl(9_000_000, 1)}, {"expectRevision": 3})
    s.ok("getProject", pid)
    out.append(s.finish())
    return out


# ---------------------------------------------------------------- scenarios: updating clips

def update_scenarios() -> list[dict[str, Any]]:
    out = []
    s = Scenario("update media clips")
    pid, (video, audio), _ = new_project(s, "Update media", "video", "audio")
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip-720p.mp4"), "timeline": tl(0, 2_000_000)})
    v1 = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip.mkv"), "timeline": tl(3_000_000, 1_000_000), "source": tl(500_000, 1_000_000)})
    v2 = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("still.png"), "timeline": tl(5_000_000, 1_000_000)})
    image = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip.avi"), "timeline": tl(9_000_000, 250), "source": tl(0, 250)})
    tiny = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip.avi"), "timeline": tl(9_500_000, 50), "source": tl(0, 50)})
    tinier = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, audio, {"path": media("tone.wav"), "timeline": tl(0, 2_000_000)})
    a1 = segment_id(project, audio)
    s.ok("updateSegment", pid, v1, {})
    s.ok("updateSegment", pid, v1, {"timeline": tl(0, 2_500_000)})
    s.fail("updateSegment", "SOURCE_RANGE_INVALID", pid, v1, {"timeline": tl(0, 3_100_000)})
    s.ok("updateSegment", pid, v1, {"speed": 2})
    s.ok("updateSegment", pid, v1, {"source": tl(1_000_000, 1_000_000)})
    s.fail("updateSegment", "MEDIA_DURATION_MISMATCH", pid, v1, {"source": tl(0, 1_000_000), "timeline": tl(0, 400_000)})
    s.ok("updateSegment", pid, v1, {"source": tl(0, 1_000_000), "timeline": tl(100_000, 500_000)})
    s.fail("updateSegment", "TIMELINE_OVERLAP", pid, v1, {"timeline": tl(2_900_000, 500_000)})
    s.ok("updateSegment", pid, v1, {"timeline": tl(100_000, 500_000)})
    s.ok("updateSegment", pid, v1, {"speed": 2, "timeline": tl(0, 1_000_000)})
    s.ok("updateSegment", pid, v1, {"volume": 1.5, "change_pitch": True, "fit": "stretch", "material_name": "  Opening  "})
    s.ok("updateSegment", pid, v1, {"transform": {"scale_x": 1.2, "alpha": 0.9}})
    s.ok("updateSegment", pid, v1, {"transform": None})
    s.ok("updateSegment", pid, v1, {"crop": {"upper_left_x": 0.1, "lower_right_x": 0.9}})
    s.fail("updateSegment", "SEGMENT_UPDATE_INVALID", pid, v1, {"crop": None})
    s.fail("updateSegment", "SEGMENT_UPDATE_INVALID", pid, v1, {"timeline": None})
    s.fail("updateSegment", "SEGMENT_UPDATE_INVALID", pid, v1, {"volume": None, "bold": True})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, v1, {"bold": True})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, v1, {"text": "x"})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, v1, {"bubble": None})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, v1, {"text_effect_id": None})
    s.fail("updateSegment", "SOURCE_RANGE_INVALID", pid, v1, {"path": media("clip.webm")})
    s.ok("updateSegment", pid, v1, {"path": media("clip.webm"), "speed": 1})
    s.ok("updateSegment", pid, v1, {"path": media("still.jpg")})
    s.fail("updateSegment", "SPEED_INVALID", pid, v1, {"speed": 2})
    s.fail("updateSegment", "MEDIA_TRACK_MISMATCH", pid, v1, {"path": media("tone.mp3")})
    s.fail("updateSegment", "PATH_NOT_ABSOLUTE", pid, v1, {"path": "relative.mp4"})
    s.fail("updateSegment", "PATH_OUTSIDE_WHITELIST", pid, v1, {"path": media("../media-info.json")})
    s.ok("updateSegment", pid, v1, {"path": media("clip-720p.mp4"), "source": tl(0, 3_000_000)})
    s.ok("updateSegment", pid, tiny, {"speed": 100})
    s.fail("updateSegment", "VALIDATION_ERROR", pid, tinier, {"speed": 100})
    project = s.ok("addKeyframes", pid, v2, {"keyframes": [{"property": "alpha", "offset_us": 900_000, "value": 0.5}]})
    s.fail("updateSegment", "KEYFRAME_OFFSET_INVALID", pid, v2, {"timeline": tl(3_000_000, 800_000)})
    s.ok("updateSegment", pid, v2, {"timeline": tl(3_100_000, 900_000)})
    s.ok("addFade", pid, image, {"in_duration_us": 500_000, "out_duration_us": 500_000})
    s.fail("updateSegment", "FADE_INVALID", pid, image, {"timeline": tl(5_000_000, 900_000)})
    s.ok("updateSegment", pid, image, {"speed": 1, "fit": "cover", "transform": {"rotation": 90}})
    s.ok("updateSegment", pid, image, {"source": tl(0, 2_000_000)})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, a1, {"transform": {}})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, a1, {"transform": None})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, a1, {"fit": "contain"})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, a1, {"crop": {}})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, a1, {"text": "x"})
    s.ok("updateSegment", pid, a1, {"bold": True, "border": {}})
    s.ok("updateSegment", pid, a1, {"volume": 0.25, "speed": 0.5})
    s.fail("updateSegment", "SOURCE_RANGE_INVALID", pid, a1, {"timeline": tl(0, 9_000_000)})
    s.fail("updateSegment", "SEGMENT_NOT_FOUND", pid, "missing", {"volume": 1})
    s.fail("updateSegment", "PROJECT_NOT_FOUND", MISSING_PROJECT, v1, {"volume": 1})
    s.fail("updateSegment", "PROJECT_CONFLICT", pid, v1, {"volume": 1}, {"expectRevision": 3})
    s.fail("updateSegment", "VALIDATION_ERROR", pid, v1, {"volume": 3, "speed": 0.01, "path": "", "zzz": 1})
    s.fail("updateSegment", "VALIDATION_ERROR", pid, v1, {"timeline": {"duration_us": 0}, "source": "x", "fit": None, "letter_spacing": 2.5})
    s.ok("getProject", pid)
    out.append(s.finish())

    s = Scenario("update text, sticker, effect, filter and subtitle clips")
    pid, (text, sticker, effect, filt), _ = new_project(s, "Update overlays", "text", "sticker", "effect", "filter")
    project = s.ok("addTextSegment", pid, text, {"text": "One", "timeline": tl(0, 1_000_000)})
    t1 = segment_id(project, text)
    project = s.ok("addTextSegment", pid, text, {"text": "Two", "timeline": tl(2_000_000, 1_000_000)})
    project = s.ok("addStickerSegment", pid, sticker, {"resource_id": "1", "timeline": tl(0, 1_000_000)})
    s1 = segment_id(project, sticker)
    project = s.ok("addTrackEffectSegment", pid, effect, {"effect_type": "_1998", "timeline": tl(0, 1_000_000)})
    e1 = segment_id(project, effect)
    project = s.ok("addTrackFilterSegment", pid, filt, {"filter_type": "_1980", "timeline": tl(0, 1_000_000)})
    f1 = segment_id(project, filt)
    project = s.ok("addSrtSubtitles", pid, "1\n00:00:00,000 --> 00:00:01,000\nA\n\n2\n00:00:02,000 --> 00:00:03,000\nB\n")
    group = track_id(project)
    cue = segment_id(project, group, 1)
    s.ok("updateSegment", pid, t1, {"text": "New", "font_size": 20, "color": "#a1b2c3", "alignment": "right"})
    s.ok("updateSegment", pid, t1, {"bold": True, "italic": True, "underline": True, "opacity": 0.5, "vertical": True, "letter_spacing": 5, "line_spacing": -5, "auto_wrapping": False, "max_line_width": 0.5, "font_family": "Arial"})
    s.ok("updateSegment", pid, t1, {"border": {"color": "#FF0000"}, "background": {"color": "#00000080"}, "shadow": {"angle": 90}, "bubble": {"effect_id": "1", "resource_id": "2"}, "text_effect_id": "3"})
    s.ok("updateSegment", pid, t1, {"bubble": None, "text_effect_id": None, "transform": None})
    s.ok("updateSegment", pid, t1, {"transform": {"transform_x": -0.5, "transform_y": 0.5}})
    s.fail("updateSegment", "TIMELINE_OVERLAP", pid, t1, {"timeline": tl(1_500_000, 1_000_000)})
    s.ok("updateSegment", pid, t1, {"timeline": tl(0, 2_000_000)})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, t1, {"source": tl(0, 1)})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, t1, {"volume": 1})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, t1, {"path": media("clip.avi")})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, t1, {"crop": {}})
    s.fail("updateSegment", "SEGMENT_UPDATE_INVALID", pid, t1, {"border": None})
    s.fail("updateSegment", "SEGMENT_UPDATE_INVALID", pid, t1, {"text": None})
    s.ok("updateSegment", pid, s1, {"transform": {"rotation": 45}, "timeline": tl(500_000, 1_000_000)})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, s1, {"text": "x"})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, s1, {"volume": 1})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, s1, {"crop": {}})
    s.ok("updateSegment", pid, s1, {"italic": True})
    s.ok("updateSegment", pid, e1, {"timeline": tl(2_000_000, 500_000)})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, e1, {"transform": None})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, e1, {"text": "x"})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, e1, {"bold": True, "timeline": tl(0, 1)})
    s.ok("updateSegment", pid, f1, {"timeline": tl(5_000_000, 2_000_000)})
    s.fail("updateSegment", "SEGMENT_FIELD_MISMATCH", pid, f1, {"volume": 1})
    s.ok("updateSegment", pid, cue, {"timeline": tl(500_000, 1_000_000)})
    s.ok("updateSegment", pid, cue, {"text": "edited", "color": "#00ff00"})
    s.ok("getSegment", pid, cue)
    s.ok("getSegment", pid, f1)
    s.fail("getSegment", "SEGMENT_NOT_FOUND", pid, "missing")
    s.fail("getSegment", "PROJECT_NOT_FOUND", MISSING_PROJECT, t1)
    out.append(s.finish())

    s = Scenario("move and delete clips")
    pid, (video, text, sticker, effect), _ = new_project(s, "Move", "video", "text", "sticker", "effect")
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip-720p.mp4"), "timeline": tl(0, 1_000_000)})
    v1 = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip.avi"), "timeline": tl(1_000_000, 3_000_000), "source": tl(0, 1_000_000)})
    v2 = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip.m4v"), "timeline": tl(4_000_000, 3_000_000), "source": tl(0, 2_000_000)})
    v3 = segment_id(project, video)
    project = s.ok("addTextSegment", pid, text, {"text": "T", "timeline": tl(0, 1_000_000)})
    t1 = segment_id(project, text)
    project = s.ok("addStickerSegment", pid, sticker, {"resource_id": "9", "timeline": tl(0, 1_000_000)})
    s1 = segment_id(project, sticker)
    project = s.ok("addTrackEffectSegment", pid, effect, {"effect_type": "BOOM", "timeline": tl(0, 1_000_000)})
    e1 = segment_id(project, effect)
    s.ok("moveSegment", pid, v1, 10_000_000)
    s.ok("moveSegment", pid, v2, 20_000_000)
    s.ok("moveSegment", pid, v3, 30_000_000, {"expectRevision": s.revision(pid)})
    s.fail("moveSegment", "TIMELINE_OVERLAP", pid, v1, 19_500_000)
    s.fail("moveSegment", "VALIDATION_ERROR", pid, v1, -1)
    s.fail("moveSegment", "VALIDATION_ERROR", pid, v1, 1.5)
    s.fail("moveSegment", "VALIDATION_ERROR", pid, v1, "5")
    s.fail("moveSegment", "VALIDATION_ERROR", pid, v1, None)
    s.fail("moveSegment", "VALIDATION_ERROR", pid, v1, MAX_TIME_US + 1)
    s.fail("moveSegment", "VALIDATION_ERROR", pid, v1, MAX_TIME_US)
    s.fail("moveSegment", "VALIDATION_ERROR", pid, v1, -1, {"expectRevision": 0})
    s.fail("moveSegment", "VALIDATION_ERROR", pid, v1)
    s.fail("moveSegment", "SEGMENT_NOT_FOUND", pid, "missing", 0)
    s.fail("moveSegment", "SEGMENT_NOT_FOUND", pid, "missing", 0, {"expectRevision": 1})
    s.fail("moveSegment", "PROJECT_CONFLICT", pid, v1, 0, {"expectRevision": 1})
    s.fail("moveSegment", "PROJECT_NOT_FOUND", MISSING_PROJECT, v1, 0)
    s.ok("moveSegment", pid, t1, 2_000_000)
    s.ok("moveSegment", pid, s1, MAX_TIME_US - 1_000_000)
    s.ok("moveSegment", pid, e1, 0)
    s.ok("getSegment", pid, v2)
    s.ok("deleteSegment", pid, v1)
    s.fail("deleteSegment", "SEGMENT_NOT_FOUND", pid, v1)
    s.fail("deleteSegment", "PROJECT_CONFLICT", pid, v2, {"expectRevision": 2})
    s.ok("deleteSegment", pid, v2, {"expectRevision": s.revision(pid)})
    s.ok("deleteSegment", pid, e1)
    project = s.ok("addSrtSubtitles", pid, "00:00:01,000 --> 00:00:02,000\nhello")
    s.ok("deleteSegment", pid, segment_id(project, track_id(project)))
    s.fail("deleteSegment", "PROJECT_NOT_FOUND", MISSING_PROJECT, v3)
    out.append(s.finish())
    return out


# ---------------------------------------------------------------- scenarios: split + keyframes

def keyframe_scenarios() -> list[dict[str, Any]]:
    out = []
    s = Scenario("keyframes")
    pid, (video, text, audio, effect, sticker), _ = new_project(s, "Keyframes", "video", "text", "audio", "effect", "sticker")
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip-720p.mp4"), "timeline": tl(0, 2_000_000)})
    v1 = segment_id(project, video)
    project = s.ok("addTextSegment", pid, text, {"text": "K", "timeline": tl(0, 2_000_000)})
    t1 = segment_id(project, text)
    project = s.ok("addMediaSegment", pid, audio, {"path": media("tone.wav"), "timeline": tl(0, 2_000_000)})
    a1 = segment_id(project, audio)
    project = s.ok("addTrackEffectSegment", pid, effect, {"effect_type": "_1998", "timeline": tl(0, 2_000_000)})
    e1 = segment_id(project, effect)
    project = s.ok("addStickerSegment", pid, sticker, {"resource_id": "5", "timeline": tl(0, 2_000_000)})
    st = segment_id(project, sticker)
    s.ok("addKeyframes", pid, v1, {"keyframes": [
        {"property": "alpha", "offset_us": 1_000_000, "value": 1},
        {"property": "alpha", "offset_us": 0, "value": 0},
        {"property": "rotation", "offset_us": 500_000, "value": 90},
        {"property": "position_x", "offset_us": 1_500_000, "value": 0.5},
        {"property": "scale_x", "offset_us": 0, "value": 1},
        {"property": "scale_x", "offset_us": 2_000_000, "value": 2},
        {"property": "brightness", "offset_us": 0, "value": -0.5},
        {"property": "volume", "offset_us": 0, "value": "1.5"},
    ]})
    s.ok("addKeyframes", pid, v1, {"keyframes": [{"property": "alpha", "offset_us": 1_000_000, "value": 0.5}, {"property": "transform_x", "offset_us": 1_500_000, "value": -0.5}]})
    s.ok("listKeyframes", pid, v1)
    s.fail("addKeyframes", "KEYFRAME_PROPERTY_CONFLICT", pid, v1, {"keyframes": [{"property": "uniform_scale", "offset_us": 0, "value": 1}]})
    s.fail("addKeyframes", "KEYFRAME_PROPERTY_CONFLICT", pid, t1, {"keyframes": [{"property": "uniform_scale", "offset_us": 0, "value": 1}, {"property": "scale_y", "offset_us": 0, "value": 1}]})
    s.ok("addKeyframes", pid, t1, {"keyframes": [
        {"property": "uniform_scale", "offset_us": 0, "value": 1},
        {"property": "uniform_scale", "offset_us": 2_000_000, "value": 1.5},
        {"property": "position_y", "offset_us": 0, "value": -1},
        {"property": "alpha", "offset_us": 1_000_000, "value": 0.5},
        {"property": "rotation", "offset_us": 0, "value": 10},
        {"property": "rotation", "offset_us": 1_900_000, "value": 33.3},
    ]})
    s.fail("addKeyframes", "KEYFRAME_PROPERTY_CONFLICT", pid, t1, {"keyframes": [{"property": "scale_x", "offset_us": 0, "value": 1}]})
    s.fail("addKeyframes", "KEYFRAME_PROPERTY_INVALID", pid, t1, {"keyframes": [{"property": "volume", "offset_us": 0, "value": 1}]})
    s.fail("addKeyframes", "KEYFRAME_PROPERTY_INVALID", pid, t1, {"keyframes": [{"property": "contrast", "offset_us": 0, "value": 0.5}]})
    s.fail("addKeyframes", "KEYFRAME_PROPERTY_INVALID", pid, a1, {"keyframes": [{"property": "alpha", "offset_us": 0, "value": 1}]})
    s.ok("addKeyframes", pid, a1, {"keyframes": [{"property": "volume", "offset_us": 0, "value": 0}, {"property": "volume", "offset_us": 2_000_000, "value": 2}]})
    s.ok("addKeyframes", pid, e1, {"keyframes": [{"property": "alpha", "offset_us": 0, "value": 0.5}]})
    s.fail("addKeyframes", "KEYFRAME_PROPERTY_INVALID", pid, e1, {"keyframes": [{"property": "saturation", "offset_us": 0, "value": 0.5}]})
    s.fail("addKeyframes", "KEYFRAME_PROPERTY_INVALID", pid, st, {"keyframes": [{"property": "brightness", "offset_us": 0, "value": 0.5}]})
    s.ok("addKeyframes", pid, st, {"keyframes": [{"property": "scale_y", "offset_us": 2_000_000, "value": 100}, {"property": "scale_y", "offset_us": 0, "value": 0.01}]})
    s.fail("addKeyframes", "KEYFRAME_OFFSET_INVALID", pid, v1, {"keyframes": [{"property": "alpha", "offset_us": 2_000_001, "value": 1}]})
    s.fail("addKeyframes", "KEYFRAME_OFFSET_INVALID", pid, v1, {"keyframes": [{"property": "alpha", "offset_us": 2_000_001, "value": 5}]})
    for prop, value in (("alpha", 1.5), ("alpha", -0.1), ("volume", 2.5), ("scale_x", 0.001), ("scale_y", 100.5), ("uniform_scale", 101),
                        ("transform_x", 1.5), ("position_y", -1.5), ("rotation", 361), ("brightness", -1.5), ("contrast", 2), ("saturation", -2)):
        target = t1 if prop == "uniform_scale" else v1
        s.fail("addKeyframes", "KEYFRAME_VALUE_INVALID", pid, target, {"keyframes": [{"property": prop, "offset_us": 0, "value": value}]})
    s.fail("addKeyframes", "KEYFRAME_VALUE_INVALID", pid, v1, {"keyframes": [{"property": "alpha", "offset_us": 500_000, "value": 0.3}, {"property": "alpha", "offset_us": 0, "value": 9}]})
    s.ok("listKeyframes", pid, v1)
    s.fail("addKeyframes", "VALIDATION_ERROR", pid, v1, {"keyframes": []})
    s.fail("addKeyframes", "VALIDATION_ERROR", pid, v1, {"keyframes": [{"property": "alpha", "offset_us": 0, "value": 1}] * 501})
    s.fail("addKeyframes", "VALIDATION_ERROR", pid, v1, {"keyframes": [{"property": "zoom", "offset_us": -1, "value": "abc"}, {"property": "alpha", "offset_us": 0, "value": None}]})
    s.fail("addKeyframes", "VALIDATION_ERROR", pid, v1, {"keyframes": [{"property": "alpha", "offset_us": 0, "value": 1, "ease": "in"}, "bad", {"property": "alpha"}]})
    s.fail("addKeyframes", "VALIDATION_ERROR", pid, v1, {"keyframes": [{"property": "alpha", "offset_us": 0, "value": "nan"}, {"property": "alpha", "offset_us": 1.5, "value": "Infinity"}]})
    s.fail("addKeyframes", "VALIDATION_ERROR", pid, v1, {"keyframes": "x"})
    s.fail("addKeyframes", "VALIDATION_ERROR", pid, v1, {})
    s.fail("addKeyframes", "SEGMENT_NOT_FOUND", pid, "missing", {"keyframes": [{"property": "alpha", "offset_us": 0, "value": 1}]})
    s.fail("addKeyframes", "PROJECT_CONFLICT", pid, v1, {"keyframes": [{"property": "alpha", "offset_us": 0, "value": 1}]}, {"expectRevision": 2})
    s.ok("removeKeyframe", pid, v1, "position_x", 1_500_000)
    s.ok("removeKeyframe", pid, v1, "alpha", "1000000")
    s.ok("removeKeyframe", pid, v1, "volume", 0, {"expectRevision": s.revision(pid)})
    s.fail("removeKeyframe", "KEYFRAME_NOT_FOUND", pid, v1, "alpha", 123)
    s.fail("removeKeyframe", "KEYFRAME_NOT_FOUND", pid, v1, "position_x", 1_500_000)
    s.fail("removeKeyframe", "KEYFRAME_PROPERTY_INVALID", MISSING_PROJECT, v1, "zoom", 0)
    s.fail("removeKeyframe", "VALIDATION_ERROR", pid, v1, "alpha", "abc")
    s.fail("removeKeyframe", "VALIDATION_ERROR", pid, v1, "alpha", 1.5)
    s.fail("removeKeyframe", "VALIDATION_ERROR", pid, v1, 5, 0)
    s.fail("removeKeyframe", "VALIDATION_ERROR", pid, v1, "alpha")
    s.fail("removeKeyframe", "VALIDATION_ERROR", pid, v1, "alpha", 0, {"expectRevision": 0})
    s.fail("removeKeyframe", "SEGMENT_NOT_FOUND", pid, "missing", "alpha", 0)
    s.fail("removeKeyframe", "PROJECT_CONFLICT", pid, v1, "alpha", 0, {"expectRevision": 2})
    s.ok("listKeyframes", pid, e1)
    s.ok("listKeyframes", pid, st)
    s.fail("listKeyframes", "SEGMENT_NOT_FOUND", pid, "missing")
    s.fail("listKeyframes", "PROJECT_NOT_FOUND", MISSING_PROJECT, v1)
    out.append(s.finish())

    s = Scenario("split clips")
    pid, (video, video2, text, audio, effect, filt, sticker), _ = new_project(s, "Split", "video", "video", "text", "audio", "effect", "filter", "sticker")
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip-720p.mp4"), "timeline": tl(0, 2_000_000), "source": tl(500_000, 2_000_000), "transform": {"alpha": 0.5}})
    v1 = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip.mkv"), "timeline": tl(3_000_000, 10), "source": tl(0, 5)})
    v2 = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip.avi"), "timeline": tl(4_000_000, 1000), "source": tl(0, 1)})
    v3 = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video2, {"path": media("still.png"), "timeline": tl(0, MAX_TIME_US)})
    big = segment_id(project, video2)
    project = s.ok("addTextSegment", pid, text, {"text": "split me", "timeline": tl(0, 2_000_000)})
    t1 = segment_id(project, text)
    project = s.ok("addMediaSegment", pid, audio, {"path": media("tone.wav"), "timeline": tl(0, 2_000_000)})
    a1 = segment_id(project, audio)
    project = s.ok("addTrackEffectSegment", pid, effect, {"effect_type": "_1998", "params": [5], "timeline": tl(0, 2_000_000)})
    e1 = segment_id(project, effect)
    project = s.ok("addTrackFilterSegment", pid, filt, {"filter_type": "ABG", "timeline": tl(0, 2_000_000)})
    f1 = segment_id(project, filt)
    project = s.ok("addStickerSegment", pid, sticker, {"resource_id": "8", "timeline": tl(0, 2_000_000)})
    st = segment_id(project, sticker)
    s.ok("addKeyframes", pid, v1, {"keyframes": [
        {"property": "alpha", "offset_us": 0, "value": 0},
        {"property": "alpha", "offset_us": 1_000_000, "value": 1},
        {"property": "rotation", "offset_us": 500_000, "value": 45},
        {"property": "scale_x", "offset_us": 0, "value": 1.25},
        {"property": "brightness", "offset_us": 1_500_000, "value": 0.2},
        {"property": "volume", "offset_us": 500_000, "value": 0.7},
        {"property": "volume", "offset_us": 1_800_000, "value": 1.9},
    ]})
    s.ok("addEffect", pid, v1, {"effect_type": "_1998", "params": [1, 2]})
    s.ok("addTransition", pid, v1, {"transition_type": "_3D空间"})
    s.ok("addKeyframes", pid, t1, {"keyframes": [
        {"property": "uniform_scale", "offset_us": 0, "value": 1},
        {"property": "uniform_scale", "offset_us": 2_000_000, "value": 1.5},
        {"property": "rotation", "offset_us": 0, "value": 10},
        {"property": "rotation", "offset_us": 1_900_000, "value": 33.3},
        {"property": "alpha", "offset_us": 1_000_000, "value": 0.5},
    ]})
    s.ok("addKeyframes", pid, a1, {"keyframes": [{"property": "volume", "offset_us": 0, "value": 0}, {"property": "volume", "offset_us": 2_000_000, "value": 2}]})
    s.ok("addKeyframes", pid, e1, {"keyframes": [{"property": "alpha", "offset_us": 100, "value": 0.25}]})
    result = s.ok("splitSegment", pid, v1, 500_000)
    first, second = result["segments"]
    s.ok("splitSegment", pid, second["id"], 1)
    s.ok("splitSegment", pid, first["id"], 499_999, {"expectRevision": s.revision(pid)})
    s.ok("splitSegment", pid, v2, 5)
    s.fail("splitSegment", "SPLIT_OFFSET_INVALID", pid, v3, 1)
    s.fail("splitSegment", "SPLIT_OFFSET_INVALID", pid, v3, 1000)
    s.fail("splitSegment", "SPLIT_OFFSET_INVALID", pid, v3, 5000)
    s.fail("splitSegment", "VALIDATION_ERROR", pid, v3, 0)
    s.fail("splitSegment", "VALIDATION_ERROR", pid, v3, -5)
    s.fail("splitSegment", "VALIDATION_ERROR", pid, v3, 1.5)
    s.fail("splitSegment", "VALIDATION_ERROR", pid, v3, MAX_TIME_US + 1)
    s.fail("splitSegment", "VALIDATION_ERROR", pid, v3, "10")
    s.fail("splitSegment", "VALIDATION_ERROR", pid, v3)
    s.ok("splitSegment", pid, big, 43_200_000_001)
    s.ok("splitSegment", pid, t1, 1_000_000)
    s.ok("splitSegment", pid, a1, 700_000)
    s.ok("splitSegment", pid, e1, 1_000_000)
    s.ok("splitSegment", pid, f1, 1_000_000)
    s.ok("splitSegment", pid, st, 333_333)
    project = s.ok("addSrtSubtitles", pid, "1\n00:00:00,000 --> 00:00:02,000\nlong cue\n")
    s.ok("splitSegment", pid, segment_id(project, track_id(project)), 1_250_000)
    s.fail("splitSegment", "SEGMENT_NOT_FOUND", pid, "missing", 1)
    s.fail("splitSegment", "PROJECT_CONFLICT", pid, v3, 500, {"expectRevision": 2})
    s.fail("splitSegment", "PROJECT_NOT_FOUND", MISSING_PROJECT, v3, 500)
    s.ok("getProject", pid)
    out.append(s.finish())
    return out


# ---------------------------------------------------------------- scenarios: clip features

def feature_scenarios() -> list[dict[str, Any]]:
    out = []
    s = Scenario("effects, filters, animations, transitions and fades")
    pid, (video, audio, text, sticker, effect), _ = new_project(s, "Features", "video", "audio", "text", "sticker", "effect")
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip-720p.mp4"), "timeline": tl(0, 2_000_000)})
    v1 = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("still.png"), "timeline": tl(2_000_000, 2_000_000)})
    image = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip.avi"), "timeline": tl(4_000_000, 300_000)})
    short = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, audio, {"path": media("tone.wav"), "timeline": tl(0, 2_000_000)})
    a1 = segment_id(project, audio)
    project = s.ok("addTextSegment", pid, text, {"text": "F", "timeline": tl(0, 2_000_000)})
    t1 = segment_id(project, text)
    project = s.ok("addStickerSegment", pid, sticker, {"resource_id": "1", "timeline": tl(0, 2_000_000)})
    s1 = segment_id(project, sticker)
    project = s.ok("addTrackEffectSegment", pid, effect, {"effect_type": "BOOM", "timeline": tl(0, 2_000_000)})
    e1 = segment_id(project, effect)
    s.ok("addEffect", pid, v1, {"effect_type": "_1998", "params": [10, 20]})
    s.ok("addEffect", pid, v1, {"effect_type": "BOOM"})
    s.ok("addEffect", pid, image, {"effect_type": " DV界面 ", "params": []})
    s.fail("addEffect", "EFFECT_PARAMS_INVALID", pid, v1, {"effect_type": "_70s", "params": [1, 2]})
    s.ok("addEffect", pid, a1, {"effect_type": "_8bit", "params": [1, 2, 3]})
    s.ok("addEffect", pid, a1, {"effect_type": "台湾小哥"})
    s.ok("addEffect", pid, a1, {"effect_type": "Lofi", "params": None})
    s.fail("addEffect", "EFFECT_INVALID", pid, a1, {"effect_type": "_1998"})
    s.fail("addEffect", "EFFECT_INVALID", pid, v1, {"effect_type": "_8bit"})
    s.fail("addEffect", "EFFECT_INVALID", pid, v1, {"effect_type": "nope"})
    s.fail("addEffect", "SEGMENT_FIELD_MISMATCH", pid, t1, {"effect_type": "_1998"})
    s.fail("addEffect", "SEGMENT_FIELD_MISMATCH", pid, s1, {"effect_type": "_1998"})
    s.fail("addEffect", "SEGMENT_FIELD_MISMATCH", pid, e1, {"effect_type": "_1998"})
    s.fail("addEffect", "VALIDATION_ERROR", pid, v1, {"effect_type": "", "params": [101]})
    s.fail("addEffect", "VALIDATION_ERROR", pid, v1, {"params": [1] * 65})
    s.ok("addFilter", pid, v1, {"filter_type": "_1980", "intensity": 50})
    s.ok("addFilter", pid, image, {"filter_type": "ABG"})
    s.fail("addFilter", "SEGMENT_FIELD_MISMATCH", pid, a1, {"filter_type": "ABG"})
    s.fail("addFilter", "SEGMENT_FIELD_MISMATCH", pid, t1, {"filter_type": "ABG"})
    s.fail("addFilter", "DRAFT_FEATURE_INVALID", pid, v1, {"filter_type": "nope"})
    s.fail("addFilter", "VALIDATION_ERROR", pid, v1, {"filter_type": "ABG", "intensity": 100.5, "strength": 1})
    s.ok("addAnimation", pid, v1, {"animation_type": "动感放大", "duration_us": 500_000})
    s.ok("addAnimation", pid, v1, {"animation_type": "Kira游动"})
    s.ok("addAnimation", pid, image, {"animation_type": "三分割", "duration_us": None})
    s.ok("addAnimation", pid, t1, {"animation_type": "冲屏位移"})
    s.ok("addAnimation", pid, t1, {"animation_type": "VHS", "duration_us": 2_000_000})
    s.fail("addAnimation", "ANIMATION_INVALID", pid, t1, {"animation_type": "动感放大"})
    s.fail("addAnimation", "ANIMATION_INVALID", pid, v1, {"animation_type": "VHS"})
    s.fail("addAnimation", "ANIMATION_INVALID", pid, v1, {"animation_type": "动感放大", "duration_us": 2_000_001})
    s.fail("addAnimation", "SEGMENT_FIELD_MISMATCH", pid, a1, {"animation_type": "动感放大"})
    s.fail("addAnimation", "SEGMENT_FIELD_MISMATCH", pid, s1, {"animation_type": "动感放大"})
    s.fail("addAnimation", "VALIDATION_ERROR", pid, v1, {"animation_type": "动感放大", "duration_us": 0})
    s.ok("addTransition", pid, v1, {"transition_type": "_3D空间"})
    s.ok("addTransition", pid, v1, {"transition_type": "上移", "duration_us": 1_000_000})
    s.ok("addTransition", pid, short, {"transition_type": "上移"})
    s.ok("addTransition", pid, image, {"transition_type": "上移", "duration_us": 2_000_000})
    s.fail("addTransition", "TRANSITION_INVALID", pid, image, {"transition_type": "上移", "duration_us": 2_000_001})
    s.fail("addTransition", "SEGMENT_FIELD_MISMATCH", pid, t1, {"transition_type": "上移"})
    s.fail("addTransition", "SEGMENT_FIELD_MISMATCH", pid, a1, {"transition_type": "nope"})
    s.fail("addTransition", "DRAFT_FEATURE_INVALID", pid, v1, {"transition_type": "nope"})
    s.fail("addTransition", "VALIDATION_ERROR", pid, v1, {"transition_type": "上移", "duration_us": 1.5})
    s.ok("addFade", pid, v1, {"in_duration_us": 200_000, "out_duration_us": 300_000})
    s.ok("addFade", pid, a1, {"in_duration_us": 100_000})
    s.ok("addFade", pid, image, {"out_duration_us": 2_000_000})
    s.fail("addFade", "SEGMENT_FIELD_MISMATCH", pid, t1, {"in_duration_us": 100_000})
    s.fail("addFade", "FADE_INVALID", pid, v1, {"in_duration_us": 1_500_000, "out_duration_us": 500_001})
    s.fail("addFade", "VALIDATION_ERROR", pid, v1, {})
    s.fail("addFade", "VALIDATION_ERROR", pid, v1, {"in_duration_us": 0, "out_duration_us": 0})
    s.fail("addFade", "VALIDATION_ERROR", pid, v1, {"in_duration_us": -1, "out_duration_us": 1.5, "curve": 1})
    s.fail("addEffect", "SEGMENT_NOT_FOUND", pid, "missing", {"effect_type": "_1998"})
    s.fail("addFilter", "PROJECT_CONFLICT", pid, v1, {"filter_type": "ABG"}, {"expectRevision": 1})
    s.ok("getProject", pid)
    out.append(s.finish())

    s = Scenario("masks, chroma, background filling, mix modes and clearing features")
    pid, (video, audio, text), _ = new_project(s, "Visual features", "video", "audio", "text")
    project = s.ok("addMediaSegment", pid, video, {"path": media("clip-720p.mp4"), "timeline": tl(0, 2_000_000), "crop": {"upper_left_x": 0.2}})
    v1 = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, video, {"path": media("still.jpg"), "timeline": tl(2_000_000, 2_000_000)})
    image = segment_id(project, video)
    project = s.ok("addMediaSegment", pid, audio, {"path": media("tone.wav"), "timeline": tl(0, 2_000_000)})
    a1 = segment_id(project, audio)
    project = s.ok("addTextSegment", pid, text, {
        "text": "styled", "timeline": tl(0, 2_000_000), "border": {}, "background": {"color": "#00000080"},
        "shadow": {}, "bubble": {"effect_id": "1", "resource_id": "2"}, "text_effect_id": "3",
    })
    t1 = segment_id(project, text)
    s.ok("addMask", pid, v1, {"mask_type": "圆形", "center_x": 100, "center_y": -50.5, "size": 0.75, "rotation": 30, "feather": 10, "invert": True})
    s.fail("addMask", "FEATURE_EXISTS", pid, v1, {"mask_type": "线性"})
    s.ok("addMask", pid, image, {"mask_type": " 矩形 ", "rect_width": 0.5, "round_corner": 10})
    s.fail("addMask", "SEGMENT_FIELD_MISMATCH", pid, a1, {"mask_type": "圆形"})
    s.fail("addMask", "SEGMENT_FIELD_MISMATCH", pid, t1, {"mask_type": "圆形"})
    s.fail("addMask", "DRAFT_FEATURE_INVALID", MISSING_PROJECT, v1, {"mask_type": "nope"})
    s.fail("addMask", "VALIDATION_ERROR", pid, v1, {"mask_type": "", "center_x": 10001, "center_y": -10001, "size": 0, "rotation": 361, "feather": 101, "invert": "yes", "rect_width": 0, "round_corner": 101})
    s.fail("addMask", "VALIDATION_ERROR", pid, v1, {"size": 1.5, "shape": "x"})
    s.ok("addChroma", pid, v1, {"color": "#00FF00FF", "intensity": 55, "shadow": 1, "edge_smooth": 2, "spill": 3})
    s.fail("addChroma", "FEATURE_EXISTS", pid, v1, {"color": "#00FF00FF"})
    s.ok("addChroma", pid, image, {"color": "#0000ff80"})
    s.fail("addChroma", "SEGMENT_FIELD_MISMATCH", pid, t1, {"color": "#00FF00FF"})
    s.fail("addChroma", "VALIDATION_ERROR", pid, v1, {"color": "#00FF00", "intensity": 101, "shadow": -1, "edge_smooth": 100.5, "spill": "x"})
    s.fail("addChroma", "VALIDATION_ERROR", pid, v1, {})
    s.ok("addBackgroundFilling", pid, v1, {"fill_type": "blur"})
    s.ok("addBackgroundFilling", pid, image, {"fill_type": "color", "color": "#FF000080", "blur": 1})
    s.fail("addBackgroundFilling", "FEATURE_EXISTS", pid, image, {"fill_type": "blur"})
    s.fail("addBackgroundFilling", "SEGMENT_FIELD_MISMATCH", pid, a1, {"fill_type": "blur"})
    s.fail("addBackgroundFilling", "VALIDATION_ERROR", pid, v1, {"fill_type": "image", "blur": 1.5, "color": "#FF0000"})
    s.ok("setMixMode", pid, v1, {"mode": "正片叠底"})
    s.fail("setMixMode", "FEATURE_EXISTS", pid, v1, {"mode": "颜色减淡"})
    s.fail("setMixMode", "DRAFT_FEATURE_INVALID", pid, image, {"mode": "nope"})
    s.fail("setMixMode", "SEGMENT_FIELD_MISMATCH", pid, t1, {"mode": "正片叠底"})
    s.fail("setMixMode", "VALIDATION_ERROR", pid, v1, {"mode": "  "})
    s.ok("addEffect", pid, v1, {"effect_type": "_1998"})
    s.ok("addFilter", pid, v1, {"filter_type": "ABG"})
    s.ok("addAnimation", pid, v1, {"animation_type": "动感放大"})
    s.ok("addTransition", pid, v1, {"transition_type": "上移"})
    s.ok("addFade", pid, v1, {"in_duration_us": 1})
    for feature in ("effects", "filters", "animations", "transition", "fade", "mask", "chroma", "background_filling", "mix_mode", "crop", "border", "background", "shadow"):
        s.ok("clearFeature", pid, v1, feature)
    s.ok("addMask", pid, v1, {"mask_type": "爱心"})
    s.fail("clearFeature", "SEGMENT_FIELD_MISMATCH", pid, v1, "bubble")
    s.fail("clearFeature", "SEGMENT_FIELD_MISMATCH", pid, a1, "text_effect")
    for feature in ("bubble", "text_effect", "border", "background", "shadow", "effects", "animations"):
        s.ok("clearFeature", pid, t1, feature)
    s.ok("clearFeature", pid, a1, "mask")
    s.fail("clearFeature", "FEATURE_INVALID", MISSING_PROJECT, v1, "bogus")
    s.fail("clearFeature", "FEATURE_INVALID", pid, v1, "text_effect_id")
    s.fail("clearFeature", "VALIDATION_ERROR", pid, v1, 3)
    s.fail("clearFeature", "VALIDATION_ERROR", pid, v1)
    s.fail("clearFeature", "SEGMENT_NOT_FOUND", pid, "missing", "mask")
    s.fail("clearFeature", "PROJECT_CONFLICT", pid, v1, "mask", {"expectRevision": 1})
    s.ok("clearFeature", pid, image, "mask", {"expectRevision": s.revision(pid)})
    s.ok("getProject", pid)
    out.append(s.finish())
    return out


# ---------------------------------------------------------------- scenarios: markers + subtitles

SRT_FULL = (
    "﻿1\r\n00:00:01,000 --> 00:00:02,500\r\nFirst line\r\nsecond line\r\n\r\n"
    "2\r\n00:00:02.500 --> 00:00:04.000 X1:10 X2:20\r\n  Padded caption  \r\n\r\n"
    "\r\n\r\n3\r\n001:00:00,000 --> 001:00:01,000\r\nlate\r\n"
)


def subtitle_scenarios() -> list[dict[str, Any]]:
    out = []
    s = Scenario("markers")
    pid, _, _ = new_project(s, "Markers")
    s.ok("addMarker", pid, {"time_us": 1_000_000, "label": " Intro "})
    s.ok("addMarker", pid, {"time_us": 500_000, "label": "Cold open", "color": "#abcdef"})
    s.ok("addMarker", pid, {"time_us": 1_000_000, "label": "Intro v2", "color": "#FF0000"}, {"expectRevision": s.revision(pid)})
    s.ok("addMarker", pid, {"time_us": 0, "label": "Start"})
    s.ok("addMarker", pid, {"time_us": MAX_TIME_US, "label": "End"})
    s.ok("listMarkers", pid)
    s.ok("removeMarker", pid, 500_000)
    s.ok("removeMarker", pid, "1000000")
    s.fail("removeMarker", "MARKER_NOT_FOUND", pid, 42)
    s.fail("removeMarker", "VALIDATION_ERROR", pid, "x")
    s.fail("removeMarker", "VALIDATION_ERROR", pid)
    s.fail("removeMarker", "VALIDATION_ERROR", pid, None)
    s.fail("addMarker", "VALIDATION_ERROR", pid, None, {"expectRevision": 0})
    s.fail("removeMarker", "PROJECT_CONFLICT", pid, 0, {"expectRevision": 1})
    s.fail("addMarker", "VALIDATION_ERROR", pid, {"time_us": -1, "label": "  ", "color": "#FFF"})
    s.fail("addMarker", "VALIDATION_ERROR", pid, {"time_us": MAX_TIME_US + 1, "label": "l" * 201, "note": "x"})
    s.fail("addMarker", "VALIDATION_ERROR", pid, {"time_us": 1.5})
    s.fail("addMarker", "PROJECT_NOT_FOUND", MISSING_PROJECT, {"time_us": 1, "label": "x"})
    s.fail("listMarkers", "PROJECT_NOT_FOUND", MISSING_PROJECT)
    s.ok("listMarkers", pid)
    out.append(s.finish())

    s = Scenario("srt subtitles")
    pid, (text,), _ = new_project(s, "Subtitles", "text")
    s.ok("addTextSegment", pid, text, {"text": "Overlay", "timeline": tl(0, 3_000_000)})
    project = s.ok("addSrtSubtitles", pid, SRT_FULL)
    group = track_id(project)
    s.ok("addSrtSubtitles", pid, "00:00:00,000 --> 00:00:01,000\nexplicit group", {"groupId": "a" * 32})
    s.fail("addSrtSubtitles", "SUBTITLE_EXISTS", pid, "00:00:00,000 --> 00:00:01,000\nagain", {"groupId": "a" * 32})
    s.fail("addSrtSubtitles", "SUBTITLE_GROUP_INVALID", pid, "00:00:00,000 --> 00:00:01,000\nx", {"groupId": "XYZ"})
    s.fail("addSrtSubtitles", "SUBTITLE_GROUP_INVALID", pid, "00:00:00,000 --> 00:00:01,000\nx", {"groupId": "A" * 32})
    s.ok("addSrtSubtitles", pid, "1\n00:00:05,000 --> 00:00:06,000\nempty group id\n", {"groupId": ""})
    s.ok("addSrtSubtitles", pid, "00:00:00,000 --> 00:00:01,000\nnull group", {"groupId": None, "expectRevision": s.revision(pid)})
    s.fail("addSrtSubtitles", "VALIDATION_ERROR", pid, "00:00:00,000 --> 00:00:01,000\nx", {"groupId": 5, "expectRevision": 0})
    s.fail("addSrtSubtitles", "SRT_EMPTY", pid, "")
    s.fail("addSrtSubtitles", "SRT_EMPTY", pid, "  \r\n\t")
    s.fail("addSrtSubtitles", "SRT_EMPTY", pid, 5)
    s.fail("addSrtSubtitles", "SRT_EMPTY", pid, None)
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "1\n00:00:01 --> 00:00:02\nbad timing")
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "00:61:00,000 --> 00:62:00,000\nminutes")
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "00:00:05,000 --> 00:00:05,000\nzero")
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "1\nno timing here\ncaption")
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "1")
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "1\n00:00:01,000 --> 00:00:02,000\n   ")
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "24:00:00,000 --> 24:00:00,001\ntoo late")
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, " \n1\n00:00:01,000 --> 00:00:02,000\nskipped block")
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "00:00:01,000 --> 00:00:02,000\n" + "c" * 10001)
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "00:00:01,000 --> 00:00:02,000\nok\n\n00:00:03,000 -> 00:00:04,000\nbad arrow")
    s.fail("addSrtSubtitles", "PROJECT_NOT_FOUND", MISSING_PROJECT, "00:00:00,000 --> 00:00:01,000\nx")
    s.fail("addSrtSubtitles", "PROJECT_CONFLICT", pid, "00:00:00,000 --> 00:00:01,000\nx", {"expectRevision": 1})
    s.ok("addSrtSubtitles", pid, "00:00:00,000 --> 00:00:01,000\n1\n2\n\n\n\n00:00:00,500 --> 00:00:01,500\noverlap is fine")
    # str.isdigit() also takes Numeric_Type=Digit (², ①, ⑴) for the index line; \d in the timing line does not
    s.ok("addSrtSubtitles", pid, (
        "²\n00:00:07,000 --> 00:00:08,000\nsuperscript index\n\n"
        "①\n00:00:08,000 --> 00:00:09,000\ncircled index\n\n"
        "⑴²١\n00:00:09,000 --> 00:00:10,000\nmixed index\n\n"
        "\x1c 3 \u3000\n\x1c00:00:10,000 --> 00:00:11,000\x1c\ncontrol spaces\x1f"
    ))
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "²\n00:00:0²,000 --> 00:00:03,000\nsuperscript time")
    # digits added to Unicode after 15.0 (Python 3.12's table) are not digits for the original service
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "00:00:0\U0001CCF1,000 --> 00:00:03,000\nnew digit in time")
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "\U0001CCF1\n00:00:01,000 --> 00:00:02,000\nnew digit index")
    s.fail("addSrtSubtitles", "SRT_EMPTY", pid, " \n\t\u3000\x1c" * 500)
    s.fail("addSrtSubtitles", "SRT_INVALID", pid, "x" + "\n" * 1000 + "x")
    s.ok("updateTrack", pid, group, {"name": "中文字幕", "mute": True})
    s.fail("addTextSegment", "TRACK_NOT_FOUND", pid, group, {"text": "x", "timeline": tl(10_000_000, 1)})
    project = s.ok("removeSubtitleGroup", pid, "a" * 32)
    s.fail("removeSubtitleGroup", "SUBTITLE_NOT_FOUND", pid, "a" * 32)
    s.fail("removeSubtitleGroup", "SUBTITLE_NOT_FOUND", pid, text)
    s.fail("removeSubtitleGroup", "PROJECT_CONFLICT", pid, group, {"expectRevision": 1})
    ids = [item["id"] for item in project["tracks"]]
    s.ok("reorderTracks", pid, {"track_ids": list(reversed(ids))})
    s.ok("removeSubtitleGroup", pid, group)
    project = s.ok("getProject", pid)
    s.ok("deleteTrack", pid, track_id(project, 0))
    s.ok("getProject", pid)
    out.append(s.finish())
    return out


FILES = {
    "projects.json": projects_scenarios,
    "tracks.json": tracks_scenarios,
    "media-segments.json": media_scenarios,
    "overlay-segments.json": overlay_scenarios,
    "update-segments.json": update_scenarios,
    "keyframes-split.json": keyframe_scenarios,
    "segment-features.json": feature_scenarios,
    "markers-subtitles.json": subtitle_scenarios,
}


def dump(document: dict[str, Any]) -> str:
    """One compact line per step: small files, readable diffs."""
    compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
    scenarios = ",\n".join(
        "{\"name\":" + compact(item["name"]) + ",\"steps\":[\n" + ",\n".join(compact(step) for step in item["steps"]) + "\n]}"
        for item in document["scenarios"]
    )
    head = ",".join(f"{compact(key)}:{compact(value)}" for key, value in document.items() if key != "scenarios")
    text = "{" + head + ",\"scenarios\":[\n" + scenarios + "\n]}\n"
    assert json.loads(text) == document
    return text


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for filename, build in FILES.items():
        scenarios = build()
        document = {
            "generator": "node/scripts/ai-edit/generate-operation-fixtures.py",
            "source": "JianYingAPI ProjectService (service.py / models.py / storage.py)",
            "scenarios": scenarios,
        }
        target = OUTPUT / filename
        target.write_text(dump(document), encoding="utf-8")
        steps = sum(len(item["steps"]) for item in scenarios)
        print(f"{target.relative_to(REPO)}: {len(scenarios)} scenarios, {steps} steps", file=sys.stderr)


if __name__ == "__main__":
    main()

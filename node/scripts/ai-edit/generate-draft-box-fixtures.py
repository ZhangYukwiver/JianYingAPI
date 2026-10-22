"""Generate the oracle for node/aiEdit/draftBox.mjs from the original JianYingAPI.

node/aiEdit/draftBox.mjs is a Node port of JianYingAPI's draft_box.py (the Jianying draft
box: reading root_meta_info.json, creating drafts and repairing service-owned drafts in place).
This script runs the original DraftBoxService on the cases in
node/fixtures/ai-edit/draft-box/index-cases.json and stores the exact results in
node/fixtures/ai-edit/draft-box/oracle.json; draftBox.test.mjs replays the same cases in Node.

Run from anywhere with the JianYingAPI virtualenv (needs ffprobe on PATH for the media probe):

    <venv>/bin/python -I node/scripts/ai-edit/generate-draft-box-fixtures.py --jianying-api <JianYingAPI checkout>

Sections of oracle.json:
- status / status_missing_index / lists / gets: DraftBoxService on the layout + index of the cases
  file (temporary directory spelled ${BASE}).
- timestamps: _utc_timestamp for every value in cases["timestamps"] (CPython 3.12's C
  datetime.fromisoformat, including its lenient forms).
- records: _new_index_record for cases["records"].
- casefold: str.casefold for every character whose case folding differs from str.lower (the port
  folds without Python), plus a few whole strings.
- index_round_trips: json.dumps(json.loads(raw), ensure_ascii=False, separators=(",", ":"),
  allow_nan=False) for third-party index texts (big integers, 1.0, exponents, key order, escapes);
  None where Python raises.
- flow: create -> native main timeline appears -> rename -> repair, mirroring the original test
  test_create_draft_box_entry_writes_and_reads_new_draft. The index starts with another draft's
  entry holding 64-bit cloud ids and float-looking integers, and the raw index text after each
  write is stored so the port can be compared byte for byte.

What is patched on the Python side, and why:
- uuid.uuid4 / secrets.token_hex count up and the service/draft clocks are frozen, so every run
  writes the same oracle (project, track and segment ids come out as 0000...0001 and so on; all
  project timestamps fall inside 2026-10-19T08:00:00Z, the same second as the drafts' create_time).
- draft_box.uuid returns the fixed flow draft id, write support is forced on (the original only
  writes on Windows) and the index lock / atomic replace use plain POSIX calls.
Paths of the shared test media are written as ${MEDIA}.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import secrets
import sys
import tempfile
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "fixtures" / "ai-edit" / "draft-box"
MEDIA_DIR = Path(os.path.realpath(REPO / "fixtures" / "ai-edit" / "media"))
LONG_NAME = "x" * 201
LONG_QUERY = "q" * 201
FLOW_DRAFT_ID = "870F8B67-3D2F-441C-BDE0-760C8529A2DE"
FLOW_MAIN_TIMELINE = "4D2B6B0E-1C3A-4F5B-9D7E-2A1B3C4D5E6F"

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--jianying-api", required=True, type=Path, help="JianYingAPI checkout (read only)")
args = parser.parse_args()
sys.path.insert(0, str(args.jianying_api.resolve() / "src"))

# ---- determinism -----------------------------------------------------------------------
_uuid_counter = itertools.count(1)
uuid.uuid4 = lambda: uuid.UUID(int=next(_uuid_counter))
_token_counter = itertools.count(1)
secrets.token_hex = lambda nbytes=32: f"{next(_token_counter):0{nbytes * 2}x}"

from jianying_service import draft as draft_module  # noqa: E402
from jianying_service import draft_box as module  # noqa: E402
from jianying_service import service as service_module  # noqa: E402
from jianying_service.config import Settings  # noqa: E402
from jianying_service.draft_box import DraftBoxService  # noqa: E402
from jianying_service.errors import ServiceError  # noqa: E402
from jianying_service.models import (  # noqa: E402
    MediaSegmentCreate,
    ProjectCreate,
    ProjectUpdate,
    TextSegmentCreate,
    TrackCreate,
)
from jianying_service.service import ProjectService  # noqa: E402

BASE_TIME = datetime(2026, 10, 19, 8, 0, 0, 123456, tzinfo=timezone.utc)
_ticks = itertools.count()


class ServiceClock(datetime):
    @classmethod
    def now(cls, tz=None):
        moment = BASE_TIME + timedelta(milliseconds=next(_ticks))
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


class DraftClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return BASE_TIME.astimezone(tz) if tz else BASE_TIME.replace(tzinfo=None)


class FlowUuid:
    @staticmethod
    def uuid4():
        return uuid.UUID(FLOW_DRAFT_ID)


def posix_replace_index(path: Path, encoded: bytes) -> None:
    temporary = path.with_name(f".{path.name}.flow.tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)


service_module.datetime = ServiceClock
draft_module.datetime = DraftClock
module.uuid = FlowUuid
module._windows_write_supported = lambda: True
module._index_file_lock = lambda _path: nullcontext()
module._atomic_replace_index = posix_replace_index


# ---- helpers ---------------------------------------------------------------------------
def substitute(value, base: str):
    if isinstance(value, str):
        return value.replace("${BASE}", base).replace("${LONG_NAME}", LONG_NAME).replace("${LONG_QUERY}", LONG_QUERY)
    if isinstance(value, list):
        return [substitute(item, base) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, base) for key, item in value.items()}
    return value


def normalize(value, base: str):
    if isinstance(value, str):
        return value.replace(base, "${BASE}").replace(str(MEDIA_DIR), "${MEDIA}")
    if isinstance(value, list):
        return [normalize(item, base) for item in value]
    if isinstance(value, dict):
        return {key: normalize(item, base) for key, item in value.items()}
    return value


def capture(call, base: str):
    try:
        return {"result": normalize(call(), base)}
    except ServiceError as error:
        return {"error": {"code": error.code, "status": error.status}}


def build_layout(base: Path, layout: dict) -> None:
    for directory in layout["dirs"]:
        (base / directory).mkdir(parents=True, exist_ok=True)
    for name, text in layout["files"].items():
        (base / name).write_text(text, encoding="utf-8")
    for name, target in layout["symlinks"].items():
        (base / name).symlink_to(target, target_is_directory=True)


def listing(directory: Path, token: str | None = None) -> list[str]:
    names = []
    for current, directories, files in os.walk(directory):
        for name in [*directories, *files]:
            relative = os.path.relpath(os.path.join(current, name), directory)
            if token:
                relative = relative.replace(token, "${TOKEN}")
            names.append(relative)
    return sorted(names)


def dumps_index(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


# 别的草稿的条目：剪映云空间的 64 位 id、写成 1.0 的数、指数写法、整数样子的键。本身已是 Python 的输出形式，
# 新建和修复之后必须逐字节不变。
FOREIGN_ENTRY = (
    '{"draft_id":"CLOUD_OTHER","draft_name":"别人的云草稿","draft_fold_path":"${BASE}/drafts/CloudOther",'
    '"tm_draft_cloud_user_id":7234567890123456789,"tm_draft_cloud_entry_id":7345678901234567891,'
    '"tm_draft_cloud_space_id":7012345678901234567,"draft_timeline_materials_size":1.0,"tm_duration":2500000.0,'
    '"ratio":0.1,"tiny":1e-07,"huge":1e+22,"negative_zero":-0.0,"nested":{"10":[1.0,-1],"2":{"z":null,"a":true}}}'
)
# 不是 Python 输出形式的写法：Python 会规范化（2.5e6→2500000.0、1E+2→100.0、-0→0），移植要跟着一样。
UNTOUCHED = '{"keep":["all","original","fields"],"written":[2.5e6,1E+2,-0,12345678901234567890123,"\\u00e9\\n"]}'


def run_flow() -> dict:
    with tempfile.TemporaryDirectory() as raw_base:
        base = Path(os.path.realpath(raw_base))
        root = base / "drafts"
        root.mkdir()
        (base / "index").mkdir()
        index_path = base / "index" / "root_meta_info.json"
        index_before = (
            '{"root_path":' + json.dumps(str(base / "index"), ensure_ascii=False)
            + ',"all_draft_store":[' + FOREIGN_ENTRY.replace("${BASE}", str(base)) + ']'
            + ',"draft_ids":0,"untouched":' + UNTOUCHED + "}"
        )
        index_path.write_text(index_before, encoding="utf-8")
        settings = Settings(data_root=base / "runtime", media_roots=(MEDIA_DIR,))
        projects = ProjectService(settings)
        project = projects.create_project(ProjectCreate(name="原生草稿 Native"))
        tracks = {}
        for kind in ("video", "audio", "text"):
            project = projects.create_track(project.id, TrackCreate(track_type=kind))
            tracks[kind] = project.tracks[-1]["id"]
        project = projects.add_media_segment(
            project.id,
            tracks["video"],
            MediaSegmentCreate(
                path=str(MEDIA_DIR / "clip-720p.mp4"),
                timeline={"start_us": 0, "duration_us": 2_000_000},
                source={"start_us": 0, "duration_us": 2_000_000},
            ),
        )
        project = projects.add_media_segment(
            project.id,
            tracks["video"],
            MediaSegmentCreate(path=str(MEDIA_DIR / "still.png"), timeline={"start_us": 2_000_000, "duration_us": 1_000_000}),
        )
        project = projects.add_media_segment(
            project.id,
            tracks["audio"],
            MediaSegmentCreate(
                path=str(MEDIA_DIR / "tone.mp3"),
                timeline={"start_us": 0, "duration_us": 1_000_000},
                source={"start_us": 0, "duration_us": 1_000_000},
            ),
        )
        project = projects.add_text_segment(
            project.id,
            tracks["text"],
            TextSegmentCreate(text="你好剪映", timeline={"start_us": 0, "duration_us": 1_500_000}),
        )
        media = [
            {
                "path": str(info.path),
                "kind": info.kind,
                "duration_us": info.duration_us,
                "width": info.width,
                "height": info.height,
                "audio": info.audio,
                "video": info.video,
            }
            for info in projects._media_infos(project)
        ]
        project_v1 = project.model_dump(mode="json")

        box = DraftBoxService(
            index_path=index_path,
            drafts_root=root,
            drafts_root_source="test",
            app_running_check=lambda: False,
        )
        created = box.create_draft(project.id, project_builder=projects.build_draft)
        target = Path(created["path"])
        index_raw_after_create = index_path.read_text(encoding="utf-8")
        meta_after_create = json.loads((target / "draft_meta_info.json").read_text(encoding="utf-8"))
        owner = json.loads((target / ".jianying_ai_owned.json").read_text(encoding="utf-8"))
        files_after_create = listing(target)

        (target / "custom-extra.bin").write_bytes(b"keep auxiliary data")
        timelines = target / "Timelines"
        main_file = timelines / FLOW_MAIN_TIMELINE / "draft_content.json"
        main_file.parent.mkdir(parents=True)
        main_file.write_bytes(b"opaque native main timeline")
        main_file.with_suffix(".json.bak").write_bytes(b"opaque native main timeline backup")
        (timelines / "project.json").write_text(
            json.dumps({"main_timeline_id": FLOW_MAIN_TIMELINE, "timelines": [{"id": FLOW_MAIN_TIMELINE}]}),
            encoding="utf-8",
        )
        project = projects.update_project(project.id, ProjectUpdate(name="修复后的草稿 Repaired"))
        project_v2 = project.model_dump(mode="json")

        repaired = box.repair_draft(project.id, created["id"], project_builder=projects.build_draft)
        (token,) = [path.name for path in (timelines / ".jianying_ai_repair_backups").iterdir()]
        repaired_content = json.loads((target / "draft_content.json").read_text(encoding="utf-8"))
        timeline_content = json.loads(main_file.read_text(encoding="utf-8"))
        index_raw_after_repair = index_path.read_text(encoding="utf-8")
        flow = {
            "draft_id": FLOW_DRAFT_ID,
            "main_timeline_id": FLOW_MAIN_TIMELINE,
            "project_v1": project_v1,
            "project_v2": project_v2,
            "media": media,
            "index_before": index_before,
            "foreign_entry": FOREIGN_ENTRY,
            "untouched": dumps_index(json.loads(UNTOUCHED)),
            "created": created,
            "index_raw_after_create": index_raw_after_create,
            "index_after_create": json.loads(index_raw_after_create),
            "meta_after_create": meta_after_create,
            "owner": owner,
            "files_after_create": files_after_create,
            "repaired": repaired,
            "index_raw_after_repair": index_raw_after_repair,
            "index_after_repair": json.loads(index_raw_after_repair),
            "meta_after_repair": json.loads((target / "draft_meta_info.json").read_text(encoding="utf-8")),
            "files_after_repair": listing(target, token),
            "timeline_matches_content": timeline_content == {**repaired_content, "id": FLOW_MAIN_TIMELINE},
            "timeline_backup_matches_main": main_file.with_suffix(".json.bak").read_bytes() == main_file.read_bytes(),
            "root_entries_after_repair": sorted(path.name for path in root.iterdir()),
        }
        return normalize(flow, str(base))


def casefold_cases() -> dict:
    chars = {}
    for code in range(0x110000):
        if 0xD800 <= code <= 0xDFFF:
            continue
        char = chr(code)
        if char.casefold() != char.lower():
            chars[f"{code:x}"] = char.casefold()
    strings = ["ΟΔΟΣ", "ΣΑΣ σας", "Straße STRASSE ẞ", "ﬁle ﬂow ﬃ", "İstanbul ıi", "ᾈᾼῼ ᾳ", "Ꭰꭰᏸ", "µſ ͅ", "Ǆǅǆ", "草稿 Ünïcode"]
    return {"chars": chars, "strings": strings, "folded": [value.casefold() for value in strings]}


def index_round_trips() -> list:
    raws = [
        FOREIGN_ENTRY,
        UNTOUCHED,
        '{"a":1,"a":2,"b":3,"a":4}',
        '{"10":1,"2":2,"a":3,"-1":4,"01":5}',
        ' { "x" : [ 1 , 2.50 , 1E5 , 1e-5 , 0.0001 , 1e16 , 1e15 , 123456789012345678901234567890 ] }\n',
        '[1.0,-0,-0.0,0.0,5e-324,1.7976931348623157e308,9007199254740993,-9007199254740993]',
        '["\\u00e9\\u2028\\/\\b\\f\\n\\r\\t\\u0000\\u001f\\"\\\\","\\ud83d\\ude00","é"]',
        "[1e400]",
        "[-1e-400]",
        '{"x":' + "1" * 4300 + "}",
        '{"x":' + "1" * 4301 + "}",
    ]
    cases = []
    for raw in raws:
        try:
            cases.append({"raw": raw, "python": dumps_index(json.loads(raw))})
        except ValueError:
            cases.append({"raw": raw, "python": None})
    return cases


def main() -> None:
    cases = json.loads((FIXTURES / "index-cases.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as raw_base:
        base = Path(os.path.realpath(raw_base))
        build_layout(base, cases["layout"])
        index_path = base / "index" / "root_meta_info.json"
        index_path.write_text(json.dumps(substitute(cases["index"], str(base)), ensure_ascii=False), encoding="utf-8")
        service = DraftBoxService(
            index_path=index_path,
            drafts_root=base / "drafts",
            drafts_root_source="environment",
            app_running_check=lambda: False,
        )
        missing = DraftBoxService(
            index_path=base / "absent" / "root_meta_info.json",
            drafts_root=base / "drafts",
            drafts_root_source="environment",
        )
        oracle = {
            "flow": run_flow(),
            "status": capture(service.status, str(base)),
            "status_missing_index": capture(missing.status, str(base)),
            "lists": [
                capture(lambda case=case: service.list_drafts(**substitute(case, str(base))), str(base))
                for case in cases["lists"]
            ],
            "gets": [capture(lambda draft_id=draft_id: service.get_draft(draft_id), str(base)) for draft_id in cases["gets"]],
            "timestamps": [module._utc_timestamp(value) for value in cases["timestamps"]],
            "records": [
                capture(
                    lambda case=case: module._new_index_record(
                        FLOW_DRAFT_ID,
                        "Record sample",
                        base / "drafts" / FLOW_DRAFT_ID.replace("-", ""),
                        base / "drafts",
                        case["content"],
                    ),
                    str(base),
                )
                for case in cases["records"]
            ],
            "casefold": casefold_cases(),
            "index_round_trips": index_round_trips(),
        }
    output = FIXTURES / "oracle.json"
    output.write_text(json.dumps(oracle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output}", file=sys.stderr)


if __name__ == "__main__":
    main()

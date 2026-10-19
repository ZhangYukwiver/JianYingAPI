from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import uuid
from contextlib import nullcontext
from pathlib import Path

import pytest
from conftest import LocalTestClient as TestClient

from jianying_service.app import create_app
from jianying_service.config import Settings
from jianying_service import draft_box as draft_box_module
from jianying_service.draft_box import DraftBoxService
from jianying_service.mcp_server import create_server
from jianying_service.models import ProjectCreate
from jianying_service.service import ProjectService


def write_index(
    root: Path,
    *,
    index_parent: Path | None = None,
    drafts: list[dict] | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    index_directory = index_parent or root.parent / "index"
    index_path = index_directory / "root_meta_info.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    values = drafts if drafts is not None else [
        {
            "draft_id": "DRAFT_A",
            "draft_name": "Alpha",
            "draft_fold_path": str(root / "Alpha"),
            "draft_root_path": str(root),
            "tm_draft_create": 1_700_000_000_000_000,
            "tm_draft_modified": 1_700_000_002_000_000,
            "tm_duration": 2_500_000,
            "draft_timeline_materials_size": 4096,
            "draft_is_invisible": False,
            "draft_is_cloud_temp_draft": False,
        },
        {
            "draft_id": "DRAFT_B",
            "draft_name": "Beta",
            "draft_fold_path": str(root / "Beta"),
            "draft_root_path": str(root),
            "tm_draft_create": 1_700_000_000_000_000,
            "tm_draft_modified": 1_700_000_002_000_000,
            "tm_duration": 1_000_000,
            "draft_timeline_materials_size": 1024,
        },
        {
            "draft_id": "DRAFT_C",
            "draft_name": "Gamma",
            "draft_fold_path": str(root / "Gamma"),
            "draft_root_path": str(root),
            "tm_draft_create": 1_700_000_001_000_000,
            "tm_draft_modified": 1_700_000_003_000_000,
            "tm_duration": 0,
            "draft_timeline_materials_size": 0,
        },
    ]
    index_path.write_text(
        json.dumps(
            {
                "root_path": str(index_directory),
                "all_draft_store": values,
                "draft_ids": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return index_path


@pytest.fixture
def box(tmp_path: Path) -> tuple[DraftBoxService, Path]:
    root = tmp_path / "drafts"
    index_path = write_index(root)
    (root / "Alpha").mkdir()
    (root / "Alpha" / "draft_content.json").write_text("{}", encoding="utf-8")
    (root / "Alpha" / "draft_meta_info.json").write_text("{}", encoding="utf-8")
    (root / "Beta").mkdir()
    (root / "Gamma").mkdir()
    return (
        DraftBoxService(
            index_path=index_path,
            drafts_root=root,
            drafts_root_source="environment",
        ),
        root,
    )


def test_status_and_list_are_bounded_with_create_support(box) -> None:
    service, root = box
    status = service.status()
    assert status["status"] == "available"
    assert status["read_only"] is False
    assert status["operations"]["create"] == "available"
    assert status["operations"]["write_support"] == "create_and_repair_owned"
    assert status["operations"]["repair"] == "owned_draft_only"
    assert status["operations"]["edit"] == "unsupported_encrypted"
    assert status["draft_count"] == 3
    assert status["timeline_content"] == "unsupported_encrypted"

    client = TestClient(create_app(draft_box=service))
    listed = client.get("/api/draft-box", params={"limit": 2, "offset": 1})
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 3
    assert [item["id"] for item in body["items"]] == ["DRAFT_A", "DRAFT_B"]
    assert client.get("/api/draft-box/DRAFT_A").json()["draft"]["files_present"] == {
        "draft_content.json": True,
        "draft_meta_info.json": True,
    }
    assert client.get("/api/draft-box", params={"limit": 201}).status_code == 422
    assert client.get("/api/draft-box/status").json() == status
    assert client.get(f"/api/draft-box/{root.name}").status_code == 404
    assert (root / "Alpha" / "draft_content.json").read_text(encoding="utf-8") == "{}"


def test_stable_sorting_search_and_detail(box) -> None:
    service, _ = box
    client = TestClient(create_app(draft_box=service))
    first = client.get("/api/draft-box").json()["items"]
    second = client.get("/api/draft-box").json()["items"]
    assert [item["id"] for item in first] == ["DRAFT_C", "DRAFT_A", "DRAFT_B"]
    assert first == second
    match = client.get("/api/draft-box", params={"query": "alpha"}).json()
    assert [item["id"] for item in match["items"]] == ["DRAFT_A"]
    detail = client.get("/api/draft-box/DRAFT_A").json()["draft"]
    assert detail["name"] == "Alpha"
    assert detail["created_at"] == "2023-11-15T06:13:20+08:00"
    assert detail["duration_us"] == 2_500_000
    assert detail["timeline_content"] == {
        "status": "unsupported",
        "reason": "encrypted",
    }
    assert "draft_is_cloud_temp_draft" not in detail
    assert "cloud_user_id" not in detail


def test_invalid_missing_and_malicious_draft_id(box) -> None:
    service, _ = box
    client = TestClient(create_app(draft_box=service))
    missing = client.get("/api/draft-box/NOT_FOUND")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "DRAFT_BOX_NOT_FOUND"
    for draft_id in ("%2e%2e", "bad%00id", "%3Cscript%3E"):
        response = client.get(f"/api/draft-box/{draft_id}")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "DRAFT_BOX_INVALID_ID"


def test_unavailable_corrupt_and_out_of_bounds_index(tmp_path: Path) -> None:
    root = tmp_path / "drafts"
    root.mkdir()
    missing_client = TestClient(
        create_app(
            draft_box=DraftBoxService(
                index_path=tmp_path / "missing.json", drafts_root=root
            )
        )
    )
    assert missing_client.get("/api/draft-box/status").json()["reason"] == "index_not_found"
    assert missing_client.get("/api/draft-box").status_code == 404

    index = tmp_path / "broken.json"
    index.write_text("{", encoding="utf-8")
    broken_client = TestClient(
        create_app(
            draft_box=DraftBoxService(index_path=index, drafts_root=root)
        )
    )
    assert broken_client.get("/api/draft-box/status").status_code == 500
    assert broken_client.get("/api/draft-box").json()["error"]["code"] == "DRAFT_BOX_INDEX_INVALID"

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_draft = outside / "DRAFT_OUT"
    outside_draft.mkdir()
    index = write_index(
        root,
        drafts=[
            {
                "draft_id": "DRAFT_OUT",
                "draft_name": "Outside",
                "draft_fold_path": str(outside_draft),
                "draft_root_path": str(root),
            }
        ],
    )
    bounds_client = TestClient(
        create_app(
            draft_box=DraftBoxService(index_path=index, drafts_root=root)
        )
    )
    body = bounds_client.get("/api/draft-box").json()
    assert body["items"] == []
    assert body["index"]["skipped_count"] == 1


def test_permission_and_json_errors_do_not_leak_os_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    index = tmp_path / "root_meta_info.json"
    index.write_text("{}", encoding="utf-8")
    root = tmp_path / "drafts"
    root.mkdir()

    def denied(_: Path, *args, **kwargs):
        raise PermissionError("secret-user-path")

    monkeypatch.setattr(Path, "open", denied)
    service = DraftBoxService(index_path=index, drafts_root=root)
    client = TestClient(create_app(draft_box=service))
    status = client.get("/api/draft-box/status")
    assert status.status_code == 200
    assert status.json()["status"] == "unavailable"
    assert status.json()["reason"] == "index_permission_denied"
    response = client.get("/api/draft-box")
    assert response.status_code == 503
    assert "secret-user-path" not in response.text


def test_iso_timestamps_and_symlink_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "drafts"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "draft_content.json").write_text("{}", encoding="utf-8")
    (root / "DRAFT_LINK").symlink_to(outside, target_is_directory=True)
    index = write_index(
        root,
        drafts=[
            {
                "draft_id": "DRAFT_LINK",
                "draft_name": "Linked",
                "draft_fold_path": str(root / "DRAFT_LINK"),
                "draft_root_path": str(root),
                "tm_draft_modified": "2026-10-18 22:34:42",
            }
        ],
    )
    service = DraftBoxService(index_path=index, drafts_root=root)
    assert service.list_drafts()["items"] == []

    (root / "DRAFT_LINK").unlink()
    (root / "DRAFT_LINK").mkdir()
    record_index = write_index(
        root,
        drafts=[
            {
                "draft_id": "DRAFT_LINK",
                "draft_name": "Linked",
                "draft_fold_path": str(root / "DRAFT_LINK"),
                "draft_root_path": str(root),
                "tm_draft_modified": "2026-10-18 22:34:42",
            }
        ],
    )
    record = DraftBoxService(
        index_path=record_index, drafts_root=root
    ).get_draft("DRAFT_LINK")["draft"]
    assert record["modified_at"] == "2026-10-18T22:34:42+08:00"


def test_mcp_draft_box_tools_share_service(box) -> None:
    service, _ = box
    server = create_server(draft_box=service)
    tools = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {
        "get_draft_box_status",
        "list_draft_box",
        "get_draft_box_draft",
    } <= tools

    def decode(result):
        return json.loads(result.content[0].text)

    status = decode(asyncio.run(server.call_tool("get_draft_box_status", {})))
    assert status["status"] == "available"
    listed = decode(
        asyncio.run(server.call_tool("list_draft_box", {"query": "beta"}))
    )
    assert [item["id"] for item in listed["items"]] == ["DRAFT_B"]
    detail = decode(
        asyncio.run(
            server.call_tool("get_draft_box_draft", {"draft_id": "DRAFT_B"})
        )
    )
    assert detail["draft"]["name"] == "Beta"


@pytest.mark.parametrize("with_native_timeline", [False, True])
def test_create_draft_box_entry_writes_and_reads_new_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_native_timeline: bool
) -> None:
    from jianying_service import draft_box

    root = tmp_path / "drafts"
    index_path = write_index(root, index_parent=tmp_path / "index")
    (root / "Alpha").mkdir()
    (root / "Alpha" / "draft_content.json").write_text("{}", encoding="utf-8")
    (root / "Alpha" / "draft_meta_info.json").write_text("{}", encoding="utf-8")
    (root / "Beta").mkdir()
    (root / "Gamma").mkdir()
    index_document = json.loads(index_path.read_text(encoding="utf-8"))
    index_document["draft_ids"] = 0
    index_document["untouched"] = {"keep": ["all", "original", "fields"]}
    index_path.write_text(json.dumps(index_document), encoding="utf-8")
    original_bytes = index_path.read_bytes()
    before_records = [
        item["draft_id"] for item in index_document["all_draft_store"]
    ]

    settings = Settings(
        data_root=tmp_path / "runtime",
        media_roots=(tmp_path / "media",),
    )
    projects = ProjectService(settings)
    app = create_app(service=projects, settings=settings)
    client = TestClient(app)
    created_project = client.post(
        "/api/projects", json={"name": "Native new draft"}
    ).json()["project"]
    assert client.post(
        f"/api/projects/{created_project['id']}/tracks",
        json={"track_type": "text"},
    ).status_code == 200
    track_id = client.get(
        f"/api/projects/{created_project['id']}"
    ).json()["project"]["tracks"][0]["id"]
    assert client.post(
        f"/api/projects/{created_project['id']}/tracks/{track_id}/text",
        json={
            "text": "Preserved text",
            "timeline": {"start_us": 0, "duration_us": 1_000_000},
        },
    ).status_code == 200
    box_service = DraftBoxService(
        index_path=index_path,
        drafts_root=root,
        drafts_root_source="test",
        app_running_check=lambda: False,
    )
    app.state.draft_box = box_service
    response = client.post(
        f"/api/projects/{created_project['id']}/draft-box"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "written"
    assert body["source"] == "jianying_index"
    assert body["read_only"] is False
    assert body["jianying_ui_verified"] is False
    draft_id = body["id"]
    assert str(uuid.UUID(draft_id)).casefold() == draft_id.casefold()
    assert body["name"] == "Native new draft"
    target = Path(body["path"])
    assert target == root / draft_id.replace("-", "")
    assert not draft_id.startswith("DRAFT_")
    content = json.loads(
        (target / "draft_content.json").read_text(encoding="utf-8")
    )
    metadata = json.loads(
        (target / "draft_meta_info.json").read_text(encoding="utf-8")
    )
    assert content["id"] == draft_id
    assert metadata["draft_id"] == draft_id
    assert content["name"] == "Native new draft"
    assert (target / "project.json").is_file()
    assert (target / ".jianying_ai_owned.json").is_file()
    assert body["draft"]["draft_root_path"] == str(root)
    assert body["draft"]["draft_fold_path"] == str(target)
    assert body["draft"]["draft_json_file"] == str(
        target / "draft_content.json"
    )

    updated_index = json.loads(index_path.read_text(encoding="utf-8"))
    assert updated_index["draft_ids"] == 0
    assert updated_index["untouched"] == {"keep": ["all", "original", "fields"]}
    assert [item["draft_id"] for item in updated_index["all_draft_store"][:-1]] == before_records
    added = updated_index["all_draft_store"][-1]
    assert added["draft_id"] == draft_id
    assert added["draft_name"] == "Native new draft"
    assert added["tm_draft_cloud_entry_id"] == -1
    assert added["tm_draft_cloud_space_id"] == -1
    assert added["tm_draft_cloud_user_id"] == -1
    assert added["draft_is_invisible"] is False
    assert added["streaming_edit_draft_ready"] is True
    assert "cloud_user_id" not in {
        key for key in added if not key.startswith("tm_draft_cloud_")
    }
    assert index_path.read_bytes() != original_bytes

    listed = client.get(
        "/api/draft-box", params={"query": "Native new draft"}
    ).json()
    assert [item["id"] for item in listed["items"]] == [draft_id]
    detail = client.get(f"/api/draft-box/{draft_id}").json()["draft"]
    assert detail["name"] == "Native new draft"
    assert detail["files_present"] == {
        "draft_content.json": True,
        "draft_meta_info.json": True,
    }
    assert detail["files_present"] != added
    assert (
        client.get("/api/capabilities").json()["drafts"]["write_support"]
        == "create_and_repair_owned"
    )

    legacy_target = settings.drafts_root / hashlib.sha256(
        f"draft\0{created_project['id']}".encode()
    ).hexdigest()[:32].upper()
    assert not legacy_target.exists()

    old_content = (target / "draft_content.json").read_bytes()
    auxiliary = target / "custom-extra.bin"
    auxiliary.write_bytes(b"keep auxiliary data")
    main_id = str(uuid.uuid4()).upper()
    timelines = target / "Timelines"
    main_file = timelines / main_id / "draft_content.json"
    old_native_content = b"opaque native main timeline"
    old_native_backup = b"opaque native main timeline backup"
    manifest = {"main_timeline_id": main_id, "timelines": [{"id": main_id}]}
    if with_native_timeline:
        main_file.parent.mkdir(parents=True)
        main_file.write_bytes(old_native_content)
        main_file.with_suffix(".json.bak").write_bytes(old_native_backup)
        (timelines / "project.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (timelines / "other-timeline").mkdir()
        (timelines / "other-timeline" / "draft_content.json").write_bytes(
            b"keep other timeline"
        )
        (main_file.parent / "native-extra.bin").write_bytes(b"keep timeline auxiliary")
    index_with_extra = json.loads(index_path.read_text(encoding="utf-8"))
    original_record = next(
        item
        for item in index_with_extra["all_draft_store"]
        if item["draft_id"] == draft_id
    )
    original_record["future_jianying_field"] = {"keep": True}
    index_path.write_text(
        json.dumps(index_with_extra, ensure_ascii=False), encoding="utf-8"
    )
    old_index = index_path.read_bytes()
    old_created_at = added["tm_draft_create"]
    assert client.patch(
        f"/api/projects/{created_project['id']}",
        json={"name": "Native repaired draft"},
    ).status_code == 200

    original_builder = projects.build_draft

    def fail_build(*_args):
        raise draft_box.ServiceError(
            "DRAFT_BUILD_FAILED", "simulated build failure", status=500
        )

    monkeypatch.setattr(projects, "build_draft", fail_build)
    failed_build = client.put(
        f"/api/projects/{created_project['id']}/draft-box/{draft_id}"
    )
    assert failed_build.status_code == 500
    assert failed_build.json()["error"]["code"] == "DRAFT_BUILD_FAILED"
    assert (target / "draft_content.json").read_bytes() == old_content
    assert auxiliary.read_bytes() == b"keep auxiliary data"
    assert index_path.read_bytes() == old_index
    if with_native_timeline:
        assert main_file.read_bytes() == old_native_content
        assert main_file.with_suffix(".json.bak").read_bytes() == old_native_backup
    assert not any(path.name.endswith(".repair") for path in root.iterdir())
    monkeypatch.setattr(projects, "build_draft", original_builder)

    original_replace = draft_box._atomic_replace_index

    def fail_index_write(_path: Path, _encoded: bytes) -> None:
        raise draft_box.ServiceError(
            "DRAFT_BOX_INDEX_WRITE_FAILED",
            "simulated index failure",
            status=500,
        )

    monkeypatch.setattr(draft_box, "_atomic_replace_index", fail_index_write)
    failed = client.put(
        f"/api/projects/{created_project['id']}/draft-box/{draft_id}"
    )
    assert failed.status_code == 500, failed.text
    assert failed.json()["error"]["code"] == "DRAFT_BOX_INDEX_WRITE_FAILED"
    assert (target / "draft_content.json").read_bytes() == old_content
    assert index_path.read_bytes() == old_index
    if with_native_timeline:
        assert main_file.read_bytes() == old_native_content
        assert main_file.with_suffix(".json.bak").read_bytes() == old_native_backup
        assert not (timelines / ".jianying_ai_repair_backups").exists()
    assert not any(
        path.name.endswith((".repair", ".backup")) for path in root.iterdir()
    )

    monkeypatch.setattr(draft_box, "_atomic_replace_index", original_replace)
    repaired = client.put(
        f"/api/projects/{created_project['id']}/draft-box/{draft_id}"
    )
    assert repaired.status_code == 200, repaired.text
    repaired_body = repaired.json()
    assert repaired_body["status"] == "repaired"
    assert repaired_body["id"] == draft_id
    assert repaired_body["path"] == str(target)
    assert repaired_body["verification"]["draft_id_preserved"] is True
    assert repaired_body["verification"]["draft_path_preserved"] is True
    repaired_content = json.loads(
        (target / "draft_content.json").read_text(encoding="utf-8")
    )
    assert repaired_content["id"] == draft_id
    assert repaired_content["name"] == "Native repaired draft"
    repaired_metadata = json.loads(
        (target / "draft_meta_info.json").read_text(encoding="utf-8")
    )
    assert repaired_metadata["draft_fold_path"] == str(target)
    assert repaired_metadata["draft_root_path"] == str(root)
    assert auxiliary.read_bytes() == b"keep auxiliary data"
    assert repaired_body["verification"]["main_timeline_updated"] is with_native_timeline
    if with_native_timeline:
        assert repaired_body["verification"]["main_timeline_path"] == str(
            main_file.relative_to(target)
        )
        timeline_content = json.loads(main_file.read_text(encoding="utf-8"))
        assert timeline_content == {**repaired_content, "id": main_id}
        assert main_file.with_suffix(".json.bak").read_bytes() == main_file.read_bytes()
        assert json.loads((timelines / "project.json").read_text(encoding="utf-8")) == manifest
        assert (timelines / "other-timeline" / "draft_content.json").read_bytes() == b"keep other timeline"
        assert (main_file.parent / "native-extra.bin").read_bytes() == b"keep timeline auxiliary"
        snapshots = list((timelines / ".jianying_ai_repair_backups").iterdir())
        assert len(snapshots) == 1
        assert (snapshots[0] / "draft_content.json").read_bytes() == old_native_content
        assert (snapshots[0] / "draft_content.json.bak").read_bytes() == old_native_backup
        assert json.loads((snapshots[0] / "project.json").read_text(encoding="utf-8")) == manifest
    else:
        assert repaired_body["verification"]["main_timeline_path"] is None
        assert not timelines.exists()
    repaired_index = json.loads(index_path.read_text(encoding="utf-8"))
    repaired_matches = [
        item
        for item in repaired_index["all_draft_store"]
        if item["draft_id"] == draft_id
    ]
    assert len(repaired_matches) == 1
    assert repaired_matches[0]["draft_fold_path"] == str(target)
    assert repaired_matches[0]["draft_name"] == "Native repaired draft"
    assert repaired_matches[0]["tm_draft_create"] == old_created_at
    assert repaired_matches[0]["future_jianying_field"] == {"keep": True}
    assert len(repaired_index["all_draft_store"]) == 4
    assert (
        client.get("/api/capabilities").json()["drafts"]["write_support"]
        == "create_and_repair_owned"
    )


@pytest.mark.parametrize("manifest", [
    [],
    {"main_timeline_id": "../outside", "timelines": [{"id": "../outside"}]},
    {"main_timeline_id": "870F8B67-3D2F-441C-BDE0-760C8529A2DE", "timelines": []},
    {"main_timeline_id": "870F8B67-3D2F-441C-BDE0-760C8529A2DE", "timelines": [
        {"id": "870F8B67-3D2F-441C-BDE0-760C8529A2DE"},
        {"id": "870F8B67-3D2F-441C-BDE0-760C8529A2DE"},
    ]},
])
def test_main_timeline_repair_rejects_invalid_manifest(tmp_path: Path, manifest) -> None:
    source = tmp_path / "source"
    staged = tmp_path / "staged"
    timelines = source / "Timelines"
    timelines.mkdir(parents=True)
    manifest_path = timelines / "project.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = manifest_path.read_bytes()

    with pytest.raises(draft_box_module.ServiceError) as error:
        draft_box_module._stage_main_timeline_repair(source, staged, {}, "test")

    assert error.value.code == "DRAFT_BOX_TIMELINE_INVALID"
    assert manifest_path.read_bytes() == original
    assert not staged.exists()


def test_repair_rejects_unowned_indexed_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_id = str(uuid.uuid4()).upper()
    root = tmp_path / "drafts"
    target = root / draft_id.replace("-", "")
    index_path = write_index(
        root,
        index_parent=tmp_path / "index",
        drafts=[
            {
                "draft_id": draft_id,
                "draft_name": "Foreign draft",
                "draft_fold_path": str(target),
            }
        ],
    )
    target.mkdir()
    foreign_file = target / "draft_content.json"
    foreign_file.write_text("keep this content", encoding="utf-8")
    original_index = index_path.read_bytes()
    monkeypatch.setattr(draft_box_module, "_windows_write_supported", lambda: True)
    monkeypatch.setattr(
        draft_box_module, "_index_file_lock", lambda _path: nullcontext()
    )
    service = DraftBoxService(
        index_path=index_path,
        drafts_root=root,
        app_running_check=lambda: False,
    )
    client = TestClient(create_app(draft_box=service))

    response = client.put(
        f"/api/projects/unrelated-project/draft-box/{draft_id}"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DRAFT_BOX_NOT_OWNED"
    assert foreign_file.read_text(encoding="utf-8") == "keep this content"
    assert index_path.read_bytes() == original_index
    assert {path.name for path in root.iterdir()} == {target.name}


def test_create_guard_running_and_non_windows(
    box, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jianying_service import draft_box

    service, root = box
    service._app_running_check = lambda: True
    index_bytes = service.index_path.read_bytes()  # type: ignore[union-attr]

    def must_not_build(*args):
        raise AssertionError("project must not be built while app is running")

    service._project_builder = must_not_build
    client = TestClient(create_app(draft_box=service))
    response = client.post("/api/projects/not-a-project/draft-box")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DRAFT_BOX_APP_RUNNING"
    assert response.json()["error"]["details"]["action"] == "close_jianying_and_retry"
    assert service.index_path.read_bytes() == index_bytes  # type: ignore[union-attr]
    assert not any(root.glob("*-*-*-*-*"))

    service._app_running_check = lambda: False
    monkeypatch.setattr(draft_box, "_windows_write_supported", lambda: False)
    unavailable = DraftBoxService(
        index_path=service.index_path,
        drafts_root=root,
        app_running_check=lambda: False,
    )
    unavailable._project_builder = must_not_build
    response = TestClient(create_app(draft_box=unavailable)).post(
        "/api/projects/not-a-project/draft-box"
    )
    assert response.status_code == 503
    assert response.json()["error"]["details"]["reason"] == "platform_unsupported"


def test_cleanup_ownership_keeps_foreign_folder(tmp_path: Path) -> None:
    foreign = tmp_path / "ABC"
    foreign.mkdir()
    (foreign / ".jianying_ai_owned.json").write_text(
        json.dumps(
            {"draft_id": "OTHER", "generator": "jianying-ai-service"}
        ),
        encoding="utf-8",
    )
    draft_box_module._remove_owned_directory(foreign, "ABC")
    assert foreign.is_dir()
    assert (foreign / ".jianying_ai_owned.json").is_file()

    original = foreign / "foreign_file.txt"
    original.write_text("leave alone", encoding="utf-8")
    draft_box_module._remove_owned_directory(foreign, "OTHER")
    assert original.read_text(encoding="utf-8") == "leave alone"
    assert foreign.is_dir()


def test_unowned_build_output_is_not_cleaned_and_no_fallback_path(box) -> None:
    service, root = box
    service._app_running_check = lambda: False
    index_bytes = service.index_path.read_bytes()  # type: ignore[union-attr]
    original_names = {path.name for path in root.iterdir()}
    fallback = root / "drafts"
    fallback.mkdir()
    foreign = fallback / "foreign.txt"
    foreign.write_text("preserve malformed native folder", encoding="utf-8")
    target_names = original_names | {"drafts"}
    seen_id: list[str] = []

    def failing_builder(project_id: str, target: Path, draft_id: str):
        seen_id.append(draft_id)
        target.mkdir()
        (target / "partial.txt").write_text("partial", encoding="utf-8")
        class IncompleteProject:
            name = "Incomplete builder result"
            id = project_id

        return IncompleteProject(), target, b"", {"id": draft_id}

    with pytest.raises(
        draft_box_module.ServiceError,
        match="Generated draft content has an invalid identity",
    ):
        service.create_draft("missing-project", project_builder=failing_builder)
    assert len(seen_id) == 1
    assert {path.name for path in root.iterdir()} == target_names | {
        seen_id[0].replace("-", "")
    }
    assert foreign.read_text(encoding="utf-8") == "preserve malformed native folder"
    assert service.index_path.read_bytes() == index_bytes  # type: ignore[union-attr]


def test_owned_build_failure_cleans_new_folder_without_fallback(
    box, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = box
    service._app_running_check = lambda: False
    index_bytes = service.index_path.read_bytes()  # type: ignore[union-attr]
    monkeypatch.setattr(
        service, "_check_app_stopped", lambda: None
    )
    fallback = root / "drafts"
    fallback.mkdir()
    (fallback / "foreign.txt").write_text("preserve", encoding="utf-8")

    def failing_builder(_project_id: str, target: Path, draft_id: str):
        target.mkdir()
        (target / ".jianying_ai_owned.json").write_text(
            json.dumps(
                {
                    "draft_id": draft_id,
                    "project_id": "missing-project",
                    "generator": "jianying-ai-service",
                }
            ),
            encoding="utf-8",
        )
        calls_during_build.append(service._app_running_check())
        raise RuntimeError("stop after ownership marker creation")

    calls_during_build: list[bool] = []
    with pytest.raises(RuntimeError):
        service.create_draft("missing-project", project_builder=failing_builder)
    assert {path.name for path in root.iterdir()} == {
        "Alpha",
        "Beta",
        "Gamma",
        "drafts",
    }
    assert (fallback / "foreign.txt").read_text(encoding="utf-8") == "preserve"
    assert service.index_path.read_bytes() == index_bytes  # type: ignore[union-attr]
    assert calls_during_build == [False]


def test_index_failure_preserves_published_owned_folder(
    box, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = box
    service._app_running_check = lambda: False
    index_bytes = service.index_path.read_bytes()  # type: ignore[union-attr]

    def failing_replace(_path: Path, _encoded: bytes) -> None:
        raise draft_box_module.ServiceError(
            "DRAFT_BOX_INDEX_WRITE_FAILED",
            "simulated index failure",
            status=500,
        )

    monkeypatch.setattr(draft_box_module, "_atomic_replace_index", failing_replace)

    def builder(_project_id: str, target: Path, draft_id: str):
        target.mkdir()
        (target / ".jianying_ai_owned.json").write_text(
            json.dumps(
                {
                    "draft_id": draft_id,
                    "generator": "jianying-ai-service",
                }
            ),
            encoding="utf-8",
        )
        for name in (
            "draft_content.json",
            "draft_meta_info.json",
            "project.json",
        ):
            (target / name).write_text("{}", encoding="utf-8")
        content = {
            "id": draft_id,
            "name": "Published before index failure",
            "create_time": 1_700_000_000,
            "update_time": 1_700_000_000,
            "duration": 0,
            "materials": {},
        }

        class Project:
            name = "Published before index failure"
            id = "project"

        return Project(), target, b"{}", content

    with pytest.raises(draft_box_module.ServiceError):
        service.create_draft("project", project_builder=builder)

    published = [
        path
        for path in root.iterdir()
        if path.name not in {"Alpha", "Beta", "Gamma"}
    ]
    assert len(published) == 1
    assert (published[0] / ".jianying_ai_owned.json").is_file()
    assert service.index_path.read_bytes() == index_bytes  # type: ignore[union-attr]


def test_index_conflict_cleans_only_new_owned_directory(tmp_path: Path) -> None:
    root = tmp_path / "drafts"
    index_path = write_index(root, index_parent=tmp_path / "index")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["draft_ids"] = 0
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=4), encoding="utf-8"
    )
    for name in ("Alpha", "Beta", "Gamma"):
        (root / name).mkdir()

    settings = Settings(
        data_root=tmp_path / "runtime",
        media_roots=(tmp_path / "media",),
    )
    projects = ProjectService(settings)
    project = projects.create_project(ProjectCreate(name="Conflict"))
    existing = root / "DO_NOT_REMOVE"
    existing.mkdir()
    marker = existing / "foreign.txt"
    marker.write_text("foreign", encoding="utf-8")

    changes = 0
    original_names = {
        "Alpha",
        "Beta",
        "Gamma",
        "DO_NOT_REMOVE",
    }
    assert "drafts" not in original_names
    assert {path.name for path in root.iterdir()} == original_names

    def conflict_after_write():
        nonlocal changes
        if changes == 3:
            changed = json.loads(index_path.read_text(encoding="utf-8"))
            changed["all_draft_store"][0]["draft_name"] = "Changed by Jianying"
            changed["draft_ids"] = 0
            conflicting = index_path.with_name(f".{index_path.name}.competing")
            conflicting.write_text(
                json.dumps(changed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(conflicting, index_path)
        changes += 1
        return False

    service = DraftBoxService(
        index_path=index_path,
        drafts_root=root,
        app_running_check=conflict_after_write,
        project_builder=projects.build_draft,
    )
    response = TestClient(create_app(service=projects, draft_box=service)).post(
        f"/api/projects/{project.id}/draft-box"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DRAFT_BOX_INDEX_CONFLICT"
    raw_index = index_path.read_bytes()
    current = json.loads(raw_index.decode("utf-8"))
    assert current["draft_ids"] == 0
    assert current["all_draft_store"][0]["draft_name"] == "Changed by Jianying"
    assert len(current["all_draft_store"]) == 3
    assert marker.read_text(encoding="utf-8") == "foreign"
    assert {path.name for path in root.iterdir()} == original_names
    assert index_path.read_bytes() == raw_index
    assert not any(path.name.startswith(".") for path in root.iterdir())
    assert not [
        path
        for path in index_path.parent.iterdir()
        if path.name.endswith(".tmp") or path.name.endswith(".competing")
    ]


def test_invalid_project_and_corrupt_index_do_not_create_folders(
    box, tmp_path: Path
) -> None:
    service, root = box
    service._app_running_check = lambda: False
    projects = ProjectService(
        Settings(data_root=tmp_path / "runtime", media_roots=(tmp_path / "media",))
    )
    service._project_builder = projects.build_draft
    project = projects.create_project(ProjectCreate(name="Corrupt index"))
    client = TestClient(create_app(service=projects, draft_box=service))
    response = client.post("/api/projects/not-a-project/draft-box")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    assert len(list(root.iterdir())) == 3

    service.index_path.write_text("{", encoding="utf-8")  # type: ignore[union-attr]
    response = client.post(f"/api/projects/{project.id}/draft-box")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "DRAFT_BOX_INDEX_INVALID"
    assert len(list(root.iterdir())) == 3
    assert service.index_path.read_text(encoding="utf-8") == "{"  # type: ignore[union-attr]


def test_mcp_create_draft_box_entry_uses_shared_write_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "drafts"
    index_path = write_index(root, index_parent=tmp_path / "index")
    (root / "Alpha").mkdir()
    (root / "Beta").mkdir()
    (root / "Gamma").mkdir()
    projects = ProjectService(
        Settings(data_root=tmp_path / "runtime", media_roots=(tmp_path / "media",))
    )
    project = projects.create_project(ProjectCreate(name="MCP draft box"))
    box_service = DraftBoxService(
        index_path=index_path,
        drafts_root=root,
        app_running_check=lambda: False,
    )
    server = create_server(
        service=projects,
        draft_box=box_service,
    )
    tools = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "create_draft_box_entry" in tools
    result = asyncio.run(
        server.call_tool(
            "create_draft_box_entry",
            {"project_id": project.id},
        )
    )
    body = json.loads(result.content[0].text)
    assert body["ok"] is True
    assert body["status"] == "written"
    assert body["draft"]["draft_name"] == "MCP draft box"
    assert body["jianying_ui_verified"] is False
    assert box_service.get_draft(body["id"])["draft"]["name"] == "MCP draft box"
    repair_result = asyncio.run(
        server.call_tool(
            "repair_draft_box_entry",
            {"project_id": project.id, "draft_id": body["id"]},
        )
    )
    repaired = json.loads(repair_result.content[0].text)
    assert repaired["status"] == "repaired", repaired
    assert repaired["id"] == body["id"]
    assert repaired["path"] == body["path"]


def test_environment_root_and_registry_are_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jianying_service import draft_box

    local = tmp_path / "local"
    root = tmp_path / "custom"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("JY_JIANYING_DRAFTS_ROOT", str(root))
    discovery = draft_box.discover_draft_box(registry_reader=lambda: "ignored")
    assert discovery.index_path == local / draft_box.INDEX_RELATIVE_PATH
    assert discovery.drafts_root == root
    assert discovery.drafts_root_source == "environment"

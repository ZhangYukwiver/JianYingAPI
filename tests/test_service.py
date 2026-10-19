from __future__ import annotations

import json
import hashlib
import shutil
from pathlib import Path

import pyJianYingDraft
import pytest
from conftest import LocalTestClient as TestClient

from jianying_service.app import create_app
from jianying_service.config import Settings
from jianying_service.draft import publish_directory
from jianying_service.errors import ServiceError, ValidationFailure
from jianying_service.media import MediaInfo, MediaResolver
from jianying_service.models import (
    MAX_TIME_US,
    MediaSegmentCreate,
    ProjectCreate,
    TimeRange,
    TrackCreate,
)
from jianying_service.sample_media import create_samples
from jianying_service.service import ProjectService, parse_srt, parse_srt_interval

from conftest import create_project, create_track


def test_project_persists_across_service_restart(settings: Settings) -> None:
    first = ProjectService(settings)
    created = first.create_project(ProjectCreate(name="Restart test"))
    updated = first.add_track(
        created.id, TrackCreate(track_type="video")
    )
    second = ProjectService(settings)
    restored = second.get_project(created.id)
    assert restored.name == "Restart test"
    assert restored.revision == updated.revision
    assert restored.tracks[0]["track_type"] == "video"


def test_time_boundary_accepts_exact_day_but_rejects_overflow() -> None:
    assert TimeRange(start_us=0, duration_us=86_400_000_000).end_us == 86_400_000_000
    with pytest.raises(ValueError):
        TimeRange(start_us=1, duration_us=86_400_000_000)
    with pytest.raises(ValueError):
        TimeRange(duration_us=0)


def test_media_resolver_rejects_path_outside_whitelist(
    settings: Settings, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not parsed because the path is outside the whitelist")
    resolver = MediaResolver(
        settings.media_roots, settings.ffprobe, settings.max_media_bytes
    )
    with pytest.raises(ValidationFailure) as caught:
        resolver.resolve(str(outside))
    assert caught.value.code == "PATH_OUTSIDE_WHITELIST"


def test_http_health_and_revision_query(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"ok": True, "status": "ok"}

    project = create_project(client, "Revision query")
    revision = project["revision"]
    updated = client.post(
        f"/api/projects/{project['id']}/tracks",
        params={"expect_revision": revision},
        json={"track_type": "text"},
    )
    assert updated.status_code == 200, updated.text
    stale = client.post(
        f"/api/projects/{project['id']}/tracks",
        params={"expect_revision": revision},
        json={"track_type": "audio"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PROJECT_CONFLICT"


def test_http_rejects_invalid_time_with_structured_error(client: TestClient) -> None:
    project = create_project(client)
    project = create_track(client, project, "text")
    track_id = project["tracks"][0]["id"]
    response = client.post(
        f"/api/projects/{project['id']}/tracks/{track_id}/text",
        json={
            "text": "Boundary",
            "timeline": {"start_us": 1, "duration_us": 86_400_000_000},
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(
        item["path"] == "body.timeline"
        for item in body["error"]["details"]["errors"]
    )


def test_srt_parser_preserves_unicode_and_rejects_bad_interval() -> None:
    content = (
        "\ufeff1\r\n00:00:00,000 --> 00:00:01,250\r\n"
        "本地字幕\r\n\r\n2\r\n00:00:01.250 --> 00:00:02.500\r\nSecond line"
    )
    entries = parse_srt(content)
    assert [(entry.start_us, entry.end_us) for entry in entries] == [
        (0, 1_250_000),
        (1_250_000, 2_500_000),
    ]
    assert entries[0].text == "本地字幕"
    assert parse_srt_interval(
        "99:59:59,999 --> 100:00:00,000"
    ) == (359_999_999_000, 360_000_000_000)
    with pytest.raises(ValidationFailure):
        parse_srt_interval("00:00:02,000 --> 00:00:01,999")
    with pytest.raises(ValidationFailure):
        parse_srt("1\n00:00:01,000 --> 00:00:02,000\n\n")


def test_add_srt_creates_unicode_subtitle_track(client: TestClient) -> None:
    project = create_project(client)
    srt = "1\n00:00:00,000 --> 00:00:01,000\n中文字幕\n"
    response = client.post(
        f"/api/projects/{project['id']}/subtitles",
        content=srt.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        params={"group_id": "1" * 32},
    )
    assert response.status_code == 200, response.text
    track = response.json()["project"]["tracks"][0]
    assert track["kind"] == "subtitle"
    assert track["segments"][0]["text"] == "中文字幕"
    second = client.post(
        f"/api/projects/{project['id']}/subtitles",
        content=srt.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        params={"group_id": "2" * 32},
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["project"]["tracks"]) == 2


def test_http_success_and_download_is_not_exposed(client: TestClient) -> None:
    project = create_project(client, "Success")
    listed = client.get("/api/projects", params={"limit": 1})
    assert listed.status_code == 200
    assert listed.json()["projects"][0]["id"] == project["id"]
    assert client.get(f"/api/projects/{project['id']}").status_code == 200
    with_revision = client.post(
        f"/api/projects/{project['id']}/tracks",
        params={"expect_revision": project["revision"]},
        json={"track_type": "text"},
    )
    assert with_revision.status_code == 200, with_revision.text
    assert with_revision.json()["project"]["revision"] == project["revision"] + 1
    assert client.get("/api/projects/not-a-project").status_code == 404
    export = client.post(
        f"/api/projects/{project['id']}/export", json={"filename": "demo.mp4"}
    )
    assert export.status_code == 200
    assert export.json()["status"] == "unsupported"
    assert export.json()["unsupported"] is True


def test_draft_is_written_and_existing_draft_is_not_overwritten(
    settings: Settings, tmp_path: Path
) -> None:
    media = tmp_path / "media"
    image, _ = create_samples(media)
    service = ProjectService(settings, media=FakeMedia(image))
    client = TestClient(create_app(service=service))
    project = create_project(client, "Draft")
    project = create_track(client, project, "video")
    response = client.post(
        f"/api/projects/{project['id']}/tracks/{project['tracks'][0]['id']}/media",
        json={
            "path": str(media / "sample.png"),
            "timeline": {"start_us": 0, "duration_us": 1_000_000},
        },
    )
    assert response.status_code == 200, response.text
    first = client.post(f"/api/projects/{project['id']}/draft")
    assert first.status_code == 200, first.text
    draft = Path(first.json()["draft"]["path"])
    assert draft.parent == settings.drafts_root
    assert (draft / ".owned").read_bytes() == project["id"].encode("ascii")
    content = json.loads((draft / "draft_content.json").read_text(encoding="utf-8"))
    assert content["name"] == "Draft"
    assert content["canvas_config"]["width"] == 1920
    assert pyJianYingDraft.ScriptFile._load_template(  # type: ignore[attr-defined]
        str(draft / "draft_content.json")
    ).content["name"] == "Draft"
    before = (draft / "draft_content.json").read_bytes()
    second = client.post(f"/api/projects/{project['id']}/draft")
    assert second.status_code == 200, second.text
    assert (draft / "draft_content.json").read_bytes() == before
    assert second.json()["draft"]["content_sha256"] == first.json()["draft"]["content_sha256"]


def test_draft_collision_is_rejected_without_overwrite(
    settings: Settings, tmp_path: Path
) -> None:
    media = tmp_path / "media"
    image, _ = create_samples(media)
    service = ProjectService(settings, media=FakeMedia(image))
    client = TestClient(create_app(service=service))
    project = create_project(client, "Collision")
    project = create_track(client, project, "video")
    response = client.post(
        f"/api/projects/{project['id']}/tracks/{project['tracks'][0]['id']}/media",
        json={
            "path": str(media / "sample.png"),
            "timeline": {"start_us": 0, "duration_us": 1_000_000},
        },
    )
    assert response.status_code == 200, response.text
    target = settings.drafts_root / hashlib.sha256(
        f"draft\0{project['id']}".encode()
    ).hexdigest()[:32].upper()
    target.mkdir(parents=True)
    marker = target / "foreign.txt"
    marker.write_text("leave this file alone", encoding="utf-8")
    response = client.post(f"/api/projects/{project['id']}/draft")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DRAFT_EXISTS"
    assert marker.read_text(encoding="utf-8") == "leave this file alone"


def test_draft_publication_never_replaces_a_directory(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    target = tmp_path / "drafts"
    staged.mkdir()
    target.mkdir()
    (staged / "new-draft.json").write_text("new", encoding="utf-8")
    (target / "foreign.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ServiceError) as error:
        publish_directory(staged, target)

    assert error.value.code == "DRAFT_EXISTS"
    assert (staged / "new-draft.json").read_text(encoding="utf-8") == "new"
    assert (target / "foreign.txt").read_text(encoding="utf-8") == "keep"


class FakeMedia(MediaResolver):
    def __init__(self, image: Path) -> None:
        self.image = image

    def probe(self, raw: str) -> MediaInfo:
        return MediaInfo(
            path=self.image.resolve(),
            kind="image",
            duration_us=MAX_TIME_US,
            width=1280,
            height=720,
            audio=False,
            video=True,
        )


@pytest.mark.skipif(
    shutil.which("ffprobe") is None,
    reason="ffprobe is not available on PATH",
)
def test_real_sample_media_probe(settings: Settings, tmp_path: Path) -> None:
    image, audio = create_samples(tmp_path / "media")
    resolver = MediaResolver(settings.media_roots, settings.ffprobe, settings.max_media_bytes)
    image_info = resolver.probe(str(image))
    audio_info = resolver.probe(str(audio))
    assert (image_info.kind, image_info.width, image_info.height) == (
        "image",
        1280,
        720,
    )
    assert audio_info.kind == "audio"
    assert audio_info.duration_us >= 3_000_000

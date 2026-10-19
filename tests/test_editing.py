from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import LocalTestClient as TestClient
from pyJianYingDraft import IntroType, TextIntro

from jianying_service.app import create_app
from jianying_service.config import Settings
from jianying_service.media import MediaInfo, MediaResolver
from jianying_service.sample_media import create_samples
from jianying_service.service import ProjectService


class SampleMedia(MediaResolver):
    def __init__(self, image: Path, audio: Path) -> None:
        self.image = image.resolve()
        self.audio = audio.resolve()

    def probe(self, raw: str) -> MediaInfo:
        path = Path(raw).resolve()
        if path == self.audio:
            return MediaInfo(
                path=path,
                kind="audio",
                duration_us=3_000_000,
                width=None,
                height=None,
                audio=True,
                video=False,
            )
        if path == self.image:
            return MediaInfo(
                path=path,
                kind="image",
                duration_us=86_400_000_000,
                width=1280,
                height=720,
                audio=False,
                video=True,
            )
        raise AssertionError("unexpected media path")


@pytest.fixture
def editing_client(settings: Settings, tmp_path: Path) -> tuple[TestClient, Path, Path]:
    image, audio = create_samples(tmp_path / "media")
    service = ProjectService(settings, media=SampleMedia(image, audio))
    return TestClient(create_app(service=service)), image, audio


def create_project(client: TestClient, name: str = "Editing") -> dict:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["project"]


def add_clip(
    client: TestClient,
    project: dict,
    track_type: str,
    path: Path,
    *,
    timeline: dict | None = None,
    source: dict | None = None,
    speed: float | None = None,
) -> tuple[dict, dict]:
    project = client.post(
        f"/api/projects/{project['id']}/tracks", json={"track_type": track_type}
    ).json()["project"]
    track = project["tracks"][-1]
    body = {
        "path": str(path),
        "timeline": timeline or {"start_us": 0, "duration_us": 3_000_000},
    }
    if source is not None:
        body["source"] = source
    if speed is not None:
        body["speed"] = speed
    response = client.post(
        f"/api/projects/{project['id']}/tracks/{track['id']}/media", json=body
    )
    assert response.status_code == 200, response.text
    project = response.json()["project"]
    return project, project["tracks"][-1]["segments"][-1]


def test_keyframes_split_and_save_to_native_draft_json(editing_client) -> None:
    client, image, _ = editing_client
    project = create_project(client)
    project, segment = add_clip(client, project, "video", image)
    project_id, segment_id = project["id"], segment["id"]

    keyed = client.post(
        f"/api/projects/{project_id}/segments/{segment_id}/keyframes",
        json={
            "keyframes": [
                {"property": "position_x", "offset_us": 0, "value": 0.0},
                {"property": "position_x", "offset_us": 3_000_000, "value": 0.9},
            ]
        },
    )
    assert keyed.status_code == 200, keyed.text
    uniform_scale = client.post(
        f"/api/projects/{project_id}/segments/{segment_id}/keyframes",
        json={
            "keyframes": [
                {"property": "uniform_scale", "offset_us": 0, "value": 1.0},
                {"property": "uniform_scale", "offset_us": 3_000_000, "value": 1.5},
            ]
        },
    )
    assert uniform_scale.status_code == 200, uniform_scale.text
    conflict = client.post(
        f"/api/projects/{project_id}/segments/{segment_id}/keyframes",
        json={
            "keyframes": [
                {"property": "scale_x", "offset_us": 0, "value": 1.0},
            ]
        },
    )
    assert conflict.status_code == 422
    listed = client.get(
        f"/api/projects/{project_id}/segments/{segment_id}/keyframes"
    ).json()["keyframes"]
    assert [item["property"] for item in listed].count("transform_x") == 2
    assert [item["property"] for item in listed].count("uniform_scale") == 2

    split = client.post(
        f"/api/projects/{project_id}/segments/{segment_id}/split",
        json={"offset_us": 1_000_000},
    )
    assert split.status_code == 200, split.text
    clips = split.json()["segments"]
    assert [clip["timeline"]["duration_us"] for clip in clips] == [1_000_000, 2_000_000]
    assert clips[0]["source"]["duration_us"] == 1_000_000
    assert clips[1]["source"]["start_us"] == 1_000_000
    left = next(
        item for item in clips[0]["keyframes"]
        if item["property"] == "transform_x" and item["offset_us"] == 1_000_000
    )
    right = next(
        item for item in clips[1]["keyframes"]
        if item["property"] == "transform_x" and item["offset_us"] == 0
    )
    assert left["value"] == pytest.approx(0.3)
    assert right["value"] == pytest.approx(0.3)

    saved = client.post(f"/api/projects/{project_id}/draft")
    assert saved.status_code == 200, saved.text
    content_path = Path(saved.json()["draft"]["path"]) / "draft_content.json"
    content = json.loads(content_path.read_text(encoding="utf-8"))
    serialized = [segment for track in content["tracks"] for segment in track["segments"]]
    assert len(serialized) == 2
    assert all(item["common_keyframes"] for item in serialized)
    assert all(item["common_keyframes"][0]["property_type"] == "KFTypePositionX" for item in serialized)
    assert all(item["uniform_scale"]["on"] for item in serialized)
    assert all(
        any(kf["property_type"] == "KFTypeScaleX" for kf in item["common_keyframes"])
        for item in serialized
    )


def test_speed_update_retimes_audio_and_new_draft_contains_edit(editing_client) -> None:
    client, _, audio = editing_client
    project = create_project(client)
    project, segment = add_clip(
        client,
        project,
        "audio",
        audio,
        timeline={"start_us": 0, "duration_us": 1_000_000},
        source={"start_us": 0, "duration_us": 2_000_000},
        speed=2.0,
    )
    project_id, segment_id = project["id"], segment["id"]
    first = client.post(f"/api/projects/{project_id}/draft").json()["draft"]

    update = client.patch(
        f"/api/projects/{project_id}/segments/{segment_id}",
        json={"speed": 1.0},
    )
    assert update.status_code == 200, update.text
    changed = update.json()["project"]["tracks"][-1]["segments"][0]
    assert changed["timeline"]["duration_us"] == 2_000_000
    second = client.post(f"/api/projects/{project_id}/draft").json()["draft"]
    assert first["path"] != second["path"]
    content = json.loads(
        (Path(second["path"]) / "draft_content.json").read_text(encoding="utf-8")
    )
    serialized = content["tracks"][-1]["segments"][0]
    assert serialized["speed"] == 1.0
    assert serialized["target_timerange"]["duration"] == 2_000_000


def test_effect_catalog_and_video_features_serialize(editing_client) -> None:
    client, image, _ = editing_client
    options = client.get("/api/editing/options")
    assert options.status_code == 200
    names = options.json()["options"]
    assert "ABG" in names["filters"]
    project = create_project(client)
    project, segment = add_clip(client, project, "video", image)
    project_id, segment_id = project["id"], segment["id"]

    for suffix, body in [
        ("filters", {"filter_type": "ABG", "intensity": 50}),
        ("fade", {"in_duration_us": 100_000, "out_duration_us": 100_000}),
        ("transition", {"transition_type": names["transitions"][0], "duration_us": 100_000}),
    ]:
        response = client.post(
            f"/api/projects/{project_id}/segments/{segment_id}/{suffix}", json=body
        )
        assert response.status_code == 200, response.text

    saved = client.post(f"/api/projects/{project_id}/draft")
    assert saved.status_code == 200, saved.text
    content = json.loads(
        (Path(saved.json()["draft"]["path"]) / "draft_content.json").read_text(encoding="utf-8")
    )
    serialized = content["tracks"][-1]["segments"][0]
    assert serialized["common_keyframes"] == []
    assert serialized["extra_material_refs"]
    assert len(content["materials"]["effects"]) == 1
    assert len(content["materials"]["transitions"]) == 1
    assert len(content["materials"]["audio_fades"]) == 1


def test_effect_and_filter_tracks_serialize_and_support_timeline_edits(editing_client) -> None:
    client, _, _ = editing_client
    options = client.get("/api/editing/options").json()["options"]
    project = create_project(client, "Track effects")
    effect_track = client.post(
        f"/api/projects/{project['id']}/tracks",
        json={"track_type": "effect", "name": "Global effects"},
    )
    assert effect_track.status_code == 200, effect_track.text
    project = effect_track.json()["project"]
    effect_track_id = project["tracks"][-1]["id"]
    filter_track = client.post(
        f"/api/projects/{project['id']}/tracks",
        json={"track_type": "filter", "name": "Global filters"},
    )
    assert filter_track.status_code == 200, filter_track.text
    project = filter_track.json()["project"]
    filter_track_id = project["tracks"][-1]["id"]

    effect = client.post(
        f"/api/projects/{project['id']}/tracks/{effect_track_id}/effects",
        json={
            "effect_type": options["video_effects"][0],
            "timeline": {"start_us": 0, "duration_us": 2_000_000},
        },
    )
    assert effect.status_code == 200, effect.text
    effect_segment_id = effect.json()["project"]["tracks"][-2]["segments"][0]["id"]
    moved = client.post(
        f"/api/projects/{project['id']}/segments/{effect_segment_id}/move",
        json={"start_us": 500_000},
    )
    assert moved.status_code == 200, moved.text
    split = client.post(
        f"/api/projects/{project['id']}/segments/{effect_segment_id}/split",
        json={"offset_us": 1_000_000},
    )
    assert split.status_code == 200, split.text
    assert len(split.json()["segments"]) == 2

    filter_response = client.post(
        f"/api/projects/{project['id']}/tracks/{filter_track_id}/filters",
        json={
            "filter_type": options["filters"][0],
            "intensity": 60,
            "timeline": {"start_us": 0, "duration_us": 3_000_000},
        },
    )
    assert filter_response.status_code == 200, filter_response.text
    saved = client.post(f"/api/projects/{project['id']}/draft")
    assert saved.status_code == 200, saved.text
    content = json.loads(
        (Path(saved.json()["draft"]["path"]) / "draft_content.json").read_text(encoding="utf-8")
    )
    effect_track_json = next(track for track in content["tracks"] if track["type"] == "effect")
    filter_track_json = next(track for track in content["tracks"] if track["type"] == "filter")
    effect_material_ids = {item["id"] for item in content["materials"]["video_effects"]}
    filter_material_ids = {
        item["id"]
        for item in content["materials"]["effects"]
        if item.get("type") != "mix_mode"
    }
    assert len(effect_track_json["segments"]) == 2
    assert all(item["material_id"] in effect_material_ids for item in effect_track_json["segments"])
    assert len(filter_track_json["segments"]) == 1
    assert filter_track_json["segments"][0]["material_id"] in filter_material_ids


def test_crop_mask_chroma_background_and_mix_mode_serialize(editing_client) -> None:
    client, image, _ = editing_client
    options = client.get("/api/editing/options").json()["options"]
    project = create_project(client)
    project, segment = add_clip(
        client,
        project,
        "video",
        image,
        timeline={"start_us": 0, "duration_us": 2_000_000},
    )
    project_id, segment_id = project["id"], segment["id"]
    crop = client.patch(
        f"/api/projects/{project_id}/segments/{segment_id}",
        json={"crop": {"upper_left_x": 0.1, "upper_right_x": 0.9}},
    )
    assert crop.status_code == 200, crop.text
    updated_transform = client.patch(
        f"/api/projects/{project_id}/segments/{segment_id}",
        json={"transform": {
            "scale_x": 1.2,
            "rotation": 15,
            "alpha": 0.7,
            "flip_horizontal": True,
            "flip_vertical": True,
        }},
    )
    assert updated_transform.status_code == 200, updated_transform.text
    for suffix, body in [
        ("mask", {"mask_type": options["mask_types"][0], "size": 0.7}),
        ("chroma", {"color": "#00FF00FF", "intensity": 50}),
        ("background-filling", {"fill_type": "blur", "blur": 0.375}),
    ]:
        response = client.post(
            f"/api/projects/{project_id}/segments/{segment_id}/{suffix}", json=body
        )
        assert response.status_code == 200, response.text
    mix = client.put(
        f"/api/projects/{project_id}/segments/{segment_id}/mix-mode",
        json={"mode": options["mix_modes"][0]},
    )
    assert mix.status_code == 200, mix.text

    saved = client.post(f"/api/projects/{project_id}/draft")
    assert saved.status_code == 200, saved.text
    content = json.loads(
        (Path(saved.json()["draft"]["path"]) / "draft_content.json").read_text(encoding="utf-8")
    )
    serialized = content["tracks"][-1]["segments"][0]
    refs = set(serialized["extra_material_refs"])
    assert any(item["id"] in refs for item in content["materials"]["masks"])
    assert any(item["id"] in refs for item in content["materials"]["chromas"])
    assert any(item["id"] in refs for item in content["materials"]["canvases"])
    assert any(item["id"] in refs for item in content["materials"]["effects"])
    crop_material = content["materials"]["videos"][0]
    assert crop_material["crop"]["upper_left_x"] == 0.1
    assert serialized["clip"]["rotation"] == 15
    assert serialized["clip"]["alpha"] == 0.7
    assert serialized["clip"]["flip"] == {"horizontal": True, "vertical": True}


@pytest.mark.parametrize("with_local_resource", [False, True])
def test_animation_catalog_search_and_apply_serializes_resource_reference(editing_client, with_local_resource) -> None:
    client, image, _ = editing_client
    response = client.get(
        "/api/animations/catalog",
        params={"query": "动感放大", "kind": "video_intro"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    item = body["items"][0]
    metadata = IntroType.动感放大.value
    assert body["total"] == 1
    assert item["animation_type"] == "动感放大"
    assert item["resource_id"] == metadata.resource_id
    assert item["effect_id"] == metadata.effect_id
    assert item["md5"] == metadata.md5
    assert item["default_duration_us"] == metadata.duration
    assert body["resource_handling"]["download"] == "jianying_client_attempts_on_draft_open"
    assert body["resource_handling"]["local_path"] == "auto_resolved_from_media_roots"
    resource = image.parent / "animation_resources" / metadata.effect_id / metadata.md5
    if with_local_resource:
        resource.mkdir(parents=True)
        (resource / "config.json").write_text(
            json.dumps({"effect": {"Link": [{"type": "video"}]}}), encoding="utf-8"
        )

    project = create_project(client)
    project, segment = add_clip(client, project, "video", image)
    applied = client.post(
        f"/api/projects/{project['id']}/segments/{segment['id']}/animations",
        json={
            "animation_type": item["animation_type"],
            "duration_us": item["default_duration_us"],
        },
    )
    assert applied.status_code == 200, applied.text
    saved = client.post(f"/api/projects/{project['id']}/draft")
    assert saved.status_code == 200, saved.text
    content = json.loads(
        (Path(saved.json()["draft"]["path"]) / "draft_content.json").read_text(encoding="utf-8")
    )
    animation_group = content["materials"]["material_animations"][0]
    animation = animation_group["animations"][0]
    assert animation["id"] == item["effect_id"]
    assert animation["resource_id"] == item["resource_id"]
    assert animation["duration"] == item["default_duration_us"]
    if with_local_resource:
        assert animation["path"] == resource.resolve().as_posix()
    else:
        assert "path" not in animation


def test_text_animation_uses_local_package_and_rejects_incomplete_cache(editing_client) -> None:
    client, image, _ = editing_client
    project = create_project(client)
    project = client.post(
        f"/api/projects/{project['id']}/tracks", json={"track_type": "text"}
    ).json()["project"]
    track_id = project["tracks"][-1]["id"]
    project = client.post(
        f"/api/projects/{project['id']}/tracks/{track_id}/text",
        json={"text": "剧终", "timeline": {"start_us": 0, "duration_us": 2_000_000}},
    ).json()["project"]
    segment_id = project["tracks"][-1]["segments"][0]["id"]
    added = client.post(
        f"/api/projects/{project['id']}/segments/{segment_id}/animations",
        json={"animation_type": "水墨晕开", "duration_us": 2_000_000},
    )
    assert added.status_code == 200, added.text
    metadata = TextIntro.水墨晕开.value
    resource = image.parent / "animation_resources" / metadata.effect_id / metadata.md5
    resource.mkdir(parents=True)
    config = resource / "config.json"
    failed = client.post(f"/api/projects/{project['id']}/draft")
    assert failed.status_code == 422, failed.text
    assert failed.json()["error"]["code"] == "ANIMATION_RESOURCE_INVALID"
    config.write_text(json.dumps({"effect": {"Link": [{"type": "InfoSticker"}]}}), encoding="utf-8")
    saved = client.post(f"/api/projects/{project['id']}/draft")
    assert saved.status_code == 200, saved.text
    previous = Path(saved.json()["draft"]["path"]) / "draft_content.json"
    animation = json.loads(previous.read_bytes())["materials"]["material_animations"][0]["animations"][0]
    assert animation["path"] == resource.resolve().as_posix()
    assert animation["panel"] == "text"
    assert animation["resource_id"] == metadata.resource_id
    assert animation["duration"] == 2_000_000


def test_text_style_and_text_animation_serialize(editing_client) -> None:
    client, _, _ = editing_client
    options = client.get("/api/editing/options").json()["options"]
    project = create_project(client)
    track_response = client.post(
        f"/api/projects/{project['id']}/tracks", json={"track_type": "text"}
    )
    assert track_response.status_code == 200, track_response.text
    project = track_response.json()["project"]
    track_id = project["tracks"][-1]["id"]
    text = client.post(
        f"/api/projects/{project['id']}/tracks/{track_id}/text",
        json={
            "text": "片段标题",
            "timeline": {"start_us": 0, "duration_us": 2_000_000},
            "bold": True,
            "italic": True,
            "color": "#33AAFF",
            "border": {"color": "#112233", "width": 20},
            "background": {"color": "#00000088", "alpha": 0.5},
            "shadow": {"color": "#000000", "diffuse": 10},
            "bubble": {"effect_id": "bubble-effect-a", "resource_id": "bubble-resource-a"},
            "text_effect_id": "text-effect-a",
        },
    )
    assert text.status_code == 200, text.text
    project = text.json()["project"]
    segment_id = project["tracks"][-1]["segments"][0]["id"]
    style_update = client.patch(
        f"/api/projects/{project['id']}/segments/{segment_id}",
        json={
            "border": {"color": "#445566", "width": 25},
            "background": {"color": "#112233FF", "alpha": 0.75},
            "shadow": {"color": "#000000", "diffuse": 20},
            "bubble": {"effect_id": "bubble-effect-b", "resource_id": "bubble-resource-b"},
            "text_effect_id": "text-effect-b",
        },
    )
    assert style_update.status_code == 200, style_update.text
    animation = client.post(
        f"/api/projects/{project['id']}/segments/{segment_id}/animations",
        json={"animation_type": options["text_intro_animations"][0], "duration_us": 500_000},
    )
    assert animation.status_code == 200, animation.text
    saved = client.post(f"/api/projects/{project['id']}/draft")
    assert saved.status_code == 200, saved.text
    content = json.loads(
        (Path(saved.json()["draft"]["path"]) / "draft_content.json").read_text(encoding="utf-8")
    )
    serialized = content["tracks"][-1]["segments"][0]
    text_material = content["materials"]["texts"][0]
    text_addons = [
        item for item in content["materials"]["effects"]
        if item.get("type") in {"text_shape", "text_effect"}
    ]
    addon_refs = {item["id"] for item in text_addons}
    assert addon_refs.issubset(set(serialized["extra_material_refs"]))
    assert {item.get("type") for item in text_addons} == {"text_shape", "text_effect"}
    assert {item.get("effect_id") for item in text_addons} == {
        "bubble-effect-b", "text-effect-b"
    }
    assert text_material["content"]
    text_content = json.loads(text_material["content"])
    assert text_content["text"] == "片段标题"
    style = text_content["styles"][0]
    assert style["bold"] is True
    assert style["strokes"][0]["width"] == pytest.approx(0.05)
    assert text_material["background_alpha"] == 0.75
    assert serialized["common_keyframes"] == []
    assert serialized["extra_material_refs"]
    assert client.delete(
        f"/api/projects/{project['id']}/segments/{segment_id}/features/bubble"
    ).status_code == 200
    assert client.delete(
        f"/api/projects/{project['id']}/segments/{segment_id}/features/text_effect"
    ).status_code == 200


def test_segment_edits_validate_overlap_speed_and_revision(editing_client) -> None:
    client, image, _ = editing_client
    project = create_project(client)
    project, first = add_clip(client, project, "video", image)
    project_id = project["id"]
    track_id = project["tracks"][-1]["id"]
    second = client.post(
        f"/api/projects/{project_id}/tracks/{track_id}/media",
        json={
            "path": str(image),
            "timeline": {"start_us": 3_000_000, "duration_us": 3_000_000},
        },
    )
    assert second.status_code == 200, second.text
    overlap = client.patch(
        f"/api/projects/{project_id}/segments/{first['id']}",
        json={"timeline": {"start_us": 2_000_000, "duration_us": 3_000_000}},
    )
    assert overlap.status_code == 422
    too_fast = client.post(
        f"/api/projects/{project_id}/tracks/{track_id}/media",
        json={
            "path": str(image),
            "timeline": {"start_us": 4_000_000, "duration_us": 1_000_000},
            "source": {"start_us": 0, "duration_us": 3_000_000},
            "speed": 2.0,
        },
    )
    assert too_fast.status_code == 422
    stale = client.delete(
        f"/api/projects/{project_id}/segments/{first['id']}",
        params={"expect_revision": 1},
    )
    assert stale.status_code == 409

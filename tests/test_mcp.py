from __future__ import annotations

import asyncio
import json
from pathlib import Path

from jianying_service.config import Settings
from jianying_service.mcp_server import create_server

from conftest import create_project, create_track
from test_service import create_samples


EXPECTED_TOOLS = {
    "add_animation",
    "add_effect",
    "add_fade",
    "add_filter",
    "add_keyframes",
    "add_marker",
    "add_mask",
    "add_chroma",
    "add_background_filling",
    "set_mix_mode",
    "health",
    "list_projects",
    "create_project",
    "get_project",
    "create_track",
    "add_media_clip",
    "add_text_clip",
    "add_track_effect",
    "add_track_filter",
    "add_track_effect",
    "add_track_filter",
    "add_srt",
    "save_draft",
    "create_draft_box_entry",
    "repair_draft_box_entry",
    "update_project",
    "set_canvas",
    "update_track",
    "delete_track",
    "reorder_tracks",
    "add_sticker_clip",
    "update_segment",
    "delete_segment",
    "move_segment",
    "split_segment",
    "list_keyframes",
    "delete_keyframe",
    "clear_segment_feature",
    "add_transition",
    "remove_subtitles",
    "list_markers",
    "remove_marker",
    "diagnose",
    "capabilities",
    "get_draft_box_status",
    "list_draft_box",
    "get_draft_box_draft",
    "editing_options",
    "search_animations",
    "export_project",
    "get_export_status",
    "get_material_store_status",
    "diagnose_material_store",
    "open_material_store",
    "get_material_store_state",
    "search_material_store",
    "browse_material_store",
    "get_sticker_library_state",
    "open_sticker_library",
    "search_sticker_library",
    "browse_sticker_library",
}


def decode_result(result: object) -> dict:
    content = getattr(result, "content")
    return json.loads(content[0].text)


def test_mcp_exposes_exact_tool_catalog_with_schemas(settings: Settings) -> None:
    server = create_server(settings=settings)
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    by_name = {tool.name: tool for tool in tools}
    assert by_name["create_project"].input_schema["properties"]["request"]["$ref"]
    assert by_name["add_media_clip"].input_schema["properties"]["request"]["$ref"]
    assert by_name["add_track_effect"].input_schema["properties"]["request"]["$ref"]
    assert by_name["add_track_filter"].input_schema["properties"]["request"]["$ref"]
    assert by_name["export_project"].input_schema["required"] == ["project_id"]
    assert by_name["search_sticker_library"].input_schema["required"] == ["query"]
    assert "kind" in by_name["search_animations"].input_schema["properties"]


def test_mcp_tool_call_creates_project_and_reports_export_limit(
    settings: Settings, client
) -> None:
    server = create_server(settings=settings)
    health = decode_result(asyncio.run(server.call_tool("health", {})))
    assert health == {"ok": True, "status": "ok"}
    created = asyncio.run(
        server.call_tool("create_project", {"request": {"name": "MCP project"}})
    )
    body = decode_result(created)
    assert body["ok"] is True
    assert body["project"]["name"] == "MCP project"
    project_id = body["project"]["id"]

    listed = decode_result(asyncio.run(server.call_tool("list_projects", {})))
    assert listed["projects"][0]["id"] == project_id
    export = decode_result(
        asyncio.run(
            server.call_tool("export_project", {"project_id": project_id})
        )
    )
    assert export["status"] == "unsupported"
    http_project = client.get(f"/api/projects/{project_id}")
    assert http_project.status_code == 200


def test_mcp_can_search_animation_catalog(settings: Settings) -> None:
    server = create_server(settings=settings)
    result = decode_result(
        asyncio.run(
            server.call_tool(
                "search_animations",
                {"query": "动感放大", "kind": "video_intro"},
            )
        )
    )
    assert result["ok"] is True
    assert result["total"] == 1
    assert result["items"][0]["animation_type"] == "动感放大"
    assert result["items"][0]["resource_id"]
    assert result["resource_handling"]["download"] == "jianying_client_attempts_on_draft_open"


def test_mcp_can_write_effect_and_filter_tracks(settings: Settings) -> None:
    server = create_server(settings=settings)
    project = decode_result(
        asyncio.run(
            server.call_tool("create_project", {"request": {"name": "MCP track effects"}})
        )
    )["project"]
    options = decode_result(asyncio.run(server.call_tool("editing_options", {})))["options"]

    effect_track = decode_result(
        asyncio.run(
            server.call_tool(
                "create_track",
                {"project_id": project["id"], "request": {"track_type": "effect"}},
            )
        )
    )["project"]["tracks"][-1]
    effect = decode_result(
        asyncio.run(
            server.call_tool(
                "add_track_effect",
                {
                    "project_id": project["id"],
                    "track_id": effect_track["id"],
                    "request": {
                        "effect_type": options["video_effects"][0],
                        "timeline": {"start_us": 0, "duration_us": 1_000_000},
                    },
                },
            )
        )
    )
    assert effect["project"]["tracks"][0]["segments"][0]["kind"] == "track_effect"

    filter_track = decode_result(
        asyncio.run(
            server.call_tool(
                "create_track",
                {"project_id": project["id"], "request": {"track_type": "filter"}},
            )
        )
    )["project"]["tracks"][-1]
    filter_result = decode_result(
        asyncio.run(
            server.call_tool(
                "add_track_filter",
                {
                    "project_id": project["id"],
                    "track_id": filter_track["id"],
                    "request": {
                        "filter_type": options["filters"][0],
                        "timeline": {"start_us": 0, "duration_us": 1_000_000},
                    },
                },
            )
        )
    )
    assert filter_result["project"]["tracks"][1]["segments"][0]["kind"] == "track_filter"


def test_mcp_sticker_library_tools_share_the_local_surface(
    settings: Settings,
    material_store,
) -> None:
    from jianying_service.material_store import MaterialStoreService

    server = create_server(
        settings=settings,
        material_store=MaterialStoreService(material_store),
    )
    result = decode_result(
        asyncio.run(server.call_tool("search_sticker_library", {"query": "花朵"}))
    )
    assert result["ok"] is True
    assert result["surface"] == "sticker_library"
    assert result["current_query"] == "花朵"
    assert material_store.calls[-1] == ("library", "search", "花朵")


def test_mcp_works_through_connected_client(settings: Settings) -> None:
    from mcp.client import Client

    server = create_server(settings=settings)

    async def exercise() -> None:
        async with Client(server, raise_exceptions=True) as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS
            result = await client.call_tool(
                "create_project", {"request": {"name": "Connected MCP"}}
            )
            assert decode_result(result)["project"]["name"] == "Connected MCP"

    asyncio.run(exercise())

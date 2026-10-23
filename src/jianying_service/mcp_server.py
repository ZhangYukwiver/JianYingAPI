from __future__ import annotations

import sys
from functools import update_wrapper
from inspect import Signature, signature
from typing import Annotated, Any, get_type_hints

from mcp.server.mcpserver import MCPServer
from pydantic import Field, RootModel, StrictInt

from .config import Settings
from .draft_box import DraftBoxService
from .draft import animation_catalog, animation_resource_handling, editing_options
from .errors import ServiceError
from .material_store import MaterialStoreService
from .models import (
    AnimationCreate,
    BackgroundFillCreate,
    CanvasUpdate,
    ChromaCreate,
    EffectCreate,
    ExportRequest,
    ExportStatus,
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
    TrackOrder,
    TrackCreate,
    TrackEffectCreate,
    TrackFilterCreate,
    TrackUpdate,
    TransitionCreate,
)
from .service import ProjectService
from .user_info import JianyingUserReader
from .membership import MembershipReader


OK = {"ok": True}
Revision = Annotated[StrictInt, Field(ge=1)]


class ToolResult(RootModel[dict[str, Any]]):
    pass


def ok(project: Project) -> dict[str, Any]:
    return {**OK, "project": project}


def export_status() -> dict[str, Any]:
    return {
        **OK,
        "status": ExportStatus.UNSUPPORTED,
        "needs_gui": False,
        "unsupported": True,
    }


def tool(fn: Any) -> Any:
    hints = get_type_hints(
        fn, localns={"ProjectCreate": ProjectCreate, "ToolResult": ToolResult}
    )
    parameters = [
        parameter.replace(annotation=hints.get(parameter.name, parameter.annotation))
        for parameter in signature(fn).parameters.values()
    ]

    def wrapped(*args: Any, **kwargs: Any) -> ToolResult:
        call_args = list(args)
        for parameter in parameters[len(call_args) :]:
            if parameter.name in kwargs:
                call_args.append(kwargs.pop(parameter.name))
            else:
                call_args.append(parameter.default)
        if kwargs:
            raise TypeError("Unexpected tool arguments")
        try:
            return ToolResult.model_validate(fn(*call_args))
        except ServiceError as error:
            return ToolResult.model_validate(error.as_dict())

    wrapped.__signature__ = Signature(parameters, return_annotation=ToolResult)
    wrapped.__annotations__ = {
        **{parameter.name: parameter.annotation for parameter in parameters},
        "return": ToolResult,
    }
    update_wrapper(wrapped, fn, updated=())
    return wrapped


def create_server(
    service: ProjectService | None = None,
    *,
    settings: Settings | None = None,
    material_store: MaterialStoreService | None = None,
    draft_box: DraftBoxService | None = None,
    user_info: JianyingUserReader | None = None,
    membership: MembershipReader | None = None,
) -> MCPServer:
    settings = settings or Settings.from_env()
    service = service or ProjectService(settings)
    material_store = material_store or MaterialStoreService()
    draft_box = draft_box or DraftBoxService()
    user_info = user_info or JianyingUserReader()
    membership = membership or MembershipReader()
    server = MCPServer(
        name="jianying-ai-service",
        version="0.1.0",
        description="Local Jianying draft project service",
    )

    @server.tool(structured_output=False, name="health")
    @tool
    def health() -> dict[str, Any]:
        """Check whether the local service process is ready."""
        return {**OK, "status": "ok"}

    @server.tool(structured_output=False, name="list_projects")
    @tool
    def list_projects(
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
        offset: Annotated[int, Field(ge=0, le=1_000_000)] = 0,
    ) -> dict[str, Any]:
        """List saved projects."""
        return {**OK, "projects": service.list_projects(limit=limit, offset=offset)}

    @server.tool(structured_output=False, name="create_project")
    @tool
    def create_project(request: ProjectCreate) -> dict[str, Any]:
        """Create a project and return it."""
        return ok(service.create_project(request))

    @server.tool(structured_output=False, name="get_project")
    @tool
    def get_project(project_id: str) -> dict[str, Any]:
        """Get one project with its tracks and segments."""
        return ok(service.get_project(project_id))

    @server.tool(structured_output=False, name="update_project")
    @tool
    def update_project(
        project_id: str,
        request: ProjectUpdate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Rename a project."""
        return ok(service.update_project(project_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="set_canvas")
    @tool
    def set_canvas(
        project_id: str,
        request: CanvasUpdate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Set canvas size and frame rate."""
        return ok(service.set_canvas(project_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="create_track")
    @tool
    def create_track(
        project_id: str,
        request: TrackCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Create a video, audio, text, sticker, effect, or filter track."""
        return ok(
            service.add_track(
                project_id, request, expect_revision=expect_revision
            )
        )

    @server.tool(structured_output=False, name="update_track")
    @tool
    def update_track(
        project_id: str,
        track_id: str,
        request: TrackUpdate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Rename or mute a track."""
        return ok(service.update_track(project_id, track_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="delete_track")
    @tool
    def delete_track(
        project_id: str,
        track_id: str,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Delete a track and its clips."""
        return ok(service.delete_track(project_id, track_id, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="reorder_tracks")
    @tool
    def reorder_tracks(
        project_id: str,
        request: TrackOrder,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Reorder all tracks."""
        return ok(service.reorder_tracks(project_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_media_clip")
    @tool
    def add_media_clip(
        project_id: str,
        track_id: str,
        request: MediaSegmentCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add an image, video, or audio clip to a track."""
        return ok(
            service.add_media_segment(
                project_id, track_id, request, expect_revision=expect_revision
            )
        )

    @server.tool(structured_output=False, name="add_text_clip")
    @tool
    def add_text_clip(
        project_id: str,
        track_id: str,
        request: TextSegmentCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add a text clip to a text track."""
        return ok(
            service.add_text_segment(
                project_id, track_id, request, expect_revision=expect_revision
            )
        )

    @server.tool(structured_output=False, name="add_sticker_clip")
    @tool
    def add_sticker_clip(
        project_id: str,
        track_id: str,
        request: StickerSegmentCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add a sticker resource to a video track."""
        return ok(service.add_sticker_segment(project_id, track_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_track_effect")
    @tool
    def add_track_effect(
        project_id: str,
        track_id: str,
        request: TrackEffectCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add a global video effect segment to an effect track."""
        return ok(
            service.add_track_effect_segment(
                project_id, track_id, request, expect_revision=expect_revision
            )
        )

    @server.tool(structured_output=False, name="add_track_filter")
    @tool
    def add_track_filter(
        project_id: str,
        track_id: str,
        request: TrackFilterCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add a filter segment to a filter track."""
        return ok(
            service.add_track_filter_segment(
                project_id, track_id, request, expect_revision=expect_revision
            )
        )

    @server.tool(structured_output=False, name="update_segment")
    @tool
    def update_segment(
        project_id: str,
        segment_id: str,
        request: SegmentUpdate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Update clip timing, source trim, speed, volume, transform or text style."""
        return ok(service.update_segment(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="delete_segment")
    @tool
    def delete_segment(
        project_id: str,
        segment_id: str,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Delete one clip."""
        return ok(service.delete_segment(project_id, segment_id, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="move_segment")
    @tool
    def move_segment(
        project_id: str,
        segment_id: str,
        request: SegmentMove,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Move a clip to a new timeline start."""
        return ok(service.move_segment(project_id, segment_id, request.start_us, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="split_segment")
    @tool
    def split_segment(
        project_id: str,
        segment_id: str,
        request: SegmentSplit,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Split a clip at an offset from its start."""
        project, segments = service.split_segment(
            project_id, segment_id, request.offset_us, expect_revision=expect_revision
        )
        return {**OK, "project": project, "segments": segments}

    @server.tool(structured_output=False, name="add_keyframes")
    @tool
    def add_keyframes(
        project_id: str,
        segment_id: str,
        request: KeyframeBatch,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Create or replace keyframes for clip properties."""
        return ok(service.add_keyframes(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="list_keyframes")
    @tool
    def list_keyframes(project_id: str, segment_id: str) -> dict[str, Any]:
        """List keyframes attached to a clip."""
        return {**OK, **service.list_keyframes(project_id, segment_id)}

    @server.tool(structured_output=False, name="delete_keyframe")
    @tool
    def delete_keyframe(
        project_id: str,
        segment_id: str,
        property_name: str,
        offset_us: Annotated[int, Field(ge=0)],
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Delete one keyframe."""
        return ok(service.remove_keyframe(project_id, segment_id, property_name, offset_us, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_effect")
    @tool
    def add_effect(
        project_id: str,
        segment_id: str,
        request: EffectCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add a video or audio effect by pyJianYingDraft enum name."""
        return ok(service.add_effect(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_filter")
    @tool
    def add_filter(
        project_id: str,
        segment_id: str,
        request: FilterCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add a video filter."""
        return ok(service.add_filter(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_transition")
    @tool
    def add_transition(
        project_id: str,
        segment_id: str,
        request: TransitionCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add a transition to a video clip."""
        return ok(service.add_transition(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_fade")
    @tool
    def add_fade(
        project_id: str,
        segment_id: str,
        request: FadeCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add audio fade in/out to a media clip."""
        return ok(service.add_fade(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_mask")
    @tool
    def add_mask(
        project_id: str,
        segment_id: str,
        request: MaskCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add a mask to a video or image clip."""
        return ok(service.add_mask(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_chroma")
    @tool
    def add_chroma(
        project_id: str,
        segment_id: str,
        request: ChromaCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add chroma key settings to a video or image clip."""
        return ok(service.add_chroma(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_background_filling")
    @tool
    def add_background_filling(
        project_id: str,
        segment_id: str,
        request: BackgroundFillCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add a blurred or solid background fill."""
        return ok(service.add_background_filling(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="set_mix_mode")
    @tool
    def set_mix_mode(
        project_id: str,
        segment_id: str,
        request: MixModeCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Set the blend mode for a video or image clip."""
        return ok(service.set_mix_mode(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_animation")
    @tool
    def add_animation(
        project_id: str,
        segment_id: str,
        request: AnimationCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add an intro, outro or group animation."""
        return ok(service.add_animation(project_id, segment_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="clear_segment_feature")
    @tool
    def clear_segment_feature(
        project_id: str,
        segment_id: str,
        feature: Annotated[str, Field(pattern=r"^(effects|filters|animations|transition|fade)$")],
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Remove all instances of one clip feature."""
        return ok(service.clear_feature(project_id, segment_id, feature, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_srt")
    @tool
    def add_srt(
        project_id: str,
        srt: Annotated[str, Field(min_length=1)],
        group_id: str | None = None,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Add SRT captions as a subtitle track."""
        return ok(
            service.add_srt_subtitles(
                project_id,
                srt,
                group_id=group_id,
                expect_revision=expect_revision,
            )
        )

    @server.tool(structured_output=False, name="remove_subtitles")
    @tool
    def remove_subtitles(
        project_id: str,
        track_id: str,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Remove one imported subtitle group."""
        return ok(service.remove_subtitle_group(project_id, track_id, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="add_marker")
    @tool
    def add_marker(
        project_id: str,
        request: MarkerCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Create or replace a timeline marker."""
        return ok(service.add_marker(project_id, request, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="list_markers")
    @tool
    def list_markers(project_id: str) -> dict[str, Any]:
        """List timeline markers."""
        return {**OK, "markers": service.list_markers(project_id)}

    @server.tool(structured_output=False, name="remove_marker")
    @tool
    def remove_marker(
        project_id: str,
        time_us: Annotated[int, Field(ge=0)],
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        """Remove a timeline marker by time."""
        return ok(service.remove_marker(project_id, time_us, expect_revision=expect_revision))

    @server.tool(structured_output=False, name="save_draft")
    @tool
    def save_draft(project_id: str) -> dict[str, Any]:
        """Build and save the Jianying draft for a project."""
        project, draft = service.save_draft(project_id)
        return {**OK, "project": project, "draft": draft}

    @server.tool(structured_output=False, name="create_draft_box_entry")
    @tool
    def create_draft_box_entry(project_id: str) -> dict[str, Any]:
        """Add a new native draft and index entry; close Jianying first."""
        return draft_box.create_draft(
            project_id,
            project_builder=service.build_draft,
        )

    @server.tool(structured_output=False, name="repair_draft_box_entry")
    @tool
    def repair_draft_box_entry(project_id: str, draft_id: str) -> dict[str, Any]:
        """Rebuild one service-owned native draft in place; close Jianying first."""
        return draft_box.repair_draft(
            project_id,
            draft_id,
            project_builder=service.build_draft,
        )

    @server.tool(structured_output=False, name="diagnose")
    @tool
    def diagnose() -> dict[str, Any]:
        """Report service paths and export support."""
        try:
            return {
                **OK,
                "settings": service.settings.public_dict(),
                "export": export_status(),
                "material_store": material_store.status(),
            }
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise ServiceError(
                "DIAGNOSE_FAILED", "Service diagnostics are unavailable", status=500
            ) from exc

    @server.tool(structured_output=False, name="capabilities")
    @tool
    def capabilities() -> dict[str, Any]:
        """List supported business operations."""
        return {
            **OK,
            "projects": ["create", "list", "get", "update_name", "set_canvas"],
            "tracks": [
                "video", "audio", "text", "sticker", "effect", "filter",
                "create", "update", "delete", "reorder", "mute",
            ],
            "track_effects": ["create", "move", "split", "delete"],
            "track_filters": ["create", "move", "split", "delete"],
            "clips": [
                "media",
                "text",
                "sticker",
                "update",
                "delete",
                "move",
                "split",
                "speed",
                "volume",
                "transform",
                "crop",
                "keyframes",
                "effects",
                "filters",
                "transition",
                "fade",
                "animation",
                "mask",
                "chroma_key",
                "background_filling",
                "mix_mode",
                "text_style",
            ],
            "subtitles": ["add_srt", "remove_group"],
            "markers": ["create_or_replace", "list", "delete"],
            "timeline_markers": "service_metadata_only",
            "animation_resources": animation_resource_handling(),
            "drafts": {
                "save": "available",
                "read_only_box": "available",
                "create_box": "available",
                "repair_owned_box": "available",
                "edit_box": "unsupported_encrypted",
                "delete_box": "unsupported_encrypted",
                "write_support": "create_and_repair_owned",
                "timeline_content": "unsupported_encrypted",
            },
            "export": export_status(),
            "jianying_user": {
                "read": "available",
                "local_sources": "settings_and_draft_index",
                "nickname": "window_ocr",
                "avatar": "remote_only",
                "membership": "window_ocr",
                "network_access": "none",
            },
            "jianying_membership": {
                "read": "available",
                "needs_gui": True,
                "source": "account_badge_ocr",
                "requires_foreground": True,
            },
            "material_store": material_store.capabilities(),
        }

    @server.tool(structured_output=False, name="editing_options")
    @tool
    def get_editing_options() -> dict[str, Any]:
        """List pyJianYingDraft supported effect, filter, transition and animation names."""
        return {**OK, "options": editing_options()}

    @server.tool(structured_output=False, name="search_animations")
    @tool
    def search_animations(
        query: Annotated[str, Field(max_length=100)] = "",
        kind: Annotated[
            str | None,
            Field(pattern=r"^(video_intro|video_outro|video_group|text_intro|text_outro|text_loop)$"),
        ] = None,
        target: Annotated[str | None, Field(pattern=r"^(video|text)$")] = None,
        vip_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        offset: Annotated[int, Field(ge=0, le=10_000)] = 0,
    ) -> dict[str, Any]:
        """Search built-in animation metadata for values accepted by add_animation.

        Jianying resolves uncached animation resources when opening the draft;
        animation draft records do not use local media paths.
        """
        return {
            **OK,
            **animation_catalog(
                query=query,
                kind=kind,
                target=target,
                vip_only=vip_only,
                limit=limit,
                offset=offset,
            ),
            "resource_handling": animation_resource_handling(),
        }

    @server.tool(structured_output=False, name="jianying_user")
    @tool
    def jianying_user(
        mask_uid: bool = False,
        include_paths: bool = True,
    ) -> dict[str, Any]:
        """Read the local Jianying account profile without network or writes.

        Returns the account id the client stores locally, the login platform,
        client build, preferences and draft counts. The display name and the
        membership tier are only visible in the Jianying window; use
        jianying_membership to read them through OCR.
        """
        return user_info.read(mask_uid=mask_uid, include_paths=include_paths)

    @server.tool(structured_output=False, name="jianying_membership")
    @tool
    def jianying_membership(focus: bool = False) -> dict[str, Any]:
        """Read membership tier and expiry from the Jianying account badge.

        Jianying disables Qt accessibility, so this captures the badge block and
        OCRs it. The Jianying window must already be in the foreground; pass
        focus=true to raise it first, which moves the user's foreground window.
        """
        return membership.read(focus=focus)

    @server.tool(structured_output=False, name="jianying_membership_status")
    @tool
    def jianying_membership_status() -> dict[str, Any]:
        """Report whether the badge OCR dependency is installed."""
        return membership.status()

    @server.tool(structured_output=False, name="jianying_user_status")
    @tool
    def jianying_user_status() -> dict[str, Any]:
        """Report whether a local Jianying user data directory was found."""
        return user_info.status()

    @server.tool(structured_output=False, name="get_draft_box_status")
    @tool
    def get_draft_box_status() -> dict[str, Any]:
        """Report native draft box reads and new-draft write support."""
        return draft_box.status()

    @server.tool(structured_output=False, name="list_draft_box")
    @tool
    def list_draft_box(
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        offset: Annotated[int, Field(ge=0, le=10_000)] = 0,
        query: Annotated[str, Field(max_length=200)] = "",
    ) -> dict[str, Any]:
        """List draft metadata from Jianying's local index."""
        return draft_box.list_drafts(limit=limit, offset=offset, query=query)

    @server.tool(structured_output=False, name="get_draft_box_draft")
    @tool
    def get_draft_box_draft(draft_id: str) -> dict[str, Any]:
        """Get one draft's index metadata; timeline content is encrypted."""
        return draft_box.get_draft(draft_id)

    @server.tool(structured_output=False, name="get_material_store_status")
    @tool
    def get_material_store_status() -> dict[str, Any]:
        """Report verified material store UI capabilities."""
        return material_store.status()

    @server.tool(structured_output=False, name="diagnose_material_store")
    @tool
    def diagnose_material_store() -> dict[str, Any]:
        """Report semantic control observations from the material store."""
        return material_store.diagnostic()

    @server.tool(structured_output=False, name="open_material_store")
    @tool
    def open_material_store() -> dict[str, Any]:
        """Open the material store when its UI controls are verified."""
        return material_store.open_store()

    @server.tool(structured_output=False, name="get_material_store_state")
    @tool
    def get_material_store_state() -> dict[str, Any]:
        """Read visible store items without activating a material action."""
        return material_store.observe()

    @server.tool(structured_output=False, name="search_material_store")
    @tool
    def search_material_store(
        query: Annotated[str, Field(min_length=1, max_length=200)],
    ) -> dict[str, Any]:
        """Set the verified store search field and read back its value."""
        return material_store.search(query)

    @server.tool(structured_output=False, name="browse_material_store")
    @tool
    def browse_material_store(
        category: Annotated[str, Field(min_length=1, max_length=80)],
    ) -> dict[str, Any]:
        """Select one verified store category and confirm its selection."""
        return material_store.browse(category)

    @server.tool(structured_output=False, name="get_sticker_library_state")
    @tool
    def get_sticker_library_state() -> dict[str, Any]:
        """Read the editor sticker library (a different surface from the store window)."""
        return material_store.library("state")

    @server.tool(structured_output=False, name="open_sticker_library")
    @tool
    def open_sticker_library() -> dict[str, Any]:
        """Show the editor sticker library, closing the overlay store if necessary."""
        return material_store.library("open")

    @server.tool(structured_output=False, name="search_sticker_library")
    @tool
    def search_sticker_library(query: Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[^\x00-\x1f]+$")]) -> dict[str, Any]:
        """Search stickers in the editor and verify the visible query and changed result grid."""
        return material_store.library("search", query)

    @server.tool(structured_output=False, name="browse_sticker_library")
    @tool
    def browse_sticker_library(category: Annotated[str, Field(min_length=1, max_length=80)]) -> dict[str, Any]:
        """Choose a currently visible editor sticker category and verify the changed grid."""
        return material_store.library("browse", category)

    @server.tool(structured_output=False, name="export_project")
    @tool
    def export_project(
        project_id: str, request: ExportRequest | None = None
    ) -> dict[str, Any]:
        """Check export availability; no rendering is supported."""
        service.get_project(project_id)
        return export_status()

    @server.tool(structured_output=False, name="get_export_status")
    @tool
    def get_export_status(project_id: str) -> dict[str, Any]:
        """Get current export availability."""
        service.get_project(project_id)
        return export_status()

    return server


def main() -> None:
    create_server().run("stdio")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"MCP server failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None

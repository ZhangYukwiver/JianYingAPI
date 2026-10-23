from __future__ import annotations

from typing import Annotated, Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .config import Settings
from .draft_box import DraftBoxService
from .draft import animation_catalog, animation_resource_handling, editing_options
from .errors import ServiceError
from .http_security import LocalRequestMiddleware
from .material_store import MaterialStoreService
from .membership import MembershipReader
from .models import (
    AnimationCreate,
    BackgroundFillCreate,
    CanvasUpdate,
    ChromaCreate,
    ExportRequest,
    ExportStatus,
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
    SegmentUpdate,
    StickerSegmentCreate,
    SegmentMove,
    SegmentSplit,
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


OK = {"ok": True}
Revision = Annotated[int, Field(ge=1)]


def service_error_response(error: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content=error.as_dict())


def validation_error_response(error: RequestValidationError) -> JSONResponse:
    details = [
        {
            "path": ".".join(str(part) for part in item["loc"]),
            "type": item["type"],
            "message": item["msg"],
        }
        for item in error.errors()
    ]
    failure = ServiceError(
        "VALIDATION_ERROR",
        "Request validation failed",
        status=422,
        details={"errors": details},
    )
    return service_error_response(failure)


def ok(project: Project) -> dict[str, Any]:
    return {**OK, "project": project}


class MaterialStoreQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]


class MaterialStoreBrowse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ]


class StickerLibraryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40, pattern=r"^[^\x00-\x1f]+$")]


def export_status() -> dict[str, Any]:
    return {
        **OK,
        "status": ExportStatus.UNSUPPORTED,
        "needs_gui": False,
        "unsupported": True,
    }


def create_app(
    service: ProjectService | None = None,
    *,
    settings: Settings | None = None,
    material_store: MaterialStoreService | None = None,
    draft_box: DraftBoxService | None = None,
    user_info: JianyingUserReader | None = None,
    membership: MembershipReader | None = None,
    emit_deprecation_warnings: bool = False,
) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or ProjectService(settings)
    app = FastAPI(
        title="Jianying AI Service",
        version="0.1.0",
        generate_unique_id_function=lambda route: route.name,
        emit_deprecation_warnings=emit_deprecation_warnings,
    )
    app.state.service = service
    app.state.settings = settings
    app.state.material_store = material_store or MaterialStoreService()
    app.state.draft_box = draft_box or DraftBoxService()
    app.state.user_info = user_info or JianyingUserReader()
    app.state.membership = membership or MembershipReader()
    app.add_middleware(LocalRequestMiddleware)

    @app.exception_handler(ServiceError)
    async def handle_service_error(_: Request, error: ServiceError) -> JSONResponse:
        return service_error_response(error)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, error: RequestValidationError
    ) -> JSONResponse:
        return validation_error_response(error)

    @app.get("/api/health", operation_id="health")
    def health() -> dict[str, Any]:
        return {**OK, "status": "ok"}

    @app.get("/api/projects", operation_id="list_projects")
    def list_projects(
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    ) -> dict[str, Any]:
        return {**OK, "projects": service.list_projects(limit=limit, offset=offset)}

    @app.post("/api/projects", operation_id="create_project")
    def create_project(request: ProjectCreate) -> dict[str, Any]:
        return ok(service.create_project(request))

    @app.get("/api/projects/{project_id}", operation_id="get_project")
    def get_project(project_id: str) -> dict[str, Any]:
        return ok(service.get_project(project_id))

    @app.patch("/api/projects/{project_id}", operation_id="update_project")
    def update_project(
        project_id: str,
        request: ProjectUpdate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.update_project(project_id, request, expect_revision=expect_revision))

    @app.put("/api/projects/{project_id}/canvas", operation_id="set_canvas")
    def set_canvas(
        project_id: str,
        request: CanvasUpdate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.set_canvas(project_id, request, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/tracks", operation_id="create_track")
    def create_track(
        project_id: str,
        request: TrackCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(
            service.add_track(
                project_id, request, expect_revision=expect_revision
            )
        )

    @app.patch("/api/projects/{project_id}/tracks/{track_id}", operation_id="update_track")
    def update_track(
        project_id: str,
        track_id: str,
        request: TrackUpdate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.update_track(project_id, track_id, request, expect_revision=expect_revision))

    @app.delete("/api/projects/{project_id}/tracks/{track_id}", operation_id="delete_track")
    def delete_track(
        project_id: str,
        track_id: str,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.delete_track(project_id, track_id, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/tracks/reorder", operation_id="reorder_tracks")
    def reorder_tracks(
        project_id: str,
        request: TrackOrder,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.reorder_tracks(project_id, request, expect_revision=expect_revision))

    @app.post(
        "/api/projects/{project_id}/tracks/{track_id}/media",
        operation_id="add_media_clip",
    )
    def add_media_clip(
        project_id: str,
        track_id: str,
        request: MediaSegmentCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(
            service.add_media_segment(
                project_id, track_id, request, expect_revision=expect_revision
            )
        )

    @app.post(
        "/api/projects/{project_id}/tracks/{track_id}/text",
        operation_id="add_text_clip",
    )
    def add_text_clip(
        project_id: str,
        track_id: str,
        request: TextSegmentCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(
            service.add_text_segment(
                project_id, track_id, request, expect_revision=expect_revision
            )
        )

    @app.post(
        "/api/projects/{project_id}/tracks/{track_id}/stickers",
        operation_id="add_sticker_clip",
    )
    def add_sticker_clip(
        project_id: str,
        track_id: str,
        request: StickerSegmentCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_sticker_segment(project_id, track_id, request, expect_revision=expect_revision))

    @app.post(
        "/api/projects/{project_id}/tracks/{track_id}/effects",
        operation_id="add_track_effect",
    )
    def add_track_effect(
        project_id: str,
        track_id: str,
        request: TrackEffectCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(
            service.add_track_effect_segment(
                project_id, track_id, request, expect_revision=expect_revision
            )
        )

    @app.post(
        "/api/projects/{project_id}/tracks/{track_id}/filters",
        operation_id="add_track_filter",
    )
    def add_track_filter(
        project_id: str,
        track_id: str,
        request: TrackFilterCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(
            service.add_track_filter_segment(
                project_id, track_id, request, expect_revision=expect_revision
            )
        )

    @app.patch("/api/projects/{project_id}/segments/{segment_id}", operation_id="update_segment")
    def update_segment(
        project_id: str,
        segment_id: str,
        request: SegmentUpdate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.update_segment(project_id, segment_id, request, expect_revision=expect_revision))

    @app.delete("/api/projects/{project_id}/segments/{segment_id}", operation_id="delete_segment")
    def delete_segment(
        project_id: str,
        segment_id: str,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.delete_segment(project_id, segment_id, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/segments/{segment_id}/move", operation_id="move_segment")
    def move_segment(
        project_id: str,
        segment_id: str,
        request: SegmentMove,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.move_segment(project_id, segment_id, request.start_us, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/segments/{segment_id}/split", operation_id="split_segment")
    def split_segment(
        project_id: str,
        segment_id: str,
        request: SegmentSplit,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        project, segments = service.split_segment(
            project_id, segment_id, request.offset_us, expect_revision=expect_revision
        )
        return {**OK, "project": project, "segments": segments}

    @app.post("/api/projects/{project_id}/segments/{segment_id}/keyframes", operation_id="add_keyframes")
    def add_keyframes(
        project_id: str,
        segment_id: str,
        request: KeyframeBatch,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_keyframes(project_id, segment_id, request, expect_revision=expect_revision))

    @app.get("/api/projects/{project_id}/segments/{segment_id}/keyframes", operation_id="list_keyframes")
    def list_keyframes(project_id: str, segment_id: str) -> dict[str, Any]:
        return {**OK, **service.list_keyframes(project_id, segment_id)}

    @app.delete(
        "/api/projects/{project_id}/segments/{segment_id}/keyframes/{property_name}/{offset_us}",
        operation_id="delete_keyframe",
    )
    def delete_keyframe(
        project_id: str,
        segment_id: str,
        property_name: str,
        offset_us: int,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(
            service.remove_keyframe(
                project_id,
                segment_id,
                property_name,
                offset_us,
                expect_revision=expect_revision,
            )
        )

    @app.post("/api/projects/{project_id}/segments/{segment_id}/effects", operation_id="add_effect")
    def add_effect(
        project_id: str,
        segment_id: str,
        request: EffectCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_effect(project_id, segment_id, request, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/segments/{segment_id}/filters", operation_id="add_filter")
    def add_filter(
        project_id: str,
        segment_id: str,
        request: FilterCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_filter(project_id, segment_id, request, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/segments/{segment_id}/transition", operation_id="add_transition")
    def add_transition(
        project_id: str,
        segment_id: str,
        request: TransitionCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_transition(project_id, segment_id, request, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/segments/{segment_id}/fade", operation_id="add_fade")
    def add_fade(
        project_id: str,
        segment_id: str,
        request: FadeCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_fade(project_id, segment_id, request, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/segments/{segment_id}/mask", operation_id="add_mask")
    def add_mask(
        project_id: str,
        segment_id: str,
        request: MaskCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_mask(project_id, segment_id, request, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/segments/{segment_id}/chroma", operation_id="add_chroma")
    def add_chroma(
        project_id: str,
        segment_id: str,
        request: ChromaCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_chroma(project_id, segment_id, request, expect_revision=expect_revision))

    @app.post(
        "/api/projects/{project_id}/segments/{segment_id}/background-filling",
        operation_id="add_background_filling",
    )
    def add_background_filling(
        project_id: str,
        segment_id: str,
        request: BackgroundFillCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_background_filling(project_id, segment_id, request, expect_revision=expect_revision))

    @app.put("/api/projects/{project_id}/segments/{segment_id}/mix-mode", operation_id="set_mix_mode")
    def set_mix_mode(
        project_id: str,
        segment_id: str,
        request: MixModeCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.set_mix_mode(project_id, segment_id, request, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/segments/{segment_id}/animations", operation_id="add_animation")
    def add_animation(
        project_id: str,
        segment_id: str,
        request: AnimationCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_animation(project_id, segment_id, request, expect_revision=expect_revision))

    @app.delete("/api/projects/{project_id}/segments/{segment_id}/features/{feature}", operation_id="clear_segment_feature")
    def clear_segment_feature(
        project_id: str,
        segment_id: str,
        feature: str,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.clear_feature(project_id, segment_id, feature, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/subtitles", operation_id="add_srt")
    def add_srt(
        project_id: str,
        srt: Annotated[str, Body(media_type="text/plain")],
        group_id: str | None = None,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(
            service.add_srt_subtitles(
                project_id,
                srt,
                group_id=group_id,
                expect_revision=expect_revision,
            )
        )

    @app.delete("/api/projects/{project_id}/subtitles/{track_id}", operation_id="remove_subtitles")
    def remove_subtitles(
        project_id: str,
        track_id: str,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.remove_subtitle_group(project_id, track_id, expect_revision=expect_revision))

    @app.get("/api/projects/{project_id}/markers", operation_id="list_markers")
    def list_markers(project_id: str) -> dict[str, Any]:
        return {**OK, "markers": service.list_markers(project_id)}

    @app.post("/api/projects/{project_id}/markers", operation_id="add_marker")
    def add_marker(
        project_id: str,
        request: MarkerCreate,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.add_marker(project_id, request, expect_revision=expect_revision))

    @app.delete("/api/projects/{project_id}/markers/{time_us}", operation_id="remove_marker")
    def remove_marker(
        project_id: str,
        time_us: int,
        expect_revision: Revision | None = None,
    ) -> dict[str, Any]:
        return ok(service.remove_marker(project_id, time_us, expect_revision=expect_revision))

    @app.post("/api/projects/{project_id}/draft", operation_id="save_draft")
    def save_draft(project_id: str) -> dict[str, Any]:
        project, draft = service.save_draft(project_id)
        return {**OK, "project": project, "draft": draft}

    @app.post(
        "/api/projects/{project_id}/draft-box",
        operation_id="create_draft_box_entry",
    )
    def create_draft_box_entry(project_id: str) -> dict[str, Any]:
        return app.state.draft_box.create_draft(
            project_id,
            project_builder=service.build_draft,
        )

    @app.put(
        "/api/projects/{project_id}/draft-box/{draft_id}",
        operation_id="repair_draft_box_entry",
    )
    def repair_draft_box_entry(project_id: str, draft_id: str) -> dict[str, Any]:
        return app.state.draft_box.repair_draft(
            project_id,
            draft_id,
            project_builder=service.build_draft,
        )

    @app.get("/api/diagnose", operation_id="diagnose")
    def diagnose() -> dict[str, Any]:
        try:
            return {
                **OK,
                "settings": service.settings.public_dict(),
                "export": export_status(),
                "material_store": app.state.material_store.status(),
            }
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise ServiceError(
                "DIAGNOSE_FAILED", "Service diagnostics are unavailable", status=500
            ) from exc

    @app.get("/api/capabilities", operation_id="capabilities")
    def capabilities() -> dict[str, Any]:
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
            "material_store": app.state.material_store.capabilities(),
        }

    @app.get("/api/editing/options", operation_id="editing_options")
    def get_editing_options() -> dict[str, Any]:
        return {**OK, "options": editing_options()}

    @app.get("/api/animations/catalog", operation_id="search_animations")
    def search_animations(
        query: Annotated[str, Query(max_length=100)] = "",
        kind: Annotated[
            str | None,
            Query(pattern=r"^(video_intro|video_outro|video_group|text_intro|text_outro|text_loop)$"),
        ] = None,
        target: Annotated[str | None, Query(pattern=r"^(video|text)$")] = None,
        vip_only: bool = False,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    ) -> dict[str, Any]:
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

    @app.get("/api/jianying/user", operation_id="jianying_user")
    def jianying_user(
        mask_uid: bool = False,
        include_paths: bool = True,
    ) -> dict[str, Any]:
        return app.state.user_info.read(
            mask_uid=mask_uid, include_paths=include_paths
        )

    @app.get("/api/jianying/user/status", operation_id="jianying_user_status")
    def jianying_user_status() -> dict[str, Any]:
        return app.state.user_info.status()

    @app.get("/api/jianying/membership", operation_id="jianying_membership")
    def jianying_membership(focus: bool = False) -> dict[str, Any]:
        return app.state.membership.read(focus=focus)

    @app.get(
        "/api/jianying/membership/status",
        operation_id="jianying_membership_status",
    )
    def jianying_membership_status() -> dict[str, Any]:
        return app.state.membership.status()

    @app.get("/api/draft-box/status", operation_id="get_draft_box_status")
    def get_draft_box_status() -> dict[str, Any]:
        return app.state.draft_box.status()

    @app.get("/api/draft-box", operation_id="list_draft_box")
    def list_draft_box(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
        query: Annotated[str, Query(max_length=200)] = "",
    ) -> dict[str, Any]:
        return app.state.draft_box.list_drafts(
            limit=limit, offset=offset, query=query
        )

    @app.get("/api/draft-box/{draft_id}", operation_id="get_draft_box_draft")
    def get_draft_box_draft(draft_id: str) -> dict[str, Any]:
        return app.state.draft_box.get_draft(draft_id)

    @app.get(
        "/api/material-store/status",
        operation_id="get_material_store_status",
    )
    def get_material_store_status() -> dict[str, Any]:
        return app.state.material_store.status()

    @app.get(
        "/api/material-store/diagnose",
        operation_id="diagnose_material_store",
    )
    def diagnose_material_store() -> dict[str, Any]:
        return app.state.material_store.diagnostic()

    @app.post(
        "/api/material-store/open",
        operation_id="open_material_store",
    )
    def open_material_store(
    ) -> dict[str, Any]:
        return app.state.material_store.open_store()

    @app.get(
        "/api/material-store/state",
        operation_id="get_material_store_state",
    )
    def get_material_store_state() -> dict[str, Any]:
        return app.state.material_store.observe()

    @app.post(
        "/api/material-store/search",
        operation_id="search_material_store",
    )
    def search_material_store(request: MaterialStoreQuery) -> dict[str, Any]:
        return app.state.material_store.search(request.query)

    @app.post(
        "/api/material-store/browse",
        operation_id="browse_material_store",
    )
    def browse_material_store(request: MaterialStoreBrowse) -> dict[str, Any]:
        return app.state.material_store.browse(request.category)

    @app.get("/api/sticker-library/state", operation_id="get_sticker_library_state")
    def get_sticker_library_state() -> dict[str, Any]:
        return app.state.material_store.library("state")

    @app.post("/api/sticker-library/open", operation_id="open_sticker_library")
    def open_sticker_library() -> dict[str, Any]:
        return app.state.material_store.library("open")

    @app.post("/api/sticker-library/search", operation_id="search_sticker_library")
    def search_sticker_library(request: StickerLibraryQuery) -> dict[str, Any]:
        return app.state.material_store.library("search", request.query)

    @app.post("/api/sticker-library/browse", operation_id="browse_sticker_library")
    def browse_sticker_library(request: MaterialStoreBrowse) -> dict[str, Any]:
        return app.state.material_store.library("browse", request.category)

    @app.post("/api/projects/{project_id}/export", operation_id="export_project")
    def export_project(
        project_id: str, request: ExportRequest | None = None
    ) -> dict[str, Any]:
        service.get_project(project_id)
        return export_status()

    @app.get(
        "/api/projects/{project_id}/export/status",
        operation_id="get_export_status",
    )
    def get_export_status(project_id: str) -> dict[str, Any]:
        service.get_project(project_id)
        return export_status()

    return app


def __getattr__(name: str) -> Any:
    if name == "app":
        value = create_app()
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

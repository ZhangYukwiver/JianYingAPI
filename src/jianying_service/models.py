from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)


MAX_TIME_US = 24 * 60 * 60 * 1_000_000


def finite_nonnegative(value: float) -> float:
    if not math.isfinite(value) or value < 0:
        raise ValueError("must be finite and non-negative")
    return value


TimeUs = Annotated[StrictInt, Field(ge=0, le=MAX_TIME_US)]
DurationUs = Annotated[StrictInt, Field(gt=0, le=MAX_TIME_US)]
NonnegativeFloat = Annotated[FiniteFloat, AfterValidator(finite_nonnegative)]
PositiveFloat = Annotated[FiniteFloat, Field(gt=0, le=100)]
Speed = Annotated[FiniteFloat, Field(gt=0.05, le=100)]
Label = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_us: TimeUs = 0
    duration_us: DurationUs

    @model_validator(mode="after")
    def valid_end(self) -> "TimeRange":
        if self.start_us + self.duration_us > MAX_TIME_US:
            raise ValueError("time range exceeds 24 hours")
        return self

    @property
    def end_us(self) -> int:
        return self.start_us + self.duration_us


class ClipTransform(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scale_x: PositiveFloat = 1.0
    scale_y: PositiveFloat = 1.0
    transform_x: Annotated[FiniteFloat, Field(ge=-1, le=1)] = 0.0
    transform_y: Annotated[FiniteFloat, Field(ge=-1, le=1)] = 0.0
    rotation: Annotated[FiniteFloat, Field(ge=-360, le=360)] = 0.0
    alpha: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0
    flip_horizontal: bool = False
    flip_vertical: bool = False


class CropSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    upper_left_x: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.0
    upper_left_y: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.0
    upper_right_x: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0
    upper_right_y: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.0
    lower_left_x: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.0
    lower_left_y: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0
    lower_right_x: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0
    lower_right_y: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0


class Canvas(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    width: Annotated[StrictInt, Field(ge=16, le=7680)] = 1920
    height: Annotated[StrictInt, Field(ge=16, le=7680)] = 1080
    fps: Annotated[StrictInt, Field(ge=1, le=120)] = 30


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Label
    canvas: Canvas = Canvas()


class CanvasUpdate(Canvas):
    pass


class TrackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_type: Literal["video", "audio", "text", "sticker", "effect", "filter"]
    name: Label | None = None
    mute: StrictBool = False


class TrackUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Label | None = None
    mute: StrictBool | None = None


class TrackOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = Field(
        min_length=1, max_length=100
    )


class MediaSegmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=32767)
    timeline: TimeRange
    source: TimeRange | None = None
    volume: Annotated[FiniteFloat, Field(ge=0, le=2)] = 1.0
    speed: Speed | None = None
    change_pitch: StrictBool = False
    fit: Literal["contain", "cover", "stretch"] = "contain"
    transform: ClipTransform | None = None
    crop: CropSettings | None = None
    material_name: Label | None = None


class TextSegmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(min_length=1, max_length=10000)]
    fit: Literal["contain", "cover", "stretch"] = "contain"
    timeline: TimeRange
    transform: ClipTransform | None = None
    font_size: Annotated[FiniteFloat, Field(ge=1, le=100)] = 8.0
    color: Annotated[
        str,
        StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$"),
    ] = "#FFFFFF"
    alignment: Literal["left", "center", "right"] = "center"
    font_family: Label | None = None
    bold: StrictBool = False
    italic: StrictBool = False
    underline: StrictBool = False
    opacity: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0
    vertical: StrictBool = False
    letter_spacing: Annotated[StrictInt, Field(ge=-1000, le=1000)] = 0
    line_spacing: Annotated[StrictInt, Field(ge=-1000, le=1000)] = 0
    auto_wrapping: StrictBool = True
    max_line_width: Annotated[FiniteFloat, Field(gt=0, le=1)] = 0.82
    border: "TextBorderOptions | None" = None
    background: "TextBackgroundOptions | None" = None
    shadow: "TextShadowOptions | None" = None
    bubble: "TextBubbleOptions | None" = None
    text_effect_id: Label | None = None


class TextBubbleOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effect_id: Label
    resource_id: Label


class TextBorderOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")] = "#000000"
    alpha: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0
    width: Annotated[FiniteFloat, Field(ge=0, le=100)] = 40.0


class TextBackgroundOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{8}$")]
    style: Literal[1, 2] = 1
    alpha: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0
    round_radius: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.0
    height: Annotated[FiniteFloat, Field(gt=0, le=1)] = 0.14
    width: Annotated[FiniteFloat, Field(gt=0, le=1)] = 0.14
    horizontal_offset: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.5
    vertical_offset: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.5


class TextShadowOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")] = "#000000"
    alpha: Annotated[FiniteFloat, Field(ge=0, le=1)] = 1.0
    diffuse: Annotated[FiniteFloat, Field(ge=0, le=100)] = 15.0
    distance: Annotated[FiniteFloat, Field(ge=0, le=100)] = 5.0
    angle: Annotated[FiniteFloat, Field(ge=-360, le=360)] = -45.0


class SegmentUpdate(BaseModel):
    """Fields that can be changed without replacing a material."""

    model_config = ConfigDict(extra="forbid")

    timeline: TimeRange | None = None
    source: TimeRange | None = None
    volume: Annotated[FiniteFloat, Field(ge=0, le=2)] | None = None
    speed: Speed | None = None
    change_pitch: StrictBool | None = None
    fit: Literal["contain", "cover", "stretch"] | None = None
    transform: ClipTransform | None = None
    text: Annotated[str, StringConstraints(min_length=1, max_length=10000)] | None = None
    font_size: Annotated[FiniteFloat, Field(ge=1, le=100)] | None = None
    color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")] | None = None
    alignment: Literal["left", "center", "right"] | None = None
    path: str | None = Field(default=None, min_length=1, max_length=32767)
    material_name: Label | None = None
    crop: CropSettings | None = None
    font_family: Label | None = None
    bold: StrictBool | None = None
    italic: StrictBool | None = None
    underline: StrictBool | None = None
    opacity: Annotated[FiniteFloat, Field(ge=0, le=1)] | None = None
    vertical: StrictBool | None = None
    letter_spacing: Annotated[StrictInt, Field(ge=-1000, le=1000)] | None = None
    line_spacing: Annotated[StrictInt, Field(ge=-1000, le=1000)] | None = None
    auto_wrapping: StrictBool | None = None
    max_line_width: Annotated[FiniteFloat, Field(gt=0, le=1)] | None = None
    border: TextBorderOptions | None = None
    background: TextBackgroundOptions | None = None
    shadow: TextShadowOptions | None = None
    bubble: TextBubbleOptions | None = None
    text_effect_id: Label | None = None


class MaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mask_type: Label
    center_x: Annotated[FiniteFloat, Field(ge=-10000, le=10000)] = 0.0
    center_y: Annotated[FiniteFloat, Field(ge=-10000, le=10000)] = 0.0
    size: Annotated[FiniteFloat, Field(gt=0, le=1)] = 0.5
    rotation: Annotated[FiniteFloat, Field(ge=-360, le=360)] = 0.0
    feather: Annotated[FiniteFloat, Field(ge=0, le=100)] = 0.0
    invert: StrictBool = False
    rect_width: Annotated[FiniteFloat, Field(gt=0, le=1)] | None = None
    round_corner: Annotated[FiniteFloat, Field(ge=0, le=100)] | None = None


class ChromaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{8}$")]
    intensity: Annotated[FiniteFloat, Field(ge=0, le=100)] = 20.0
    shadow: Annotated[FiniteFloat, Field(ge=0, le=100)] = 0.0
    edge_smooth: Annotated[FiniteFloat, Field(ge=0, le=100)] = 0.0
    spill: Annotated[FiniteFloat, Field(ge=0, le=100)] = 0.0


class BackgroundFillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fill_type: Literal["blur", "color"]
    blur: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.0625
    color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{8}$")] = "#00000000"


class MixModeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Label


KeyframePropertyName = Literal[
    "transform_x",
    "transform_y",
    "position_x",
    "position_y",
    "scale_x",
    "scale_y",
    "uniform_scale",
    "rotation",
    "alpha",
    "volume",
    "brightness",
    "contrast",
    "saturation",
]


class KeyframeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property: KeyframePropertyName
    offset_us: TimeUs
    value: FiniteFloat


class KeyframeBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyframes: list[KeyframeCreate] = Field(min_length=1, max_length=500)


class EffectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect_type: Label
    params: list[Annotated[FiniteFloat, Field(ge=0, le=100)]] | None = Field(
        default=None, max_length=64
    )


class FilterCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filter_type: Label
    intensity: Annotated[FiniteFloat, Field(ge=0, le=100)] = 100.0


class TrackEffectCreate(EffectCreate):
    timeline: TimeRange


class TrackFilterCreate(FilterCreate):
    timeline: TimeRange


class TransitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transition_type: Label
    duration_us: DurationUs | None = None


class FadeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_duration_us: TimeUs = 0
    out_duration_us: TimeUs = 0

    @model_validator(mode="after")
    def valid_fade(self) -> "FadeCreate":
        if self.in_duration_us == 0 and self.out_duration_us == 0:
            raise ValueError("fade duration must be positive")
        return self


class AnimationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    animation_type: Label
    duration_us: DurationUs | None = None


class StickerSegmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    timeline: TimeRange
    transform: ClipTransform | None = None


class MarkerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_us: TimeUs
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    color: Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")] = "#FFFFFF"


class SegmentMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_us: TimeUs


class SegmentSplit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset_us: DurationUs


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Label | None = None


class SubtitleEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_us: TimeUs
    end_us: Annotated[int, Field(gt=0, le=MAX_TIME_US)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=10000)]

    @model_validator(mode="after")
    def valid_interval(self) -> "SubtitleEntry":
        if self.end_us <= self.start_us:
            raise ValueError("end_us must be greater than start_us")
        return self


class DraftInfo(BaseModel):
    path: str
    content_sha256: str
    bytes: int
    modified_at: datetime


class Project(ProjectCreate):
    id: str
    created_at: datetime
    updated_at: datetime
    revision: int
    tracks: list[dict] = Field(default_factory=list)
    markers: list[dict] = Field(default_factory=list)
    draft: DraftInfo | None = None


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: Annotated[StrictInt, Field(ge=16, le=3840)] = 640
    height: Annotated[StrictInt, Field(ge=16, le=2160)] = 360
    fps: Annotated[StrictInt, Field(ge=1, le=60)] = 25


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: Label | None = None


class DraftOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reveal_directory: bool = False


class SubtitleRemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: str


class ExportStatus(StrEnum):
    UNSUPPORTED = "unsupported"
    NEEDS_GUI = "needs_gui"

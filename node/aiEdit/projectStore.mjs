// 移植自 JianYingAPI（service.py 的 ProjectService、models.py 的请求校验、storage.py 的读写与版本语义）；
// 特效/滤镜/转场/动画/蒙版/混合模式名称取自 pyJianYingDraft（Apache-2.0，见 node/licenses）导出的目录。
// 行为以 node/scripts/ai-edit/generate-operation-fixtures.py 录下的原仓库输出为准，逐条回放对照。
import { randomBytes } from "node:crypto";
import { link, mkdir, open, readdir, readFile, realpath, stat, unlink } from "node:fs/promises";
import path from "node:path";

import { members } from "./catalog.mjs";
import { EditError, invalid } from "./errors.mjs";
import { renameRetrying } from "./rename.mjs";

export const MAX_TIME_US = 24 * 60 * 60 * 1_000_000;
const MAX_SRT_BYTES = 4 * 1024 * 1024;
const MAX_PROJECT_BYTES = 32 * 1024 * 1024;
const PROJECT_ID = /^[0-9a-f]{32}$/;
const MEDIA_KINDS = new Set(["video", "image", "audio"]);

// ---------------------------------------------------------------- 请求校验（pydantic 的子集）
// 错误按 FastAPI 路由的顺序收集（pydantic Python 模式 + from_attributes）：每个对象先按字段声明顺序报，最后报多余字段；路径相对请求体。

const INVALID = Symbol("invalid");
const REQUIRED = Symbol("required");
// pydantic 的 strip_whitespace 用 Rust 的 trim（Unicode White_Space）；Python str.strip() / isspace() 还多认 \x1c-\x1f
const RUST_SPACE_CODES = [
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x20, 0x85, 0xa0, 0x1680,
  ...Array.from({ length: 11 }, (_, index) => 0x2000 + index), 0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
];
const PY_SPACE_CODES = [...RUST_SPACE_CODES, 0x1c, 0x1d, 0x1e, 0x1f];
const PY_S = `[${PY_SPACE_CODES.map((code) => `\\u{${code.toString(16)}}`).join("")}]`;

// 从两头往里扫，线性时间。别换回 /^\s+|\s+$/g：中间夹一长段空白时 V8 会逐位回溯成平方复杂度，
// 模型吐出几万个空格就能把采集器主线程卡住几秒。空白都在 BMP 内，charCodeAt 即可。
function trimmer(codes) {
  const spaces = new Set(codes);
  return (text) => {
    let start = 0;
    let end = text.length;
    while (start < end && spaces.has(text.charCodeAt(start))) start += 1;
    while (end > start && spaces.has(text.charCodeAt(end - 1))) end -= 1;
    return start === 0 && end === text.length ? text : text.slice(start, end);
  };
}

const rustTrim = trimmer(RUST_SPACE_CODES);
const pyStrip = trimmer(PY_SPACE_CODES);
const codePoints = (text) => {
  let count = 0;
  for (const _ of text) count += 1;
  return count;
};

function issue(errors, loc, type, message) {
  errors.push({ loc, type, message });
  return INVALID;
}

const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

function bounded(value, { gt, ge, le }, loc, errors) {
  if (gt !== undefined && !(value > gt)) return issue(errors, loc, "greater_than", `Input should be greater than ${gt}`);
  if (ge !== undefined && !(value >= ge)) return issue(errors, loc, "greater_than_equal", `Input should be greater than or equal to ${ge}`);
  if (le !== undefined && !(value <= le)) return issue(errors, loc, "less_than_equal", `Input should be less than or equal to ${le}`);
  return value;
}

/** StrictInt。JSON 解析后分不清 2.0 和 2，这里都当整数收下。 */
function strictInt(limits = {}) {
  return (value, loc, errors) => (typeof value === "number" && Number.isInteger(value)
    ? bounded(value, limits, loc, errors)
    : issue(errors, loc, "int_type", "Input should be a valid integer"));
}

/** 普通 int（查询/路径参数）：接受布尔、整数值的小数和数字字符串。 */
function laxInt(limits = {}) {
  return (value, loc, errors) => {
    let number = value;
    if (typeof value === "boolean") number = value ? 1 : 0;
    else if (typeof value === "string") {
      const text = rustTrim(value);
      if (!/^[+-]?\d(?:_?\d)*(?:\.0+)?$/u.test(text)) {
        return issue(errors, loc, "int_parsing", "Input should be a valid integer, unable to parse string as an integer");
      }
      number = Number(text.replaceAll("_", "").replace(/\.0+$/u, ""));
    } else if (typeof value !== "number" || !Number.isFinite(value)) {
      return issue(errors, loc, "int_type", "Input should be a valid integer");
    } else if (!Number.isInteger(value)) {
      return issue(errors, loc, "int_from_float", "Input should be a valid integer, got a number with a fractional part");
    }
    return bounded(number, limits, loc, errors);
  };
}

const FLOAT_TEXT = /^[+-]?(?:\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?|\.\d(?:_?\d)*)(?:[eE][+-]?\d(?:_?\d)*)?$/u;

/** FiniteFloat（宽松模式）：数字、布尔、数字字符串都收，非有限值报 finite_number。 */
function finiteFloat(limits = {}) {
  return (value, loc, errors) => {
    let number;
    if (typeof value === "number") number = value;
    else if (typeof value === "boolean") number = value ? 1 : 0;
    else if (typeof value === "string") {
      const text = rustTrim(value);
      if (/^[+-]?(?:inf|infinity|nan)$/iu.test(text)) number = Number.NaN;
      else if (FLOAT_TEXT.test(text)) number = Number(text.replaceAll("_", ""));
      else return issue(errors, loc, "float_parsing", "Input should be a valid number, unable to parse string as a number");
    } else return issue(errors, loc, "float_type", "Input should be a valid number");
    if (!Number.isFinite(number)) return issue(errors, loc, "finite_number", "Input should be a finite number");
    return bounded(number, limits, loc, errors);
  };
}

const strictBool = (value, loc, errors) => (typeof value === "boolean"
  ? value
  : issue(errors, loc, "bool_type", "Input should be a valid boolean"));

const TRUE_WORDS = new Set(["1", "on", "t", "true", "y", "yes"]);
const FALSE_WORDS = new Set(["0", "off", "f", "false", "n", "no"]);

/** 普通 bool（ClipTransform 的翻转）：0/1 和 yes/no 一类字符串都算。 */
function laxBool(value, loc, errors) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (value === 0 || value === 1) return value === 1;
    return Number.isInteger(value)
      ? issue(errors, loc, "bool_parsing", "Input should be a valid boolean, unable to interpret input")
      : issue(errors, loc, "bool_type", "Input should be a valid boolean");
  }
  if (typeof value === "string") {
    const lower = value.toLowerCase();
    if (TRUE_WORDS.has(lower)) return true;
    if (FALSE_WORDS.has(lower)) return false;
    return issue(errors, loc, "bool_parsing", "Input should be a valid boolean, unable to interpret input");
  }
  return issue(errors, loc, "bool_type", "Input should be a valid boolean");
}

function str({ strip = false, min, max, pattern } = {}) {
  return (value, loc, errors) => {
    if (typeof value !== "string") return issue(errors, loc, "string_type", "Input should be a valid string");
    const text = strip ? rustTrim(value) : value;
    const length = codePoints(text);
    if (min !== undefined && length < min) return issue(errors, loc, "string_too_short", `String should have at least ${plural(min, "character")}`);
    if (max !== undefined && length > max) return issue(errors, loc, "string_too_long", `String should have at most ${plural(max, "character")}`);
    if (pattern && !pattern.test(text)) return issue(errors, loc, "string_pattern_mismatch", `String should match pattern '${pattern.source}'`);
    return text;
  };
}

function literal(options) {
  const shown = options.map((option) => (typeof option === "string" ? `'${option}'` : String(option)));
  const expected = shown.length === 1 ? shown[0] : `${shown.slice(0, -1).join(", ")} or ${shown.at(-1)}`;
  return (value, loc, errors) => {
    for (const option of options) {
      if (value === option) return option;
      // Literal[1, 2] 在 pydantic 里也认 True（=1）
      if (typeof option === "number" && typeof value === "boolean" && Number(value) === option) return option;
    }
    return issue(errors, loc, "literal_error", `Input should be ${expected}`);
  };
}

const nullable = (type) => (value, loc, errors) => (value === null ? null : type(value, loc, errors));

function list(item, { min, max } = {}) {
  return (value, loc, errors) => {
    if (!Array.isArray(value)) return issue(errors, loc, "list_type", "Input should be a valid list");
    if (max !== undefined && value.length > max) {
      return issue(errors, loc, "too_long", `List should have at most ${plural(max, "item")} after validation, not ${value.length}`);
    }
    const before = errors.length;
    const result = value.map((entry, index) => item(entry, [...loc, index], errors));
    if (errors.length > before) return INVALID;
    if (min !== undefined && result.length < min) {
      return issue(errors, loc, "too_short", `List should have at least ${plural(min, "item")} after validation, not ${result.length}`);
    }
    return result;
  };
}

/** extra="forbid" 的 BaseModel；fields 是 [名字, 类型, 默认值 | REQUIRED | () => 默认值]。 */
function model(fields, { after } = {}) {
  const names = new Set(fields.map(([name]) => name));
  return (value, loc, errors) => {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      return issue(errors, loc, "model_attributes_type", "Input should be a valid dictionary or object to extract fields from");
    }
    const before = errors.length;
    const result = {};
    for (const [name, type, fallback] of fields) {
      if (present(value, name)) result[name] = type(value[name], [...loc, name], errors);
      else if (fallback === REQUIRED) issue(errors, [...loc, name], "missing", "Field required");
      else result[name] = typeof fallback === "function" ? fallback() : fallback;
    }
    for (const key of givenKeys(value)) {
      if (!names.has(key)) issue(errors, [...loc, key], "extra_forbidden", "Extra inputs are not permitted");
    }
    if (errors.length > before) return INVALID;
    const problem = after?.(result);
    return problem ? issue(errors, loc, "value_error", `Value error, ${problem}`) : result;
  };
}

function validationError(errors) {
  return new EditError("VALIDATION_ERROR", "Request validation failed", {
    status: 422,
    details: { errors: errors.map(({ loc, type, message }) => ({ path: loc.join("."), type, message })) },
  });
}

/**
 * 依次校验 [模式, 输入]，错误合并成一个 VALIDATION_ERROR（同原仓库路由：先路径/查询参数，后请求体）。
 * FastAPI 在交给 pydantic 之前先把 None 的请求体当成没传，直接报 missing。
 */
function check(...pairs) {
  const errors = [];
  const values = pairs.map(([schema, input]) => (input === null || input === undefined
    ? issue(errors, [], "missing", "Field required")
    : schema(input, [], errors)));
  if (errors.length) throw validationError(errors);
  return values;
}

const defined = (object) => Object.fromEntries(Object.entries(object).filter(([, value]) => value !== undefined));
// 路径/查询参数：FastAPI 把 None 和没传一样处理（取默认值，必填的报 missing）
const given = (object) => Object.fromEntries(Object.entries(object).filter(([, value]) => value !== undefined && value !== null));
// 请求体按 JSON 看待：值为 undefined 的键等于没传
const present = (object, key) => Object.hasOwn(object, key) && object[key] !== undefined;
const givenKeys = (object) => Object.keys(object).filter((key) => object[key] !== undefined);

// ---------------------------------------------------------------- models.py

const TimeUs = strictInt({ ge: 0, le: MAX_TIME_US });
const DurationUs = strictInt({ gt: 0, le: MAX_TIME_US });
const PositiveFloat = finiteFloat({ gt: 0, le: 100 });
const Speed = finiteFloat({ gt: 0.05, le: 100 });
const Label = str({ strip: true, min: 1, max: 200 });
const Unit = finiteFloat({ ge: 0, le: 1 });
const Percent = finiteFloat({ ge: 0, le: 100 });
const Angle = finiteFloat({ ge: -360, le: 360 });
const Color6 = str({ pattern: /^#[0-9A-Fa-f]{6}$/ });
const Color8 = str({ pattern: /^#[0-9A-Fa-f]{8}$/ });
const Fit = literal(["contain", "cover", "stretch"]);
const Alignment = literal(["left", "center", "right"]);
const TextBody = str({ min: 1, max: 10000 });
const MediaPath = str({ min: 1, max: 32767 });
const Volume = finiteFloat({ ge: 0, le: 2 });
const FontSize = finiteFloat({ ge: 1, le: 100 });
const Spacing = strictInt({ ge: -1000, le: 1000 });
const LineWidth = finiteFloat({ gt: 0, le: 1 });

const TimeRange = model([["start_us", TimeUs, 0], ["duration_us", DurationUs, REQUIRED]], {
  after: (range) => (range.start_us + range.duration_us > MAX_TIME_US ? "time range exceeds 24 hours" : null),
});

const ClipTransform = model([
  ["scale_x", PositiveFloat, 1], ["scale_y", PositiveFloat, 1],
  ["transform_x", finiteFloat({ ge: -1, le: 1 }), 0], ["transform_y", finiteFloat({ ge: -1, le: 1 }), 0],
  ["rotation", Angle, 0], ["alpha", Unit, 1],
  ["flip_horizontal", laxBool, false], ["flip_vertical", laxBool, false],
]);

const CropSettings = model([
  ["upper_left_x", Unit, 0], ["upper_left_y", Unit, 0], ["upper_right_x", Unit, 1], ["upper_right_y", Unit, 0],
  ["lower_left_x", Unit, 0], ["lower_left_y", Unit, 1], ["lower_right_x", Unit, 1], ["lower_right_y", Unit, 1],
]);

const Canvas = model([
  ["width", strictInt({ ge: 16, le: 7680 }), 1920],
  ["height", strictInt({ ge: 16, le: 7680 }), 1080],
  ["fps", strictInt({ ge: 1, le: 120 }), 30],
]);

const ProjectCreate = model([["name", Label, REQUIRED], ["canvas", Canvas, () => ({ width: 1920, height: 1080, fps: 30 })]]);
const ProjectUpdate = model([["name", nullable(Label), null]]);

const TRACK_TYPES = ["video", "audio", "text", "sticker", "effect", "filter"];
const TrackCreate = model([["track_type", literal(TRACK_TYPES), REQUIRED], ["name", nullable(Label), null], ["mute", strictBool, false]]);
const TrackUpdate = model([["name", nullable(Label), null], ["mute", nullable(strictBool), null]]);
const TrackOrder = model([["track_ids", list(str({ min: 1, max: 64 }), { min: 1, max: 100 }), REQUIRED]]);

const MediaSegmentCreate = model([
  ["path", MediaPath, REQUIRED],
  ["timeline", TimeRange, REQUIRED],
  ["source", nullable(TimeRange), null],
  ["volume", Volume, 1],
  ["speed", nullable(Speed), null],
  ["change_pitch", strictBool, false],
  ["fit", Fit, "contain"],
  ["transform", nullable(ClipTransform), null],
  ["crop", nullable(CropSettings), null],
  ["material_name", nullable(Label), null],
]);

const TextBubbleOptions = model([["effect_id", Label, REQUIRED], ["resource_id", Label, REQUIRED]]);
const TextBorderOptions = model([["color", Color6, "#000000"], ["alpha", Unit, 1], ["width", Percent, 40]]);
const TextBackgroundOptions = model([
  ["color", Color8, REQUIRED], ["style", literal([1, 2]), 1], ["alpha", Unit, 1], ["round_radius", Unit, 0],
  ["height", LineWidth, 0.14], ["width", LineWidth, 0.14], ["horizontal_offset", Unit, 0.5], ["vertical_offset", Unit, 0.5],
]);
const TextShadowOptions = model([
  ["color", Color6, "#000000"], ["alpha", Unit, 1], ["diffuse", Percent, 15], ["distance", Percent, 5], ["angle", Angle, -45],
]);

const TextSegmentCreate = model([
  ["text", TextBody, REQUIRED],
  ["fit", Fit, "contain"],
  ["timeline", TimeRange, REQUIRED],
  ["transform", nullable(ClipTransform), null],
  ["font_size", FontSize, 8],
  ["color", Color6, "#FFFFFF"],
  ["alignment", Alignment, "center"],
  ["font_family", nullable(Label), null],
  ["bold", strictBool, false],
  ["italic", strictBool, false],
  ["underline", strictBool, false],
  ["opacity", Unit, 1],
  ["vertical", strictBool, false],
  ["letter_spacing", Spacing, 0],
  ["line_spacing", Spacing, 0],
  ["auto_wrapping", strictBool, true],
  ["max_line_width", LineWidth, 0.82],
  ["border", nullable(TextBorderOptions), null],
  ["background", nullable(TextBackgroundOptions), null],
  ["shadow", nullable(TextShadowOptions), null],
  ["bubble", nullable(TextBubbleOptions), null],
  ["text_effect_id", nullable(Label), null],
]);

const SegmentUpdate = model([
  ["timeline", nullable(TimeRange), null],
  ["source", nullable(TimeRange), null],
  ["volume", nullable(Volume), null],
  ["speed", nullable(Speed), null],
  ["change_pitch", nullable(strictBool), null],
  ["fit", nullable(Fit), null],
  ["transform", nullable(ClipTransform), null],
  ["text", nullable(TextBody), null],
  ["font_size", nullable(FontSize), null],
  ["color", nullable(Color6), null],
  ["alignment", nullable(Alignment), null],
  ["path", nullable(MediaPath), null],
  ["material_name", nullable(Label), null],
  ["crop", nullable(CropSettings), null],
  ["font_family", nullable(Label), null],
  ["bold", nullable(strictBool), null],
  ["italic", nullable(strictBool), null],
  ["underline", nullable(strictBool), null],
  ["opacity", nullable(Unit), null],
  ["vertical", nullable(strictBool), null],
  ["letter_spacing", nullable(Spacing), null],
  ["line_spacing", nullable(Spacing), null],
  ["auto_wrapping", nullable(strictBool), null],
  ["max_line_width", nullable(LineWidth), null],
  ["border", nullable(TextBorderOptions), null],
  ["background", nullable(TextBackgroundOptions), null],
  ["shadow", nullable(TextShadowOptions), null],
  ["bubble", nullable(TextBubbleOptions), null],
  ["text_effect_id", nullable(Label), null],
]);

const MaskCreate = model([
  ["mask_type", Label, REQUIRED],
  ["center_x", finiteFloat({ ge: -10000, le: 10000 }), 0],
  ["center_y", finiteFloat({ ge: -10000, le: 10000 }), 0],
  ["size", LineWidth, 0.5],
  ["rotation", Angle, 0],
  ["feather", Percent, 0],
  ["invert", strictBool, false],
  ["rect_width", nullable(LineWidth), null],
  ["round_corner", nullable(Percent), null],
]);

const ChromaCreate = model([
  ["color", Color8, REQUIRED], ["intensity", Percent, 20], ["shadow", Percent, 0], ["edge_smooth", Percent, 0], ["spill", Percent, 0],
]);
const BackgroundFillCreate = model([["fill_type", literal(["blur", "color"]), REQUIRED], ["blur", Unit, 0.0625], ["color", Color8, "#00000000"]]);
const MixModeCreate = model([["mode", Label, REQUIRED]]);

const KEYFRAME_PROPERTIES = [
  "transform_x", "transform_y", "position_x", "position_y", "scale_x", "scale_y", "uniform_scale",
  "rotation", "alpha", "volume", "brightness", "contrast", "saturation",
];
const KeyframeCreate = model([["property", literal(KEYFRAME_PROPERTIES), REQUIRED], ["offset_us", TimeUs, REQUIRED], ["value", finiteFloat(), REQUIRED]]);
const KeyframeBatch = model([["keyframes", list(KeyframeCreate, { min: 1, max: 500 }), REQUIRED]]);

const effectFields = [["effect_type", Label, REQUIRED], ["params", nullable(list(Percent, { max: 64 })), null]];
const filterFields = [["filter_type", Label, REQUIRED], ["intensity", Percent, 100]];
const EffectCreate = model(effectFields);
const FilterCreate = model(filterFields);
const TrackEffectCreate = model([...effectFields, ["timeline", TimeRange, REQUIRED]]);
const TrackFilterCreate = model([...filterFields, ["timeline", TimeRange, REQUIRED]]);
const TransitionCreate = model([["transition_type", Label, REQUIRED], ["duration_us", nullable(DurationUs), null]]);
const FadeCreate = model([["in_duration_us", TimeUs, 0], ["out_duration_us", TimeUs, 0]], {
  after: (fade) => (fade.in_duration_us === 0 && fade.out_duration_us === 0 ? "fade duration must be positive" : null),
});
const AnimationCreate = model([["animation_type", Label, REQUIRED], ["duration_us", nullable(DurationUs), null]]);
const StickerSegmentCreate = model([["resource_id", Label, REQUIRED], ["timeline", TimeRange, REQUIRED], ["transform", nullable(ClipTransform), null]]);
const MarkerCreate = model([["time_us", TimeUs, REQUIRED], ["label", Label, REQUIRED], ["color", Color6, "#FFFFFF"]]);
const SegmentMove = model([["start_us", TimeUs, REQUIRED]]);
const SegmentSplit = model([["offset_us", DurationUs, REQUIRED]]);
const SubtitleEntry = model([["start_us", TimeUs, REQUIRED], ["end_us", laxInt({ gt: 0, le: MAX_TIME_US }), REQUIRED], ["text", TextBody, REQUIRED]], {
  after: (entry) => (entry.end_us <= entry.start_us ? "end_us must be greater than start_us" : null),
});

// 原仓库 HTTP 路由上的查询/路径参数
const Revision = ["expect_revision", nullable(laxInt({ ge: 1 })), null];
const RevisionParams = model([Revision]);
const ListParams = model([["limit", laxInt(), 100], ["offset", laxInt(), 0]]);
const KeyframeParams = model([["property_name", str(), REQUIRED], ["offset_us", laxInt(), REQUIRED], Revision]);
const FeatureParams = model([["feature", str(), REQUIRED], Revision]);
const MarkerParams = model([["time_us", laxInt(), REQUIRED], Revision]);
const SrtParams = model([["group_id", nullable(str()), null], Revision]);

const revisionInput = (expectRevision) => given({ expect_revision: expectRevision });

/** 服务内部用 TimeRange(...) 构造区间时同样会校验（原仓库此时抛 pydantic 错误）。 */
function timeRange(fields) {
  const [range] = check([TimeRange, fields]);
  return range;
}

// ---------------------------------------------------------------- 数值细节

/** Python round()：四舍六入五成双。 */
function pyRound(value) {
  const floor = Math.floor(value);
  const rest = value - floor;
  if (rest > 0.5) return floor + 1;
  if (rest < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/**
 * 从片段时长和倍速反推截取长度。先照原仓库取 round(时长×倍速)；它超出素材剩下的长度时，
 * 素材剩下的长度只要换算回来也是这个时长就用它。
 * 例：整段素材 D=2739955µs 按 2 倍速放，时长 round(D/2)=1369978，反推 1369978×2=D+1 会超出素材，
 * 其实 D 本身就对得上（round(D/2) 正是 1369978）。
 */
function sourceDurationFor(durationUs, speed, availableUs) {
  const preferred = pyRound(durationUs * speed);
  if (preferred > availableUs && availableUs > 0 && pyRound(availableUs / speed) === durationUs) return availableUs;
  return preferred;
}

const bitLength = (value) => value.toString(2).length;

/** Python 的 int * int / int：对精确商只舍入一次。乘积可能超过 2^53，用 BigInt 复现。 */
function exactRatio(numerator, denominator) {
  if (numerator === 0n) return 0;
  let shift = BigInt(53 - (bitLength(numerator) - bitLength(denominator)));
  const quotient = () => (shift >= 0n
    ? [(numerator << shift) / denominator, (numerator << shift) % denominator, denominator]
    : [numerator / (denominator << -shift), numerator % (denominator << -shift), denominator << -shift]);
  let [q, r, d] = quotient();
  if (q >= 1n << 53n) {
    shift -= 1n;
    [q, r, d] = quotient();
  }
  if (2n * r > d || (2n * r === d && q % 2n === 1n)) q += 1n;
  const scale = 2 ** Number(shift);
  return Number(q) / scale;
}

/** 浮点数按 Python repr 写进报错文字（1 → "1.0"）。 */
const pyFloat = (value) => (Number.isInteger(value) ? value.toFixed(1) : String(value));

/** pydantic 把 UTC 时间写成 2026-01-01T00:00:00Z / 2026-01-01T00:00:01.500000Z。 */
function isoTime(date) {
  const text = date.toISOString();
  return date.getUTCMilliseconds() ? text.replace(/\.(\d{3})Z$/u, ".$1000Z") : text.replace(/\.\d{3}Z$/u, "Z");
}

const byPropertyThenOffset = (left, right) => (left.property < right.property ? -1
  : left.property > right.property ? 1 : left.offset_us - right.offset_us);

// ---------------------------------------------------------------- SRT

// Python 的 \d 认所有 Unicode 十进制数字（Nd），int() 也能换算。原仓库跑在 Python 3.12（Unicode 15.0），
// 这里减掉 15.0 之后才加进 Nd 的几组数字，免得随 Node/Electron 的 Unicode 版本漂移。
const PY_DECIMAL = String.raw`[\p{Nd}--[\u{10d40}-\u{10d49}\u{116d0}-\u{116e3}\u{11bf0}-\u{11bf9}\u{11de0}-\u{11de9}\u{16130}-\u{16139}\u{16d70}-\u{16d79}\u{1ccf0}-\u{1ccf9}\u{1e5f1}-\u{1e5fa}]]`;
// str.isdigit() 另外还认 Numeric_Type=Digit（²、①、⑴ 这类），JS 没有这个属性，按 Python 3.12 导出：
// [c for c in range(0x110000) if chr(c).isdigit() and not chr(c).isdecimal()]
const PY_DIGIT_ONLY = String.raw`\u{b2}-\u{b3}\u{b9}\u{1369}-\u{1371}\u{19da}\u{2070}\u{2074}-\u{2079}\u{2080}-\u{2089}\u{2460}-\u{2468}\u{2474}-\u{247c}\u{2488}-\u{2490}\u{24ea}\u{24f5}-\u{24fd}\u{24ff}\u{2776}-\u{277e}\u{2780}-\u{2788}\u{278a}-\u{2792}\u{10a40}-\u{10a43}\u{10e60}-\u{10e68}\u{11052}-\u{1105a}\u{1f100}-\u{1f10a}`;
const PY_DECIMAL_CHAR = new RegExp(`^${PY_DECIMAL}$`, "v");
const SRT_INDEX = new RegExp(`^[${PY_DECIMAL}${PY_DIGIT_ONLY}]+$`, "v");

function digitValue(text) {
  let total = 0;
  for (const char of text) {
    let start = char.codePointAt(0);
    while (PY_DECIMAL_CHAR.test(String.fromCodePoint(start - 1))) start -= 1;
    total = total * 10 + ((char.codePointAt(0) - start) % 10);
  }
  return total;
}

const SRT_TIME = `(${PY_DECIMAL}{1,3}):(${PY_DECIMAL}{2}):(${PY_DECIMAL}{2})[,.](${PY_DECIMAL}{3})`;
const SRT_INTERVAL = new RegExp(`^${PY_S}*${SRT_TIME}${PY_S}*-->${PY_S}*${SRT_TIME}(?:${PY_S}+[^\\n]*)?$`, "v");

function srtTimestamp(hours, minutes, seconds, millis) {
  const minute = digitValue(minutes);
  const second = digitValue(seconds);
  if (minute > 59 || second > 59) throw invalid("SRT_INVALID", "SRT time component is invalid");
  return ((digitValue(hours) * 60 + minute) * 60 + second) * 1_000_000 + digitValue(millis) * 1000;
}

function parseSrtInterval(line) {
  const match = SRT_INTERVAL.exec(line);
  if (!match) throw invalid("SRT_INVALID", "SRT timing is invalid");
  const start = srtTimestamp(match[1], match[2], match[3], match[4]);
  const end = srtTimestamp(match[5], match[6], match[7], match[8]);
  if (end <= start) throw invalid("SRT_INVALID", "SRT interval must be positive");
  return [start, end];
}

export function parseSrt(content) {
  const text = content.replace(/^﻿+/u, "").replaceAll("\r\n", "\n").replaceAll("\r", "\n");
  const entries = [];
  for (const block of text.split("\n\n").filter((item) => pyStrip(item))) {
    let lines = block.split("\n");
    if (!lines.length || !pyStrip(lines[0])) continue;
    if (SRT_INDEX.test(pyStrip(lines[0]))) lines = lines.slice(1);
    if (!lines.length || !lines[0].includes("-->")) throw invalid("SRT_INVALID", "Expected an SRT timing line");
    const [start, end] = parseSrtInterval(lines[0]);
    const caption = pyStrip(lines.slice(1).join("\n"));
    if (!caption) throw invalid("SRT_INVALID", "SRT caption is empty");
    const errors = [];
    const entry = SubtitleEntry({ start_us: start, end_us: end, text: caption }, [], errors);
    if (errors.length) throw invalid("SRT_INVALID", "SRT times exceed the 24-hour limit");
    entries.push(entry);
  }
  if (!entries.length) throw invalid("SRT_INVALID", "SRT contains no captions");
  return entries;
}

// ---------------------------------------------------------------- 工程查找与校验（service.py 的静态方法）

const KEYFRAME_RANGES = {
  alpha: [0, 1], volume: [0, 2], scale_x: [0.01, 100], scale_y: [0.01, 100], uniform_scale: [0.01, 100],
  transform_x: [-1, 1], transform_y: [-1, 1], rotation: [-360, 360], brightness: [-1, 1], contrast: [-1, 1], saturation: [-1, 1],
};

function canonicalKeyframeProperty(value) {
  const canonical = { position_x: "transform_x", position_y: "transform_y" }[value] ?? value;
  if (typeof canonical !== "string" || !Object.hasOwn(KEYFRAME_RANGES, canonical)) {
    throw invalid("KEYFRAME_PROPERTY_INVALID", "Unsupported keyframe property");
  }
  return canonical;
}

function validateKeyframeValue(item) {
  const property = canonicalKeyframeProperty(item.property);
  const [low, high] = KEYFRAME_RANGES[property];
  if (!(low <= item.value && item.value <= high)) {
    throw invalid("KEYFRAME_VALUE_INVALID", `Value for ${property} must be between ${pyFloat(low)} and ${pyFloat(high)}`);
  }
}

function splitKeyframes(keyframes, offset) {
  const left = [];
  const right = [];
  for (const property of new Set(keyframes.map((item) => item.property))) {
    const points = keyframes.filter((item) => item.property === property).sort((a, b) => a.offset_us - b.offset_us);
    const before = points.filter((item) => item.offset_us < offset);
    const exact = points.filter((item) => item.offset_us === offset);
    const after = points.filter((item) => item.offset_us > offset);
    left.push(...before.map((item) => ({ ...item })));
    right.push(...after.map((item) => ({ ...item, offset_us: item.offset_us - offset })));
    if (exact.length) {
      const boundary = { ...exact[0] };
      left.push(boundary);
      right.push({ ...boundary, offset_us: 0 });
    } else if (before.length && after.length) {
      const first = before.at(-1);
      const last = after[0];
      const ratio = (offset - first.offset_us) / (last.offset_us - first.offset_us);
      const boundary = { property, offset_us: offset, value: first.value + (last.value - first.value) * ratio };
      left.push(boundary);
      right.push({ ...boundary, offset_us: 0 });
    }
  }
  return [left.sort(byPropertyThenOffset), right.sort(byPropertyThenOffset)];
}

function trackAny(project, trackId) {
  const track = project.tracks.find((item) => item.id === trackId);
  if (!track) throw invalid("TRACK_NOT_FOUND", "Track does not exist");
  return track;
}

function editableTrack(project, trackId) {
  const track = project.tracks.find((item) => item.id === trackId && item.kind !== "subtitle");
  if (!track) throw invalid("TRACK_NOT_FOUND", "Track does not exist");
  return track;
}

function segmentLocation(project, segmentId) {
  for (const track of project.tracks) {
    for (const segment of track.segments ?? []) {
      if (segment.id === segmentId) return [track, segment];
    }
  }
  throw invalid("SEGMENT_NOT_FOUND", "Segment does not exist");
}

function validatePlacement(project, trackId, timeline, ignoreSegmentId = null) {
  for (const track of project.tracks) {
    if (track.kind === "subtitle" || (trackId !== null && track.id !== trackId)) continue;
    for (const segment of track.segments ?? []) {
      if (ignoreSegmentId !== null && segment.id === ignoreSegmentId) continue;
      const existing = segment.timeline;
      if (timeline.start_us < existing.start_us + existing.duration_us
        && existing.start_us < timeline.start_us + timeline.duration_us) {
        throw invalid("TIMELINE_OVERLAP", "Clips overlap on the track");
      }
    }
  }
}

// 挪到或复制到别的轨道时，每种片段能去哪种轨道（原仓库没有跨轨道操作，这里是新增的规则）
const TRACK_FOR_KIND = { video: "video", image: "video", audio: "audio", text: "text", sticker: "sticker", track_effect: "effect", track_filter: "filter" };

function checkTrackFits(segment, from, to) {
  if (from.kind === "subtitle") throw invalid("SUBTITLE_CLIP_LOCKED", "Subtitle clips stay in their subtitle group; they cannot be moved to or copied onto another track");
  if (to.kind === "subtitle") throw invalid("SUBTITLE_TRACK_LOCKED", "Subtitle tracks only take whole subtitle groups");
  const wanted = TRACK_FOR_KIND[segment.kind];
  if (wanted !== to.track_type) throw invalid("TRACK_TYPE_MISMATCH", `A ${segment.kind} clip can only go on a ${wanted ?? "matching"} track`);
}

/** 复制出来的片段换新 id；嵌套里有自己 id 的也换，免得两段共用一个。 */
function withFreshIds(value, newId) {
  if (Array.isArray(value)) return value.map((item) => withFreshIds(item, newId));
  if (!value || typeof value !== "object") return value;
  const copy = Object.fromEntries(Object.entries(value).map(([key, item]) => [key, withFreshIds(item, newId)]));
  if (typeof copy.id === "string") copy.id = newId();
  return copy;
}

/** 依次在几个枚举里按名字找，都没有就返回 null（对应原仓库 try/except 循环）。 */
function findMember(enumNames, raw) {
  for (const enumName of enumNames) {
    const found = members(enumName).find((member) => member.name === raw)
      ?? members(enumName).find((member) => (member.meta.name ?? member.meta.title) === raw);
    if (found) return found;
  }
  return null;
}

function requireMember(enumName, raw) {
  const found = findMember([enumName], raw);
  if (!found) throw invalid("DRAFT_FEATURE_INVALID", `Unknown ${enumName}: ${raw}`);
  return found;
}

const VIDEO_EFFECTS = ["VideoSceneEffectType", "VideoCharacterEffectType"];
const AUDIO_EFFECTS = ["AudioSceneEffectType", "ToneEffectType", "SpeechToSongType"];
const TEXT_ANIMATIONS = ["TextIntro", "TextOutro", "TextLoopAnim"];
const VIDEO_ANIMATIONS = ["IntroType", "OutroType", "GroupAnimationType"];
const CLEARABLE = new Set([
  "effects", "filters", "animations", "transition", "fade", "mask", "chroma", "background_filling",
  "mix_mode", "crop", "border", "background", "shadow", "bubble", "text_effect",
]);
const TEXT_ONLY_FIELDS = ["font_family", "bold", "italic", "underline", "opacity", "vertical", "letter_spacing",
  "line_spacing", "auto_wrapping", "max_line_width", "border", "background", "shadow", "bubble", "text_effect_id"];
const TEXT_CONTENT_FIELDS = ["text", "font_size", "color", "alignment", "bubble", "text_effect_id"];

const mediaKey = async (raw) => (await realpath(raw).catch(() => path.resolve(raw))).toLowerCase();

function storeError(code, message, status, details) {
  return new EditError(code, message, { status, details });
}

const projectNotFound = () => storeError("PROJECT_NOT_FOUND", "Project not found", 404);

// ---------------------------------------------------------------- ProjectStore

export class ProjectStore {
  #directory;
  #queues = new Map();

  /**
   * @param {object} options
   * @param {string} options.directory 工程文件夹，每个工程一个 <id>.json
   * @param {string[]} [options.mediaRoots] 素材白名单根目录
   * @param {(raw: string, options: object) => Promise<object>} [options.probe] 默认用 media.mjs 的 probeMedia
   */
  constructor({ directory, mediaRoots = [], probe, newId, now, maxProjectBytes = MAX_PROJECT_BYTES, maxMediaBytes } = {}) {
    if (!directory) throw new Error("ProjectStore needs a directory");
    this.#directory = path.resolve(directory);
    this.mediaRoots = mediaRoots;
    this.maxMediaBytes = maxMediaBytes;
    this.maxProjectBytes = maxProjectBytes;
    // media.mjs 在别的模块里，按需加载，免得它没就绪时这里也加载失败
    this.probe = probe ?? (async (raw, options) => (await import("./media.mjs")).probeMedia(raw, options));
    this.newId = newId ?? (() => randomBytes(16).toString("hex"));
    this.now = now ?? (() => new Date());
  }

  // ---- storage.py

  #file(projectId) {
    if (typeof projectId !== "string" || !PROJECT_ID.test(projectId)) throw projectNotFound();
    return path.join(this.#directory, `${projectId}.json`);
  }

  async #get(projectId) {
    const file = this.#file(projectId);
    let text;
    try {
      text = await readFile(file, "utf8");
    } catch {
      throw projectNotFound();
    }
    let project;
    try {
      project = JSON.parse(text);
    } catch {
      throw storeError("STORE_CORRUPT", "Stored project cannot be read", 500);
    }
    if (!project || typeof project !== "object" || typeof project.name !== "string" || !project.canvas
      || typeof project.created_at !== "string" || typeof project.updated_at !== "string"
      || !Number.isInteger(project.revision) || !Array.isArray(project.tracks) || !Array.isArray(project.markers)) {
      throw storeError("STORE_CORRUPT", "Stored project cannot be read", 500);
    }
    if (project.id !== projectId) throw storeError("STORE_CORRUPT", "Stored project identity mismatch", 500);
    const { name, canvas, id, created_at, updated_at, revision, tracks, markers, draft = null } = project;
    return { name, canvas, id, created_at, updated_at, revision, tracks, markers, draft };
  }

  async #write(project, { replace }) {
    const encoded = Buffer.from(`${JSON.stringify(project, null, 2)}\n`, "utf8");
    if (encoded.length > this.maxProjectBytes) throw storeError("PROJECT_TOO_LARGE", "Project exceeds size limit", 413);
    await mkdir(this.#directory, { recursive: true, mode: 0o700 });
    const target = this.#file(project.id);
    const temporary = path.join(this.#directory, `.${project.id}.json.${randomBytes(8).toString("hex")}.tmp`);
    // 同 storage.atomic_write：写入、落盘、发布任何一步失败，临时文件都要删掉（磁盘满时别越积越多）
    try {
      const handle = await open(temporary, "wx", 0o600);
      try {
        await handle.writeFile(encoded);
        await handle.sync();
      } finally {
        await handle.close();
      }
      if (replace) {
        await renameRetrying(temporary, target);
      } else {
        await link(temporary, target).catch((error) => {
          if (error.code === "EEXIST") throw storeError("PROJECT_CONFLICT", "Target already exists", 409);
          throw error;
        });
      }
    } finally {
      await unlink(temporary).catch(() => undefined);
    }
  }

  async #save(project, expectRevision) {
    const current = await this.#get(project.id);
    if (current.revision !== expectRevision) {
      throw storeError("PROJECT_CONFLICT", "Project changed; read it again before saving", 409, { current_revision: current.revision });
    }
    const next = { ...project, revision: expectRevision + 1 };
    await this.#write(next, { replace: true });
    return next;
  }

  /** 同一工程的写入排队执行，读改写之间不会被别的写插进来。 */
  #serial(projectId, task) {
    const key = String(projectId);
    const previous = this.#queues.get(key) ?? Promise.resolve();
    const run = previous.then(task, task);
    const tail = run.then(() => undefined, () => undefined);
    this.#queues.set(key, tail);
    tail.then(() => {
      if (this.#queues.get(key) === tail) this.#queues.delete(key);
    });
    return run;
  }

  #timestamp() {
    return isoTime(new Date(this.now()));
  }

  async #expected(projectId, expectRevision) {
    const project = await this.#get(projectId);
    if (expectRevision !== null && expectRevision !== undefined && project.revision !== expectRevision) {
      throw storeError("PROJECT_CONFLICT", "Project changed; read it again before saving", 409, { current_revision: project.revision });
    }
    return project;
  }

  #touch(project, updates) {
    return this.#save({ ...project, ...updates, updated_at: this.#timestamp(), draft: null }, project.revision);
  }

  #saveTracks(project, tracks) {
    return this.#touch(project, { tracks });
  }

  #replaceSegment(project, trackId, segmentId, replacement) {
    return this.#saveTracks(project, project.tracks.map((track) => (track.id === trackId
      ? { ...track, segments: (track.segments ?? []).map((item) => (item.id === segmentId ? replacement : item)) }
      : track)));
  }

  #appendSegment(project, track, segment) {
    return this.#saveTracks(project, project.tracks.map((item) => (item.id === track.id
      ? { ...item, segments: [...item.segments, segment] }
      : item)));
  }

  #probe(raw) {
    return this.probe(raw, defined({ roots: this.mediaRoots, maxBytes: this.maxMediaBytes }));
  }

  #segmentId() {
    return this.newId();
  }

  // ---- 工程

  async createProject(request) {
    const [body] = check([ProjectCreate, request]);
    const time = this.#timestamp();
    const exists = (id) => stat(this.#file(id)).then(() => true, () => false);
    let id = this.newId();
    while (await exists(id)) id = this.newId();
    const project = { name: body.name, canvas: body.canvas, id, created_at: time, updated_at: time, revision: 1, tracks: [], markers: [], draft: null };
    await this.#serial(id, async () => {
      if (await exists(id)) {
        throw storeError("PROJECT_EXISTS", "Project already exists", 409);
      }
      await this.#write(project, { replace: false });
    });
    return project;
  }

  async listProjects({ limit, offset } = {}) {
    const [params] = check([ListParams, given({ limit, offset })]);
    if (!(params.limit >= 1 && params.limit <= 1000) || !(params.offset >= 0 && params.offset <= 1_000_000)) {
      throw invalid("PAGINATION_INVALID", "Pagination is out of range");
    }
    const names = await readdir(this.#directory).catch(() => []);
    const projects = [];
    for (const name of names) {
      const match = /^([0-9a-f]{32})\.json$/u.exec(name);
      if (!match) continue;
      try {
        projects.push(await this.#get(match[1]));
      } catch (error) {
        if (!(error instanceof EditError)) throw error;
      }
    }
    projects.sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at));
    return projects.slice(params.offset, params.offset + params.limit);
  }

  getProject(projectId) {
    return this.#get(projectId);
  }

  /** 原仓库没有删除接口；会话删掉时用，连文件一起删。 */
  async deleteProject(projectId, { expectRevision } = {}) {
    const [params] = check([RevisionParams, revisionInput(expectRevision)]);
    return this.#serial(projectId, async () => {
      await this.#expected(projectId, params.expect_revision);
      await unlink(this.#file(projectId)).catch((error) => {
        if (error.code !== "ENOENT") throw error;
      });
    });
  }

  async updateProject(projectId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [ProjectUpdate, request]);
    const updates = Object.fromEntries(givenKeys(request).map((key) => [key, body[key]]));
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      if (Object.values(updates).some((value) => value === null)) {
        throw invalid("PROJECT_UPDATE_INVALID", "Project name cannot be null");
      }
      if (!Object.keys(updates).length) return project;
      return this.#touch(project, updates);
    });
  }

  async setCanvas(projectId, canvas, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [Canvas, canvas]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      return this.#touch(project, { canvas: body });
    });
  }

  /** 原仓库没有：撤销、退回时把存档里的整份工程写回（名字、画布、轨道、标记），revision 照常 +1。 */
  async restoreProject(projectId, { name, canvas, tracks, markers } = {}) {
    const [body] = check([ProjectCreate, { name, canvas }]);
    if (!Array.isArray(tracks) || !Array.isArray(markers)) throw invalid("VALIDATION_ERROR", "Snapshot tracks and markers must be lists");
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, null);
      return this.#touch(project, { name: body.name, canvas: body.canvas, tracks: structuredClone(tracks), markers: structuredClone(markers) });
    });
  }

  // ---- 轨道

  async createTrack(projectId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [TrackCreate, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const existing = new Set(project.tracks.map((track) => track.id));
      let id = this.newId();
      while (existing.has(id)) id = this.newId();
      const track = {
        id,
        track_type: body.track_type,
        name: body.name || body.track_type[0].toUpperCase() + body.track_type.slice(1),
        mute: body.mute,
        segments: [],
      };
      return this.#touch(project, { tracks: [...project.tracks, track] });
    });
  }

  async updateTrack(projectId, trackId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [TrackUpdate, request]);
    const changes = Object.fromEntries(givenKeys(request).map((key) => [key, body[key]]));
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      trackAny(project, trackId);
      if (Object.values(changes).some((value) => value === null)) {
        throw invalid("TRACK_UPDATE_INVALID", "Track fields cannot be null");
      }
      if (!Object.keys(changes).length) return project;
      return this.#saveTracks(project, project.tracks.map((track) => (track.id === trackId ? { ...track, ...changes } : track)));
    });
  }

  async deleteTrack(projectId, trackId, { expectRevision } = {}) {
    const [params] = check([RevisionParams, revisionInput(expectRevision)]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      trackAny(project, trackId);
      return this.#saveTracks(project, project.tracks.filter((track) => track.id !== trackId));
    });
  }

  async reorderTracks(projectId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [TrackOrder, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const current = new Set(project.tracks.map((track) => String(track.id)));
      const requested = body.track_ids;
      const unique = new Set(requested);
      if (unique.size !== requested.length || unique.size !== current.size || requested.some((id) => !current.has(id))) {
        throw invalid("TRACK_ORDER_INVALID", "track_ids must contain every track exactly once");
      }
      const byId = new Map(project.tracks.map((track) => [String(track.id), track]));
      return this.#saveTracks(project, requested.map((id) => byId.get(id)));
    });
  }

  // ---- 添加片段

  async addMediaSegment(projectId, trackId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [MediaSegmentCreate, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const track = editableTrack(project, trackId);
      const info = await this.#probe(body.path);
      if (info.kind === "audio" && track.track_type !== "audio") throw invalid("MEDIA_TRACK_MISMATCH", "Audio needs an audio track");
      if (info.kind !== "audio" && track.track_type !== "video") throw invalid("MEDIA_TRACK_MISMATCH", "Picture needs a video track");
      const source = body.source ?? timeRange({
        duration_us: body.speed !== null
          ? sourceDurationFor(body.timeline.duration_us, body.speed, info.duration_us)
          : body.timeline.duration_us,
      });
      const speed = body.speed ?? source.duration_us / body.timeline.duration_us;
      if (source.start_us + source.duration_us > info.duration_us) throw invalid("SOURCE_RANGE_INVALID", "Source range exceeds media duration");
      if (info.kind === "image" && speed !== 1) throw invalid("SPEED_INVALID", "Still images do not support speed changes");
      if (info.kind === "audio" && (body.transform !== null || body.fit !== "contain")) {
        throw invalid("SEGMENT_FIELD_MISMATCH", "Audio clips do not support visual transforms");
      }
      if (info.kind === "audio" && body.crop !== null) throw invalid("SEGMENT_FIELD_MISMATCH", "Audio clips do not support cropping");
      const mismatch = () => invalid("MEDIA_DURATION_MISMATCH", "Timeline duration must equal source duration divided by speed");
      if (body.speed !== null && pyRound(source.duration_us / speed) !== body.timeline.duration_us) throw mismatch();
      validatePlacement(project, trackId, body.timeline);
      if (pyRound(source.duration_us / speed) !== body.timeline.duration_us) throw mismatch();
      const segment = {
        id: this.#segmentId(),
        kind: info.kind,
        path: info.path,
        timeline: body.timeline,
        source,
        volume: body.volume,
        speed,
        change_pitch: body.change_pitch,
        fit: body.fit,
        transform: body.transform,
        crop: body.crop,
        material_name: body.material_name || path.basename(info.path),
        keyframes: [],
        effects: [],
        filters: [],
        animations: [],
        transition: null,
        fade: null,
      };
      return this.#appendSegment(project, track, segment);
    });
  }

  async addTextSegment(projectId, trackId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [TextSegmentCreate, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const track = editableTrack(project, trackId);
      if (track.track_type !== "text") throw invalid("TRACK_TYPE_MISMATCH", "Text needs a text track");
      validatePlacement(project, trackId, body.timeline);
      const segment = {
        id: this.#segmentId(),
        kind: "text",
        text: body.text,
        timeline: body.timeline,
        transform: body.transform,
        font_size: body.font_size,
        color: body.color.toUpperCase(),
        alignment: body.alignment,
        font_family: body.font_family,
        bold: body.bold,
        italic: body.italic,
        underline: body.underline,
        opacity: body.opacity,
        vertical: body.vertical,
        letter_spacing: body.letter_spacing,
        line_spacing: body.line_spacing,
        auto_wrapping: body.auto_wrapping,
        max_line_width: body.max_line_width,
        border: body.border,
        background: body.background,
        shadow: body.shadow,
        bubble: body.bubble,
        text_effect_id: body.text_effect_id,
        keyframes: [],
        animations: [],
      };
      return this.#appendSegment(project, track, segment);
    });
  }

  async addStickerSegment(projectId, trackId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [StickerSegmentCreate, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const track = editableTrack(project, trackId);
      if (track.track_type !== "sticker") throw invalid("TRACK_TYPE_MISMATCH", "Stickers need a sticker track");
      validatePlacement(project, trackId, body.timeline);
      const segment = {
        id: this.#segmentId(),
        kind: "sticker",
        resource_id: body.resource_id,
        timeline: body.timeline,
        transform: body.transform,
        keyframes: [],
      };
      return this.#appendSegment(project, track, segment);
    });
  }

  async addTrackEffectSegment(projectId, trackId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [TrackEffectCreate, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const track = editableTrack(project, trackId);
      if (track.track_type !== "effect") throw invalid("TRACK_TYPE_MISMATCH", "Effects need an effect track");
      const effect = findMember(VIDEO_EFFECTS, body.effect_type);
      if (!effect) throw invalid("EFFECT_INVALID", "Unknown video effect");
      if (body.params !== null && body.params.length > effect.meta.params.length) {
        throw invalid("EFFECT_PARAMS_INVALID", "Too many effect parameters");
      }
      validatePlacement(project, trackId, body.timeline);
      const segment = {
        id: this.#segmentId(),
        kind: "track_effect",
        effect_type: body.effect_type,
        params: body.params,
        timeline: body.timeline,
      };
      return this.#appendSegment(project, track, segment);
    });
  }

  async addTrackFilterSegment(projectId, trackId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [TrackFilterCreate, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const track = editableTrack(project, trackId);
      if (track.track_type !== "filter") throw invalid("TRACK_TYPE_MISMATCH", "Filters need a filter track");
      requireMember("FilterType", body.filter_type);
      validatePlacement(project, trackId, body.timeline);
      const segment = {
        id: this.#segmentId(),
        kind: "track_filter",
        filter_type: body.filter_type,
        intensity: body.intensity,
        timeline: body.timeline,
      };
      return this.#appendSegment(project, track, segment);
    });
  }

  // ---- 改、移、删、分割片段

  async updateSegment(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [SegmentUpdate, request]);
    const changes = Object.fromEntries(givenKeys(request).map((key) => [key, body[key]]));
    return this.#serial(projectId, () => this.#updateSegment(projectId, segmentId, changes, params.expect_revision));
  }

  async #updateSegment(projectId, segmentId, changes, expectRevision) {
    const project = await this.#expected(projectId, expectRevision);
    const [track, current] = segmentLocation(project, segmentId);
    if (!Object.keys(changes).length) return project;
    const kind = current.kind;
    const has = (...keys) => keys.some((key) => Object.hasOwn(changes, key));
    const nullable = new Set(["transform", "bubble", "text_effect_id"]);
    if (Object.entries(changes).some(([key, value]) => value === null && !nullable.has(key))) {
      throw invalid("SEGMENT_UPDATE_INVALID", "Only transform may be cleared with null");
    }
    if (kind === "text") {
      if (has("source", "volume", "speed", "change_pitch", "fit", "path", "material_name")) {
        throw invalid("SEGMENT_FIELD_MISMATCH", "Text clips only accept timeline, text style and transform fields");
      }
    } else if ((kind === "video" || kind === "image") && has(...TEXT_ONLY_FIELDS)) {
      throw invalid("SEGMENT_FIELD_MISMATCH", "Text style fields require a text clip");
    } else if (kind === "audio" && has("transform", "fit")) {
      throw invalid("SEGMENT_FIELD_MISMATCH", "Audio clips do not support visual transforms");
    } else if (kind === "audio" && has("crop")) {
      throw invalid("SEGMENT_FIELD_MISMATCH", "Audio clips do not support cropping");
    } else if (kind !== "sticker" && has(...TEXT_CONTENT_FIELDS)) {
      throw invalid("SEGMENT_FIELD_MISMATCH", "Text style fields require a text clip");
    } else if (kind === "sticker" && has(...TEXT_CONTENT_FIELDS)) {
      throw invalid("SEGMENT_FIELD_MISMATCH", "Text style fields require a text clip");
    } else if ((kind === "track_effect" || kind === "track_filter") && Object.keys(changes).some((key) => key !== "timeline")) {
      throw invalid("SEGMENT_FIELD_MISMATCH", "Track effect clips only accept timeline updates");
    } else if (!["video", "image", "audio", "sticker", "track_effect", "track_filter"].includes(kind)) {
      throw invalid("SEGMENT_KIND_INVALID", "Segment cannot be edited");
    }
    if (kind === "sticker" && has("source", "volume", "speed", "change_pitch", "fit", "path", "material_name", "crop")) {
      throw invalid("SEGMENT_FIELD_MISMATCH", "Sticker clips only accept timeline and transform fields");
    }

    let timeline = changes.timeline ?? timeRange(current.timeline);
    let sourceData = current.source;
    const speed = Number(has("speed") ? changes.speed : current.speed ?? 1);
    if (MEDIA_KINDS.has(kind)) {
      const info = await this.#probe(has("path") ? changes.path : current.path);
      if ((track.track_type === "audio") !== (info.kind === "audio")) {
        throw invalid("MEDIA_TRACK_MISMATCH", "Clip media type does not match track");
      }
      if (info.kind === "image" && speed !== 1) throw invalid("SPEED_INVALID", "Still images do not support speed changes");
      if (has("path")) {
        changes.path = info.path;
        changes.kind = info.kind;
        sourceData = null;
      }
      let source;
      if (has("source")) {
        if (changes.source === null) throw invalid("SOURCE_RANGE_INVALID", "A media source range is required");
        source = changes.source;
        sourceData = source;
      } else if (sourceData) {
        source = timeRange(sourceData);
      } else {
        source = timeRange({ duration_us: pyRound(timeline.duration_us * speed) });
      }
      if (has("timeline") && !has("source") && !has("path")) {
        // 只挪位置（时长和倍速都没变）就保留原截取区间，不来回换算：
        // 整段素材加速后 round(round(D/倍速)×倍速) 可能是 D+1，原仓库这里会报截取超出素材长度
        const moveOnly = sourceData && timeline.duration_us === current.timeline.duration_us && speed === Number(current.speed ?? 1);
        if (!moveOnly) {
          const available = info.duration_us - (sourceData?.start_us ?? 0);
          sourceData = { ...sourceData, duration_us: sourceDurationFor(timeline.duration_us, speed, available) };
        }
        source = timeRange(sourceData);
      } else if (!has("timeline") && has("source", "speed")) {
        timeline = timeRange({ start_us: timeline.start_us, duration_us: pyRound(source.duration_us / speed) });
        changes.timeline = timeline;
      }
      if (pyRound(source.duration_us / speed) !== timeline.duration_us) {
        throw invalid("MEDIA_DURATION_MISMATCH", "Timeline duration must equal source duration divided by speed");
      }
      if (source.start_us + source.duration_us > info.duration_us) {
        throw invalid("SOURCE_RANGE_INVALID", "Source range exceeds media duration");
      }
      changes.source = { start_us: source.start_us, duration_us: source.duration_us };
      changes.speed = speed;
    }
    if (has("timeline")) {
      validatePlacement(project, track.id, timeline, segmentId);
      changes.timeline = { start_us: timeline.start_us, duration_us: timeline.duration_us };
    }
    if (has("volume") && !MEDIA_KINDS.has(kind)) throw invalid("SEGMENT_FIELD_MISMATCH", "Volume is only valid for media clips");
    if (has("crop") && kind !== "video" && kind !== "image") {
      throw invalid("SEGMENT_FIELD_MISMATCH", "Cropping requires a video or image clip");
    }
    if (has("color") && changes.color !== null) changes.color = changes.color.toUpperCase();
    const updated = { ...current, ...changes };
    for (const keyframe of updated.keyframes ?? []) {
      if (keyframe.offset_us > updated.timeline.duration_us) {
        throw invalid("KEYFRAME_OFFSET_INVALID", "Updated clip duration would move a keyframe outside the clip");
      }
    }
    const fade = updated.fade;
    if (fade && fade.in_duration_us + fade.out_duration_us > updated.timeline.duration_us) {
      throw invalid("FADE_INVALID", "Fade duration exceeds clip duration");
    }
    return this.#replaceSegment(project, track.id, segmentId, updated);
  }

  /** trackId 是新增的：挪到另一条同类轨道，从原轨道拿走、放进新轨道是同一次写入。 */
  async moveSegment(projectId, segmentId, startUs, { expectRevision, trackId = null } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [SegmentMove, defined({ start_us: startUs })]);
    return this.#serial(projectId, async () => {
      const [track, segment] = segmentLocation(await this.#get(projectId), segmentId);
      const timeline = timeRange({ start_us: body.start_us, duration_us: segment.timeline.duration_us });
      if (trackId === null || trackId === track.id) return this.#updateSegment(projectId, segmentId, { timeline }, params.expect_revision);
      const project = await this.#expected(projectId, params.expect_revision);
      const target = trackAny(project, trackId);
      checkTrackFits(segment, track, target);
      validatePlacement(project, target.id, timeline);
      return this.#saveTracks(project, project.tracks.map((item) => {
        if (item.id === track.id) return { ...item, segments: item.segments.filter((entry) => entry.id !== segmentId) };
        if (item.id === target.id) return { ...item, segments: [...(item.segments ?? []), { ...segment, timeline }] };
        return item;
      }));
    });
  }

  /** 原仓库没有：复制一个片段（关键帧、变换、特效、动画、蒙版、截取这些全带上）放到 start_us，默认放在原轨道。返回 { project, segment }。 */
  async duplicateSegment(projectId, segmentId, startUs, { expectRevision, trackId = null } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [SegmentMove, defined({ start_us: startUs })]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const [track, current] = segmentLocation(project, segmentId);
      const target = trackId === null ? track : trackAny(project, trackId);
      checkTrackFits(current, track, target);
      const segment = withFreshIds(current, () => this.#segmentId());
      segment.timeline = timeRange({ start_us: body.start_us, duration_us: current.timeline.duration_us });
      validatePlacement(project, target.id, segment.timeline);
      return { project: await this.#appendSegment(project, target, segment), segment };
    });
  }

  async deleteSegment(projectId, segmentId, { expectRevision } = {}) {
    const [params] = check([RevisionParams, revisionInput(expectRevision)]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const [track] = segmentLocation(project, segmentId);
      return this.#saveTracks(project, project.tracks.map((item) => (item.id === track.id
        ? { ...item, segments: (item.segments ?? []).filter((segment) => segment.id !== segmentId) }
        : item)));
    });
  }

  /** 返回 { project, segments: [前半, 后半] }。 */
  async splitSegment(projectId, segmentId, offsetUs, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [SegmentSplit, defined({ offset_us: offsetUs })]);
    const offset = body.offset_us;
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const [track, current] = segmentLocation(project, segmentId);
      const duration = Math.trunc(current.timeline.duration_us);
      if (!(offset > 0 && offset < duration)) throw invalid("SPLIT_OFFSET_INVALID", "offset_us must be inside the segment");
      const first = { ...current };
      const second = { ...current };
      first.id = this.#segmentId();
      second.id = this.#segmentId();
      first.timeline = { ...current.timeline, duration_us: offset };
      second.timeline = { ...current.timeline, start_us: current.timeline.start_us + offset, duration_us: duration - offset };
      if (current.source) {
        const source = current.source;
        const firstSource = pyRound(exactRatio(BigInt(source.duration_us) * BigInt(offset), BigInt(duration)));
        if (firstSource <= 0 || firstSource >= source.duration_us) {
          throw invalid("SPLIT_OFFSET_INVALID", "Split would create an empty source range");
        }
        first.source = { ...source, duration_us: firstSource };
        second.source = { ...source, start_us: source.start_us + firstSource, duration_us: source.duration_us - firstSource };
      }
      [first.keyframes, second.keyframes] = splitKeyframes(current.keyframes ?? [], offset);
      const segments = (track.segments ?? []).flatMap((item) => (item.id === segmentId ? [first, second] : [item]));
      const saved = await this.#saveTracks(project, project.tracks.map((item) => (item.id === track.id ? { ...item, segments } : item)));
      return { project: saved, segments: [first, second] };
    });
  }

  async getSegment(projectId, segmentId) {
    const [, segment] = segmentLocation(await this.#get(projectId), segmentId);
    return { ...segment };
  }

  // ---- 关键帧

  async addKeyframes(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [KeyframeBatch, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const [track, current] = segmentLocation(project, segmentId);
      let keyframes = (current.keyframes ?? []).map((item) => ({ ...item }));
      const duration = Math.trunc(current.timeline.duration_us);
      for (const item of body.keyframes) {
        if (item.offset_us > duration) throw invalid("KEYFRAME_OFFSET_INVALID", "Keyframe offset must be within the segment");
        validateKeyframeValue(item);
        const property = canonicalKeyframeProperty(item.property);
        const kind = current.kind;
        if (property === "volume" && !MEDIA_KINDS.has(kind)) {
          throw invalid("KEYFRAME_PROPERTY_INVALID", "Volume keyframes require a media clip");
        }
        if (property !== "volume" && kind === "audio") throw invalid("KEYFRAME_PROPERTY_INVALID", "Audio clips support volume keyframes only");
        if (["brightness", "contrast", "saturation"].includes(property) && kind !== "video" && kind !== "image") {
          throw invalid("KEYFRAME_PROPERTY_INVALID", "Color keyframes require a video or image clip");
        }
        keyframes = keyframes.filter((existing) => !(existing.property === property && existing.offset_us === item.offset_us));
        keyframes.push({ property, offset_us: item.offset_us, value: item.value });
      }
      keyframes.sort(byPropertyThenOffset);
      const properties = new Set(keyframes.map((item) => item.property));
      if (properties.has("uniform_scale") && (properties.has("scale_x") || properties.has("scale_y"))) {
        throw invalid("KEYFRAME_PROPERTY_CONFLICT", "Uniform scale keyframes cannot be combined with axis scale keyframes");
      }
      return this.#replaceSegment(project, track.id, segmentId, { ...current, keyframes });
    });
  }

  async listKeyframes(projectId, segmentId) {
    const segment = await this.getSegment(projectId, segmentId);
    return { segment_id: segmentId, keyframes: segment.keyframes ?? [] };
  }

  async removeKeyframe(projectId, segmentId, propertyName, offsetUs, { expectRevision } = {}) {
    const [params] = check([KeyframeParams, given({ property_name: propertyName, offset_us: offsetUs, expect_revision: expectRevision })]);
    const property = canonicalKeyframeProperty(params.property_name);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const [track, current] = segmentLocation(project, segmentId);
      const existing = current.keyframes ?? [];
      const keyframes = existing.filter((item) => !(item.property === property && item.offset_us === params.offset_us));
      if (keyframes.length === existing.length) throw storeError("KEYFRAME_NOT_FOUND", "Keyframe does not exist", 404);
      return this.#replaceSegment(project, track.id, segmentId, { ...current, keyframes });
    });
  }

  // ---- 片段特性

  async addEffect(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [EffectCreate, request]);
    return this.#serial(projectId, () => this.#appendFeature(projectId, segmentId, "effects", body, params.expect_revision));
  }

  async addFilter(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [FilterCreate, request]);
    return this.#serial(projectId, () => this.#appendFeature(projectId, segmentId, "filters", body, params.expect_revision));
  }

  async addAnimation(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [AnimationCreate, request]);
    return this.#serial(projectId, () => this.#appendFeature(projectId, segmentId, "animations", body, params.expect_revision));
  }

  async #appendFeature(projectId, segmentId, feature, value, expectRevision) {
    const project = await this.#expected(projectId, expectRevision);
    const [track, current] = segmentLocation(project, segmentId);
    const kind = current.kind;
    if (!["video", "image", "audio", "text"].includes(kind)) {
      throw invalid("SEGMENT_FIELD_MISMATCH", "Feature requires a media or text clip");
    }
    let item;
    if (feature === "effects") {
      if (!MEDIA_KINDS.has(kind)) throw invalid("SEGMENT_FIELD_MISMATCH", "Effects require a media clip");
      const effect = findMember(kind === "audio" ? AUDIO_EFFECTS : VIDEO_EFFECTS, value.effect_type);
      if (!effect) throw invalid("EFFECT_INVALID", "Unknown effect for this clip type");
      if (value.params !== null && value.params.length > effect.meta.params.length) {
        throw invalid("EFFECT_PARAMS_INVALID", "Too many effect parameters");
      }
      item = { effect_type: value.effect_type, params: value.params };
    } else if (feature === "filters") {
      if (kind !== "video" && kind !== "image") throw invalid("SEGMENT_FIELD_MISMATCH", "Filters require a video or image clip");
      requireMember("FilterType", value.filter_type);
      item = { filter_type: value.filter_type, intensity: value.intensity };
    } else {
      if (kind === "audio") throw invalid("SEGMENT_FIELD_MISMATCH", "Animations require a visual clip");
      if (!findMember(kind === "text" ? TEXT_ANIMATIONS : VIDEO_ANIMATIONS, value.animation_type)) {
        throw invalid("ANIMATION_INVALID", "Unknown animation for this clip type");
      }
      if (value.duration_us !== null && value.duration_us > current.timeline.duration_us) {
        throw invalid("ANIMATION_INVALID", "Animation exceeds clip duration");
      }
      item = { animation_type: value.animation_type, duration_us: value.duration_us };
    }
    return this.#replaceSegment(project, track.id, segmentId, { ...current, [feature]: [...(current[feature] ?? []), item] });
  }

  async addTransition(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [TransitionCreate, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const [track, current] = segmentLocation(project, segmentId);
      if (current.kind !== "video" && current.kind !== "image") {
        throw invalid("SEGMENT_FIELD_MISMATCH", "Transitions require a video or image clip");
      }
      requireMember("TransitionType", body.transition_type);
      const duration = body.duration_us || Math.min(500_000, current.timeline.duration_us);
      if (duration > current.timeline.duration_us) throw invalid("TRANSITION_INVALID", "Transition is longer than the clip");
      return this.#replaceSegment(project, track.id, segmentId, {
        ...current,
        transition: { transition_type: body.transition_type, duration_us: duration },
      });
    });
  }

  async addFade(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [FadeCreate, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const [track, current] = segmentLocation(project, segmentId);
      if (!MEDIA_KINDS.has(current.kind)) throw invalid("SEGMENT_FIELD_MISMATCH", "Fades require a media clip");
      if (body.in_duration_us + body.out_duration_us > current.timeline.duration_us) {
        throw invalid("FADE_INVALID", "Fade duration exceeds clip duration");
      }
      return this.#replaceSegment(project, track.id, segmentId, { ...current, fade: body });
    });
  }

  async clearFeature(projectId, segmentId, feature, { expectRevision } = {}) {
    const [params] = check([FeatureParams, given({ feature, expect_revision: expectRevision })]);
    if (!CLEARABLE.has(params.feature)) throw invalid("FEATURE_INVALID", "Unknown segment feature");
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const [track, current] = segmentLocation(project, segmentId);
      if ((params.feature === "bubble" || params.feature === "text_effect") && current.kind !== "text") {
        throw invalid("SEGMENT_FIELD_MISMATCH", "Text features require a text clip");
      }
      const value = ["effects", "filters", "animations"].includes(params.feature) ? [] : null;
      const field = params.feature === "text_effect" ? "text_effect_id" : params.feature;
      return this.#replaceSegment(project, track.id, segmentId, { ...current, [field]: value });
    });
  }

  async addMask(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [MaskCreate, request]);
    requireMember("MaskType", body.mask_type);
    return this.#setVisualFeature(projectId, segmentId, "mask", body, params.expect_revision);
  }

  async addChroma(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [ChromaCreate, request]);
    return this.#setVisualFeature(projectId, segmentId, "chroma", body, params.expect_revision);
  }

  async addBackgroundFilling(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [BackgroundFillCreate, request]);
    return this.#setVisualFeature(projectId, segmentId, "background_filling", body, params.expect_revision);
  }

  async setMixMode(projectId, segmentId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [MixModeCreate, request]);
    requireMember("MixModeType", body.mode);
    return this.#setVisualFeature(projectId, segmentId, "mix_mode", body, params.expect_revision);
  }

  #setVisualFeature(projectId, segmentId, feature, value, expectRevision) {
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, expectRevision);
      const [track, current] = segmentLocation(project, segmentId);
      if (current.kind !== "video" && current.kind !== "image") {
        throw invalid("SEGMENT_FIELD_MISMATCH", "Feature requires a video or image clip");
      }
      if (current[feature] !== null && current[feature] !== undefined) throw invalid("FEATURE_EXISTS", `Clip already has ${feature}`);
      return this.#replaceSegment(project, track.id, segmentId, { ...current, [feature]: value });
    });
  }

  // ---- 标记

  async addMarker(projectId, request, { expectRevision } = {}) {
    const [params, body] = check([RevisionParams, revisionInput(expectRevision)], [MarkerCreate, request]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const markers = [...project.markers.filter((marker) => marker.time_us !== body.time_us), body];
      markers.sort((left, right) => left.time_us - right.time_us);
      return this.#touch(project, { markers });
    });
  }

  async listMarkers(projectId) {
    return [...(await this.#get(projectId)).markers];
  }

  async removeMarker(projectId, timeUs, { expectRevision } = {}) {
    const [params] = check([MarkerParams, given({ time_us: timeUs, expect_revision: expectRevision })]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      const markers = project.markers.filter((marker) => marker.time_us !== params.time_us);
      if (markers.length === project.markers.length) throw storeError("MARKER_NOT_FOUND", "Timeline marker does not exist", 404);
      return this.#touch(project, { markers });
    });
  }

  // ---- 字幕

  async addSrtSubtitles(projectId, srt, { groupId, expectRevision } = {}) {
    const [params] = check([SrtParams, given({ group_id: groupId, expect_revision: expectRevision })]);
    if (typeof srt !== "string" || !pyStrip(srt)) throw invalid("SRT_EMPTY", "SRT content is empty");
    if (Buffer.byteLength(srt, "utf8") > MAX_SRT_BYTES) throw invalid("SRT_TOO_LARGE", "SRT content exceeds size limit");
    const entries = parseSrt(srt);
    const group = params.group_id || this.newId();
    if (codePoints(group) !== 32 || /[^0-9a-f]/u.test(group)) throw invalid("SUBTITLE_GROUP_INVALID", "Subtitle group is invalid");
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      if (project.tracks.some((track) => track.kind === "subtitle" && track.id === group)) {
        throw storeError("SUBTITLE_EXISTS", "Subtitle group already exists", 409);
      }
      const track = {
        id: group,
        kind: "subtitle",
        track_type: "text",
        name: "Subtitles",
        mute: false,
        segments: entries.map((entry) => ({
          id: this.#segmentId(),
          kind: "text",
          text: entry.text,
          timeline: timeRange({ start_us: entry.start_us, duration_us: entry.end_us - entry.start_us }),
          transform: null,
          font_size: 8,
          color: "#FFFFFF",
          alignment: "center",
        })),
      };
      return this.#touch(project, { tracks: [...project.tracks, track] });
    });
  }

  async removeSubtitleGroup(projectId, trackId, { expectRevision } = {}) {
    const [params] = check([RevisionParams, revisionInput(expectRevision)]);
    return this.#serial(projectId, async () => {
      const project = await this.#expected(projectId, params.expect_revision);
      if (!project.tracks.some((item) => item.id === trackId && item.kind === "subtitle")) {
        throw invalid("SUBTITLE_NOT_FOUND", "Subtitle group does not exist");
      }
      return this.#saveTracks(project, project.tracks.filter((item) => item.id !== trackId));
    });
  }

  // ---- 草稿用

  /** 工程用到的每个素材探测一次（同一文件只算一份），顺带确认素材没被换短。 */
  async mediaInfos(project) {
    const paths = new Map();
    for (const track of project.tracks) {
      if (track.kind === "subtitle") continue;
      for (const segment of track.segments ?? []) {
        if (MEDIA_KINDS.has(segment.kind)) paths.set(await mediaKey(segment.path), segment.path);
      }
    }
    const infos = [];
    for (const [key, raw] of paths) {
      const info = await this.#probe(raw);
      for (const track of project.tracks) {
        for (const segment of track.segments ?? []) {
          if (!MEDIA_KINDS.has(segment.kind) || (await mediaKey(segment.path)) !== key) continue;
          if (segment.source.start_us + segment.source.duration_us > info.duration_us) {
            throw invalid("SOURCE_RANGE_INVALID", "Media changed since it was added");
          }
        }
      }
      infos.push(info);
    }
    return infos;
  }
}

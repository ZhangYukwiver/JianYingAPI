// 移植自 pyJianYingDraft 0.3.1（Apache-2.0，见 node/licenses）与 JianYingAPI。
// 只搬 JianYingAPI 写草稿时用到的部分：ScriptFile、轨道、各类片段与素材的 export_json。
// 字段顺序、取值、整数/浮点类型都与 Python 输出逐字段一致（浮点用 pyFloat 标出）。

import { readFileSync } from "node:fs";

import { invalid } from "../errors.mjs";
import { dumps, loads, pyFloat as F } from "./pyjson.mjs";

let template = null;

function contentTemplate() {
  template ??= readFileSync(new URL("../draftTemplates/draft_content_template.json", import.meta.url), "utf8");
  return loads(template);
}

/** 原库里这些都是抛给调用方的 ValueError（HTTP 500）；这里统一成 422 的结构化错误。 */
function featureError(message) {
  return invalid("DRAFT_FEATURE_INVALID", message);
}

export class Timerange {
  constructor(start, duration) {
    this.start = start;
    this.duration = duration;
  }

  get end() {
    return this.start + this.duration;
  }

  overlaps(other) {
    return !(this.end <= other.start || other.end <= this.start);
  }

  toString() {
    return `[start=${this.start}, end=${this.end}]`;
  }

  exportJson() {
    return { start: this.start, duration: this.duration };
  }
}

export const KEYFRAME_PROPERTY = {
  position_x: "KFTypePositionX",
  position_y: "KFTypePositionY",
  rotation: "KFTypeRotation",
  scale_x: "KFTypeScaleX",
  scale_y: "KFTypeScaleY",
  uniform_scale: "UNIFORM_SCALE",
  alpha: "KFTypeAlpha",
  saturation: "KFTypeSaturation",
  contrast: "KFTypeContrast",
  brightness: "KFTypeBrightness",
  volume: "KFTypeVolume",
};

class KeyframeList {
  constructor(ids, property) {
    this.id = ids();
    this.property = property;
    this.keyframes = [];
  }

  add(ids, timeOffset, value) {
    this.keyframes.push({ id: ids(), timeOffset, value });
    this.keyframes.sort((a, b) => a.timeOffset - b.timeOffset);
  }

  exportJson() {
    return {
      id: this.id,
      keyframe_list: this.keyframes.map((frame) => ({
        curveType: "Line",
        graphID: "",
        left_control: { x: F(0), y: F(0) },
        right_control: { x: F(0), y: F(0) },
        id: frame.id,
        time_offset: frame.timeOffset,
        values: [F(frame.value)],
      })),
      material_id: "",
      property_type: KEYFRAME_PROPERTY[this.property],
    };
  }
}

class Speed {
  constructor(ids, speed) {
    this.id = ids();
    this.speed = speed;
  }

  exportJson() {
    return { curve_speed: null, id: this.id, mode: 0, speed: F(this.speed), type: "speed" };
  }
}

class AudioFade {
  constructor(ids, inDuration, outDuration) {
    this.id = ids();
    this.inDuration = inDuration;
    this.outDuration = outDuration;
  }

  exportJson() {
    return {
      id: this.id,
      fade_in_duration: this.inDuration,
      fade_out_duration: this.outDuration,
      fade_type: 0,
      type: "audio_fade",
    };
  }
}

export class ClipSettings {
  constructor({
    alpha = 1, flip_horizontal = false, flip_vertical = false, rotation = 0,
    scale_x = 1, scale_y = 1, transform_x = 0, transform_y = 0,
  } = {}) {
    Object.assign(this, { alpha, flip_horizontal, flip_vertical, rotation, scale_x, scale_y, transform_x, transform_y });
  }

  exportJson() {
    return {
      alpha: F(this.alpha),
      flip: { horizontal: Boolean(this.flip_horizontal), vertical: Boolean(this.flip_vertical) },
      rotation: F(this.rotation),
      scale: { x: F(this.scale_x), y: F(this.scale_y) },
      transform: { x: F(this.transform_x), y: F(this.transform_y) },
    };
  }
}

export class CropSettings {
  static FIELDS = [
    ["upper_left_x", 0], ["upper_left_y", 0], ["upper_right_x", 1], ["upper_right_y", 0],
    ["lower_left_x", 0], ["lower_left_y", 1], ["lower_right_x", 1], ["lower_right_y", 1],
  ];

  constructor(values = {}) {
    for (const [name, fallback] of CropSettings.FIELDS) this[name] = values[name] ?? fallback;
  }

  exportJson() {
    return Object.fromEntries(CropSettings.FIELDS.map(([name]) => [name, F(this[name])]));
  }
}

// ---------- 素材 ----------

export class VideoMaterial {
  constructor(ids, { path, name, duration, width, height, type, crop }) {
    this.materialId = ids();
    this.localMaterialId = "";
    Object.assign(this, { path, name, duration, width, height, type });
    this.crop = crop ?? new CropSettings();
  }

  exportJson() {
    return {
      audio_fade: null,
      category_id: "",
      category_name: "local",
      check_flag: 63487,
      crop: this.crop.exportJson(),
      crop_ratio: "free",
      crop_scale: F(1),
      duration: this.duration,
      height: this.height,
      id: this.materialId,
      local_material_id: this.localMaterialId,
      material_id: this.materialId,
      material_name: this.name,
      media_path: "",
      path: this.path,
      type: this.type,
      width: this.width,
    };
  }
}

export class AudioMaterial {
  constructor(ids, { path, name, duration }) {
    this.materialId = ids();
    Object.assign(this, { path, name, duration });
  }

  exportJson() {
    return {
      app_id: 0,
      category_id: "",
      category_name: "local",
      check_flag: 3,
      copyright_limit_type: "none",
      duration: this.duration,
      effect_id: "",
      formula_id: "",
      id: this.materialId,
      local_material_id: this.materialId,
      music_id: this.materialId,
      name: this.name,
      path: this.path,
      source_platform: 0,
      type: "extract_music",
      wave_points: [],
    };
  }
}

// ---------- 特效类素材 ----------

function parseParams(meta, params) {
  const values = params ?? [];
  return meta.params.map((param, index) => {
    let value = param.default_value;
    const input = index < values.length ? values[index] : null;
    if (input !== null && input !== undefined) {
      if (input < 0 || input > 100) {
        throw featureError(`Invalid parameter value ${input} for ${param.name}`);
      }
      value = param.min_value + (param.max_value - param.min_value) * input / 100;
    }
    return { param, index, value };
  });
}

function exportParam({ param, index, value }) {
  return {
    default_value: F(param.default_value),
    max_value: F(param.max_value),
    min_value: F(param.min_value),
    name: param.name,
    parameterIndex: index,
    portIndex: 0,
    value: F(value),
  };
}

class VideoEffect {
  /** member: { enumName, name, meta }，enumName 区分场景特效与人物特效。 */
  constructor(ids, member, params, applyTargetType = 0) {
    this.id = ids();
    this.meta = member.meta;
    this.type = member.enumName === "VideoSceneEffectType" ? "video_effect" : "face_effect";
    this.applyTargetType = applyTargetType;
    this.params = parseParams(member.meta, params);
  }

  exportJson() {
    return {
      adjust_params: this.params.map(exportParam),
      apply_target_type: this.applyTargetType,
      apply_time_range: null,
      category_id: "",
      category_name: "",
      common_keyframes: [],
      disable_effect_faces: [],
      effect_id: this.meta.effect_id,
      formula_id: "",
      id: this.id,
      name: this.meta.name,
      platform: "all",
      render_index: 11000,
      resource_id: this.meta.resource_id,
      source_platform: 0,
      time_range: null,
      track_render_index: 0,
      type: this.type,
      value: F(1),
      version: "",
    };
  }
}

class Filter {
  constructor(ids, meta, intensity, applyTargetType = 0) {
    this.id = ids();
    this.meta = meta;
    this.intensity = intensity;
    this.applyTargetType = applyTargetType;
  }

  exportJson() {
    return {
      adjust_params: [],
      algorithm_artifact_path: "",
      apply_target_type: this.applyTargetType,
      bloom_params: null,
      category_id: "",
      category_name: "",
      color_match_info: { source_feature_path: "", target_feature_path: "", target_image_path: "" },
      effect_id: this.meta.effect_id,
      enable_skin_tone_correction: false,
      exclusion_group: [],
      face_adjust_params: [],
      formula_id: "",
      id: this.id,
      intensity_key: "",
      multi_language_current: "",
      name: this.meta.name,
      panel_id: "",
      platform: "all",
      resource_id: this.meta.resource_id,
      source_platform: 1,
      sub_type: "none",
      time_range: null,
      type: "filter",
      value: F(this.intensity),
      version: "",
    };
  }
}

class Transition {
  constructor(ids, meta, duration) {
    this.id = ids();
    this.meta = meta;
    this.duration = duration ?? meta.default_duration;
  }

  exportJson() {
    return {
      category_id: "",
      category_name: "",
      duration: this.duration,
      effect_id: this.meta.effect_id,
      id: this.id,
      is_overlap: this.meta.is_overlap,
      name: this.meta.name,
      platform: "all",
      resource_id: this.meta.resource_id,
      type: "transition",
    };
  }
}

function normalizeRgba(color) {
  const value = String(color).trim();
  if (!value.startsWith("#") || value.length !== 9 || !/^[0-9a-fA-F]{8}$/.test(value.slice(1))) {
    throw featureError("颜色必须使用 '#RRGGBBAA' 格式");
  }
  return `#${value.slice(1).toLowerCase()}`;
}

class Mask {
  constructor(ids, meta, { centerX, centerY, width, height, ratio, rotation, invert, feather, roundCorner }) {
    this.id = ids();
    this.meta = meta;
    Object.assign(this, { centerX, centerY, width, height, ratio, rotation, invert, feather, roundCorner });
  }

  exportJson() {
    return {
      config: {
        aspectRatio: F(this.ratio),
        centerX: F(this.centerX),
        centerY: F(this.centerY),
        feather: F(this.feather),
        height: F(this.height),
        invert: Boolean(this.invert),
        rotation: F(this.rotation),
        roundCorner: F(this.roundCorner),
        width: F(this.width),
      },
      id: this.id,
      name: this.meta.name,
      platform: "all",
      position_info: "",
      resource_type: this.meta.resource_type,
      resource_id: this.meta.resource_id,
      type: "mask",
    };
  }

  /** 新版剪映读 materials.common_mask；id 与 masks 里那条相同。 */
  exportCommonJson() {
    const json = this.exportJson();
    Object.assign(json, {
      category: "video",
      category_id: "jichu",
      category_name: "基础",
      is_old_version: false,
      loader_work_space: "",
      panel: "",
      source_platform: 0,
      track_segment: "",
      text_config: {
        align_type: 15,
        bold_width: F(0),
        char_spacing: F(0),
        content: "",
        font_name: "",
        font_path: "",
        font_resource_id: "",
        font_size: F(15),
        has_underline: false,
        italic_degree: 0,
        line_gap: F(0),
        scale: F(1),
      },
    });
    json.config = { ...json.config, expansion: F(0) };
    return json;
  }
}

class Chroma {
  constructor(ids, color, intensity, shadow, edgeSmooth, spill) {
    // 原库用 str(uuid4()).upper()：带横线的大写 UUID
    const hex = ids().toUpperCase();
    this.id = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`;
    this.color = normalizeRgba(color);
    Object.assign(this, { intensity, shadow, edgeSmooth, spill });
  }

  exportJson() {
    return {
      color: this.color,
      edge_smooth_value: F(this.edgeSmooth),
      id: this.id,
      intensity_value: F(this.intensity),
      shadow_value: F(this.shadow),
      should_transfer_color: true,
      spill_value: F(this.spill),
      type: "chroma",
      version: "v2",
    };
  }
}

class BackgroundFilling {
  constructor(ids, fillType, blur, color) {
    this.id = ids();
    this.fillType = fillType;
    this.blur = blur;
    this.color = normalizeRgba(color);
  }

  exportJson() {
    return { id: this.id, type: this.fillType, blur: F(this.blur), color: this.color, source_platform: 0 };
  }
}

class MixMode {
  constructor(ids, meta) {
    this.id = ids();
    this.meta = meta;
  }

  exportJson() {
    return {
      type: "mix_mode",
      name: this.meta.name,
      effect_id: this.meta.effect_id,
      resource_id: this.meta.resource_id,
      value: F(1),
      apply_target_type: 0,
      platform: "all",
      source_platform: 0,
      category_id: "",
      category_name: "",
      sub_type: "none",
      time_range: null,
      id: this.id,
    };
  }
}

class TextBubble {
  constructor(ids, effectId, resourceId) {
    this.id = ids();
    this.effectId = effectId;
    this.resourceId = resourceId;
  }

  exportJson() {
    return {
      apply_target_type: 0,
      effect_id: this.effectId,
      id: this.id,
      resource_id: this.resourceId,
      type: "text_shape",
      value: F(1),
    };
  }
}

class TextEffect extends TextBubble {
  exportJson() {
    const json = super.exportJson();
    json.type = "text_effect";
    json.source_platform = 1;
    return json;
  }
}

class AudioEffect {
  static CATEGORIES = {
    AudioSceneEffectType: ["sound_effect", "场景音", 1],
    ToneEffectType: ["tone", "音色", 2],
    SpeechToSongType: ["speech_to_song", "声音成曲", 3],
  };

  constructor(ids, member, params) {
    this.enumName = member.enumName;
    this.meta = member.meta;
    this.id = ids();
    [this.categoryId, this.categoryName, this.categoryIndex] = AudioEffect.CATEGORIES[member.enumName];
    this.params = parseParams(member.meta, params);
  }

  exportJson() {
    const json = {
      audio_adjust_params: this.params.map(exportParam),
      id: this.id,
      name: this.meta.name,
      resource_id: this.meta.resource_id,
      type: "audio_effect",
    };
    if (this.enumName === "ToneEffectType") return json;
    return Object.assign(json, {
      category_id: this.categoryId,
      category_name: this.categoryName,
      is_ugc: false,
      production_path: "",
      speaker_id: "",
      sub_type: this.categoryIndex,
      time_range: { duration: 0, start: 0 },
    });
  }
}

// ---------- 动画 ----------

const VIDEO_ANIMATION_TYPES = { IntroType: "in", OutroType: "out", GroupAnimationType: "group" };
const TEXT_ANIMATION_TYPES = { TextIntro: "in", TextOutro: "out", TextLoopAnim: "loop" };

class SegmentAnimations {
  constructor(ids) {
    this.id = ids();
    this.animations = [];
  }

  range(type) {
    const found = this.animations.find((animation) => animation.type === type);
    return found ? new Timerange(found.start, found.duration) : null;
  }

  add(animation) {
    if (this.animations.some((item) => item.type === animation.type)) {
      throw featureError(`当前片段已存在类型为 '${animation.type}' 的动画`);
    }
    if (animation.video) {
      if (this.animations.some((item) => item.type === "group")) {
        throw featureError("当前片段已存在组合动画, 此时不能添加其它动画");
      }
      if (animation.type === "group" && this.animations.length > 0) {
        throw featureError("当前片段已存在动画时, 不能添加组合动画");
      }
    } else if (this.animations.some((item) => item.type === "loop")) {
      throw featureError("当前片段已存在循环动画, 若希望同时使用循环动画和入出场动画, 请先添加出入场动画再添加循环动画");
    }
    this.animations.push(animation);
  }

  exportJson() {
    return {
      id: this.id,
      type: "sticker_animation",
      multi_language_current: "none",
      animations: this.animations.map((animation) => ({
        anim_adjust_params: null,
        platform: "all",
        panel: animation.video ? "video" : "",
        material_type: animation.video ? "video" : "sticker",
        name: animation.meta.title,
        id: animation.meta.effect_id,
        type: animation.type,
        resource_id: animation.meta.resource_id,
        start: animation.start,
        duration: animation.duration,
      })),
    };
  }
}

// 同 time_util.tim 对数字的处理：int(round(x))，Python round 是银行家舍入
function tim(value) {
  const floor = Math.floor(value);
  const diff = value - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

// ---------- 片段 ----------

class BaseSegment {
  constructor(ids, materialId, targetTimerange) {
    this.segmentId = ids();
    this.materialId = materialId;
    this.target = targetTimerange;
    this.commonKeyframes = [];
  }

  get end() {
    return this.target.end;
  }

  exportJson() {
    return {
      enable_adjust: true,
      enable_color_correct_adjust: false,
      enable_color_curves: true,
      enable_color_match_adjust: false,
      enable_color_wheels: true,
      enable_lut: true,
      enable_smart_color_adjust: false,
      last_nonzero_volume: F(1),
      reverse: false,
      track_attribute: 0,
      track_render_index: 0,
      visible: true,
      id: this.segmentId,
      material_id: this.materialId,
      target_timerange: this.target.exportJson(),
      common_keyframes: this.commonKeyframes.map((list) => list.exportJson()),
      keyframe_refs: [],
    };
  }
}

class MediaSegment extends BaseSegment {
  constructor(ids, materialId, source, target, speed, volume, changePitch) {
    super(ids, materialId, target);
    this.source = source;
    this.speed = new Speed(ids, speed);
    this.volume = volume;
    this.changePitch = changePitch;
    this.extraMaterialRefs = [this.speed.id];
  }

  exportJson() {
    return Object.assign(super.exportJson(), {
      source_timerange: this.source ? this.source.exportJson() : null,
      speed: F(this.speed.speed),
      volume: F(this.volume),
      extra_material_refs: this.extraMaterialRefs,
      is_tone_modify: Boolean(this.changePitch),
    });
  }
}

class VisualSegment extends MediaSegment {
  constructor(ids, materialId, source, target, speed, volume, changePitch, clipSettings) {
    super(ids, materialId, source, target, speed, volume, changePitch);
    this.clip = clipSettings ?? new ClipSettings();
    this.uniformScale = true;
    this.animations = null;
  }

  addKeyframe(ids, property, timeOffset, value) {
    let key = property;
    if ((key === "scale_x" || key === "scale_y") && this.uniformScale) {
      this.uniformScale = false;
    } else if (key === "uniform_scale") {
      if (!this.uniformScale) throw featureError("已设置 scale_x 或 scale_y 时, 不能再设置 uniform_scale");
      key = "scale_x";
    }
    let list = this.commonKeyframes.find((item) => item.property === key);
    if (!list) {
      list = new KeyframeList(ids, key);
      list.add(ids, timeOffset, value);
      this.commonKeyframes.push(list);
      return;
    }
    list.add(ids, timeOffset, value);
  }

  exportJson() {
    return Object.assign(super.exportJson(), {
      clip: this.clip.exportJson(),
      uniform_scale: { on: this.uniformScale, value: F(1) },
    });
  }
}

/** 素材截取范围 + 片段时间范围 → 速度（同原库：给了 source 没给 speed 时 speed = 源时长/目标时长）。 */
function checkedSource(material, source, target) {
  if (source.end > material.duration) {
    throw invalid("SOURCE_RANGE_INVALID", `截取的素材时间范围 ${source} 超出了素材时长(${material.duration})`);
  }
  return source.duration / target.duration;
}

export class VideoSegment extends VisualSegment {
  static TRACK = "video";

  constructor(ids, material, target, { source, volume = 1, changePitch = false, clipSettings = null }) {
    const speed = checkedSource(material, source, target);
    super(ids, material.materialId, source, target, speed, volume, changePitch, clipSettings);
    this.material = material;
    this.effects = [];
    this.filters = [];
    this.mixMode = null;
    this.transition = null;
    this.mask = null;
    this.backgroundFilling = null;
    this.chroma = null;
    this.fade = null;
  }

  addAnimation(ids, member, duration) {
    const type = VIDEO_ANIMATION_TYPES[member.enumName];
    let length = duration === null || duration === undefined ? null : tim(duration);
    let start = 0;
    if (type === "in") {
      length = length || member.meta.duration;
    } else if (type === "out") {
      length = length || member.meta.duration;
      start = this.target.duration - length;
    } else {
      length = length || this.target.duration;
    }
    if (!this.animations) {
      this.animations = new SegmentAnimations(ids);
      this.extraMaterialRefs.push(this.animations.id);
    }
    this.animations.add({ meta: member.meta, type, start, duration: length, video: true });
  }

  addEffect(ids, member, params) {
    if (params !== null && params !== undefined && params.length > member.meta.params.length) {
      throw featureError(`为音频效果 ${member.meta.name} 传入了过多的参数`);
    }
    const effect = new VideoEffect(ids, member, params);
    this.effects.push(effect);
    this.extraMaterialRefs.push(effect.id);
  }

  addFade(ids, inDuration, outDuration) {
    if (this.fade) throw featureError("当前片段已存在淡入淡出效果");
    this.fade = new AudioFade(ids, inDuration, outDuration);
    this.extraMaterialRefs.push(this.fade.id);
  }

  addFilter(ids, meta, intensity = 100) {
    const filter = new Filter(ids, meta, intensity / 100);
    this.filters.push(filter);
    this.extraMaterialRefs.push(filter.id);
  }

  setMixMode(ids, meta) {
    if (this.mixMode) {
      this.mixMode.meta = meta;
      return;
    }
    this.mixMode = new MixMode(ids, meta);
    this.extraMaterialRefs.push(this.mixMode.id);
  }

  addMask(ids, member, { centerX = 0, centerY = 0, size = 0.5, rotation = 0, feather = 0, invert = false, rectWidth = null, roundCorner = null }) {
    if (this.mask) throw featureError("当前片段已有蒙版, 不能再添加新的蒙版");
    const rectangle = member.name === "矩形";
    if ((rectWidth !== null || roundCorner !== null) && !rectangle) {
      throw featureError("`rect_width` 以及 `round_corner` 仅在蒙版类型为矩形时允许设置");
    }
    if (rectWidth === null && rectangle) rectWidth = size;
    if (roundCorner === null) roundCorner = 0;
    const [width, height] = [this.material.width, this.material.height];
    const ratio = member.meta.default_aspect_ratio;
    this.mask = new Mask(ids, member.meta, {
      centerX: centerX / (width / 2),
      centerY: centerY / (height / 2),
      width: rectWidth || size * height * ratio / width,
      height: size,
      ratio,
      rotation,
      invert,
      feather: feather / 100,
      roundCorner: roundCorner / 100,
    });
    this.extraMaterialRefs.push(this.mask.id);
  }

  addTransition(ids, meta, duration) {
    if (this.transition) throw featureError("当前片段已有转场, 不能再添加新的转场");
    this.transition = new Transition(ids, meta, duration);
    this.extraMaterialRefs.push(this.transition.id);
  }

  addBackgroundFilling(ids, fillType, blur = 0.0625, color = "#00000000") {
    if (this.backgroundFilling) throw featureError("当前片段已有背景填充效果");
    const type = { blur: "canvas_blur", color: "canvas_color" }[fillType];
    if (!type) throw featureError(`无效的背景填充类型 ${fillType}`);
    this.backgroundFilling = new BackgroundFilling(ids, type, blur, color);
    this.extraMaterialRefs.push(this.backgroundFilling.id);
  }

  addChroma(ids, color, { intensity = 20, shadow = 0, edgeSmooth = 0, spill = 0 }) {
    if (this.chroma) throw featureError("当前片段已有色度抠图, 不能再添加新的色度抠图");
    for (const [name, value] of [["intensity", intensity], ["shadow", shadow], ["edge_smooth", edgeSmooth], ["spill", spill]]) {
      if (!(value >= 0 && value <= 100)) throw featureError(`色度抠图参数 ${name}=${value} 超出了范围 0~100`);
    }
    this.chroma = new Chroma(ids, color, intensity / 100, shadow / 100, edgeSmooth / 100, spill / 100);
    this.extraMaterialRefs.push(this.chroma.id);
  }

  exportJson() {
    return Object.assign(super.exportJson(), { hdr_settings: { intensity: F(1), mode: 1, nits: 1000 } });
  }
}

export class StickerSegment extends VisualSegment {
  static TRACK = "sticker";

  constructor(ids, resourceId, target, clipSettings) {
    super(ids, ids(), null, target, 1, 1, false, clipSettings);
    this.resourceId = resourceId;
  }

  exportMaterial() {
    return {
      id: this.materialId,
      resource_id: this.resourceId,
      sticker_id: this.resourceId,
      source_platform: 1,
      type: "sticker",
    };
  }
}

export class AudioSegment extends MediaSegment {
  static TRACK = "audio";

  constructor(ids, material, target, { source, volume = 1, changePitch = false }) {
    const speed = checkedSource(material, source, target);
    super(ids, material.materialId, source, target, speed, volume, changePitch);
    this.material = material;
    this.fade = null;
    this.effects = [];
  }

  addEffect(ids, member, params) {
    if (params !== null && params !== undefined && params.length > member.meta.params.length) {
      throw featureError(`为音频效果 ${member.meta.name} 传入了过多的参数`);
    }
    const effect = new AudioEffect(ids, member, params);
    if (this.effects.some((item) => item.categoryId === effect.categoryId)) {
      throw featureError(`当前音频片段已经有此类型 (${effect.categoryName}) 的音效了`);
    }
    this.effects.push(effect);
    this.extraMaterialRefs.push(effect.id);
  }

  addFade(ids, inDuration, outDuration) {
    if (this.fade) throw featureError("当前片段已存在淡入淡出效果");
    this.fade = new AudioFade(ids, inDuration, outDuration);
    this.extraMaterialRefs.push(this.fade.id);
  }

  addKeyframe(ids, timeOffset, volume) {
    let list = this.commonKeyframes.find((item) => item.property === "volume");
    if (!list) {
      list = new KeyframeList(ids, "volume");
      list.add(ids, timeOffset, volume);
      this.commonKeyframes.push(list);
      return;
    }
    list.add(ids, timeOffset, volume);
  }

  exportJson() {
    return Object.assign(super.exportJson(), { clip: null, hdr_settings: null });
  }
}

export class TextSegment extends VisualSegment {
  static TRACK = "text";

  /**
   * style: { size, bold, italic, underline, color: [r,g,b], alpha, align, vertical,
   *          letterSpacing, lineSpacing, autoWrapping, maxLineWidth }
   * border: { alpha, color, width(0-100) }；shadow: { alpha, color, diffuse, distance, angle }
   * background: TextBackgroundOptions 原样字段
   */
  constructor(ids, text, target, { font = null, style, clipSettings = null, border = null, background = null, shadow = null }) {
    super(ids, ids(), null, target, 1, 1, false, clipSettings);
    this.text = text;
    this.font = font;
    this.style = style;
    this.border = border && { alpha: border.alpha, color: border.color, width: border.width / 100 * 0.2 };
    this.background = background && {
      style: background.style ?? 1,
      alpha: background.alpha ?? 1,
      color: background.color,
      round_radius: background.round_radius ?? 0,
      height: background.height ?? 0.14,
      width: background.width ?? 0.14,
      horizontal_offset: (background.horizontal_offset ?? 0.5) * 2 - 1,
      vertical_offset: (background.vertical_offset ?? 0.5) * 2 - 1,
    };
    this.shadow = shadow;
    this.bubble = null;
    this.effect = null;
  }

  addAnimation(ids, member, duration) {
    const type = TEXT_ANIMATION_TYPES[member.enumName];
    let length = Math.min(tim(duration ?? member.meta.duration), this.target.duration);
    let start = 0;
    if (type === "out") {
      start = this.target.duration - length;
    } else if (type === "loop") {
      const intro = this.animations && this.animations.range("in");
      const outro = this.animations && this.animations.range("out");
      start = intro ? intro.start : 0;
      length = this.target.duration - start - (outro ? outro.duration : 0);
    }
    if (!this.animations) {
      this.animations = new SegmentAnimations(ids);
      this.extraMaterialRefs.push(this.animations.id);
    }
    this.animations.add({ meta: member.meta, type, start, duration: length, video: false });
  }

  addBubble(ids, effectId, resourceId) {
    this.bubble = new TextBubble(ids, effectId, resourceId);
    this.extraMaterialRefs.push(this.bubble.id);
  }

  addEffect(ids, effectId) {
    this.effect = new TextEffect(ids, effectId, effectId);
    this.extraMaterialRefs.push(this.effect.id);
  }

  exportMaterial() {
    let checkFlag = 7;
    if (this.border) checkFlag |= 8;
    if (this.background) checkFlag |= 16;
    if (this.shadow) checkFlag |= 32;
    const style = {
      fill: {
        alpha: F(1),
        content: { render_type: "solid", solid: { alpha: F(1), color: this.style.color.map(F) } },
      },
      // Python 的 len 按码点计数
      range: [0, [...this.text].length],
      size: F(this.style.size),
      bold: this.style.bold,
      italic: this.style.italic,
      underline: this.style.underline,
      strokes: this.border
        ? [{ content: { solid: { alpha: F(this.border.alpha), color: this.border.color.map(F) } }, width: F(this.border.width) }]
        : [],
    };
    if (this.font) style.font = { id: this.font.resource_id, path: "D:" };
    if (this.effect) style.effectStyle = { id: this.effect.effectId, path: "C:" };
    if (this.shadow) {
      style.shadows = [{
        diffuse: F(this.shadow.diffuse / 100 / 6),
        alpha: F(this.shadow.alpha),
        distance: F(this.shadow.distance),
        content: { solid: { color: this.shadow.color.map(F) } },
        angle: F(this.shadow.angle),
      }];
    }
    const material = {
      id: this.materialId,
      content: dumps({ styles: [style], text: this.text }),
      typesetting: this.style.vertical ? 1 : 0,
      alignment: this.style.align,
      letter_spacing: F(this.style.letterSpacing * 0.05),
      line_spacing: F(0.02 + this.style.lineSpacing * 0.05),
      line_feed: 1,
      line_max_width: F(this.style.maxLineWidth),
      force_apply_line_max_width: false,
      check_flag: checkFlag,
      type: this.style.autoWrapping ? "subtitle" : "text",
      global_alpha: F(this.style.alpha),
    };
    if (this.background) {
      Object.assign(material, {
        background_style: this.background.style,
        background_color: this.background.color,
        background_alpha: F(this.background.alpha),
        background_round_radius: F(this.background.round_radius),
        background_height: F(this.background.height),
        background_width: F(this.background.width),
        background_horizontal_offset: F(this.background.horizontal_offset),
        background_vertical_offset: F(this.background.vertical_offset),
      });
    }
    return material;
  }
}

export class EffectSegment extends BaseSegment {
  static TRACK = "effect";

  constructor(ids, member, target, params) {
    const effect = new VideoEffect(ids, member, params, 2);
    super(ids, effect.id, target);
    this.effect = effect;
  }
}

export class FilterSegment extends BaseSegment {
  static TRACK = "filter";

  constructor(ids, meta, target, intensity) {
    const filter = new Filter(ids, meta, intensity);
    super(ids, filter.id, target);
    this.filter = filter;
  }
}

// ---------- 轨道与草稿 ----------

class Track {
  constructor(ids, type, name, mute) {
    this.id = ids();
    this.type = type;
    this.name = name;
    this.mute = mute;
    this.segments = [];
  }

  add(segment) {
    if (segment.constructor.TRACK !== this.type) {
      throw invalid("TRACK_TYPE_MISMATCH", `Segment does not match the ${this.type} track`);
    }
    for (const existing of this.segments) {
      if (existing.target.overlaps(segment.target)) {
        throw invalid("TIMELINE_OVERLAP", "Clips overlap on the track");
      }
    }
    this.segments.push(segment);
  }

  exportJson() {
    return {
      attribute: this.mute ? 1 : 0,
      flag: 0,
      id: this.id,
      is_default_name: this.name.length === 0,
      name: this.name,
      segments: this.segments.map((segment) => segment.exportJson()),
      type: this.type,
    };
  }
}

/**
 * 对应 ScriptFile（只用到新建草稿的路径）。
 * ponytail: 原库按轨道名索引轨道，重名会抛 NameError；这里按加入顺序保存、由调用方持有轨道引用，
 * 同名轨道（例如两组都叫 Subtitles 的字幕）也能写出。
 */
export class ScriptFile {
  constructor(ids, width, height, fps) {
    this.ids = ids;
    Object.assign(this, { width, height, fps, duration: 0 });
    this.content = contentTemplate();
    this.tracks = [];
    this.materials = {
      audios: [], videos: [], stickers: [], texts: [],
      audio_effects: [], audio_fades: [], animations: [], video_effects: [], chromas: [],
      speeds: [], masks: [], common_mask: [], transitions: [], filters: [], mix_modes: [], canvases: [],
    };
  }

  appendTrack(type, name, mute) {
    const track = new Track(this.ids, type, name, mute);
    this.tracks.push(track);
    return track;
  }

  #addOnce(list, item) {
    if (!list.some((existing) => existing.id === item.id)) list.push(item);
  }

  addMaterial(material) {
    const list = material instanceof AudioMaterial ? this.materials.audios : this.materials.videos;
    if (!list.some((existing) => existing.materialId === material.materialId)) list.push(material);
  }

  addSegment(segment, track) {
    track.add(segment);
    this.duration = Math.max(this.duration, segment.end);
    const materials = this.materials;
    if (segment instanceof VideoSegment) {
      if (segment.animations) this.#addOnce(materials.animations, segment.animations);
      if (segment.fade) this.#addOnce(materials.audio_fades, segment.fade);
      for (const effect of segment.effects) this.#addOnce(materials.video_effects, effect);
      for (const filter of segment.filters) this.#addOnce(materials.filters, filter);
      if (segment.mixMode) this.#addOnce(materials.mix_modes, segment.mixMode);
      if (segment.mask) {
        materials.masks.push(segment.mask.exportJson());
        materials.common_mask.push(segment.mask.exportCommonJson());
      }
      if (segment.transition) this.#addOnce(materials.transitions, segment.transition);
      if (segment.backgroundFilling) materials.canvases.push(segment.backgroundFilling);
      if (segment.chroma) this.#addOnce(materials.chromas, segment.chroma);
      materials.speeds.push(segment.speed);
    } else if (segment instanceof StickerSegment) {
      materials.stickers.push(segment.exportMaterial());
    } else if (segment instanceof AudioSegment) {
      if (segment.fade) this.#addOnce(materials.audio_fades, segment.fade);
      for (const effect of segment.effects) this.#addOnce(materials.audio_effects, effect);
      materials.speeds.push(segment.speed);
    } else if (segment instanceof TextSegment) {
      if (segment.animations) this.#addOnce(materials.animations, segment.animations);
      if (segment.bubble) materials.filters.push(segment.bubble);
      if (segment.effect) materials.filters.push(segment.effect);
      materials.texts.push(segment.exportMaterial());
    }
    if (segment instanceof VideoSegment || segment instanceof AudioSegment) this.addMaterial(segment.material);
  }

  addEffect(member, timerange, track, params) {
    const segment = new EffectSegment(this.ids, member, timerange, params);
    track.add(segment);
    this.duration = Math.max(this.duration, timerange.start + timerange.duration);
    this.#addOnce(this.materials.video_effects, segment.effect);
    return segment;
  }

  addFilter(meta, timerange, track, intensity = 100) {
    const segment = new FilterSegment(this.ids, meta, timerange, intensity / 100);
    track.add(segment);
    this.duration = Math.max(this.duration, timerange.end);
    this.materials.filters.push(segment.filter);
    return segment;
  }

  #exportMaterials() {
    const m = this.materials;
    const json = (list) => list.map((item) => item.exportJson());
    return {
      ai_translates: [],
      audio_balances: [],
      audio_effects: json(m.audio_effects),
      audio_fades: json(m.audio_fades),
      audio_track_indexes: [],
      audios: json(m.audios),
      beats: [],
      canvases: json(m.canvases),
      chromas: json(m.chromas),
      common_mask: m.common_mask,
      color_curves: [],
      digital_humans: [],
      drafts: [],
      effects: [...json(m.filters), ...json(m.mix_modes)],
      flowers: [],
      green_screens: [],
      handwrites: [],
      hsl: [],
      images: [],
      log_color_wheels: [],
      loudnesses: [],
      manual_deformations: [],
      masks: m.masks,
      material_animations: json(m.animations),
      material_colors: [],
      multi_language_refs: [],
      placeholders: [],
      plugin_effects: [],
      primary_color_wheels: [],
      realtime_denoises: [],
      shapes: [],
      smart_crops: [],
      smart_relights: [],
      sound_channel_mappings: [],
      speeds: json(m.speeds),
      stickers: m.stickers,
      tail_leaders: [],
      text_templates: [],
      texts: m.texts,
      time_marks: [],
      transitions: json(m.transitions),
      video_effects: json(m.video_effects),
      video_trackings: [],
      videos: json(m.videos),
      vocal_beautifys: [],
      vocal_separations: [],
    };
  }

  /** 对应 ScriptFile.dumps()，但返回对象；轨道按加入顺序导出，render_index = 轨道序号。 */
  exportContent() {
    const content = this.content;
    content.fps = this.fps;
    content.duration = this.duration;
    content.config.maintrack_adsorb = true;
    content.canvas_config = { width: this.width, height: this.height, ratio: "original" };
    content.materials = this.#exportMaterials();
    content.tracks = this.tracks.map((track, index) => {
      const json = track.exportJson();
      for (const segment of json.segments) {
        segment.render_index = index;
        segment.track_render_index = 0;
      }
      return json;
    });
    return content;
  }
}

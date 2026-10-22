// 移植自 pyJianYingDraft（Apache-2.0）与 JianYingAPI：工程 → 剪映草稿 JSON → 草稿文件夹。
// 对应原仓库 draft.py（serialize_draft / create_draft_folder / editing_options / animation_catalog）
// 和 service.py 里 save_draft / build_draft 计算 material_names、_subtitle_segments 的部分。
// 输出与原仓库逐字段一致（对照夹具见 node/fixtures/ai-edit/drafts/），有意的差异：
// - 修了 fit: cover/stretch 在素材比画布更宽时不铺满（原 draft.py:88-99 只算了一个方向）；
// - fit 按转正后的宽高算：手机竖拍常是编码横屏 + 显示矩阵转 90 度，剪映渲染时会自己转正（素材表里仍写编码宽高）；
// - 修了同一素材在 materials.videos 里出现两份（原 draft.py:625 在片段构造之后才改素材 id）；
// - 素材时长/宽高取探测结果（media.mjs），不再用 pymediainfo；图片素材时长仍是原库的 3 小时；
// - 原库里会变成 500 的内部异常（重叠、动画冲突、参数越界等）改成 422 结构化错误，同名轨道照常导出。

import { createHash, randomBytes, randomUUID } from "node:crypto";
import { lstatSync, readFileSync, realpathSync, statSync } from "node:fs";
import { link, lstat, mkdir, open, rename, rm, stat, unlink, copyFile } from "node:fs/promises";
import path from "node:path";

import { members } from "./catalog.mjs";
import { EditError, invalid } from "./errors.mjs";
import { dumps, loads, PyFloat, pyFloat } from "./draft/pyjson.mjs";
import {
  AudioMaterial,
  AudioSegment,
  ClipSettings,
  CropSettings,
  ScriptFile,
  StickerSegment,
  TextSegment,
  Timerange,
  VideoMaterial,
  VideoSegment,
} from "./draft/script.mjs";

export { PyFloat };
/** 读草稿 JSON 并保留「1.0 这种浮点」的类型，改完用 dumpDraftJson 写回才与原格式一致。 */
export const parseDraftJson = loads;
/** 按 Python json.dumps(ensure_ascii=False) 的格式写 JSON，草稿主文件用 { indent: 4 }。 */
export const dumpDraftJson = dumps;

/**
 * 所有平台差异都在这里：主文件名、platform / last_modified_platform 块、版本号。
 * windows 与原仓库一致；mac 按调研先填（剪映 Mac 11.6.0 实测：主文件叫 draft_info.json，
 * platform.os 写 windows 或 mac 都能列出），等用本机真实草稿再校正。
 */
export const PLATFORM_PROFILES = Object.freeze({
  windows: Object.freeze({
    contentFile: "draft_content.json",
    platform: Object.freeze({ app_id: 3704, app_source: "lv", app_version: "11.6.0.14552", os: "windows" }),
    newVersion: "110.0.0",
    version: 360000,
  }),
  mac: Object.freeze({
    contentFile: "draft_info.json",
    platform: Object.freeze({
      app_id: 3704,
      app_source: "lv",
      app_version: "11.6.0",
      device_id: "",
      hard_disk_id: "",
      mac_address: "",
      os: "mac",
      os_version: "",
    }),
    newVersion: "110.0.0",
    version: 360000,
  }),
});

const PLATFORM_ALIASES = new Map([["windows", "windows"], ["win32", "windows"], ["mac", "mac"], ["darwin", "mac"]]);

function platformProfile(platform) {
  // 用 Map 而不是对象字面量：toString / __proto__ 这类原型链键名也要落到 422
  const name = PLATFORM_ALIASES.get(platform);
  if (!name) throw invalid("DRAFT_PLATFORM_INVALID", `Unsupported draft platform: ${platform}`);
  return PLATFORM_PROFILES[name];
}

const DRAFT_UUID = /^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$/;
const TRACK_TYPES = new Set(["video", "audio", "text", "sticker", "effect", "filter"]);
const PHOTO_DURATION = 10_800_000_000;
const CLIP_DEFAULTS = {
  scale_x: 1, scale_y: 1, transform_x: 0, transform_y: 0, rotation: 0, alpha: 1,
  flip_horizontal: false, flip_vertical: false,
};
const KEYFRAME_PROPERTIES = {
  transform_x: "position_x",
  transform_y: "position_y",
  scale_x: "scale_x",
  scale_y: "scale_y",
  uniform_scale: "uniform_scale",
  rotation: "rotation",
  alpha: "alpha",
  volume: "volume",
  brightness: "brightness",
  contrast: "contrast",
  saturation: "saturation",
};
const ANIMATION_CATEGORIES = [
  ["video_intro", "video", "in", "IntroType"],
  ["video_outro", "video", "out", "OutroType"],
  ["video_group", "video", "group", "GroupAnimationType"],
  ["text_intro", "text", "in", "TextIntro"],
  ["text_outro", "text", "out", "TextOutro"],
  ["text_loop", "text", "loop", "TextLoopAnim"],
];

// ---------- 与 Python 对齐的小工具 ----------

/** 原仓库的 _id：sha256("scope\0label") 取前 32 位。 */
export function draftHashId(label, scope) {
  return createHash("sha256").update(`${scope}\0${label}`, "utf8").digest("hex").slice(0, 32);
}

/** 近似 Python str.casefold()（只用于路径比较和素材 id）。 */
function casefold(value) {
  return String(value).toLowerCase().toUpperCase().toLowerCase().replace(/ς/g, "σ");
}

/** 同 pathlib.Path.resolve(strict=False)：能解析的前缀按真实路径解析，后面不存在的部分照抄。 */
function resolvePath(raw) {
  const absolute = path.resolve(String(raw));
  const tail = [];
  let head = absolute;
  for (;;) {
    try {
      return path.join(realpathSync.native(head), ...tail);
    } catch {
      const parent = path.dirname(head);
      if (parent === head) return absolute;
      tail.unshift(path.basename(head));
      head = parent;
    }
  }
}

function pathKey(raw) {
  return casefold(resolvePath(raw));
}

function pySuffix(value) {
  const name = path.basename(String(value));
  const dot = name.lastIndexOf(".");
  return dot > 0 && dot < name.length - 1 ? name.slice(dot) : "";
}

function pyStem(value) {
  const name = path.basename(String(value));
  const dot = name.lastIndexOf(".");
  return dot > 0 && dot < name.length - 1 ? name.slice(0, dot) : name;
}

/** Python 的真值判断：None、空串、0、空列表、空字典都算假。 */
function present(value) {
  if (value === null || value === undefined || value === false || value === 0 || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object" && !(value instanceof PyFloat)) return Object.keys(value).length > 0;
  return true;
}

function pyInt(value) {
  return Math.trunc(Number(value));
}

function num(value) {
  return Number(value);
}

function rgb(color) {
  return [1, 3, 5].map((index) => Number.parseInt(String(color).slice(index, index + 2), 16) / 255);
}

function isInside(child, parent) {
  const relative = path.relative(parent, child);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

/**
 * 同 draft.py 的 _enum_member，按顺序在几个枚举里找成员名；都找不到时再认目录里的显示名
 * （和 catalog.mjs 一样是原仓库的超集，但不会改变原仓库能解析的结果）。
 */
function resolveMember(enumNames, raw, message) {
  for (const pass of ["name", "display"]) {
    for (const enumName of enumNames) {
      const found = members(enumName).find((member) => (pass === "name"
        ? member.name === raw
        : (member.meta.name ?? member.meta.title) === raw));
      if (found) return { enumName, name: found.name, meta: found.meta };
    }
  }
  throw invalid("DRAFT_FEATURE_INVALID", message ?? `Unknown ${enumNames.at(-1)}: ${raw}`);
}

function timerange(value) {
  return new Timerange(pyInt(value.start_us), pyInt(value.duration_us));
}

/**
 * 对应 draft.py 的 _transform（含 bug 修复）。剪映里缩放 1.0 = 素材等比放进画布（contain）；
 * cover 要按「画布比 / 素材比」与其倒数中大的那个等比放大，stretch 要把短的那一边拉满。
 * 素材比用转正后的宽高（剪映 Mac 11.6 实测：带 90 度旋转的 1280×720 放竖屏画布，缩放 1.0 已铺满）。
 */
function clipSettings(transform, fit = "contain", info = null, canvas = null) {
  if (!present(transform) && fit === "contain") return null;
  const values = { ...CLIP_DEFAULTS, ...(present(transform) ? transform : {}) };
  for (const key of ["scale_x", "scale_y", "transform_x", "transform_y", "rotation", "alpha"]) {
    values[key] = num(values[key]);
  }
  if (fit !== "contain" && info && canvas && info.width && info.height) {
    const turned = info.rotation === 90 || info.rotation === 270;
    const [width, height] = turned ? [info.height, info.width] : [info.width, info.height];
    const factor = (canvas.width / canvas.height) / (width / height);
    if (fit === "cover") {
      const uniform = Math.max(factor, 1 / factor);
      values.scale_x *= uniform;
      values.scale_y *= uniform;
    } else if (fit === "stretch") {
      if (factor >= 1) values.scale_x *= factor;
      else values.scale_y *= 1 / factor;
    } else {
      values.scale_y *= Math.max(1, factor);
    }
  }
  return new ClipSettings(values);
}

function textStyle(segment) {
  const alignments = { left: 0, center: 1, right: 2 };
  const alignment = segment.alignment ?? "center";
  if (!Object.hasOwn(alignments, alignment)) throw invalid("DRAFT_FEATURE_INVALID", `Unknown alignment: ${alignment}`);
  return {
    size: num(segment.font_size ?? 8),
    bold: Boolean(segment.bold ?? false),
    italic: Boolean(segment.italic ?? false),
    underline: Boolean(segment.underline ?? false),
    color: rgb(segment.color ?? "#FFFFFF"),
    alpha: num(segment.opacity ?? 1),
    align: alignments[alignment],
    vertical: Boolean(segment.vertical ?? false),
    letterSpacing: pyInt(segment.letter_spacing ?? 0),
    lineSpacing: pyInt(segment.line_spacing ?? 0),
    autoWrapping: Boolean(segment.auto_wrapping ?? true),
    maxLineWidth: num(segment.max_line_width ?? 0.82),
  };
}

function applyKeyframes(ids, instance, segment) {
  for (const item of segment.keyframes ?? []) {
    const property = Object.hasOwn(KEYFRAME_PROPERTIES, item.property) ? KEYFRAME_PROPERTIES[item.property] : null;
    if (!property) throw invalid("DRAFT_FEATURE_INVALID", "Unknown keyframe property");
    if (instance instanceof AudioSegment) {
      if (property !== "volume") throw invalid("DRAFT_FEATURE_INVALID", "Audio supports volume keyframes only");
      instance.addKeyframe(ids, pyInt(item.offset_us), num(item.value));
    } else {
      instance.addKeyframe(ids, property, pyInt(item.offset_us), num(item.value));
    }
  }
}

function applyFeatures(ids, instance, segment) {
  applyKeyframes(ids, instance, segment);
  if (instance instanceof VideoSegment) {
    for (const item of segment.effects ?? []) {
      instance.addEffect(ids, resolveMember(["VideoSceneEffectType", "VideoCharacterEffectType"], item.effect_type), item.params ?? null);
    }
    for (const item of segment.filters ?? []) {
      instance.addFilter(ids, resolveMember(["FilterType"], item.filter_type).meta, num(item.intensity ?? 100));
    }
    const { transition, fade, mask, chroma, background_filling: background, mix_mode: mixMode } = segment;
    if (present(transition)) {
      instance.addTransition(ids, resolveMember(["TransitionType"], transition.transition_type).meta, pyInt(transition.duration_us));
    }
    if (present(fade)) instance.addFade(ids, pyInt(fade.in_duration_us), pyInt(fade.out_duration_us));
    if (present(mask)) {
      instance.addMask(ids, resolveMember(["MaskType"], mask.mask_type), {
        centerX: num(mask.center_x ?? 0),
        centerY: num(mask.center_y ?? 0),
        size: num(mask.size ?? 0.5),
        rotation: num(mask.rotation ?? 0),
        feather: num(mask.feather ?? 0),
        invert: Boolean(mask.invert ?? false),
        rectWidth: mask.rect_width ?? null,
        roundCorner: mask.round_corner ?? null,
      });
    }
    if (present(chroma)) {
      instance.addChroma(ids, chroma.color, {
        intensity: num(chroma.intensity ?? 20),
        shadow: num(chroma.shadow ?? 0),
        edgeSmooth: num(chroma.edge_smooth ?? 0),
        spill: num(chroma.spill ?? 0),
      });
    }
    if (present(background)) {
      instance.addBackgroundFilling(ids, background.fill_type, num(background.blur ?? 0.0625), background.color ?? "#00000000");
    }
    if (present(mixMode)) instance.setMixMode(ids, resolveMember(["MixModeType"], mixMode.mode).meta);
    for (const item of segment.animations ?? []) {
      const member = resolveMember(
        ["IntroType", "OutroType", "GroupAnimationType"], item.animation_type, `Unknown animation: ${item.animation_type}`,
      );
      instance.addAnimation(ids, member, item.duration_us ?? null);
    }
  } else if (instance instanceof AudioSegment) {
    if (present(segment.fade)) instance.addFade(ids, pyInt(segment.fade.in_duration_us), pyInt(segment.fade.out_duration_us));
    for (const item of segment.effects ?? []) {
      instance.addEffect(ids, resolveMember(["AudioSceneEffectType", "ToneEffectType", "SpeechToSongType"], item.effect_type), item.params ?? null);
    }
  } else if (instance instanceof TextSegment) {
    if (present(segment.bubble)) instance.addBubble(ids, segment.bubble.effect_id, segment.bubble.resource_id);
    if (present(segment.text_effect_id)) instance.addEffect(ids, segment.text_effect_id);
    for (const item of segment.animations ?? []) {
      const member = resolveMember(
        ["TextIntro", "TextOutro", "TextLoopAnim"], item.animation_type, `Unknown text animation: ${item.animation_type}`,
      );
      instance.addAnimation(ids, member, item.duration_us ?? null);
    }
  }
}

function epochSeconds(value) {
  if (value instanceof Date) return Math.trunc(value.getTime() / 1000);
  const text = String(value);
  // Python 的 timestamp() 带微秒再取整；Date.parse 只认毫秒，先去掉小数秒
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/i.exec(text);
  const millis = Date.parse(match ? `${match[1]}${match[2] ?? ""}` : text);
  if (!Number.isFinite(millis)) throw invalid("DRAFT_SERIALIZE_FAILED", "Project timestamps are invalid");
  return Math.trunc(millis / 1000);
}

/** 对应 _attach_local_animation_paths：本机装好的动画资源包直接绑定路径，剪映打开时不必下载。 */
function attachLocalAnimationPaths(content, roots) {
  if (!roots?.length) return;
  const metadata = new Map();
  for (const [category, , , enumName] of ANIMATION_CATEGORIES) {
    for (const member of members(enumName)) {
      metadata.set(`${member.meta.effect_id}\0${member.meta.resource_id}`, { category, meta: member.meta });
    }
  }
  for (const group of content.materials.material_animations ?? []) {
    for (const animation of group.animations ?? []) {
      const entry = metadata.get(`${animation.id}\0${animation.resource_id}`);
      if (!entry) continue;
      for (const configuredRoot of roots) {
        const root = resolvePath(configuredRoot);
        const candidate = path.join(root, "animation_resources", entry.meta.effect_id, entry.meta.md5);
        let candidateStat;
        try {
          candidateStat = lstatSync(candidate);
        } catch {
          continue;
        }
        let resource;
        try {
          resource = realpathSync.native(candidate);
          if (!isInside(resource, root) || !statSync(resource).isDirectory() || candidateStat.isSymbolicLink()) {
            throw new Error("Invalid animation package directory");
          }
          const config = path.join(resource, "config.json");
          const configStat = statSync(config);
          if (!isInside(realpathSync.native(config), resource) || !configStat.isFile() || configStat.size > 1024 * 1024) {
            throw new Error("Invalid animation package configuration");
          }
          const pkg = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(readFileSync(config)));
          const effect = pkg && typeof pkg === "object" && !Array.isArray(pkg) ? pkg.effect : null;
          if (!effect || typeof effect !== "object" || Array.isArray(effect) || !Array.isArray(effect.Link) || effect.Link.length === 0) {
            throw new Error("Invalid animation package configuration");
          }
        } catch {
          throw invalid(
            "ANIMATION_RESOURCE_INVALID",
            "An installed animation package is incomplete or outside configured media roots",
          );
        }
        animation.path = resource.split(path.sep).join("/");
        if (entry.category.startsWith("text_")) animation.panel = "text";
        break;
      }
    }
  }
}

function lookupName(materialNames, key) {
  if (!materialNames) return undefined;
  if (materialNames instanceof Map) return materialNames.get(key);
  return Object.hasOwn(materialNames, key) ? materialNames[key] : undefined;
}

function cropKey(crop) {
  const floats = Object.fromEntries(Object.entries(crop).map(([key, value]) => [key, typeof value === "number" ? pyFloat(value) : value]));
  return dumps(floats, { sortKeys: true, separators: [",", ":"] });
}

function defaultNewId() {
  return randomUUID().replaceAll("-", "");
}

/** serialize_draft 的主体：返回草稿对象（未写成文本）。 */
function buildContent({
  project, media = [], subtitleTracks = [], materialNames = {}, draftId = null,
  platform = "windows", animationResourceRoots = [], materialPath = null, newId = defaultNewId,
}) {
  const profile = platformProfile(platform);
  const ids = newId;
  const canvas = project.canvas;
  const script = new ScriptFile(ids, canvas.width, canvas.height, canvas.fps);
  const mediaByPath = new Map((media ?? []).map((info) => [casefold(String(info.path)), info]));
  const materials = new Map();
  const allTracks = project.tracks ?? [];
  const subtitleById = new Map((subtitleTracks ?? []).map(([track, segments]) => [track.id, segments]));
  const refs = new Map();

  for (const track of allTracks) {
    let type = track.track_type;
    if (!TRACK_TYPES.has(type)) {
      if (track.kind !== "subtitle") throw invalid("TRACK_TYPE_INVALID", "Unsupported track type");
      type = "text";
    }
    refs.set(track.id, script.appendTrack(type, String(track.name), Boolean(track.mute)));
  }

  const segments = [];
  for (const track of allTracks) {
    if (track.kind === "subtitle") continue;
    for (const segment of track.segments ?? []) segments.push([track.id, segment]);
  }
  for (const track of allTracks) {
    if (track.kind !== "subtitle") continue;
    const list = subtitleById.has(track.id) ? subtitleById.get(track.id) : track.segments ?? [];
    for (const segment of list) segments.push([track.id, segment]);
  }

  for (const [trackId, segment] of segments) {
    const ref = refs.get(trackId);
    const target = timerange(segment.timeline);
    if (segment.kind === "track_effect") {
      const member = resolveMember(["VideoSceneEffectType", "VideoCharacterEffectType"], segment.effect_type, "Unknown video effect");
      script.addEffect(member, target, ref, segment.params ?? null).segmentId = draftHashId(segment.id, "segment");
      continue;
    }
    if (segment.kind === "track_filter") {
      const member = resolveMember(["FilterType"], segment.filter_type);
      script.addFilter(member.meta, target, ref, num(segment.intensity)).segmentId = draftHashId(segment.id, "segment");
      continue;
    }
    if (segment.kind === "sticker") {
      const instance = new StickerSegment(ids, segment.resource_id, target, clipSettings(segment.transform ?? null));
      instance.segmentId = draftHashId(segment.id, "segment");
      applyFeatures(ids, instance, segment);
      script.addSegment(instance, ref);
      continue;
    }
    if (segment.kind === "text") {
      const border = present(segment.border) ? segment.border : null;
      const shadow = present(segment.shadow) ? segment.shadow : null;
      const instance = new TextSegment(ids, segment.text, target, {
        font: present(segment.font_family) ? resolveMember(["FontType"], segment.font_family).meta : null,
        style: textStyle(segment),
        clipSettings: clipSettings(segment.transform ?? null),
        border: border && { alpha: num(border.alpha ?? 1), color: rgb(border.color ?? "#000000"), width: num(border.width ?? 40) },
        background: present(segment.background) ? segment.background : null,
        shadow: shadow && {
          alpha: num(shadow.alpha ?? 1),
          color: rgb(shadow.color ?? "#000000"),
          diffuse: num(shadow.diffuse ?? 15),
          distance: num(shadow.distance ?? 5),
          angle: num(shadow.angle ?? -45),
        },
      });
      instance.segmentId = draftHashId(segment.id, "segment");
      instance.materialId = draftHashId(segment.id, "text-material");
      applyFeatures(ids, instance, segment);
      script.addSegment(instance, ref);
      continue;
    }

    const info = typeof segment.path === "string" ? mediaByPath.get(pathKey(segment.path)) : undefined;
    if (!info) throw invalid("MEDIA_NOT_FOUND", "Clip material is unavailable");
    const key = casefold(String(info.path));
    const crop = info.kind !== "audio" ? segment.crop ?? null : null;
    const materialKey = crop === null ? key : `${key}\0crop\0${cropKey(crop)}`;
    let material = materials.get(materialKey);
    if (!material) {
      const name = lookupName(materialNames, key) || path.basename(String(info.path));
      const file = materialPath ? materialPath(info) : path.resolve(String(info.path));
      material = info.kind === "audio"
        ? new AudioMaterial(ids, { path: file, name, duration: info.duration_us })
        : new VideoMaterial(ids, {
          path: file,
          name,
          duration: info.kind === "image" ? PHOTO_DURATION : info.duration_us,
          width: info.width ?? 0,
          height: info.height ?? 0,
          type: info.kind === "image" ? "photo" : "video",
          crop: new CropSettings(present(crop) ? Object.fromEntries(Object.entries(crop).map(([k, v]) => [k, num(v)])) : {}),
        });
      // 修 bug：素材 id 要在建片段之前定好，否则第一个片段引用随机 id、素材表里出现两份
      material.materialId = draftHashId(materialKey, "material");
      materials.set(materialKey, material);
    }
    const common = {
      source: timerange(segment.source),
      volume: num(segment.volume),
      changePitch: Boolean(segment.change_pitch ?? false),
    };
    const instance = info.kind === "audio"
      ? new AudioSegment(ids, material, target, common)
      : new VideoSegment(ids, material, target, {
        ...common,
        clipSettings: clipSettings(segment.transform ?? null, segment.fit ?? "contain", info, canvas),
      });
    if (segment.speed !== null && segment.speed !== undefined) instance.speed.speed = num(segment.speed);
    instance.segmentId = draftHashId(segment.id, "segment");
    applyFeatures(ids, instance, segment);
    script.addSegment(instance, ref);
  }

  const content = script.exportContent();
  const generated = draftId || draftHashId(project.id, "draft").toUpperCase();
  content.id = `${generated.slice(0, 8)}-${generated.slice(8, 12)}-${generated.slice(12, 16)}-${generated.slice(16, 20)}-${generated.slice(20, 32)}`;
  content.name = project.name;
  content.create_time = epochSeconds(project.created_at);
  content.update_time = epochSeconds(project.updated_at);
  content.last_modified_platform = { ...profile.platform };
  content.platform = { ...profile.platform };
  content.new_version = profile.newVersion;
  content.version = profile.version;
  attachLocalAnimationPaths(content, animationResourceRoots);
  return content;
}

function renderContent(content) {
  try {
    return dumps(content, { indent: 4, allowNaN: false });
  } catch {
    throw invalid("DRAFT_SERIALIZE_FAILED", "Draft content is invalid");
  }
}

// ---------- 对外接口 ----------

/**
 * 移植 service.py save_draft / build_draft 里准备草稿输入的部分。
 * subtitleTracks: [[track, segments], ...]；materialNames: { 规范化素材路径: 素材名 }。
 */
export function draftInputs(project) {
  const tracks = project.tracks ?? [];
  const subtitleTracks = tracks.filter((track) => track.kind === "subtitle").map((track) => [track, track.segments]);
  const materialNames = {};
  for (const track of tracks) {
    if (track.kind === "subtitle") continue;
    for (const segment of track.segments ?? []) {
      if (segment.kind === "text" || segment.kind === "sticker" || !present(segment.material_name)) continue;
      const key = pathKey(segment.path);
      let name = segment.material_name;
      if (casefold(pySuffix(name)) === casefold(pySuffix(segment.path))) name = pyStem(name);
      if (!Object.hasOwn(materialNames, key)) materialNames[key] = name;
    }
  }
  return { subtitleTracks, materialNames };
}

/**
 * 同 draft.py serialize_draft，返回草稿主文件的 JSON 文本（4 空格缩进，与原仓库逐字节一致）。
 * 额外可选参数：materialPath(info) → 写进草稿的素材路径（Mac 把素材克隆进草稿目录时用）；
 * newId() → 随机 id 生成器（测试注入）。
 */
export function serializeDraft(options) {
  return renderContent(buildContent(options));
}

async function exists(target) {
  try {
    await lstat(target);
    return true;
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ENOTDIR") return false;
    throw error;
  }
}

function draftExists() {
  return new EditError("DRAFT_EXISTS", "Draft folder already exists", { status: 409 });
}

/** 硬链接到目标且不覆盖；文件系统不支持硬链接（如 exFAT 外接盘）时退回独占复制。 */
async function linkExclusive(source, target) {
  try {
    await link(source, target);
  } catch (error) {
    if (!["EPERM", "ENOTSUP", "EXDEV", "ENOSYS", "EOPNOTSUPP"].includes(error.code)) throw error;
    await copyFile(source, target, 1 /* COPYFILE_EXCL */);
  }
}

/** 同 storage.atomic_write(replace=False)：独占写临时文件、fsync，再链接到目标（已存在即 409）。 */
async function atomicWrite(target, data) {
  await mkdir(path.dirname(target), { recursive: true });
  const temp = path.join(path.dirname(target), `.${path.basename(target)}.${randomBytes(8).toString("hex")}.tmp`);
  try {
    const handle = await open(temp, "wx", 0o600);
    try {
      await handle.writeFile(data);
      await handle.sync();
    } finally {
      await handle.close();
    }
    try {
      await linkExclusive(temp, target);
    } catch (error) {
      if (error.code === "EEXIST") throw new EditError("PROJECT_CONFLICT", "Target already exists", { status: 409 });
      throw error;
    }
  } finally {
    await unlink(temp).catch(() => {});
  }
}

async function publishDirectory(temporary, target) {
  if (await exists(target)) throw draftExists();
  try {
    await rename(temporary, target);
  } catch (error) {
    if (["EEXIST", "ENOTEMPTY"].includes(error.code) || await exists(target)) throw draftExists();
    throw error;
  }
}

function sha256(data) {
  return createHash("sha256").update(data).digest("hex");
}

async function metadata(project, directory, content, media) {
  let size = 0;
  for (const info of media ?? []) {
    try {
      size += (await stat(String(info.path))).size;
    } catch {
      // 原仓库同样只统计还存在的素材
    }
  }
  return {
    draft_id: content.id,
    draft_name: project.name,
    draft_fold_path: directory,
    draft_root_path: path.dirname(directory),
    draft_timeline_materials_size_: size,
    tm_draft_create: content.create_time * 1_000_000,
    tm_draft_modified: content.update_time * 1_000_000,
    tm_duration: content.duration,
  };
}

/**
 * 同 draft.py create_draft_folder：在 root 下发布 <32 位大写 id>/ 草稿目录，不覆盖已有目录。
 * 目录里有：主文件（windows draft_content.json / mac draft_info.json）、draft_meta_info.json、
 * project.json（工程快照）和 owned 时的 .owned（内容是工程 id）。
 * folderName 换掉目录名（草稿箱按草稿名命名文件夹时用），只能是一层目录名；不给时同原仓库按 id 命名。
 */
export async function createDraftFolder({
  project, root, draftId, media = [], subtitleTracks = [], materialNames = {}, owned = true,
  platform = "windows", animationResourceRoots = [], materialPath = null, newId = defaultNewId,
  folderName = null,
}) {
  const profile = platformProfile(platform);
  let id = String(draftId ?? "");
  let generated = draftHashId(project.id, "draft").toUpperCase();
  if (DRAFT_UUID.test(id)) {
    id = id.replaceAll("-", "");
    generated = id.toUpperCase();
  }
  id = id.toUpperCase();
  const rootPath = resolvePath(root);
  if (!/^[0-9A-F]{32}$/.test(id)) throw invalid("DRAFT_ID_INVALID", "Draft identifier is invalid");
  const folder = folderName == null ? id : String(folderName);
  if (!folder || folder === "." || folder === ".." || /[\\/\0]/.test(folder)) {
    throw invalid("PATH_UNSAFE", "Draft folder name is unsafe");
  }
  const target = path.join(rootPath, folder);
  if (!isInside(target, rootPath)) throw invalid("PATH_UNSAFE", "Draft path left its root");
  await mkdir(rootPath, { recursive: true, mode: 0o700 });
  if (await exists(target)) throw draftExists();

  const temporary = path.join(rootPath, `.${id}.${randomBytes(8).toString("hex")}.tmp`);
  const claim = path.join(rootPath, `.${id}.${randomBytes(8).toString("hex")}.claim`);
  try {
    try {
      await mkdir(claim, { mode: 0o700 });
      if (owned) await atomicWrite(path.join(claim, "owned"), Buffer.from(String(project.id), "utf8"));
      await mkdir(temporary, { mode: 0o700 });
    } catch (error) {
      if (error.code === "EEXIST") throw draftExists();
      throw error;
    }
    const document = buildContent({
      project, media, subtitleTracks, materialNames, draftId: generated,
      platform, animationResourceRoots, materialPath, newId,
    });
    const content = renderContent(document);
    const contentBytes = Buffer.from(content, "utf8");
    const projectBytes = Buffer.from(dumps(project, { indent: 2, allowNaN: false }), "utf8");
    const metadataBytes = Buffer.from(dumps(await metadata(project, target, document, media), { indent: 2 }), "utf8");
    await atomicWrite(path.join(temporary, profile.contentFile), contentBytes);
    await atomicWrite(path.join(temporary, "draft_meta_info.json"), metadataBytes);
    await atomicWrite(path.join(temporary, "project.json"), projectBytes);
    if (owned) await linkExclusive(path.join(claim, "owned"), path.join(temporary, ".owned"));
    await publishDirectory(temporary, target);
    const manifest = createHash("sha256");
    for (const [name, payload] of [[profile.contentFile, contentBytes], ["draft_meta_info.json", metadataBytes], ["project.json", projectBytes]]) {
      manifest.update(name, "ascii");
      manifest.update(payload);
    }
    return {
      directory: target,
      contentPath: path.join(target, profile.contentFile),
      content,
      contentHash: sha256(contentBytes),
      draftHash: manifest.digest("hex"),
      size: contentBytes.length + metadataBytes.length,
    };
  } finally {
    await rm(temporary, { recursive: true, force: true });
    await rm(claim, { recursive: true, force: true });
  }
}

const memberNames = (enumName) => members(enumName).map((member) => member.name);

/** 同 draft.py editing_options：各类可选项的成员名。 */
export function editingOptions() {
  return {
    video_effects: memberNames("VideoSceneEffectType"),
    character_effects: memberNames("VideoCharacterEffectType"),
    audio_effects: memberNames("AudioSceneEffectType"),
    tone_effects: memberNames("ToneEffectType"),
    filters: memberNames("FilterType"),
    mask_types: memberNames("MaskType"),
    mix_modes: memberNames("MixModeType"),
    text_font_families: memberNames("FontType"),
    transitions: memberNames("TransitionType"),
    video_intro_animations: memberNames("IntroType"),
    video_outro_animations: memberNames("OutroType"),
    video_group_animations: memberNames("GroupAnimationType"),
    text_intro_animations: memberNames("TextIntro"),
    text_outro_animations: memberNames("TextOutro"),
    text_loop_animations: memberNames("TextLoopAnim"),
    text_addons: ["bubble", "effect"],
    keyframe_properties: Object.keys(KEYFRAME_PROPERTIES),
    marker_support: ["service_metadata_only"],
  };
}

/** 同 draft.py animation_catalog：按关键词（忽略空白、下划线、大小写）/种类/作用对象搜动画。 */
export function animationCatalog({ query = "", kind = null, target = null, vipOnly = false, limit = 50, offset = 0 } = {}) {
  const normalize = (value) => casefold(String(value).replace(/[\s_]+/gu, ""));
  const queryKey = normalize(query);
  const items = [];
  for (const [category, animationTarget, mode, enumName] of ANIMATION_CATEGORIES) {
    if (kind !== null && kind !== undefined && category !== kind) continue;
    if (target !== null && target !== undefined && animationTarget !== target) continue;
    for (const member of members(enumName)) {
      const meta = member.meta;
      if (vipOnly && !meta.is_vip) continue;
      if (queryKey) {
        const searchable = [category, member.name, meta.title, meta.resource_id, meta.effect_id];
        if (!searchable.some((value) => normalize(value).includes(queryKey))) continue;
      }
      items.push({
        kind: category,
        target: animationTarget,
        animation_type: member.name,
        animation_mode: mode,
        name: meta.title,
        resource_id: meta.resource_id,
        effect_id: meta.effect_id,
        md5: meta.md5,
        default_duration_us: meta.duration,
        is_vip: meta.is_vip,
      });
    }
  }
  return { total: items.length, limit, offset, items: items.slice(offset, offset + limit) };
}

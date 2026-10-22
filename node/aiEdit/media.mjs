// AI 剪辑的媒体探测：移植自 JianYingAPI 的 media.py（MediaResolver.resolve / probe）。
// 原仓库调用 ffprobe 取「流类型、宽高、时长」；安装包里不带 ffprobe，这里直接解析容器头，
// 时长按 ffprobe（FFmpeg 9）各解复用器的算法取值，再走原仓库「打印成 %f 秒 → float → 取整微秒」
// 的同一条换算，所以 mp4/mov/wav/flac 等能与原仓库逐微秒对上。
// 大文件只定位读必要的部分（文件头、moov/moof、尾部若干页），不整份读进内存。

import { open, realpath, stat } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { EditError, invalid } from "./errors.mjs";

export const VIDEO_SUFFIXES = new Set([".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"]);
export const IMAGE_SUFFIXES = new Set([".png", ".jpg", ".jpeg", ".webp"]);
export const AUDIO_SUFFIXES = new Set([".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"]);

/** 同 models.py 的 MAX_TIME_US（24 小时）；图片的时长固定取它。 */
export const MAX_TIME_US = 24 * 60 * 60 * 1_000_000;
const DEFAULT_MAX_BYTES = 8 * 1024 ** 3;

const READ_WINDOW = 256 * 1024;
// moov / Info / Tracks 这类元数据要整块读进来解析；正常文件远小于这个数，超过当作坏文件。
const MAX_METADATA_BYTES = 64 * 1024 * 1024;
const US = [1, 1_000_000];
// 原仓库给 ffprobe 设了 timeout=30，超时报 MEDIA_PROBE_FAILED。这里在采集器主进程里解析，
// 还要每隔一小段让出事件循环，免得一个构造出来的文件把采集器卡住。
const PROBE_TIMEOUT_MS = 30_000;
const YIELD_INTERVAL_MS = 20;

export function mediaKind(filePath) {
  const suffix = pathSuffix(String(filePath)).toLowerCase();
  if (VIDEO_SUFFIXES.has(suffix)) return "video";
  if (IMAGE_SUFFIXES.has(suffix)) return "image";
  if (AUDIO_SUFFIXES.has(suffix)) return "audio";
  throw invalid("MEDIA_TYPE_UNSUPPORTED", "Unsupported local media type");
}

/** 同 MediaResolver.resolve：校验顺序与错误码逐条对应原仓库。返回规范化后的绝对路径。 */
export async function resolveMediaPath(raw, { roots = [], maxBytes = DEFAULT_MAX_BYTES } = {}) {
  if (typeof raw !== "string") throw invalid("PATH_INVALID", "Path must be a string");
  if (raw.includes("\0")) throw invalid("PATH_INVALID", "Path contains a null byte");
  const candidate = expandUser(raw);
  if (!isAbsolutePath(candidate)) throw invalid("PATH_NOT_ABSOLUTE", "Media paths must be absolute");
  let normalized;
  try {
    normalized = await realpath(candidate);
  } catch {
    throw invalid("MEDIA_NOT_FOUND", "Media file does not exist");
  }
  const list = typeof roots === "string" ? [roots] : Array.from(roots ?? []);
  const allowed = await Promise.all(list.map((root) => resolveLoose(String(root))));
  if (!allowed.some((root) => isWithin(normalized, root))) {
    throw invalid("PATH_OUTSIDE_WHITELIST", "Media path is outside configured roots");
  }
  mediaKind(normalized);
  let info;
  try {
    info = await stat(normalized);
  } catch (error) {
    // Python 的 is_file() 把「不存在」一类错误当成 False，其余读不了的才走 MEDIA_NOT_FOUND。
    if (["ENOENT", "ENOTDIR", "EBADF", "ELOOP"].includes(error?.code)) {
      throw invalid("MEDIA_NOT_FILE", "Media path is not a regular file");
    }
    throw invalid("MEDIA_NOT_FOUND", "Media file cannot be read");
  }
  if (!info.isFile()) throw invalid("MEDIA_NOT_FILE", "Media path is not a regular file");
  if (info.size === 0) throw invalid("MEDIA_EMPTY", "Media file is empty");
  if (info.size > maxBytes) throw invalid("MEDIA_TOO_LARGE", "Media file exceeds size limit");
  return normalized;
}

/**
 * 同 MediaResolver.probe：返回 { path, kind, duration_us, width, height, rotation, audio, video }。
 * width/height 是编码宽高（同 ffprobe 和原仓库）；rotation 是原仓库没有的：mp4/mov 视频轨显示矩阵的旋转角
 * （0/90/180/270，ffprobe 显示的 rotation 换成 0 到 359，比如 -90 记 270），手机竖拍常是编码横屏 + 转 90 度。
 * 显示矩阵不是纯旋转（镜像、任意角度）和其他容器一律记 0。
 * timeoutMs 对应原仓库 ffprobe 的超时（默认 30 秒），一般不用传。
 */
export async function probeMedia(raw, options = {}) {
  const filePath = await resolveMediaPath(raw, options);
  const expected = mediaKind(filePath);
  const container = await parseFile(filePath, options.timeoutMs ?? PROBE_TIMEOUT_MS);

  const streams = container.streams;
  const hasVideo = streams.some((item) => item.type === "video");
  const hasAudio = streams.some((item) => item.type === "audio");
  const candidates = ffprobeDurations(container);
  const best = candidates.length ? candidates.reduce((a, b) => (b > a ? b : a)) : 0n;
  if (best < 0n) throw invalid("MEDIA_INVALID", "Media metadata is invalid");
  const durationUs = pythonFloorMicros(best);

  let dims = {};
  let duration_us;
  if (expected === "image") {
    if (!hasVideo) throw invalid("MEDIA_KIND_MISMATCH", "Image has no picture stream");
    duration_us = MAX_TIME_US;
    dims = streams.find((item) => item.type === "video");
  } else if (expected === "video") {
    if (!hasVideo) throw invalid("MEDIA_KIND_MISMATCH", "Video has no picture stream");
    if (best <= 0n) throw invalid("MEDIA_INVALID", "Video duration is missing");
    duration_us = Math.min(MAX_TIME_US, durationUs);
    dims = streams.find((item) => item.type === "video");
  } else {
    if (hasVideo) throw invalid("MEDIA_KIND_MISMATCH", "Audio must not contain video");
    if (!hasAudio || best <= 0n) throw invalid("MEDIA_INVALID", "Audio duration is missing");
    duration_us = Math.min(MAX_TIME_US, durationUs);
  }
  if (expected !== "image" && duration_us === 0) {
    throw invalid("MEDIA_INVALID", "Media has no usable duration");
  }
  return {
    path: filePath,
    kind: expected,
    duration_us,
    width: safeDimension(dims.width),
    height: safeDimension(dims.height),
    rotation: dims.rotation ?? 0,
    audio: hasAudio,
    video: hasVideo,
  };
}

// ---------------------------------------------------------------------------
// 路径工具（对应 pathlib 的 suffix / expanduser / is_absolute / resolve / is_relative_to）

function pathSuffix(filePath) {
  const name = path.basename(filePath);
  const index = name.lastIndexOf(".");
  return index > 0 && index < name.length - 1 ? name.slice(index) : "";
}

function expandUser(raw) {
  const windows = process.platform === "win32";
  if (raw === "~" || raw.startsWith("~/") || (windows && raw.startsWith("~\\"))) {
    return os.homedir() + raw.slice(1);
  }
  return raw;
}

function isAbsolutePath(candidate) {
  if (process.platform !== "win32") return candidate.startsWith("/");
  // Windows 上 pathlib 要求盘符加根（C:\），或者 UNC（\\server\share）。
  return /^[A-Za-z]:[\\/]/.test(candidate) || /^[\\/]{2}[^\\/]+[\\/][^\\/]+/.test(candidate);
}

/** Path.resolve()（非 strict）：存在的部分解开符号链接，不存在的部分原样拼回去。 */
async function resolveLoose(target) {
  const absolute = path.resolve(target);
  try {
    return await realpath(absolute);
  } catch {
    const parent = path.dirname(absolute);
    if (parent === absolute) return absolute;
    return path.join(await resolveLoose(parent), path.basename(absolute));
  }
}

function isWithin(child, root) {
  const relative = path.relative(root, child);
  if (relative === "") return true;
  if (path.isAbsolute(relative)) return false;
  return relative !== ".." && !relative.startsWith(`..${path.sep}`);
}

function safeDimension(value) {
  return Number.isInteger(value) && value >= 1 && value <= 100000 ? value : null;
}

// ---------------------------------------------------------------------------
// 时长换算：模拟 ffprobe 的 update_stream_timings / fill_all_stream_timings 与 "%f" 打印，
// 再按原仓库 max(各值) → floor(秒 × 1e6) 取整。

/** av_rescale_rnd(a, b, c, AV_ROUND_NEAR_INF)。 */
function rescale(a, b, c) {
  const value = BigInt(a);
  const mul = BigInt(b);
  const div = BigInt(c);
  if (div <= 0n) return 0n;
  if (value < 0n) return -rescale(-value, mul, div);
  return (value * mul + div / 2n) / div;
}

function rescaleQ(value, from, to) {
  return rescale(value, BigInt(from[0]) * BigInt(to[1]), BigInt(to[0]) * BigInt(from[1]));
}

/** C 的 printf("%f")：按 double 的精确值保留 6 位小数（逢五取偶），返回微秒整数。 */
function printfMicros(seconds) {
  if (!Number.isFinite(seconds)) return null;
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, seconds);
  const bits = view.getBigUint64(0);
  const negative = bits >> 63n === 1n;
  const exponentBits = Number((bits >> 52n) & 0x7ffn);
  let mantissa = bits & ((1n << 52n) - 1n);
  let exponent;
  if (exponentBits === 0) {
    exponent = -1074;
  } else {
    mantissa |= 1n << 52n;
    exponent = exponentBits - 1075;
  }
  let numerator = mantissa * 1_000_000n;
  let denominator = 1n;
  if (exponent >= 0) numerator <<= BigInt(exponent);
  else denominator <<= BigInt(-exponent);
  let quotient = numerator / denominator;
  const twice = (numerator % denominator) * 2n;
  if (twice > denominator || (twice === denominator && quotient % 2n === 1n)) quotient += 1n;
  return negative ? -quotient : quotient;
}

/** Python 侧：float("秒.微秒") × 1_000_000 再 floor（例如 2.066667 会落到 2066666）。 */
function pythonFloorMicros(micros) {
  const negative = micros < 0n;
  const absolute = negative ? -micros : micros;
  const text = `${negative ? "-" : ""}${absolute / 1_000_000n}.${String(absolute % 1_000_000n).padStart(6, "0")}`;
  return Math.floor(Number(text) * 1_000_000);
}

function isText(item) {
  return item.type === "subtitle" || item.type === "data";
}

function ffprobeDurations(container) {
  const streams = container.streams;
  // update_stream_timings：解复用器没给容器时长时，取各流时长与「最晚结束 − 最早开始」的较大者。
  let start = null;
  let startText = null;
  let end = null;
  let endText = null;
  let duration = null;
  let durationText = null;
  const max = (a, b) => (a === null || b > a ? b : a);
  const min = (a, b) => (a === null || b < a ? b : a);
  for (const item of streams) {
    const text = isText(item);
    if (item.start != null) {
      const start1 = rescaleQ(item.start, item.timeBase, US);
      if (text) startText = min(startText, start1);
      else start = min(start, start1);
      if (item.duration != null) {
        const end1 = rescaleQ(item.duration, item.timeBase, US) + start1;
        if (text) endText = max(endText, end1);
        else end = max(end, end1);
      }
    }
    if (item.duration != null) {
      const duration1 = rescaleQ(item.duration, item.timeBase, US);
      if (text) durationText = max(durationText, duration1);
      else duration = max(duration, duration1);
    }
  }
  if (start === null || (startText !== null && start > startText && start - startText < 1_000_000n)) start = startText;
  if (end === null || (endText !== null && end < endText && endText - end < 1_000_000n)) end = endText;
  if (duration === null || (durationText !== null && duration < durationText && durationText - duration < 1_000_000n)) {
    duration = durationText;
  }
  if (start !== null && end !== null && end >= start) duration = max(duration, end - start);
  const formatUs = container.durationUs ?? (duration !== null && duration > 0n ? duration : null);

  const values = formatUs === null ? [] : [formatUs];
  for (const item of streams) {
    // fill_all_stream_timings：没有起点的流，时长改用容器时长。
    const value = item.start == null && formatUs !== null ? rescaleQ(formatUs, US, item.timeBase) : item.duration;
    if (value == null) continue;
    const micros = printfMicros((item.timeBase[0] / item.timeBase[1]) * Number(value));
    if (micros !== null) values.push(micros);
  }
  return values;
}

// ---------------------------------------------------------------------------
// 读文件：按位置读，带一个小窗口缓存，方便顺序扫帧。

class Malformed extends Error {}

class ProbeTimeout extends Error {}

function malformed(message = "Malformed media") {
  return new Malformed(message);
}

class FileSource {
  constructor(handle, size, timeoutMs = PROBE_TIMEOUT_MS) {
    this.handle = handle;
    this.size = size;
    this.cache = Buffer.alloc(0);
    this.cacheStart = 0;
    this.deadline = Date.now() + timeoutMs;
    this.yieldedAt = Date.now();
  }

  /** window：缓存不命中时一次多读多少；顺序扫帧用大窗口，跳着读盒子头用小窗口。 */
  async read(position, length, window = READ_WINDOW) {
    // 命中缓存的 await 只排微任务、不让出事件循环，所以在这里统一计时、让路和判超时。
    const now = Date.now();
    if (now >= this.deadline) throw new ProbeTimeout();
    if (now - this.yieldedAt >= YIELD_INTERVAL_MS) {
      await new Promise((resolve) => setImmediate(resolve));
      this.yieldedAt = Date.now();
    }
    if (!Number.isSafeInteger(position) || position < 0 || length < 0) throw malformed("Bad read");
    const end = Math.min(this.size, position + length);
    if (end <= position) return Buffer.alloc(0);
    const cacheEnd = this.cacheStart + this.cache.length;
    if (position >= this.cacheStart && end <= cacheEnd) {
      return this.cache.subarray(position - this.cacheStart, end - this.cacheStart);
    }
    const want = Math.min(Math.max(end - position, window), this.size - position);
    const buffer = Buffer.allocUnsafe(want);
    let filled = 0;
    while (filled < want) {
      const { bytesRead } = await this.handle.read(buffer, filled, want - filled, position + filled);
      if (!bytesRead) break;
      filled += bytesRead;
    }
    this.yieldedAt = Date.now();
    this.cache = buffer.subarray(0, filled);
    this.cacheStart = position;
    return this.cache.subarray(0, Math.min(filled, end - position));
  }

  async exact(position, length) {
    const buffer = await this.read(position, length);
    if (buffer.length < length) throw malformed("Unexpected end of file");
    return buffer;
  }
}

// 原仓库只有 ffprobe 本身起不来（OSError）或超时才报 MEDIA_PROBE_FAILED；文件打不开、读盘出错
// 都是 ffprobe 非 0 退出，报 MEDIA_INVALID。本进程句柄耗尽相当于「起不来」，单独归到前者。
const PROCESS_LIMIT_CODES = new Set(["EMFILE", "ENFILE"]);

function probeFailure(error) {
  if (error instanceof EditError) return error;
  if (error instanceof ProbeTimeout || PROCESS_LIMIT_CODES.has(error?.code)) {
    return invalid("MEDIA_PROBE_FAILED", "Media probing failed");
  }
  return invalid("MEDIA_INVALID", "Media file could not be parsed");
}

async function parseFile(filePath, timeoutMs) {
  let handle;
  try {
    handle = await open(filePath, "r");
  } catch (error) {
    throw probeFailure(error);
  }
  try {
    const { size } = await handle.stat();
    return await parseMedia(new FileSource(handle, size, timeoutMs));
  } catch (error) {
    throw probeFailure(error);
  } finally {
    await handle.close().catch(() => {});
  }
}

const ISO_TOP_LEVEL = new Set([
  "ftyp", "moov", "mdat", "free", "skip", "wide", "pnot", "uuid", "styp", "sidx", "moof", "junk", "udta",
]);

/** 按内容认格式（ffprobe 也不看扩展名），扩展名只决定期望的 kind。 */
async function parseMedia(src) {
  const head = await src.read(0, 64);
  if (head.length < 4) throw malformed("File too small");
  const tag = head.toString("latin1", 0, 4);
  const form = head.toString("latin1", 8, 12);
  if (head.readUInt32BE(0) === 0x1a45dfa3) return parseMatroska(src);
  if ((tag === "RIFF" || tag === "RF64" || tag === "BW64") && form === "WAVE") return parseWav(src);
  if (tag === "RIFF" && form === "AVI ") return parseAvi(src);
  if (tag === "RIFF" && form === "WEBP") return parseWebp(src);
  if (head.length >= 8 && head.readUInt32BE(0) === 0x89504e47 && head.readUInt32BE(4) === 0x0d0a1a0a) {
    return parsePng(src);
  }
  if (head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) return parseJpeg(src);
  if (tag === "GIF8" && (head.toString("latin1", 0, 6) === "GIF87a" || head.toString("latin1", 0, 6) === "GIF89a")) {
    return image(head.readUInt16LE(6), head.readUInt16LE(8));
  }
  if (tag === "fLaC") return parseFlac(src, 0);
  if (tag === "OggS") return parseOgg(src);
  if (head.length >= 8 && ISO_TOP_LEVEL.has(head.toString("latin1", 4, 8))) return parseIsoBmff(src);
  if (head[0] === 0x42 && head[1] === 0x4d && head.length >= 26) {
    const bmp = parseBmp(head);
    if (bmp) return bmp;
  }

  // 剩下的是裸音频流：先跳过开头的 ID3v2 标签（可以有好几个）。
  let offset = 0;
  for (;;) {
    const id3 = await src.read(offset, 10);
    if (id3.length < 10 || id3.toString("latin1", 0, 3) !== "ID3") break;
    if (id3[3] === 0xff || id3[4] === 0xff || (id3[6] | id3[7] | id3[8] | id3[9]) & 0x80) break;
    const length = ((id3[6] & 0x7f) << 21) | ((id3[7] & 0x7f) << 14) | ((id3[8] & 0x7f) << 7) | (id3[9] & 0x7f);
    offset += 10 + length + (id3[5] & 0x10 ? 10 : 0);
  }
  const probe = await src.read(offset, 4);
  if (probe.length >= 4 && probe.toString("latin1", 0, 4) === "fLaC") return parseFlac(src, offset);
  const found = await findRawAudio(src, offset);
  if (found?.kind === "adts") return parseAdts(src, found.position);
  if (found?.kind === "mpeg") return parseMp3(src, offset, found.position);
  throw malformed("Unknown media format");
}

function image(width, height, rotation = 0) {
  const stream = { type: "video", width, height, rotation, timeBase: [1, 25], start: null, duration: null };
  return { streams: [stream], durationUs: null };
}

function audioOnly(timeBase, start, duration) {
  return { streams: [{ type: "audio", timeBase, start, duration }], durationUs: null };
}

/** mov_update_dts_shift：负的显示时间偏移把整条轨道的解码时间往后推。 */
function dtsShiftFor(offset) {
  return offset === -0x80000000 ? 0x7fffffff : -offset;
}

// ---------------------------------------------------------------------------
// 图片

async function parsePng(src) {
  const head = await src.exact(0, 24);
  if (head.toString("latin1", 12, 16) !== "IHDR") throw malformed("PNG without IHDR");
  return image(head.readUInt32BE(16), head.readUInt32BE(20));
}

async function parseJpeg(src) {
  let pos = 2;
  let rotation = 0;
  while (pos + 1 < src.size) {
    const marker = await src.exact(pos, 2);
    // 段与段之间的填充字节和垃圾跳过去，跟解码器一样找下一个 0xFF。
    if (marker[0] !== 0xff || marker[1] === 0xff) {
      pos = await nextJpegMarker(src, pos);
      continue;
    }
    const code = marker[1];
    if (code === 0x01 || (code >= 0xd0 && code <= 0xd8)) {
      pos += 2;
      continue;
    }
    if (code === 0xd9 || code === 0xda) break;
    const length = (await src.exact(pos + 2, 2)).readUInt16BE(0);
    if (length < 2) throw malformed("Bad JPEG segment");
    // 手机竖拍的照片常是横着存 + EXIF 方向标记，看图软件和剪映都会转正；按转正后的宽高算，分屏和画面适配才对得上
    if (code === 0xe1 && !rotation) rotation = exifRotation(await src.exact(pos + 4, length - 2));
    const isFrame = code >= 0xc0 && code <= 0xcf && code !== 0xc4 && code !== 0xc8 && code !== 0xcc;
    if (isFrame) {
      const frame = await src.exact(pos + 4, 5);
      return image(frame.readUInt16BE(3), frame.readUInt16BE(1), rotation);
    }
    pos += 2 + length;
  }
  throw malformed("JPEG without frame header");
}

/** EXIF（APP1 段）里的方向标记换成旋转角：5、6 是转 90 度，7、8 是 270 度，3、4 是 180 度；读不懂就当 0。 */
export function exifRotation(data) {
  if (data.length < 14 || data.toString("latin1", 0, 6) !== "Exif\0\0") return 0;
  const tiff = data.subarray(6);
  const order = tiff.toString("latin1", 0, 2);
  if (order !== "II" && order !== "MM") return 0;
  const little = order === "II";
  const u16 = (offset) => (little ? tiff.readUInt16LE(offset) : tiff.readUInt16BE(offset));
  const ifd = little ? tiff.readUInt32LE(4) : tiff.readUInt32BE(4);
  if (ifd + 2 > tiff.length) return 0;
  for (let index = 0, count = u16(ifd); index < count; index += 1) {
    const entry = ifd + 2 + index * 12;
    if (entry + 12 > tiff.length) break;
    if (u16(entry) !== 0x0112) continue;
    const orientation = u16(entry + 8);
    return orientation === 5 || orientation === 6 ? 90 : orientation === 7 || orientation === 8 ? 270 : orientation === 3 || orientation === 4 ? 180 : 0;
  }
  return 0;
}

/**
 * 从 start 起找下一个「0xFF + 非 0xFF」标记，返回那个 0xFF 的位置（找不到就是文件末尾）。
 * 在整块窗口里找，不逐字节 await：几十 MB 的垃圾或填充也只是几次读盘。
 */
async function nextJpegMarker(src, start) {
  let pos = start;
  while (pos + 1 < src.size) {
    const window = await src.read(pos, READ_WINDOW);
    let index = window.indexOf(0xff);
    while (index >= 0 && index + 1 < window.length && window[index + 1] === 0xff) index += 1;
    if (index < 0) pos += window.length;
    // 窗口最后一个字节是 0xFF：从它重读，看后面跟的是什么。
    else if (index + 1 >= window.length) pos += Math.max(1, index);
    else return pos + index;
  }
  return src.size;
}

async function parseWebp(src) {
  const head = await src.read(12, 8 + 10);
  if (head.length < 13) throw malformed("WebP too small");
  const chunk = head.toString("latin1", 0, 4);
  const body = head.subarray(8);
  if (chunk === "VP8 ") {
    if (body[3] !== 0x9d || body[4] !== 0x01 || body[5] !== 0x2a) throw malformed("Bad VP8 header");
    return image(body.readUInt16LE(6) & 0x3fff, body.readUInt16LE(8) & 0x3fff);
  }
  if (chunk === "VP8L") {
    if (body[0] !== 0x2f) throw malformed("Bad VP8L header");
    const bits = body.readUInt32LE(1);
    return image((bits & 0x3fff) + 1, ((bits >>> 14) & 0x3fff) + 1);
  }
  if (chunk === "VP8X") {
    return image(body.readUIntLE(4, 3) + 1, body.readUIntLE(7, 3) + 1);
  }
  throw malformed("Unknown WebP chunk");
}

function parseBmp(head) {
  const headerSize = head.readUInt32LE(14);
  if (head.readUInt32LE(6) !== 0) return null;
  if (headerSize === 12) return image(head.readUInt16LE(18), head.readUInt16LE(20));
  if (headerSize < 40 || headerSize > 124) return null;
  return image(Math.abs(head.readInt32LE(18)), Math.abs(head.readInt32LE(22)));
}

// ---------------------------------------------------------------------------
// ISO BMFF（mp4 / mov / m4v / m4a），对照 FFmpeg mov.c

function* boxes(buffer, start, end) {
  let pos = start;
  while (pos + 8 <= end) {
    let size = buffer.readUInt32BE(pos);
    const type = buffer.toString("latin1", pos + 4, pos + 8);
    let header = 8;
    if (size === 1) {
      if (pos + 16 > end) return;
      size = Number(buffer.readBigUInt64BE(pos + 8));
      header = 16;
    } else if (size === 0) {
      size = end - pos;
    }
    if (size < header) return;
    const boxEnd = Math.min(end, pos + size);
    yield { type, start: pos + header, end: boxEnd };
    pos = boxEnd;
  }
}

function child(buffer, box, type) {
  for (const item of boxes(buffer, box.start, box.end)) if (item.type === type) return item;
  return null;
}

async function readTopBox(src, pos) {
  // 分片 mp4 可能有上万个 moof/mdat，每个盒子头只顺带读 8KB（通常正好盖住紧跟的 moof）。
  const head = await src.read(pos, 16, 8 * 1024);
  if (head.length < 8) return null;
  let size = head.readUInt32BE(0);
  const type = head.toString("latin1", 4, 8);
  let header = 8;
  if (size === 1) {
    if (head.length < 16) return null;
    size = Number(head.readBigUInt64BE(8));
    header = 16;
  } else if (size === 0) {
    size = src.size - pos;
  }
  if (size < header) return null;
  return { type, start: pos + header, end: Math.min(src.size, pos + size), declaredEnd: pos + size };
}

async function loadBox(src, box) {
  const length = box.end - box.start;
  if (length > MAX_METADATA_BYTES) throw malformed("Metadata box too large");
  // 把整块内容放进新 Buffer，盒子内偏移从 0 开始。
  return Buffer.from(await src.exact(box.start, length));
}

function readUint(buffer, pos, version) {
  return version === 1 ? buffer.readBigUInt64BE(pos) : BigInt(buffer.readUInt32BE(pos));
}

async function parseIsoBmff(src) {
  const movie = {
    timescale: 0, tracks: [], byId: new Map(), trex: new Map(), advancedEditlist: true, matrix: IDENTITY_MATRIX,
  };
  let pos = 0;
  let sawMoov = false;
  let sawMdat = false;
  let fragmentIndexComplete = false;
  while (pos < src.size) {
    const box = await readTopBox(src, pos);
    if (!box) break;
    if (box.type === "moov" && !sawMoov) {
      // moov 被截断的文件剪映也打不开；ffprobe 有时能读出半截轨道，这里统一当坏文件。
      if (box.declaredEnd > src.size) throw malformed("Truncated moov");
      parseMoov(await loadBox(src, box), movie);
      sawMoov = true;
    } else if (box.type === "moof" && sawMoov) {
      // 下载到一半的分片文件：最后一个 moof 被截断就当到此为止，不把整份判成坏文件。
      if (box.declaredEnd > src.size) break;
      parseMoof(await loadBox(src, box), movie, src.size);
    } else if (box.type === "sidx" && sawMoov) {
      if (await parseSidx(src, box, movie)) fragmentIndexComplete = true;
    } else if (box.type === "mdat") {
      sawMdat = true;
    }
    // mov_read_default：有了 moov 和 mdat、sidx 又说明分片索引完整时，就不再往后读。
    if (sawMoov && sawMdat && fragmentIndexComplete) break;
    if (box.declaredEnd <= pos) break;
    pos = box.declaredEnd;
  }
  if (!sawMoov) throw malformed("moov atom not found");
  return { streams: movie.tracks.map(finishTrack), durationUs: null };
}

function parseMoov(buffer, movie) {
  const root = { start: 0, end: buffer.length };
  for (const box of boxes(buffer, root.start, root.end)) {
    if (box.type === "mvhd") {
      const version = buffer[box.start];
      const timescale = buffer.readUInt32BE(box.start + (version === 1 ? 20 : 12));
      movie.timescale = timescale > 0 && timescale <= 0x7fffffff ? timescale : 1;
      const matrix = box.start + (version === 1 ? 48 : 36);
      if (matrix + 36 <= box.end) movie.matrix = readMatrix(buffer, matrix);
    } else if (box.type === "trak") {
      const track = parseTrak(buffer, box, movie);
      movie.tracks.push(track);
      if (!movie.byId.has(track.id)) movie.byId.set(track.id, track);
    } else if (box.type === "mvex") {
      for (const item of boxes(buffer, box.start, box.end)) {
        if (item.type !== "trex" || item.end - item.start < 24) continue;
        movie.trex.set(buffer.readUInt32BE(item.start + 4), {
          duration: buffer.readUInt32BE(item.start + 12),
          size: buffer.readUInt32BE(item.start + 16),
        });
      }
    }
  }
}

function parseTrak(buffer, trak, movie) {
  const track = {
    id: 0,
    type: "data",
    width: 0,
    height: 0,
    tkhdWidth: 0,
    tkhdHeight: 0,
    rotation: 0,
    timescale: 0,
    duration: 0n,
    elst: null,
    sttsCount: 0,
    sttsDuration: 0n,
    sampleCount: 0,
    indexEntries: 0,
    firstOffset: 0,
    dtsShift: 0,
    timeOffset: 0n,
    trackEnd: 0n,
    fragmentStart: null,
  };
  for (const box of boxes(buffer, trak.start, trak.end)) {
    if (box.type === "tkhd") {
      const version = buffer[box.start];
      track.id = buffer.readUInt32BE(box.start + (version === 1 ? 20 : 12));
      const end = box.start + (version === 1 ? 96 : 84);
      if (end <= box.end) {
        track.tkhdWidth = buffer.readUInt32BE(end - 8) >>> 16;
        track.tkhdHeight = buffer.readUInt32BE(end - 4) >>> 16;
        track.rotation = matrixRotation(buffer, end - 44, movie.matrix);
      }
    } else if (box.type === "edts") {
      const elst = child(buffer, box, "elst");
      if (elst) track.elst = parseElst(buffer, elst);
    } else if (box.type === "mdia") {
      parseMdia(buffer, box, track);
    }
  }
  if (track.timescale <= 0) track.timescale = 1;

  // mov_build_index：编辑列表决定分片轨道的 time_offset。
  if (track.elst?.length) {
    let emptyDuration = 0n;
    let startTime = 0n;
    let editStart = 0;
    track.elst.forEach((entry, index) => {
      if (index === 0 && entry.time === -1n) {
        emptyDuration = entry.duration;
        editStart = 1;
      } else if (index === editStart && entry.time >= 0n) {
        startTime = entry.time;
      }
    });
    if ((emptyDuration || startTime) && movie.timescale > 0) {
      if (emptyDuration) emptyDuration = rescale(emptyDuration, track.timescale, movie.timescale);
      track.timeOffset = startTime - emptyDuration;
    }
  }
  // mov_read_trak：一条没有 stts 的轨道（分片 mp4）会把高级编辑列表整体关掉。
  if (!track.sttsCount && movie.advancedEditlist) movie.advancedEditlist = false;

  track.start = null;
  if (movie.advancedEditlist && track.elst?.length && track.sampleCount > 0) {
    // mov_fix_index：起点 = 开头空编辑的总长，时长不超过全部编辑段之和。
    let sum = 0n;
    let empty = 0n;
    let foundNonEmpty = false;
    if (movie.timescale > 0) {
      for (const entry of track.elst) {
        let duration = rescale(entry.duration, track.timescale, movie.timescale);
        if (duration < 0n) duration = 0n;
        sum += duration;
        if (entry.time === -1n) {
          if (!foundNonEmpty) empty += duration;
          continue;
        }
        foundNonEmpty = true;
      }
    }
    track.start = empty;
    if (sum < track.duration) track.duration = sum;
  } else if (track.sampleCount > 0) {
    // 没走编辑列表：起点是第一个样本的显示时间（视频在建索引时定，其余流取第一个包）。
    // mov_build_index 建索引前先 current_dts -= dts_shift，起点再加回 dts_shift 和首个显示偏移。
    const firstDts = (movie.advancedEditlist ? 0n : -track.timeOffset) - BigInt(track.dtsShift);
    track.start = firstDts + BigInt(track.dtsShift) + BigInt(track.firstOffset);
  }
  track.trackEnd = track.sttsDuration;
  track.indexEntries = track.sampleCount;
  return track;
}

const IDENTITY_MATRIX = [65536n, 0n, 0n, 0n, 65536n, 0n, 0n, 0n, 1n << 30n];

/** 显示矩阵 {a b u; c d v; x y w}：前两列 16.16 定点，最后一列 2.30。 */
function readMatrix(buffer, pos) {
  return Array.from({ length: 9 }, (_, index) => BigInt(buffer.readInt32BE(pos + index * 4)));
}

/**
 * mov_read_tkhd：轨道矩阵右乘 mvhd 的矩阵，得到 ffprobe 报的显示矩阵；
 * 只认转 90 度整数倍（允许等比缩放；ffprobe 的 rotation = -atan2(b, a)，这里换成 0 到 359），
 * 镜像、任意角度、带透视的都当 0。
 */
function matrixRotation(buffer, pos, movieMatrix) {
  const track = readMatrix(buffer, pos);
  const shifts = [16n, 16n, 30n];
  const at = (row, column) => {
    let sum = 0n;
    for (let index = 0; index < 3; index += 1) sum += (track[row * 3 + index] * movieMatrix[index * 3 + column]) >> shifts[index];
    return BigInt.asIntN(32, sum);
  };
  const [a, b, u, c, d, v] = [at(0, 0), at(0, 1), at(0, 2), at(1, 0), at(1, 1), at(1, 2)];
  if (u !== 0n || v !== 0n) return 0;
  if (a === 0n && d === 0n && b !== 0n && b === -c) return b < 0n ? 90 : 270;
  if (b === 0n && c === 0n && a < 0n && a === d) return 180;
  return 0;
}

function parseElst(buffer, box) {
  const version = buffer[box.start];
  let count = buffer.readUInt32BE(box.start + 4);
  const entrySize = version === 1 ? 20 : 12;
  const available = box.end - box.start - 8;
  if (available !== count * entrySize) count = Math.floor(available / entrySize);
  const entries = [];
  let pos = box.start + 8;
  for (let index = 0; index < count; index += 1) {
    const duration = version === 1 ? buffer.readBigInt64BE(pos) : BigInt(buffer.readUInt32BE(pos));
    const time = version === 1 ? buffer.readBigInt64BE(pos + 8) : BigInt(buffer.readInt32BE(pos + 4));
    if (duration < 0n) throw malformed("Invalid edit list duration");
    entries.push({ duration, time });
    pos += entrySize;
  }
  return entries;
}

function parseMdia(buffer, mdia, track) {
  for (const box of boxes(buffer, mdia.start, mdia.end)) {
    if (box.type === "mdhd") {
      const version = buffer[box.start];
      const base = box.start + (version === 1 ? 20 : 12);
      track.timescale = buffer.readInt32BE(base);
      track.duration = readUint(buffer, base + 4, version);
      if (track.duration === (version === 1 ? 0xffffffffffffffffn : 0xffffffffn)) track.duration = 0n;
    } else if (box.type === "hdlr") {
      const handler = buffer.toString("latin1", box.start + 8, box.start + 12);
      if (handler === "vide") track.type = "video";
      else if (handler === "soun" || handler === "m1a ") track.type = "audio";
      else if (handler === "subp" || handler === "clcp") track.type = "subtitle";
    } else if (box.type === "minf") {
      const stbl = child(buffer, box, "stbl");
      if (stbl) parseStbl(buffer, stbl, track);
    }
  }
}

function parseStbl(buffer, stbl, track) {
  for (const box of boxes(buffer, stbl.start, stbl.end)) {
    if (box.type === "stsd") {
      const entry = box.start + 8;
      if (track.type === "video" && entry + 36 <= box.end) {
        track.width = buffer.readUInt16BE(entry + 32);
        track.height = buffer.readUInt16BE(entry + 34);
      }
    } else if (box.type === "stts") {
      parseStts(buffer, box, track);
    } else if (box.type === "ctts") {
      const entries = buffer.readUInt32BE(box.start + 4);
      let first = true;
      for (let index = 0; index < entries; index += 1) {
        const pos = box.start + 8 + index * 8;
        if (pos + 8 > box.end) break;
        const count = buffer.readInt32BE(pos);
        const offset = buffer.readInt32BE(pos + 4);
        if (count <= 0) continue;
        if (first) {
          track.firstOffset = offset;
          first = false;
        }
        if (index + 2 < entries && offset < 0) track.dtsShift = Math.max(track.dtsShift, dtsShiftFor(offset));
      }
    } else if (box.type === "stsz") {
      track.sampleCount = buffer.readUInt32BE(box.start + 8);
    } else if (box.type === "stz2") {
      track.sampleCount = buffer.readUInt32BE(box.start + 8);
    }
  }
}

// mov_read_stts：含 FFmpeg 对「超大增量当负数修正」的处理，时长取 min(mdhd, stts 总和)。
const MAX_STTS_DELTA = 0xffffffff - 48000 * 10;

function parseStts(buffer, box, track) {
  const entries = buffer.readUInt32BE(box.start + 4);
  let duration = 0n;
  let currentDts = 0n;
  let correctedDts = 0n;
  let index = 0;
  for (; index < entries; index += 1) {
    const pos = box.start + 8 + index * 8;
    if (pos + 8 > box.end) break;
    const count = BigInt(buffer.readUInt32BE(pos));
    const delta = buffer.readUInt32BE(pos + 4);
    let sampleDuration = BigInt(delta);
    if (delta > MAX_STTS_DELTA) {
      const magnitude = buffer.readInt32BE(pos + 4);
      sampleDuration = 1n;
      correctedDts += (magnitude < 0 ? BigInt(magnitude) : 1n) * count;
    } else {
      correctedDts += sampleDuration * count;
    }
    currentDts += sampleDuration * count;
    if (currentDts > correctedDts) {
      const drift = (currentDts - correctedDts) / (count > 1n ? count : 1n);
      const correction = sampleDuration > drift ? drift : sampleDuration - 1n;
      currentDts -= correction * count;
      sampleDuration -= correction;
    }
    duration += sampleDuration * count;
  }
  track.sttsCount = index;
  track.sttsDuration = duration;
  if (duration && duration < track.duration) track.duration = duration;
}

// mov_read_trun 的上限：索引表 UINT_MAX / sizeof(AVIndexEntry) 条（超出只截断），
// 时间表 UINT_MAX / sizeof(MOVTimeToSample) 条（超出算坏文件）；解码时间是 int64。
const MAX_INDEX_ENTRIES = Math.floor(0xffffffff / 24);
const MAX_TTS_ENTRIES = Math.floor(0xffffffff / 12);
const INT64_MAX = (1n << 63n) - 1n;
const TRUN_ENTRY_FIELDS = [0x100, 0x200, 0x400, 0x800];

function parseMoof(buffer, movie, fileSize) {
  // 同 mov_read_moof / mov_read_tfhd：每个 moof 都要先有一个对得上 trex 的 tfhd，trun 才算数。
  let foundTfhd = false;
  for (const traf of boxes(buffer, 0, buffer.length)) {
    if (traf.type !== "traf") continue;
    let track = null;
    let defaultDuration = 0;
    let defaultSize = 0;
    let tfdt = null;
    let nextTrunDts = null;
    for (const box of boxes(buffer, traf.start, traf.end)) {
      if (box.type === "tfhd") {
        const flags = buffer.readUIntBE(box.start + 1, 3);
        const trackId = buffer.readUInt32BE(box.start + 4);
        if (!trackId) throw malformed("tfhd without track id");
        const trex = movie.trex.get(trackId);
        if (!trex) continue;
        foundTfhd = true;
        track = movie.byId.get(trackId) ?? null;
        nextTrunDts = null;
        let pos = box.start + 8;
        if (flags & 0x01) pos += 8;
        if (flags & 0x02) pos += 4;
        defaultDuration = trex.duration;
        if (flags & 0x08) {
          defaultDuration = buffer.readUInt32BE(pos);
          pos += 4;
        }
        defaultSize = flags & 0x10 ? buffer.readUInt32BE(pos) : trex.size;
      } else if (box.type === "tfdt" && track) {
        tfdt = readUint(buffer, box.start + 4, buffer[box.start]);
        track.trackEnd = tfdt;
      } else if (box.type === "trun") {
        if (!foundTfhd) throw malformed("trun without tfhd");
        if (!track) continue;
        const added = parseTrun(buffer, box, track, {
          defaultDuration,
          defaultSize,
          fileSize,
          firstDts: (nextTrunDts !== null ? nextTrunDts : tfdt !== null ? tfdt : track.trackEnd) - track.timeOffset,
        });
        if (added) nextTrunDts = track.trackEnd;
      }
    }
  }
}

/**
 * mov_read_trun：样本数先按 FFmpeg 的界限校验（每条字段占的字节、文件能装下的样本数），
 * 超出就是坏文件；没有逐条字段时直接按默认时长相乘，不逐条循环。返回是否加进了样本。
 */
function parseTrun(buffer, box, track, { defaultDuration, defaultSize, fileSize, firstDts }) {
  const flags = buffer.readUIntBE(box.start + 1, 3);
  let entries = buffer.readUInt32BE(box.start + 4);
  // 默认样本大小是 0 又不逐条给大小：FFmpeg 直接拒绝。
  if (entries && !defaultSize && !(flags & 0x200)) throw malformed("trun without sample sizes");
  if (entries + track.indexEntries >= MAX_TTS_ENTRIES) throw malformed("Too many trun entries");
  let pos = box.start + 8;
  if (flags & 0x001) pos += 4;
  if (flags & 0x004) pos += 4;
  const entrySize = 4 * TRUN_ENTRY_FIELDS.filter((bit) => flags & bit).length;
  let maxEntries = fileSize - track.indexEntries;
  if (entrySize) maxEntries = Math.min(maxEntries, Math.trunc((box.end - pos) / entrySize));
  if (entries > maxEntries) throw malformed("trun sample count exceeds the input");

  let dts = firstDts;
  if (track.indexEntries + entries >= MAX_INDEX_ENTRIES) entries = MAX_INDEX_ENTRIES - track.indexEntries;
  if (!entries) return false;
  track.indexEntries += entries;
  if (!entrySize) {
    const sampleDuration = defaultDuration > MAX_STTS_DELTA ? 1 : defaultDuration;
    if (track.fragmentStart === null) track.fragmentStart = dts;
    dts += BigInt(entries) * BigInt(sampleDuration);
  } else {
    // 逐条累加先用 Number（每条不到 2^32，攒到 2^52 再并进 BigInt 都是精确的），省掉逐条的 BigInt 运算。
    let pending = 0;
    for (let index = 0; index < entries; index += 1) {
      let sampleDuration = defaultDuration;
      if (flags & 0x100) {
        sampleDuration = buffer.readUInt32BE(pos);
        pos += 4;
      }
      if (flags & 0x200) pos += 4;
      if (flags & 0x400) pos += 4;
      let compositionOffset = 0;
      if (flags & 0x800) {
        compositionOffset = buffer.readInt32BE(pos);
        pos += 4;
      }
      if (sampleDuration > MAX_STTS_DELTA) sampleDuration = 1;
      if (compositionOffset < 0) track.dtsShift = Math.max(track.dtsShift, dtsShiftFor(compositionOffset));
      if (index === 0 && track.fragmentStart === null) track.fragmentStart = dts + BigInt(compositionOffset);
      pending += sampleDuration;
      if (pending >= 2 ** 52) {
        dts += BigInt(pending);
        pending = 0;
      }
    }
    dts += BigInt(pending);
  }
  // 样本时长都不是负数，最后一个不溢出前面就都不溢出。
  if (dts > INT64_MAX) throw malformed("trun timestamps overflow");
  track.trackEnd = dts + track.timeOffset;
  if (track.duration < track.trackEnd) track.duration = track.trackEnd;
  return true;
}

/** 顶层 sidx：给对应轨道定总时长；返回分片索引是否覆盖到文件末尾。 */
async function parseSidx(src, box, movie) {
  const buffer = await loadBox(src, box);
  const version = buffer[0];
  if (version > 1) return false;
  const track = movie.byId.get(buffer.readUInt32BE(4));
  if (!track) return false;
  if (buffer.readInt32BE(8) <= 0) throw malformed("Invalid sidx timescale");
  let pts = readUint(buffer, 12, version);
  const firstOffset = readUint(buffer, version === 1 ? 20 : 16, version);
  let pos = version === 1 ? 28 : 20;
  const count = buffer.readUInt16BE(pos + 2);
  pos += 4;
  if (!count) throw malformed("Empty sidx");
  let offset = BigInt(box.declaredEnd) + firstOffset;
  for (let index = 0; index < count; index += 1) {
    const size = buffer.readUInt32BE(pos);
    // 层级 sidx（引用另一个 sidx）FFmpeg 也不支持；这里只是忽略，不当成坏文件。
    if (size & 0x80000000) return false;
    offset += BigInt(size);
    pts += BigInt(buffer.readUInt32BE(pos + 4));
    pos += 12;
  }
  track.duration = pts;
  track.trackEnd = pts;
  const fileSize = BigInt(src.size);
  if (offset === fileSize) return true;
  const tail = await src.read(src.size - 4, 4);
  return tail.length === 4 && offset === fileSize - BigInt(tail.readUInt32BE(0));
}

function finishTrack(track) {
  let start = track.start;
  if (start === null && track.fragmentStart !== null) start = track.fragmentStart + BigInt(track.dtsShift);
  return {
    type: track.type,
    width: track.type === "video" ? track.width || track.tkhdWidth : null,
    height: track.type === "video" ? track.height || track.tkhdHeight : null,
    rotation: track.type === "video" ? track.rotation : 0,
    timeBase: [1, track.timescale],
    start,
    duration: track.duration,
  };
}

// ---------------------------------------------------------------------------
// Matroska / WebM（EBML），对照 FFmpeg matroskadec.c

const EBML = {
  segment: 0x18538067,
  seekHead: 0x114d9b74,
  seek: 0x4dbb,
  seekId: 0x53ab,
  seekPosition: 0x53ac,
  info: 0x1549a966,
  timecodeScale: 0x2ad7b1,
  duration: 0x4489,
  tracks: 0x1654ae6b,
  trackEntry: 0xae,
  trackType: 0x83,
  codecId: 0x86,
  video: 0xe0,
  pixelWidth: 0xb0,
  pixelHeight: 0xba,
  cluster: 0x1f43b675,
};

function readVint(buffer, pos, keepMarker) {
  const first = buffer[pos];
  if (first === undefined || first === 0) return null;
  const length = Math.clz32(first) - 23;
  if (pos + length > buffer.length) return null;
  let value = BigInt(keepMarker ? first : first & (0xff >> length));
  let allOnes = (first & (0xff >> length)) === 0xff >> length;
  for (let index = 1; index < length; index += 1) {
    value = (value << 8n) | BigInt(buffer[pos + index]);
    if (buffer[pos + index] !== 0xff) allOnes = false;
  }
  return { value, length, unknown: !keepMarker && allOnes };
}

function readElementHeader(buffer, pos) {
  const id = readVint(buffer, pos, true);
  if (!id || id.length > 4) return null;
  const size = readVint(buffer, pos + id.length, false);
  if (!size) return null;
  const dataStart = pos + id.length + size.length;
  return { id: Number(id.value), dataStart, size: size.unknown ? null : Number(size.value) };
}

function* elements(buffer, start, end) {
  let pos = start;
  while (pos < end) {
    const header = readElementHeader(buffer, pos);
    if (!header) return;
    const elementEnd = header.size === null ? end : Math.min(end, header.dataStart + header.size);
    yield { id: header.id, start: header.dataStart, end: elementEnd };
    pos = elementEnd;
  }
}

function ebmlUint(buffer, element) {
  let value = 0;
  for (let pos = element.start; pos < element.end; pos += 1) value = value * 256 + buffer[pos];
  return value;
}

function ebmlFloat(buffer, element) {
  const length = element.end - element.start;
  if (length === 4) return buffer.readFloatBE(element.start);
  if (length === 8) return buffer.readDoubleBE(element.start);
  if (length === 0) return 0;
  throw malformed("Bad EBML float");
}

async function readTopElement(src, pos) {
  const head = await src.read(pos, 12);
  const header = readElementHeader(head, 0);
  if (!header) return null;
  return { id: header.id, start: pos + header.dataStart, size: header.size };
}

async function parseMatroska(src) {
  const ebml = await readTopElement(src, 0);
  if (!ebml || ebml.size === null) throw malformed("Bad EBML header");
  let pos = ebml.start + ebml.size;
  let segment = null;
  while (pos < src.size) {
    const element = await readTopElement(src, pos);
    if (!element) break;
    if (element.id === EBML.segment) {
      segment = element;
      break;
    }
    if (element.size === null) break;
    pos = element.start + element.size;
  }
  if (!segment) throw malformed("Matroska segment not found");
  const segmentEnd = segment.size === null ? src.size : Math.min(src.size, segment.start + segment.size);
  const state = { timecodeScale: 1_000_000, duration: 0, tracks: null, info: false };
  const seeks = new Map();

  const handle = async (element) => {
    if (element.size === null || ![EBML.info, EBML.tracks, EBML.seekHead].includes(element.id)) return;
    const length = Math.min(element.size, segmentEnd - element.start);
    if (length > MAX_METADATA_BYTES) throw malformed("Matroska element too large");
    const buffer = Buffer.from(await src.exact(element.start, length));
    const root = { start: 0, end: buffer.length };
    if (element.id === EBML.info && !state.info) {
      state.info = true;
      for (const item of elements(buffer, root.start, root.end)) {
        if (item.id === EBML.timecodeScale) state.timecodeScale = ebmlUint(buffer, item);
        else if (item.id === EBML.duration) state.duration = ebmlFloat(buffer, item);
      }
    } else if (element.id === EBML.tracks && !state.tracks) {
      state.tracks = [];
      for (const entry of elements(buffer, root.start, root.end)) {
        if (entry.id !== EBML.trackEntry) continue;
        const track = { kind: 0, codec: "", width: 0, height: 0 };
        for (const item of elements(buffer, entry.start, entry.end)) {
          if (item.id === EBML.trackType) track.kind = ebmlUint(buffer, item);
          else if (item.id === EBML.codecId) track.codec = buffer.toString("latin1", item.start, item.end);
          else if (item.id === EBML.video) {
            for (const video of elements(buffer, item.start, item.end)) {
              if (video.id === EBML.pixelWidth) track.width = ebmlUint(buffer, video);
              else if (video.id === EBML.pixelHeight) track.height = ebmlUint(buffer, video);
            }
          }
        }
        state.tracks.push(track);
      }
    } else if (element.id === EBML.seekHead) {
      for (const seek of elements(buffer, root.start, root.end)) {
        if (seek.id !== EBML.seek) continue;
        let id = null;
        let position = null;
        for (const item of elements(buffer, seek.start, seek.end)) {
          if (item.id === EBML.seekId) id = ebmlUint(buffer, item);
          else if (item.id === EBML.seekPosition) position = ebmlUint(buffer, item);
        }
        if (id !== null && position !== null && !seeks.has(id)) seeks.set(id, segment.start + position);
      }
    }
  };

  pos = segment.start;
  while (pos < segmentEnd) {
    const element = await readTopElement(src, pos);
    if (!element || element.id === EBML.cluster) break;
    await handle(element);
    if (element.size === null) break;
    pos = element.start + element.size;
  }
  // Info / Tracks 写在 Cluster 之后的文件：按 SeekHead 跳过去读（同 matroska_execute_seekhead）。
  for (const id of [EBML.info, EBML.tracks]) {
    const done = id === EBML.info ? state.info : state.tracks;
    const target = seeks.get(id);
    if (done || target === undefined || target >= segmentEnd) continue;
    const element = await readTopElement(src, target);
    if (element?.id === id) await handle(element);
  }
  if (!state.tracks) throw malformed("Matroska tracks not found");

  const timecodeScale = state.timecodeScale || 1_000_000;
  const duration = Number.isNaN(state.duration) ? 0 : state.duration;
  let durationUs = null;
  if (duration && Number.isFinite(duration)) {
    durationUs = BigInt(Math.trunc((duration * timecodeScale * 1000) / 1_000_000));
  }
  const timeBase = [timecodeScale, 1_000_000_000];
  const streams = [];
  for (const track of state.tracks) {
    // 只有这四类、且 CodecID 首字母对得上的轨道会变成流（matroska_parse_tracks）；封面附件不算画面。
    const letter = track.codec[0];
    const textual = letter === "S" || letter === "D";
    let type = null;
    if (track.kind === 1 && letter === "V") type = "video";
    else if (track.kind === 2 && letter === "A") type = "audio";
    else if (track.kind === 0x11 && textual) type = "subtitle";
    else if (track.kind === 0x21 && textual) type = "data";
    if (!type) continue;
    const video = type === "video";
    streams.push({
      type,
      width: video ? track.width : null,
      height: video ? track.height : null,
      timeBase,
      start: 0n,
      duration: null,
    });
  }
  return { streams, durationUs };
}

// ---------------------------------------------------------------------------
// AVI，对照 FFmpeg avidec.c

async function parseAvi(src) {
  const riffEnd = (await src.exact(4, 4)).readUInt32LE(0) + 8;
  const streams = [];
  let framePeriod = 0;
  let current = null;

  const walk = async (start, end) => {
    let pos = start;
    while (pos + 8 <= end) {
      const head = await src.exact(pos, 12 <= end - pos ? 12 : 8);
      const id = head.toString("latin1", 0, 4);
      const size = head.readUInt32LE(4);
      const body = pos + 8;
      if (id === "LIST") {
        const listType = head.length >= 12 ? head.toString("latin1", 8, 12) : "";
        if (listType === "movi") return false;
        if (listType === "hdrl" || listType === "strl") {
          if (!(await walk(body + 4, Math.min(end, body + size)))) return false;
        }
      } else if (id === "idx1") {
        return false;
      } else if (id === "avih") {
        framePeriod = (await src.exact(body, 4)).readUInt32LE(0);
      } else if (id === "strh") {
        const data = await src.exact(body, Math.min(size, 48));
        const fccType = data.toString("latin1", 0, 4);
        let scale = data.readUInt32LE(20);
        let rate = data.readUInt32LE(24);
        if (!(scale && rate)) {
          if (framePeriod) {
            rate = 1_000_000;
            scale = framePeriod;
          } else {
            rate = 25;
            scale = 1;
          }
        }
        const length = data.readUInt32LE(32);
        let sampleSize = data.length >= 48 ? data.readInt32LE(44) : 0;
        let type = "data";
        if (fccType === "vids" || fccType === "iavs") type = "video";
        else if (fccType === "auds") type = "audio";
        else if (fccType === "txts") type = "subtitle";
        if (type === "video" || sampleSize < 0) sampleSize = 0;
        let duration = null;
        if (sampleSize === 0) {
          duration = BigInt(length);
          // 文件被截断时按实际大小折算（同 avidec 的 "File is truncated adjusting duration"）。
          if (duration > 0n && src.size > 0 && riffEnd > src.size) duration = rescale(duration, src.size, riffEnd);
        }
        current = { type, width: null, height: null, timeBase: [scale, rate], start: 0n, duration };
        streams.push(current);
      } else if (id === "strf" && current?.type === "video" && size >= 12) {
        const data = await src.exact(body, 12);
        current.width = Math.abs(data.readInt32LE(4));
        current.height = Math.abs(data.readInt32LE(8));
      }
      pos = body + size + (size & 1);
    }
    return true;
  };

  await walk(12, Math.min(src.size, riffEnd));
  if (!streams.length) throw malformed("AVI without streams");
  return { streams, durationUs: null };
}

// ---------------------------------------------------------------------------
// WAV（RIFF / RF64 / BW64），对照 FFmpeg wavdec.c

const PCM_TAGS = new Set([0x0001, 0x0003, 0x0006, 0x0007]);

async function parseWav(src) {
  const head = await src.exact(0, 12);
  const big64 = head.toString("latin1", 0, 4) !== "RIFF";
  let pos = 12;
  let fmt = null;
  let dataSize = 0n;
  let dataOffset = null;
  let dataEnd = null;
  let sampleCount = 0n;
  let ds64Data = 0n;
  while (pos + 8 <= src.size) {
    const chunk = await src.exact(pos, 8);
    const id = chunk.toString("latin1", 0, 4);
    const size = chunk.readUInt32LE(4);
    const body = pos + 8;
    let next = body + size + (size & 1);
    if (id === "ds64" && big64) {
      const data = await src.exact(body, 24);
      ds64Data = data.readBigUInt64LE(8);
      sampleCount = data.readBigUInt64LE(16);
    } else if (id === "fmt ") {
      const data = await src.exact(body, Math.min(size, 40));
      let tag = data.readUInt16LE(0);
      if (tag === 0xfffe && data.length >= 26) tag = data.readUInt16LE(24);
      fmt = {
        tag,
        channels: data.readUInt16LE(2),
        sampleRate: data.readUInt32LE(4),
        byteRate: data.readUInt32LE(8),
        bits: data.length >= 16 ? data.readUInt16LE(14) : 0,
      };
    } else if (id === "fact" && !sampleCount) {
      sampleCount = BigInt((await src.exact(body, 4)).readUInt32LE(0));
    } else if (id === "data") {
      dataOffset = body;
      if (big64) {
        dataSize = ds64Data;
        dataEnd = BigInt(body) + ds64Data;
        next = Number(dataEnd + (ds64Data & 1n));
      } else if (size > 0 && size !== 0xffffffff) {
        dataSize = BigInt(size);
        dataEnd = BigInt(body + size);
      } else {
        dataSize = 0n;
        dataEnd = null;
        break;
      }
    }
    if (next <= pos) break;
    pos = next;
  }
  if (!fmt || dataOffset === null || !fmt.sampleRate) throw malformed("WAV without fmt/data");

  const pcm = PCM_TAGS.has(fmt.tag);
  // 只有 PCM 类能直接按数据大小折算采样数（av_get_exact_bits_per_sample），其余靠 fact 或码率。
  const bits = pcm ? (fmt.tag === 0x0006 || fmt.tag === 0x0007 ? 8 : Math.ceil(fmt.bits / 8) * 8) : 0;
  const channels = BigInt(fmt.channels);
  const bitRate = BigInt(fmt.byteRate) * 8n;
  if (bitRate > 0n && dataSize > 0n && sampleCount > 0n && channels > 1n && sampleCount % channels === 0n) {
    const ratio = (8 * Number(dataSize) * fmt.channels * fmt.sampleRate) / Number(sampleCount) / Number(bitRate);
    if (Math.abs(ratio - 1) < 0.3) sampleCount /= channels;
  }
  if (dataSize > 0n && sampleCount && channels && (dataSize * 8n) / sampleCount / channels > BigInt(fmt.bits + 1)) {
    sampleCount = 0n;
  }
  if (pcm && channels && dataSize && bits && dataEnd !== null && dataEnd <= BigInt(src.size)) {
    sampleCount = (dataSize * 8n) / (channels * BigInt(bits));
  }
  let duration = sampleCount || null;
  if (duration === null && bitRate > 0n && src.size > dataOffset) {
    // 不知道采样数时按码率估（estimate_timings_from_bit_rate）。
    duration = rescale(src.size - dataOffset, 8n * BigInt(fmt.sampleRate), bitRate);
  }
  return audioOnly([1, fmt.sampleRate], null, duration);
}

// ---------------------------------------------------------------------------
// FLAC，对照 FFmpeg flacdec.c：STREAMINFO 里的总采样数。

async function parseFlac(src, offset) {
  const block = await src.exact(offset + 4, 4 + 18);
  if ((block[0] & 0x7f) !== 0) throw malformed("FLAC without STREAMINFO");
  const info = block.subarray(4);
  const sampleRate = (info[10] << 12) | (info[11] << 4) | (info[12] >> 4);
  const total = (BigInt(info[13] & 0x0f) << 32n) | BigInt(info.readUInt32BE(14));
  if (!sampleRate) throw malformed("FLAC without sample rate");
  return audioOnly([1, sampleRate], 0n, total > 0n ? total : null);
}

// ---------------------------------------------------------------------------
// Ogg（Opus / Vorbis / FLAC / Speex），对照 FFmpeg oggdec.c 的 ogg_get_length：
// 时长 = 文件末尾最后一页的 granule（FFmpeg 在这一步还不知道起点，所以不减 pre-skip）。

const OGG_MAX_PAGE = 65307;
const OGG_CRC = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index << 24;
    for (let bit = 0; bit < 8; bit += 1) value = value & 0x80000000 ? (value << 1) ^ 0x04c11db7 : value << 1;
    table[index] = value >>> 0;
  }
  return table;
})();

function oggPage(buffer, pos) {
  if (pos + 27 > buffer.length || buffer.toString("latin1", pos, pos + 4) !== "OggS" || buffer[pos + 4] !== 0) return null;
  const segments = buffer[pos + 26];
  if (pos + 27 + segments > buffer.length) return null;
  let length = 0;
  for (let index = 0; index < segments; index += 1) length += buffer[pos + 27 + index];
  const end = pos + 27 + segments + length;
  if (end > buffer.length) return null;
  let crc = 0;
  for (let index = pos; index < end; index += 1) {
    const byte = index >= pos + 22 && index < pos + 26 ? 0 : buffer[index];
    crc = ((crc << 8) ^ OGG_CRC[((crc >>> 24) ^ byte) & 0xff]) >>> 0;
  }
  if (crc !== buffer.readUInt32LE(pos + 22)) return null;
  return {
    flags: buffer[pos + 5],
    granule: buffer.readBigInt64LE(pos + 6),
    serial: buffer.readUInt32LE(pos + 14),
    body: pos + 27 + segments,
    end,
  };
}

async function parseOgg(src) {
  const streams = new Map();
  let pos = 0;
  for (;;) {
    const head = await src.read(pos, 27 + 255);
    if (head.length < 27) break;
    const segments = head[26];
    let length = 0;
    for (let index = 0; index < segments && 27 + index < head.length; index += 1) length += head[27 + index];
    const page = oggPage(await src.read(pos, 27 + segments + length), 0);
    if (!page || !(page.flags & 0x02)) break;
    const body = await src.read(pos + page.body, Math.min(64, page.end - page.body));
    streams.set(page.serial, oggCodec(body));
    pos += page.end;
  }
  if (!streams.size) throw malformed("Ogg without streams");

  const tailStart = src.size > OGG_MAX_PAGE ? src.size - OGG_MAX_PAGE : 0;
  const tail = await src.read(tailStart, src.size - tailStart);
  const last = new Map();
  let scan = 0;
  while (scan + 27 <= tail.length) {
    const found = tail.indexOf("OggS", scan, "latin1");
    if (found < 0) break;
    const page = oggPage(tail, found);
    if (!page) {
      scan = found + 1;
      continue;
    }
    if (page.granule !== -1n && page.granule !== 0n) last.set(page.serial, page.granule);
    scan = page.end;
  }

  const list = [];
  for (const [serial, codec] of streams) {
    if (!codec) continue;
    const granule = last.get(serial);
    const duration = codec.type === "audio" && granule !== undefined ? granule : null;
    list.push({ ...codec, start: codec.type === "audio" ? 0n : null, duration });
  }
  if (!list.length) throw malformed("Ogg without known codecs");
  return { streams: list, durationUs: null };
}

function oggCodec(body) {
  const text = body.toString("latin1", 0, 8);
  if (text === "OpusHead") return { type: "audio", timeBase: [1, 48000] };
  if (text.startsWith("\x01vorbis") && body.length >= 16) {
    const rate = body.readUInt32LE(12);
    return rate ? { type: "audio", timeBase: [1, rate] } : null;
  }
  if (text.startsWith("\x7fFLAC") && body.length >= 30) {
    const info = body.subarray(17);
    const rate = (info[10] << 12) | (info[11] << 4) | (info[12] >> 4);
    return rate ? { type: "audio", timeBase: [1, rate] } : null;
  }
  if (text === "Speex   " && body.length >= 40) {
    const rate = body.readUInt32LE(36);
    return rate ? { type: "audio", timeBase: [1, rate] } : null;
  }
  if (text.startsWith("\x80theora") && body.length >= 20) {
    return { type: "video", width: body.readUIntBE(14, 3), height: body.readUIntBE(17, 3), timeBase: [1, 1] };
  }
  return null;
}

// ---------------------------------------------------------------------------
// AAC（ADTS）：逐帧数采样数。FFmpeg 对裸 ADTS 只按开头的平均码率估时长，长文件能差出百分之几，
// 这里取真实帧数。

const ADTS_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350];

function adtsHeader(buffer, pos) {
  if (pos + 7 > buffer.length) return null;
  if (buffer[pos] !== 0xff || (buffer[pos + 1] & 0xf6) !== 0xf0) return null;
  const rateIndex = (buffer[pos + 2] >> 2) & 0x0f;
  if (rateIndex >= ADTS_RATES.length) return null;
  const length = ((buffer[pos + 3] & 0x03) << 11) | (buffer[pos + 4] << 3) | (buffer[pos + 5] >> 5);
  if (length < 7) return null;
  return { length, rate: ADTS_RATES[rateIndex], blocks: (buffer[pos + 6] & 0x03) + 1 };
}

async function parseAdts(src, start) {
  let pos = start;
  let samples = 0n;
  let rate = 0;
  while (pos + 7 <= src.size) {
    const head = await src.read(pos, 10);
    const frame = adtsHeader(head, 0);
    if (frame) {
      if (pos + frame.length > src.size) break;
      if (!rate) rate = frame.rate;
      // 换算到第一帧的采样率（ADTS 中途不会变；万一变了按时长折算）。
      samples += (BigInt(frame.blocks) * 1024n * BigInt(rate)) / BigInt(frame.rate);
      pos += frame.length;
      continue;
    }
    if (head.toString("latin1", 0, 3) === "ID3" && head.length >= 10) {
      const length = ((head[6] & 0x7f) << 21) | ((head[7] & 0x7f) << 14) | ((head[8] & 0x7f) << 7) | (head[9] & 0x7f);
      pos += 10 + length + (head[5] & 0x10 ? 10 : 0);
      continue;
    }
    // 中间夹了垃圾：往后找下一个帧头（adts_aac_resync）。
    const window = await src.read(pos + 1, READ_WINDOW);
    let found = -1;
    for (let index = 0; index + 7 <= window.length; index += 1) {
      if (adtsHeader(window, index)) {
        found = index;
        break;
      }
    }
    if (found < 0) break;
    pos += 1 + found;
  }
  if (!rate) throw malformed("ADTS without frames");
  // 28224000 是所有 ADTS 采样率的公倍数（同 aacdec 的时间基）。
  return audioOnly([1, 28224000], null, (samples * 28224000n) / BigInt(rate));
}

// ---------------------------------------------------------------------------
// MP3（MPEG 音频），对照 FFmpeg mp3dec.c：Xing/Info（含 LAME 编码延迟）或 VBRI 帧数，
// 都没有就按第一帧码率估（estimate_timings_from_bit_rate）。

const MPEG_BITRATES = {
  "1-1": [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
  "1-2": [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
  "1-3": [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
  "2-1": [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
  "2-2": [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
};
const MPEG_RATES = [44100, 48000, 32000];
const MP3_MASK = 0xfffe0ccf;

function mpegHeader(value) {
  if ((value & 0xffe00000) >>> 0 !== 0xffe00000) return null;
  const versionBits = (value >>> 19) & 3;
  const layerBits = (value >>> 17) & 3;
  const bitrateIndex = (value >>> 12) & 15;
  const rateIndex = (value >>> 10) & 3;
  if (versionBits === 1 || layerBits === 0 || bitrateIndex === 15 || rateIndex === 3) return null;
  const lsf = versionBits !== 3;
  const layer = 4 - layerBits;
  const sampleRate = MPEG_RATES[rateIndex] >> (versionBits === 3 ? 0 : versionBits === 2 ? 1 : 2);
  const padding = (value >>> 9) & 1;
  const mono = ((value >>> 6) & 3) === 3;
  const table = lsf ? (layer === 1 ? "2-1" : "2-2") : `1-${layer}`;
  const kbps = MPEG_BITRATES[table][bitrateIndex];
  let frameSize = 0;
  if (kbps) {
    if (layer === 1) frameSize = (Math.floor((12000 * kbps) / sampleRate) + padding) * 4;
    else if (layer === 3 && lsf) frameSize = Math.floor((72000 * kbps) / sampleRate) + padding;
    else frameSize = Math.floor((144000 * kbps) / sampleRate) + padding;
  }
  const samplesPerFrame = layer === 1 ? 384 : layer === 3 && lsf ? 576 : 1152;
  return { lsf, layer, sampleRate, mono, bitRate: kbps * 1000, frameSize, samplesPerFrame };
}

/**
 * 从 offset 起 64KB 内找第一个「后面紧跟同类帧」的帧头（同 mp3_read_header 的 check 循环），
 * ADTS 和 MPEG 音频谁先出现算谁。
 */
async function findRawAudio(src, offset) {
  const window = await src.read(offset, 64 * 1024 + 8);
  for (let index = 0; index + 4 <= window.length; index += 1) {
    if (window[index] !== 0xff) continue;
    const position = offset + index;
    const adts = adtsHeader(window, index);
    if (adts) {
      const next = await src.read(position + adts.length, 7);
      const confirmed = position + adts.length === src.size || adtsHeader(next, 0)?.rate === adts.rate;
      if (confirmed) return { kind: "adts", position };
      continue;
    }
    const value = window.readUInt32BE(index);
    const header = mpegHeader(value);
    if (!header?.frameSize) continue;
    const next = await src.read(position + header.frameSize, 4);
    if (next.length < 4) {
      if (position + header.frameSize === src.size) return { kind: "mpeg", position };
      continue;
    }
    const nextValue = next.readUInt32BE(0);
    const sameStream = (value & MP3_MASK) >>> 0 === (nextValue & MP3_MASK) >>> 0;
    if (mpegHeader(nextValue) && sameStream) return { kind: "mpeg", position };
  }
  return null;
}

async function parseMp3(src, tagEnd, firstFrame) {
  // FFmpeg 在 ID3 之后的第一个字节处找 Xing/Info/VBRI（LAME 写的 Info 帧可能和后面音频帧的标志位不同，
  // 不一定过得了「连续两帧」检查）；那里不是帧头时退到第一个能确认的帧。
  const atTag = mpegHeader((await src.exact(tagEnd, 4)).readUInt32BE(0));
  const base = atTag ? tagEnd : firstFrame;
  const header = atTag ?? mpegHeader((await src.exact(firstFrame, 4)).readUInt32BE(0));
  const timeBase = [1, 14112000];
  let frames = 0;
  let headerFileSize = 0;
  let startPad = 0;
  let endPad = 0;
  if (header.layer === 3) {
    const spf = header.samplesPerFrame;
    const sideInfo = header.lsf ? (header.mono ? 9 : 17) : header.mono ? 17 : 32;
    const tag = await src.read(base + 4 + sideInfo, 8 + 4 + 4 + 100 + 4 + 36);
    const marker = tag.length >= 8 ? tag.toString("latin1", 0, 4) : "";
    if (marker === "Xing" || marker === "Info") {
      const flags = tag.readUInt32BE(4);
      let pos = 8;
      if (flags & 1) {
        frames = tag.readUInt32BE(pos);
        pos += 4;
      }
      if (flags & 2) {
        headerFileSize = tag.readUInt32BE(pos);
        pos += 4;
      }
      const fileSize = Math.max(0, src.size - (base + 4));
      if (fileSize && headerFileSize) {
        const low = Math.min(fileSize, headerFileSize);
        const delta = Math.max(fileSize, headerFileSize) - low;
        // 拼接文件：头里记的大小和实际差太多，帧数不可信（FFmpeg 改按码率算）。
        if (fileSize > headerFileSize && delta > Math.floor(low / 16)) frames = 0;
      }
      if (flags & 4) pos += 100;
      if (flags & 8) pos += 4;
      if (pos + 24 <= tag.length) {
        const version = tag.toString("latin1", pos, pos + 4);
        const delays = tag.readUIntBE(pos + 21, 3);
        if (version === "LAME" || version === "Lavf" || version === "Lavc") {
          startPad = delays >> 12;
          endPad = delays & 4095;
        }
      }
    }
    const vbri = await src.read(base + 4 + 32, 18);
    if (vbri.length >= 18 && vbri.toString("latin1", 0, 4) === "VBRI" && vbri.readUInt16BE(4) === 1) {
      headerFileSize = vbri.readUInt32BE(10);
      frames = vbri.readUInt32BE(14);
    }
    if (frames) {
      const samples = BigInt(frames) * BigInt(spf) - BigInt(startPad) - BigInt(endPad);
      return audioOnly(timeBase, 0n, rescaleQ(samples, [1, header.sampleRate], timeBase));
    }
  }
  // 没有 Xing/VBRI：FFmpeg 拿开头几秒的平均码率估，VBR 文件会差很多；这里逐帧数采样数。
  const samples = await countMpegSamples(src, firstFrame);
  if (!samples) throw malformed("MPEG audio without frames");
  return audioOnly(timeBase, 0n, rescaleQ(samples.count, [1, samples.rate], timeBase));
}

async function countMpegSamples(src, start) {
  const firstValue = (await src.exact(start, 4)).readUInt32BE(0);
  const first = mpegHeader(firstValue);
  if (!first?.frameSize) return null;
  const signature = (firstValue & MP3_MASK) >>> 0;
  const matches = (value) => ((value & MP3_MASK) >>> 0) === signature && mpegHeader(value)?.frameSize > 0;
  let pos = start;
  let count = 0n;
  while (pos + 4 <= src.size) {
    const value = (await src.read(pos, 4)).readUInt32BE(0);
    if (matches(value)) {
      const header = mpegHeader(value);
      if (pos + header.frameSize > src.size) break;
      count += BigInt(header.samplesPerFrame);
      pos += header.frameSize;
      continue;
    }
    // 中间的 ID3 / 尾部的 ID3v1、APE 标签或者垃圾：往后找下一个同类帧头。
    const window = await src.read(pos + 1, READ_WINDOW);
    let found = -1;
    for (let index = 0; index + 4 <= window.length; index += 1) {
      if (window[index] === 0xff && matches(window.readUInt32BE(index))) {
        found = index;
        break;
      }
    }
    if (found < 0) break;
    pos += 1 + found;
  }
  return count ? { count, rate: first.sampleRate } : null;
}

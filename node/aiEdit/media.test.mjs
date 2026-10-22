import { chmod, copyFile, mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { EditError } from "./errors.mjs";
import {
  AUDIO_SUFFIXES,
  IMAGE_SUFFIXES,
  MAX_TIME_US,
  VIDEO_SUFFIXES,
  exifRotation,
  mediaKind,
  probeMedia,
  resolveMediaPath,
} from "./media.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const MEDIA = path.resolve(here, "../fixtures/ai-edit/media");
// 原仓库 MediaResolver.probe（ffprobe）对这 17 个文件的输出。
const EXPECTED = JSON.parse(readFileSync(path.resolve(here, "../fixtures/ai-edit/media-info.json"), "utf8"));
// node/scripts/ai-edit/generate-media-fixtures.py 生成的边界样本与原仓库对它们的输出。
const EXTRA = path.resolve(here, "../fixtures/ai-edit/media-extra");
const EXTRA_EXPECTED = JSON.parse(readFileSync(path.join(EXTRA, "oracle.json"), "utf8"));
const EXTRA_FRAMES = JSON.parse(readFileSync(path.join(EXTRA, "frames.json"), "utf8"));
// 带显示矩阵的样本（原仓库不报旋转，没有它的对照）：由 media-extra/rotated.mp4 用 ffmpeg 9
// `-display_rotation <角度>` / `-display_hflip` 加 `-c copy` 生成，ffprobe.json 是 ffprobe 对它们的输出。
const ROTATION = path.resolve(here, "../fixtures/ai-edit/media-rotation");
const ROTATION_FFPROBE = JSON.parse(readFileSync(path.join(ROTATION, "ffprobe.json"), "utf8"));

// 有意与原仓库不同的样本（其余一律逐微秒相同）：
// - 裸 AAC、没有 Xing/VBRI 头的 MPEG 音频：容器里没有总长，ffprobe 只按开头的码率估
//   （VBR 能差出几百毫秒），这里逐帧数。改为对照 frames.json（ffprobe 逐包时长之和），
//   只差一微秒的取整。
// - 带封面图的音频：ffprobe 把封面当成一路画面，原仓库判 MEDIA_KIND_MISMATCH；这里封面不算画面，
//   结果应与去掉封面的同一段音频（nocover.*）的原仓库输出完全相同。
const FRAME_COUNTED = new Set(["tone.aac", "long.aac", "id3.aac", "cbr-noxing.mp3", "vbr-noxing.mp3", "layer2.mp3"]);
const COVER_TWINS = { "cover.mp3": "nocover.mp3", "cover.m4a": "nocover.m4a" };

let scratch;

beforeAll(async () => {
  scratch = await realpath(await mkdtemp(path.join(os.tmpdir(), "ai-edit-media-")));
});

afterAll(async () => {
  await rm(scratch, { recursive: true, force: true });
});

async function failure(promise) {
  const error = await promise.then(
    () => null,
    (caught) => caught,
  );
  expect(error).toBeInstanceOf(EditError);
  expect(error.status).toBe(422);
  return error.code;
}

async function place(name, content) {
  const target = path.join(scratch, name);
  await mkdir(path.dirname(target), { recursive: true });
  if (typeof content === "string") await copyFile(path.join(MEDIA, content), target);
  else await writeFile(target, content);
  return target;
}

async function topLevelBoxes(name) {
  const source = await readFile(path.join(MEDIA, name));
  const parts = {};
  for (let pos = 0; pos < source.length; ) {
    const size = source.readUInt32BE(pos);
    parts[source.toString("latin1", pos + 4, pos + 8)] = source.subarray(pos, pos + size);
    pos += size;
  }
  return { source, parts };
}

function isoBox(type, ...bodies) {
  const body = Buffer.concat(bodies);
  const head = Buffer.alloc(8);
  head.writeUInt32BE(8 + body.length, 0);
  head.write(type, 4, "latin1");
  return Buffer.concat([head, body]);
}

/** version/flags + 若干个 32 位字段，拼成 tfhd / trun。 */
function fullBox(type, flags, ...fields) {
  const body = Buffer.alloc(4 + 4 * fields.length);
  body.writeUInt32BE(flags, 0);
  fields.forEach((value, index) => body.writeUInt32BE(value >>> 0, 4 + 4 * index));
  return isoBox(type, body);
}

describe("对照原仓库 ffprobe 结果（node/fixtures/ai-edit/media）", () => {
  it.each(Object.keys(EXPECTED))("%s", async (name) => {
    const want = EXPECTED[name];
    const got = await probeMedia(path.join(MEDIA, name), { roots: [MEDIA] });
    expect(got.path).toBe(await realpath(path.join(MEDIA, name)));
    expect({ kind: got.kind, width: got.width, height: got.height, audio: got.audio, video: got.video }).toEqual({
      kind: want.kind,
      width: want.width,
      height: want.height,
      audio: want.audio,
      video: want.video,
    });
    expect(got.rotation).toBe(0);
    expect(Number.isInteger(got.duration_us)).toBe(true);
    if (FRAME_COUNTED.has(name)) {
      // 裸 AAC：ffprobe 按码率估，这里数帧（见上）；共享素材没有逐包参照，只要求 30ms 以内。
      expect(Math.abs(got.duration_us - want.duration_us)).toBeLessThanOrEqual(30_000);
    } else {
      expect(got.duration_us).toBe(want.duration_us);
    }
  });

  it("分片 mp4 按 moof/trun 累加，并复现 ffprobe 打印 2.066667 后 floor 成 2066666", async () => {
    const got = await probeMedia(path.join(MEDIA, "fragmented.mp4"), { roots: [MEDIA] });
    expect(got.duration_us).toBe(2_066_666);
  });

  it("图片时长固定为 24 小时", async () => {
    const got = await probeMedia(path.join(MEDIA, "still.png"), { roots: [MEDIA] });
    expect(got.duration_us).toBe(MAX_TIME_US);
    expect(MAX_TIME_US).toBe(86_400_000_000);
  });
});

describe("对照原仓库 ffprobe 结果：边界样本（node/fixtures/ai-edit/media-extra）", () => {
  it.each(Object.keys(EXTRA_EXPECTED))("%s", async (name) => {
    const want = EXTRA_EXPECTED[name];
    // rotation 是原仓库没有的字段，单独断言（这批样本都没有显示矩阵；rotated.mp4 生成时用的旧写法新版 ffmpeg 不认）
    const got = await probeMedia(path.join(EXTRA, name), { roots: [EXTRA] }).then(
      ({ path: _path, rotation, ...rest }) => {
        expect(rotation).toBe(0);
        return rest;
      },
      (error) => ({ error: error.code }),
    );
    if (COVER_TWINS[name]) {
      expect(want).toEqual({ error: "MEDIA_KIND_MISMATCH" });
      expect(got).toEqual(EXTRA_EXPECTED[COVER_TWINS[name]]);
    } else if (FRAME_COUNTED.has(name)) {
      const { duration_us: duration, ...rest } = got;
      const { duration_us: _estimated, ...wantRest } = want;
      expect(rest).toEqual(wantRest);
      expect(Math.abs(duration - EXTRA_FRAMES[name])).toBeLessThanOrEqual(1);
    } else {
      expect(got).toEqual(want);
    }
  });

  it("有意差异的样本都在对照集里（重新生成样本时别漏掉）", () => {
    const counted = [...FRAME_COUNTED].filter((name) => name !== "tone.aac");
    const covers = [...Object.keys(COVER_TWINS), ...Object.values(COVER_TWINS)];
    expect(Object.keys(EXTRA_EXPECTED)).toEqual(expect.arrayContaining([...counted, ...covers]));
    expect(Object.keys(EXTRA_FRAMES)).toEqual(expect.arrayContaining(counted));
  });
});

describe("显示矩阵的旋转角（node/fixtures/ai-edit/media-rotation）", () => {
  // ffprobe 的 rotation 换成 0 到 359；镜像、45 度这类不是转 90 度整数倍的记 0
  const WANT = {
    "rot90.mp4": 90,
    "rot270.mp4": 270,
    "rot180.mov": 180,
    "hflip.mp4": 0,
    "rot90-hflip.mp4": 0,
    "rot45.mp4": 0,
  };

  it("样本都在 ffprobe 对照里", () => {
    expect(Object.keys(ROTATION_FFPROBE).sort()).toEqual(Object.keys(WANT).sort());
  });

  it.each(Object.keys(WANT))("%s", async (name) => {
    const got = await probeMedia(path.join(ROTATION, name), { roots: [ROTATION] });
    const { width, height, ffprobe_rotation: reported } = ROTATION_FFPROBE[name];
    // 宽高仍是编码宽高，与 ffprobe 一致；时长等其他字段与没加旋转的原样本相同
    expect([got.width, got.height]).toEqual([width, height]);
    expect(got.rotation).toBe(WANT[name]);
    if (WANT[name]) expect(got.rotation).toBe(((reported % 360) + 360) % 360);
    const { path: _a, rotation: _b, ...rest } = got;
    const { path: _c, rotation: _d, ...plain } = await probeMedia(path.join(EXTRA, "rotated.mp4"), { roots: [EXTRA] });
    expect(rest).toEqual(plain);
  });

  it("mvhd 的矩阵乘在轨道矩阵后面（同 ffprobe），等比缩放不影响角度", async () => {
    async function withMovieMatrix(name, source, matrix) {
      const bytes = Buffer.from(await readFile(source));
      const at = bytes.indexOf("mvhd") + 4;
      expect(bytes[at]).toBe(0);
      matrix.forEach((value, index) => bytes.writeInt32BE(value, at + 36 + index * 4));
      return place(name, bytes);
    }
    const turn90 = [0, -65536, 0, 65536, 0, 0, 0, 0, 1 << 30];
    const doubled = [131072, 0, 0, 0, 131072, 0, 0, 0, 1 << 30];
    // ffprobe 对这三个文件分别报 90、-180、90
    const movieOnly = await withMovieMatrix("rotation/movie90.mp4", path.join(EXTRA, "rotated.mp4"), turn90);
    const both = await withMovieMatrix("rotation/both90.mp4", path.join(ROTATION, "rot90.mp4"), turn90);
    const scaled = await withMovieMatrix("rotation/scaled.mp4", path.join(ROTATION, "rot90.mp4"), doubled);
    const options = { roots: [scratch] };
    expect((await probeMedia(movieOnly, options)).rotation).toBe(90);
    expect((await probeMedia(both, options)).rotation).toBe(180);
    expect((await probeMedia(scaled, options)).rotation).toBe(90);
  });

  it("非 mp4/mov 容器、图片和音频都是 0", async () => {
    for (const name of ["clip.mkv", "clip.webm", "clip.avi", "still.jpg", "tone.m4a", "tone.mp3"]) {
      expect((await probeMedia(path.join(MEDIA, name), { roots: [MEDIA] })).rotation).toBe(0);
    }
  });
});

describe("构造的坏文件不会卡住采集器", () => {
  const probe = (file, options = {}) => probeMedia(file, { roots: [scratch], ...options });

  async function fragmentWith(...trafChildren) {
    const { parts } = await topLevelBoxes("fragmented.mp4");
    return Buffer.concat([parts.ftyp, parts.moov, isoBox("moof", isoBox("traf", ...trafChildren))]);
  }

  it("trun 声明几十亿个样本：同 FFmpeg 按界限拒绝（MEDIA_INVALID），不逐条循环", async () => {
    const cases = {
      // trex 的默认大小是 0、trun 又不逐条给大小（审查给的复现文件）。
      "zero-size.mp4": await fragmentWith(fullBox("tfhd", 0x08, 1, 1), fullBox("trun", 0, 0xffffffff)),
      // 默认大小 1、样本数比文件字节还多，连拼 8 个。
      "many-truns.mp4": await fragmentWith(
        fullBox("tfhd", 0x18, 1, 1, 1),
        ...Array.from({ length: 8 }, () => fullBox("trun", 0, 10_000_000)),
      ),
      // 超出索引表上限（FFmpeg 的 UINT_MAX / sizeof(MOVTimeToSample)）。
      "over-limit.mp4": await fragmentWith(fullBox("tfhd", 0x18, 1, 1, 1), fullBox("trun", 0, 0xffffffff)),
      // 逐条给时长：盒子里只装得下 2 条。
      "per-entry.mp4": await fragmentWith(fullBox("tfhd", 0x18, 1, 1, 1), fullBox("trun", 0x100, 1000, 1, 1)),
    };
    for (const [name, content] of Object.entries(cases)) {
      const file = await place(`dos/${name}`, content);
      const started = performance.now();
      expect(await failure(probe(file))).toBe("MEDIA_INVALID");
      expect(performance.now() - started).toBeLessThan(100);
    }
  });

  it("JPEG 里夹着几 MB 垃圾：整块找下一个标记，不逐字节读", async () => {
    const junk = Buffer.alloc(8 * 1024 * 1024, 0x11);
    junk.set([0xff, 0xd8, 0xff, 0xe0]);
    const file = await place("dos/junk.jpg", junk);
    const started = performance.now();
    expect(await failure(probe(file))).toBe("MEDIA_INVALID");
    expect(performance.now() - started).toBeLessThan(500);
  });

  it("标记前的垃圾和 0xFF 填充正好跨过读窗口（256KB）边界，照样找到画面尺寸", async () => {
    const jpeg = await readFile(path.join(MEDIA, "still.jpg"));
    const firstSegmentEnd = 4 + jpeg.readUInt16BE(4);
    const garbage = Buffer.alloc(256 * 1024 - 1, 0x11);
    const padded = Buffer.concat([
      jpeg.subarray(0, firstSegmentEnd),
      garbage,
      Buffer.from([0xff, 0xff, 0xff]),
      jpeg.subarray(firstSegmentEnd),
    ]);
    const got = await probe(await place("dos/padded.jpg", padded));
    expect([got.kind, got.width, got.height]).toEqual(["image", 1920, 1080]);
  });

  it("JPEG 带 EXIF 方向：记成旋转角，素材库按转正后的宽高算（手机竖拍照片）", async () => {
    const jpeg = await readFile(path.join(MEDIA, "still.jpg"));
    const app1 = (orientation, little = true) => {
      const tiff = Buffer.alloc(8 + 2 + 12 + 4);
      tiff.write(little ? "II" : "MM", 0, "latin1");
      const u16 = (value, offset) => (little ? tiff.writeUInt16LE(value, offset) : tiff.writeUInt16BE(value, offset));
      const u32 = (value, offset) => (little ? tiff.writeUInt32LE(value, offset) : tiff.writeUInt32BE(value, offset));
      u16(42, 2); u32(8, 4); u16(1, 8);
      u16(0x0112, 10); u16(3, 12); u32(1, 14); u16(orientation, 18);
      const data = Buffer.concat([Buffer.from("Exif\0\0", "latin1"), tiff]);
      const head = Buffer.alloc(4);
      head.writeUInt16BE(0xffe1, 0);
      head.writeUInt16BE(data.length + 2, 2);
      return Buffer.concat([head, data]);
    };
    const withExif = (segment) => Buffer.concat([jpeg.subarray(0, 2), segment, jpeg.subarray(2)]);
    const portrait = await probe(await place("exif/six.jpg", withExif(app1(6))));
    expect([portrait.width, portrait.height, portrait.rotation]).toEqual([1920, 1080, 90]);
    expect((await probe(await place("exif/eight.jpg", withExif(app1(8, false))))).rotation).toBe(270);
    expect((await probe(await place("exif/one.jpg", withExif(app1(1))))).rotation).toBe(0);
    expect(exifRotation(Buffer.from("Exif\0\0garbage", "latin1"))).toBe(0);
    expect(exifRotation(Buffer.from("not exif at all", "latin1"))).toBe(0);
  });

  it("一直解析不完的文件：期间照常让出事件循环，到时报 MEDIA_PROBE_FAILED", async () => {
    // 0xFF01 是不带长度的标记，只能一个个跳过；16MB 要走几百万步。
    const markers = Buffer.alloc(16 * 1024 * 1024, 0x01);
    for (let index = 0; index < markers.length; index += 2) markers[index] = 0xff;
    markers[1] = 0xd8;
    const file = await place("dos/markers.jpg", markers);
    let ticks = 0;
    const timer = setInterval(() => {
      ticks += 1;
    }, 5);
    try {
      expect(await failure(probe(file, { timeoutMs: 150 }))).toBe("MEDIA_PROBE_FAILED");
    } finally {
      clearInterval(timer);
    }
    expect(ticks).toBeGreaterThanOrEqual(3);
  });
});

describe("后缀与 mediaKind（同 media.py）", () => {
  it("三组后缀与原仓库一致", () => {
    expect([...VIDEO_SUFFIXES].sort()).toEqual([".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"]);
    expect([...IMAGE_SUFFIXES].sort()).toEqual([".jpeg", ".jpg", ".png", ".webp"]);
    expect([...AUDIO_SUFFIXES].sort()).toEqual([".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"]);
  });

  it("按后缀判断类型，大小写不敏感", () => {
    expect(mediaKind("/a/B.MP4")).toBe("video");
    expect(mediaKind("/a/b.JPEG")).toBe("image");
    expect(mediaKind("/a/b.tar.flac")).toBe("audio");
  });

  it.each(["/a/notes.txt", "/a/noext", "/a/.mp4", "/a/b.mp4."])("%s 不支持", (name) => {
    let code = null;
    try {
      mediaKind(name);
    } catch (error) {
      code = error.code;
      expect(error.status).toBe(422);
    }
    expect(code).toBe("MEDIA_TYPE_UNSUPPORTED");
  });
});

describe("resolveMediaPath 的校验顺序与错误码（同 MediaResolver.resolve）", () => {
  let root;
  let outside;

  beforeAll(async () => {
    root = path.join(scratch, "root");
    outside = path.join(scratch, "outside");
    await mkdir(root, { recursive: true });
    await mkdir(outside, { recursive: true });
    await mkdir(`${root}-evil`, { recursive: true });
    await copyFile(path.join(MEDIA, "clip-720p.mp4"), path.join(root, "a.mp4"));
    await copyFile(path.join(MEDIA, "clip-720p.mp4"), path.join(outside, "b.mp4"));
    await copyFile(path.join(MEDIA, "clip-720p.mp4"), path.join(`${root}-evil`, "c.mp4"));
    await symlink(path.join(outside, "b.mp4"), path.join(root, "escape.mp4"));
    await writeFile(path.join(root, "notes.txt"), "hello");
    await writeFile(path.join(root, "empty.mp4"), "");
    await mkdir(path.join(root, "folder.mp4"));
  });

  it("正常文件返回规范化的绝对路径", async () => {
    expect(await resolveMediaPath(path.join(root, "a.mp4"), { roots: [root] })).toBe(path.join(root, "a.mp4"));
    const dotted = path.join(root, "sub", "..", "a.mp4");
    expect(await resolveMediaPath(dotted, { roots: [root] })).toBe(path.join(root, "a.mp4"));
  });

  it("根目录本身经过符号链接也能认出来（同 Path.resolve）", async () => {
    const alias = path.join(scratch, "alias");
    await symlink(root, alias);
    expect(await resolveMediaPath(path.join(alias, "a.mp4"), { roots: [alias] })).toBe(path.join(root, "a.mp4"));
  });

  // 用户目录要 spy os.homedir：Windows 上它只读 USERPROFILE，改 HOME 不起作用。
  it("~ 展开成用户目录", async () => {
    const homedir = vi.spyOn(os, "homedir").mockReturnValue(root);
    try {
      expect(await resolveMediaPath("~/a.mp4", { roots: [root] })).toBe(path.join(root, "a.mp4"));
      // 展开只看 os.homedir()，不看 HOME（Windows 上 HOME 本来就不生效）。
      homedir.mockReturnValue(outside);
      await expect(resolveMediaPath("~/a.mp4", { roots: [root] })).rejects.toMatchObject({ code: "MEDIA_NOT_FOUND" });
    } finally {
      homedir.mockRestore();
    }
  });

  it.skipIf(process.platform !== "win32")("Windows 上 ~\\ 也展开成用户目录", async () => {
    const homedir = vi.spyOn(os, "homedir").mockReturnValue(root);
    try {
      expect(await resolveMediaPath("~\\a.mp4", { roots: [root] })).toBe(path.join(root, "a.mp4"));
    } finally {
      homedir.mockRestore();
    }
  });

  it.each([
    ["含 NUL", () => `${root}/a\0.mp4`, "PATH_INVALID"],
    ["相对路径里含 NUL 也先报 NUL", () => "a\0.mp4", "PATH_INVALID"],
    ["不是字符串", () => 42, "PATH_INVALID"],
    ["相对路径", () => "a.mp4", "PATH_NOT_ABSOLUTE"],
    ["不存在", () => path.join(root, "missing.mp4"), "MEDIA_NOT_FOUND"],
    ["不在白名单里", () => path.join(outside, "b.mp4"), "PATH_OUTSIDE_WHITELIST"],
    ["符号链接指到白名单外", () => path.join(root, "escape.mp4"), "PATH_OUTSIDE_WHITELIST"],
    ["前缀相同的兄弟目录", () => path.join(`${root}-evil`, "c.mp4"), "PATH_OUTSIDE_WHITELIST"],
    ["扩展名不支持", () => path.join(root, "notes.txt"), "MEDIA_TYPE_UNSUPPORTED"],
    ["是目录", () => path.join(root, "folder.mp4"), "MEDIA_NOT_FILE"],
    ["空文件", () => path.join(root, "empty.mp4"), "MEDIA_EMPTY"],
  ])("%s", async (_label, raw, code) => {
    expect(await failure(resolveMediaPath(raw(), { roots: [root] }))).toBe(code);
  });

  it("超过大小上限", async () => {
    expect(await failure(resolveMediaPath(path.join(root, "a.mp4"), { roots: [root], maxBytes: 1000 }))).toBe(
      "MEDIA_TOO_LARGE",
    );
  });

  it("没配根目录时一律拒绝", async () => {
    expect(await failure(resolveMediaPath(path.join(root, "a.mp4"), {}))).toBe("PATH_OUTSIDE_WHITELIST");
  });

  it("probeMedia 先走同样的路径校验", async () => {
    expect(await failure(probeMedia(path.join(outside, "b.mp4"), { roots: [root] }))).toBe("PATH_OUTSIDE_WHITELIST");
    expect(await failure(probeMedia(path.join(root, "empty.mp4"), { roots: [root] }))).toBe("MEDIA_EMPTY");
  });

  const cannotLock = process.platform === "win32" || process.getuid?.() === 0;
  it.skipIf(cannotLock)("文件读不了时报 MEDIA_INVALID（原仓库的 ffprobe 打不开文件、非 0 退出）", async () => {
    const locked = await place("locked/locked.mp4", "clip-720p.mp4");
    await chmod(locked, 0o000);
    try {
      expect(await failure(probeMedia(locked, { roots: [scratch] }))).toBe("MEDIA_INVALID");
    } finally {
      await chmod(locked, 0o644);
    }
  });

  it("解析超时报 MEDIA_PROBE_FAILED（对应原仓库 ffprobe 的 timeout）", async () => {
    const file = path.join(MEDIA, "clip-720p.mp4");
    expect(await failure(probeMedia(file, { roots: [MEDIA], timeoutMs: 0 }))).toBe("MEDIA_PROBE_FAILED");
  });
});

describe("按内容识别格式，扩展名只决定期望类型", () => {
  const probe = (file) => probeMedia(file, { roots: [scratch] });

  it("音频扩展名里装了视频 → MEDIA_KIND_MISMATCH", async () => {
    expect(await failure(probe(await place("kind/has-video.m4a", "clip-720p.mp4")))).toBe("MEDIA_KIND_MISMATCH");
    expect(await failure(probe(await place("kind/has-video.mp3", "clip.webm")))).toBe("MEDIA_KIND_MISMATCH");
  });

  it("视频扩展名里只有声音 → MEDIA_KIND_MISMATCH", async () => {
    expect(await failure(probe(await place("kind/no-picture.mp4", "tone.m4a")))).toBe("MEDIA_KIND_MISMATCH");
    expect(await failure(probe(await place("kind/no-picture.avi", "tone.wav")))).toBe("MEDIA_KIND_MISMATCH");
  });

  it("图片扩展名里是音频 → MEDIA_KIND_MISMATCH", async () => {
    expect(await failure(probe(await place("kind/sound.png", "tone.mp3")))).toBe("MEDIA_KIND_MISMATCH");
  });

  it("扩展名和内容对不上但类型一致时照常解析", async () => {
    const photo = await probe(await place("kind/photo.png", "still.jpg"));
    expect([photo.kind, photo.width, photo.height]).toEqual(["image", 1920, 1080]);
    const clip = await probe(await place("kind/clip.mp4", "clip.webm"));
    expect([clip.kind, clip.duration_us, clip.width, clip.height]).toEqual(["video", 1_508_000, 640, 360]);
    const tone = await probe(await place("kind/tone.mp3", "tone.flac"));
    expect([tone.kind, tone.duration_us]).toEqual(["audio", 1_800_000]);
  });

  it("认不出的内容 → MEDIA_INVALID", async () => {
    expect(await failure(probe(await place("bad/junk.mp4", Buffer.alloc(4096, 0x5a))))).toBe("MEDIA_INVALID");
    expect(await failure(probe(await place("bad/tiny.wav", Buffer.from("RI"))))).toBe("MEDIA_INVALID");
  });

  it("moov 放在文件前面（faststart）也能读，mdat 被截掉一截时头信息照常可读", async () => {
    // 共享素材的 moov 在文件末尾；这里重排成 ftyp → moov → free → mdat。
    const { parts } = await topLevelBoxes("clip-720p.mp4");
    const faststart = Buffer.concat([parts.ftyp, parts.moov, parts.free, parts.mdat]);
    const moved = await probe(await place("order/faststart.mp4", faststart));
    expect([moved.duration_us, moved.width, moved.height, moved.audio]).toEqual([3_000_000, 1280, 720, true]);
    const cut = await probe(await place("order/faststart-cut.mp4", faststart.subarray(0, 20_000)));
    expect(cut.duration_us).toBe(3_000_000);
  });

  it("moov 被截断或缺失 → MEDIA_INVALID（截断时 ffprobe 偶尔能读出半截轨道，这里统一拒绝）", async () => {
    const { source, parts } = await topLevelBoxes("clip-720p.mp4");
    const moovAt = source.length - parts.moov.length;
    const cut = await place("bad/cut-moov.mp4", source.subarray(0, moovAt + 2000));
    expect(await failure(probe(cut))).toBe("MEDIA_INVALID");
    expect(await failure(probe(await place("bad/no-moov.mp4", source.subarray(0, moovAt))))).toBe("MEDIA_INVALID");
  });

  it("Matroska 没写 Duration（直播录制那种）→ 与 ffprobe 一样拿不到时长", async () => {
    const source = Buffer.from(await readFile(path.join(MEDIA, "clip.webm")));
    const at = source.indexOf(Buffer.from([0x44, 0x89]));
    expect(at).toBeGreaterThan(0);
    source[at + 1] = 0x88; // 改成一个不认识的元素 ID，等于没有 Duration
    expect(await failure(probe(await place("bad/live.webm", source)))).toBe("MEDIA_INVALID");
  });
});

// ---------------------------------------------------------------------------
// 手工拼的文件头：覆盖共享素材里没有的格式分支。

function riff(form, chunks) {
  const body = Buffer.concat([Buffer.from(form, "latin1"), ...chunks]);
  const head = Buffer.alloc(8);
  head.write("RIFF", 0, "latin1");
  head.writeUInt32LE(body.length, 4);
  return Buffer.concat([head, body]);
}

function chunk(id, data, declared = data.length) {
  const head = Buffer.alloc(8);
  head.write(id, 0, "latin1");
  head.writeUInt32LE(declared, 4);
  return Buffer.concat([head, data, data.length & 1 ? Buffer.alloc(1) : Buffer.alloc(0)]);
}

function mp3Frames(count) {
  // MPEG-1 Layer III 128kbps 44.1kHz 立体声，每帧 417 字节、1152 个采样，没有 Xing 头。
  const frame = Buffer.alloc(417);
  frame.writeUInt32BE(0xfffb9000, 0);
  return Buffer.concat(Array.from({ length: count }, () => frame));
}

function adtsFrame(length = 100) {
  const frame = Buffer.alloc(length);
  frame.set([0xff, 0xf1, 0x50, 0x80 | ((length >> 11) & 3), (length >> 3) & 0xff, ((length & 7) << 5) | 0x1f, 0xfc]);
  return frame;
}

function id3(payload) {
  const head = Buffer.from([0x49, 0x44, 0x33, 3, 0, 0, 0, 0, 0, 0]);
  const size = payload.length;
  head[6] = (size >> 21) & 0x7f;
  head[7] = (size >> 14) & 0x7f;
  head[8] = (size >> 7) & 0x7f;
  head[9] = size & 0x7f;
  return Buffer.concat([head, payload]);
}

const CRC_TABLE = Array.from({ length: 256 }, (_, index) => {
  let value = index << 24;
  for (let bit = 0; bit < 8; bit += 1) value = value & 0x80000000 ? (value << 1) ^ 0x04c11db7 : value << 1;
  return value >>> 0;
});

function oggPage({ flags, granule, sequence, packet }) {
  const segments = [];
  let left = packet.length;
  while (left >= 255) {
    segments.push(255);
    left -= 255;
  }
  segments.push(left);
  const page = Buffer.alloc(27 + segments.length + packet.length);
  page.write("OggS", 0, "latin1");
  page[5] = flags;
  page.writeBigInt64LE(BigInt(granule), 6);
  page.writeUInt32LE(0x1234, 14);
  page.writeUInt32LE(sequence, 18);
  page[26] = segments.length;
  page.set(segments, 27);
  packet.copy(page, 27 + segments.length);
  let crc = 0;
  for (const byte of page) crc = ((crc << 8) ^ CRC_TABLE[((crc >>> 24) ^ byte) & 0xff]) >>> 0;
  page.writeUInt32LE(crc, 22);
  return page;
}

describe("其他格式分支", () => {
  const probe = (file) => probeMedia(file, { roots: [scratch] });

  it("WebP 无损（VP8L）与扩展（VP8X）", async () => {
    const lossless = Buffer.alloc(5);
    lossless[0] = 0x2f;
    lossless.writeUInt32LE((300 - 1) | ((200 - 1) << 14), 1);
    const a = await probe(await place("img/lossless.webp", riff("WEBP", [chunk("VP8L", lossless)])));
    expect([a.kind, a.width, a.height, a.duration_us]).toEqual(["image", 300, 200, MAX_TIME_US]);
    expect([a.video, a.audio]).toEqual([true, false]);

    const extended = Buffer.alloc(10);
    extended.writeUIntLE(1000 - 1, 4, 3);
    extended.writeUIntLE(700 - 1, 7, 3);
    const b = await probe(await place("img/extended.webp", riff("WEBP", [chunk("VP8X", extended)])));
    expect([b.width, b.height]).toEqual([1000, 700]);
  });

  it("GIF、BMP 内容放在 jpg/png 扩展名里", async () => {
    const gif = Buffer.alloc(32);
    gif.write("GIF89a", 0, "latin1");
    gif.writeUInt16LE(333, 6);
    gif.writeUInt16LE(222, 8);
    const a = await probe(await place("img/anim.jpg", gif));
    expect([a.width, a.height]).toEqual([333, 222]);

    const bmp = Buffer.alloc(64);
    bmp.write("BM", 0, "latin1");
    bmp.writeUInt32LE(64, 2);
    bmp.writeUInt32LE(54, 10);
    bmp.writeUInt32LE(40, 14);
    bmp.writeInt32LE(120, 18);
    bmp.writeInt32LE(-77, 22);
    const b = await probe(await place("img/bitmap.png", bmp));
    expect([b.width, b.height]).toEqual([120, 77]);
  });

  it("宽高超出 1..100000 时给 null（_safe_dimension）", async () => {
    const extended = Buffer.alloc(10);
    extended.writeUIntLE(200_000 - 1, 4, 3);
    extended.writeUIntLE(10 - 1, 7, 3);
    const got = await probe(await place("img/huge.webp", riff("WEBP", [chunk("VP8X", extended)])));
    expect([got.width, got.height]).toEqual([null, 10]);
  });

  it("WAV 扩展格式 24bit：按数据大小算采样数；data 声明超出文件时按码率估", async () => {
    const fmt = Buffer.alloc(40);
    fmt.writeUInt16LE(0xfffe, 0);
    fmt.writeUInt16LE(2, 2);
    fmt.writeUInt32LE(48000, 4);
    fmt.writeUInt32LE(288000, 8);
    fmt.writeUInt16LE(6, 12);
    fmt.writeUInt16LE(24, 14);
    fmt.writeUInt16LE(22, 16);
    fmt.writeUInt16LE(24, 18);
    fmt.writeUInt32LE(3, 20);
    Buffer.from("0100000000001000800000aa00389b71", "hex").copy(fmt, 24);
    const samples = Buffer.alloc(432000);
    // PCM 有 fact 也不信，按数据大小算（同 wavdec）。
    const fact = chunk("fact", Buffer.from([1, 0, 0, 0]));
    const whole = await probe(await place("wav/ext24.wav", riff("WAVE", [chunk("fmt ", fmt), fact, chunk("data", samples)])));
    expect([whole.kind, whole.duration_us, whole.audio, whole.video]).toEqual(["audio", 1_500_000, true, false]);

    const short = riff("WAVE", [chunk("fmt ", fmt), chunk("data", samples, 864000)]);
    const truncated = await probe(await place("wav/truncated.wav", short));
    expect(truncated.duration_us).toBe(1_500_000);
  });

  it("FLAC：STREAMINFO 的总采样数，前面可以有 ID3", async () => {
    const info = Buffer.alloc(34);
    info.writeUInt16BE(4096, 0);
    info.writeUInt16BE(4096, 2);
    info.writeBigUInt64BE((32000n << 44n) | (1n << 41n) | (15n << 36n) | 80000n, 10);
    const flac = Buffer.concat([Buffer.from("fLaC", "latin1"), Buffer.from([0x80, 0, 0, 34]), info]);
    const got = await probe(await place("flac/tagged.flac", Buffer.concat([id3(Buffer.alloc(20)), flac])));
    expect(got.duration_us).toBe(2_500_000);
  });

  it("MP3 没有 Xing 头：逐帧数采样数（跳过 ID3v2 与尾部 ID3v1）", async () => {
    const tail = Buffer.alloc(128);
    tail.write("TAG", 0, "latin1");
    const file = Buffer.concat([id3(Buffer.alloc(300)), mp3Frames(100), tail]);
    const got = await probe(await place("mp3/noxing.mp3", file));
    // 100 帧 × 1152 / 44100 = 2.612244…s
    expect(Math.abs(got.duration_us - (100 * 1152 * 1e6) / 44100)).toBeLessThan(2);
  });

  it("封面图不算画面：带 APIC 封面的 MP3 仍是音频（有意与原仓库不同，见报告）", async () => {
    const apic = Buffer.alloc(200);
    apic.write("APIC", 0, "latin1");
    apic.writeUInt32BE(190, 4);
    const got = await probe(await place("mp3/cover.mp3", Buffer.concat([id3(apic), mp3Frames(50)])));
    expect([got.kind, got.audio, got.video]).toEqual(["audio", true, false]);
  });

  it("ADTS：前面有 ID3、中间夹垃圾字节也能接着数帧", async () => {
    const frames = Array.from({ length: 50 }, () => adtsFrame());
    frames.splice(10, 0, Buffer.alloc(13, 0x11));
    const got = await probe(await place("aac/junk.aac", Buffer.concat([id3(Buffer.alloc(40)), ...frames])));
    // 50 帧 × 1024 / 44100 = 1.160997…s
    expect(Math.abs(got.duration_us - (50 * 1024 * 1e6) / 44100)).toBeLessThan(2);
  });

  it("Ogg Opus：时长取最后一页 granule（同 ffprobe，不减 pre-skip）", async () => {
    const head = Buffer.alloc(19);
    head.write("OpusHead", 0, "latin1");
    head[8] = 1;
    head[9] = 1;
    head.writeUInt16LE(312, 10);
    head.writeUInt32LE(48000, 12);
    const tags = Buffer.alloc(16);
    tags.write("OpusTags", 0, "latin1");
    const file = Buffer.concat([
      oggPage({ flags: 2, granule: 0, sequence: 0, packet: head }),
      oggPage({ flags: 0, granule: 0, sequence: 1, packet: tags }),
      oggPage({ flags: 0, granule: 48000, sequence: 2, packet: Buffer.alloc(300, 0xfc) }),
      oggPage({ flags: 4, granule: 96312, sequence: 3, packet: Buffer.alloc(300, 0xfc) }),
    ]);
    const got = await probe(await place("ogg/opus.ogg", file));
    expect(got.duration_us).toBe(2_006_500);

    // 尾页 CRC 不对就不认，退回上一页的 granule。
    const broken = Buffer.from(file);
    broken[broken.length - 1] ^= 0xff;
    const fallback = await probe(await place("ogg/broken.ogg", broken));
    expect(fallback.duration_us).toBe(1_000_000);
  });
});

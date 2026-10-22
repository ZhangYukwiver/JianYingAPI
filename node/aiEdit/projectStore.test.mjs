import { readdirSync, readFileSync } from "node:fs";
import { mkdtemp, open, readdir, readFile, realpath, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import { EditError, invalid } from "./errors.mjs";
import { draftInputs, serializeDraft } from "./draft.mjs";
import { parseSrt, ProjectStore } from "./projectStore.mjs";

const FIXTURES = fileURLToPath(new URL("../fixtures/ai-edit/", import.meta.url));
const OPERATIONS = path.join(FIXTURES, "operations");
const MEDIA_ROOT = await realpath(path.join(FIXTURES, "media"));
const MEDIA_INFO = JSON.parse(readFileSync(path.join(FIXTURES, "media-info.json"), "utf8"));
const BASE_TIME = Date.UTC(2026, 0, 1);
const KINDS = {
  video: [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"],
  image: [".png", ".jpg", ".jpeg", ".webp"],
  audio: [".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"],
};

const temporaryDirectories = [];
afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

async function makeDirectory() {
  const directory = await mkdtemp(path.join(tmpdir(), "ai-edit-projects-"));
  temporaryDirectories.push(directory);
  return directory;
}

/**
 * 测试用探测：路径检查照 media.py 的 MediaResolver.resolve（错误码一致），
 * 时长宽高取原仓库 ffprobe 量出的 media-info.json，不依赖 media.mjs。
 */
async function fakeProbe(raw, { roots }) {
  if (raw.includes("\0")) throw invalid("PATH_INVALID", "Path contains a null byte");
  if (!path.isAbsolute(raw)) throw invalid("PATH_NOT_ABSOLUTE", "Media paths must be absolute");
  let normalized;
  try {
    normalized = await realpath(raw);
  } catch {
    throw invalid("MEDIA_NOT_FOUND", "Media file does not exist");
  }
  const allowed = await Promise.all(roots.map((root) => realpath(root)));
  if (!allowed.some((root) => normalized === root || normalized.startsWith(root + path.sep))) {
    throw invalid("PATH_OUTSIDE_WHITELIST", "Media path is outside configured roots");
  }
  const suffix = path.extname(normalized).toLowerCase();
  const kind = Object.keys(KINDS).find((key) => KINDS[key].includes(suffix));
  if (!kind) throw invalid("MEDIA_TYPE_UNSUPPORTED", "Unsupported local media type");
  const info = await stat(normalized);
  if (!info.isFile()) throw invalid("MEDIA_NOT_FILE", "Media path is not a regular file");
  if (info.size === 0) throw invalid("MEDIA_EMPTY", "Media file is empty");
  const known = MEDIA_INFO[path.basename(normalized)];
  if (!known) throw new Error(`no media-info entry for ${normalized}`);
  return { path: normalized, ...known };
}

/** 和生成脚本一样的确定 id 与时钟：000…001 起递增，2026-01-01T00:00:00Z 起每次 +1.5 秒。 */
function deterministicStore(directory, overrides = {}) {
  let ids = 0;
  let ticks = 0;
  return new ProjectStore({
    directory,
    mediaRoots: [MEDIA_ROOT],
    probe: fakeProbe,
    newId: () => (++ids).toString(16).padStart(32, "0"),
    now: () => new Date(BASE_TIME + 1500 * ticks++),
    ...overrides,
  });
}

/**
 * 夹具里的 "${MEDIA}/a.mp4" 换成本机路径。后半段的 / 换成系统分隔符：Windows 上探测（realpath）
 * 返回的是全反斜杠路径，直接拼会得到 D:\…\media/a.mp4 这种混合分隔符，和存进片段的路径对不上。
 * 不用 path.join，免得把夹具里故意写的 "${MEDIA}/../x" 提前规范化掉。
 */
function expandMedia(value) {
  if (typeof value === "string") return value.startsWith("${MEDIA}") ? MEDIA_ROOT + value.slice("${MEDIA}".length).replaceAll("/", path.sep) : value;
  if (Array.isArray(value)) return value.map(expandMedia);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, expandMedia(item)]));
  return value;
}

async function outcomeOf(store, method, args) {
  if (typeof store[method] !== "function") throw new Error(`ProjectStore has no ${method}()`);
  try {
    return { result: await store[method](...args) };
  } catch (error) {
    if (!(error instanceof EditError)) throw error;
    return { error: { code: error.code, status: error.status, message: error.message, details: error.details } };
  }
}

const fixtureFiles = readdirSync(OPERATIONS).filter((name) => name.endsWith(".json")).sort();

describe("ProjectStore replays JianYingAPI ProjectService", () => {
  it("has operation fixtures for every public method", () => {
    const called = new Set();
    for (const file of fixtureFiles) {
      for (const scenario of JSON.parse(readFileSync(path.join(OPERATIONS, file), "utf8")).scenarios) {
        for (const step of scenario.steps) called.add(step.method);
      }
    }
    const methods = Object.getOwnPropertyNames(ProjectStore.prototype).filter((name) => name !== "constructor");
    // deleteProject、restoreProject、duplicateSegment 是新增的，原仓库没有，下面单独测
    expect(methods.filter((name) => !called.has(name))).toEqual(["deleteProject", "restoreProject", "duplicateSegment"]);
  });

  for (const file of fixtureFiles) {
    const { scenarios } = JSON.parse(readFileSync(path.join(OPERATIONS, file), "utf8"));
    describe(file, () => {
      for (const scenario of scenarios) {
        it(scenario.name, async () => {
          const store = deterministicStore(await makeDirectory());
          for (const [index, step] of scenario.steps.entries()) {
            const actual = await outcomeOf(store, step.method, expandMedia(step.args));
            const expected = "error" in step ? { error: step.error } : { result: expandMedia(step.result) };
            expect(actual, `step ${index} ${step.method}(${JSON.stringify(step.args).slice(0, 200)})`).toEqual(expected);
          }
        });
      }
    });
  }
});

describe("ProjectStore storage", () => {
  it("writes each project atomically to its own private file and reads it back after a restart", async () => {
    const directory = await makeDirectory();
    const store = deterministicStore(directory);
    const project = await store.createProject({ name: "Atomic" });
    const updated = await store.addMarker(project.id, { time_us: 5, label: "m" });
    expect(await readdir(directory)).toEqual([`${project.id}.json`]);
    if (process.platform !== "win32") expect((await stat(path.join(directory, `${project.id}.json`))).mode & 0o777).toBe(0o600);
    expect(JSON.parse(await readFile(path.join(directory, `${project.id}.json`), "utf8"))).toEqual(updated);
    const reopened = new ProjectStore({ directory, mediaRoots: [MEDIA_ROOT], probe: fakeProbe });
    expect(await reopened.getProject(project.id)).toEqual(updated);
    expect(await reopened.listProjects()).toEqual([updated]);
  });

  it("uses random hex ids and pydantic-style UTC times by default", async () => {
    const store = new ProjectStore({ directory: await makeDirectory() });
    const project = await store.createProject({ name: "Defaults" });
    expect(project.id).toMatch(/^[0-9a-f]{32}$/u);
    expect(project.created_at).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{6})?Z$/u);
    const withTrack = await store.createTrack(project.id, { track_type: "video" });
    expect(withTrack.tracks[0].id).toMatch(/^[0-9a-f]{32}$/u);
    expect(withTrack.tracks[0].id).not.toBe(project.id);
  });

  it("serialises concurrent writes to the same project", async () => {
    const store = deterministicStore(await makeDirectory());
    const { id } = await store.createProject({ name: "Busy" });
    const results = await Promise.all(Array.from({ length: 20 }, (_, index) => store.addMarker(id, { time_us: index, label: `m${index}` })));
    expect(results.map((project) => project.revision).sort((a, b) => a - b)).toEqual(Array.from({ length: 20 }, (_, index) => index + 2));
    const final = await store.getProject(id);
    expect(final.revision).toBe(21);
    expect(final.markers.map((marker) => marker.time_us)).toEqual(Array.from({ length: 20 }, (_, index) => index));

    // 带同一个 expectRevision 并发，只有一个能成
    const raced = await Promise.allSettled([0, 1, 2].map((index) => store.updateProject(id, { name: `n${index}` }, { expectRevision: 21 })));
    expect(raced.filter((item) => item.status === "fulfilled")).toHaveLength(1);
    expect(raced.filter((item) => item.status === "rejected").map((item) => [item.reason.code, item.reason.status, item.reason.details]))
      .toEqual([["PROJECT_CONFLICT", 409, { current_revision: 22 }], ["PROJECT_CONFLICT", 409, { current_revision: 22 }]]);
  });

  it("keeps different projects independent while one is busy", async () => {
    const store = deterministicStore(await makeDirectory(), {
      probe: async (raw, options) => {
        await new Promise((resolve) => setTimeout(resolve, 30));
        return fakeProbe(raw, options);
      },
    });
    const first = await store.createProject({ name: "Slow" });
    const second = await store.createProject({ name: "Fast" });
    const { tracks } = await store.createTrack(first.id, { track_type: "video" });
    const order = [];
    await Promise.all([
      store.addMediaSegment(first.id, tracks[0].id, { path: path.join(MEDIA_ROOT, "clip.avi"), timeline: { duration_us: 1000 } }).then(() => order.push("slow")),
      store.addMarker(second.id, { time_us: 1, label: "x" }).then(() => order.push("fast")),
    ]);
    expect(order).toEqual(["fast", "slow"]);
  });

  it("deletes projects", async () => {
    const directory = await makeDirectory();
    const store = deterministicStore(directory);
    const keep = await store.createProject({ name: "Keep" });
    const drop = await store.createProject({ name: "Drop" });
    await expect(store.deleteProject(drop.id, { expectRevision: 2 })).rejects.toMatchObject({ code: "PROJECT_CONFLICT", status: 409 });
    await expect(store.deleteProject(drop.id, { expectRevision: 0 })).rejects.toMatchObject({ code: "VALIDATION_ERROR", status: 422 });
    await store.deleteProject(drop.id, { expectRevision: 1 });
    await expect(store.getProject(drop.id)).rejects.toMatchObject({ code: "PROJECT_NOT_FOUND", status: 404 });
    await expect(store.deleteProject(drop.id)).rejects.toMatchObject({ code: "PROJECT_NOT_FOUND", status: 404 });
    await expect(store.deleteProject("../escape")).rejects.toMatchObject({ code: "PROJECT_NOT_FOUND" });
    expect(await store.listProjects()).toEqual([keep]);
    expect(await readdir(directory)).toEqual([`${keep.id}.json`]);
  });

  it("restores a whole snapshot as the next revision", async () => {
    const directory = await makeDirectory();
    const store = deterministicStore(directory);
    const created = await store.createProject({ name: "原来", canvas: { width: 1080, height: 1920, fps: 30 } });
    const withTrack = await store.createTrack(created.id, { track_type: "text", name: "标题" });
    const before = await store.addTextSegment(created.id, withTrack.tracks[0].id, { text: "周末去海边", timeline: { start_us: 0, duration_us: 1_000_000 } });
    await store.addMarker(created.id, { time_us: 500_000, label: "高潮" });
    const { name, canvas, tracks, markers } = before;
    await store.updateProject(created.id, { name: "改过" });
    await store.setCanvas(created.id, { width: 1920, height: 1080, fps: 60 });
    const current = await store.deleteTrack(created.id, withTrack.tracks[0].id);

    const restored = await store.restoreProject(created.id, { name, canvas, tracks, markers });
    expect(restored).toMatchObject({ id: created.id, name: "原来", canvas: { width: 1080, height: 1920, fps: 30 }, tracks, markers: [], revision: current.revision + 1, draft: null });
    expect(restored.created_at).toBe(created.created_at);
    expect(restored.updated_at).not.toBe(current.updated_at);
    expect(await store.getProject(created.id)).toEqual(restored);
    // 快照里的数组不和存进去的共用
    tracks[0].name = "被改了";
    expect((await store.getProject(created.id)).tracks[0].name).toBe("标题");

    await expect(store.restoreProject(created.id, { name: "", canvas, tracks, markers })).rejects.toMatchObject({ code: "VALIDATION_ERROR", status: 422 });
    await expect(store.restoreProject(created.id, { name, canvas: { width: 0, height: 10, fps: 30 }, tracks, markers })).rejects.toMatchObject({ status: 422 });
    await expect(store.restoreProject(created.id, { name, canvas, tracks: null, markers })).rejects.toMatchObject({ code: "VALIDATION_ERROR" });
    await expect(store.restoreProject("0".repeat(32), { name, canvas, tracks, markers })).rejects.toMatchObject({ code: "PROJECT_NOT_FOUND", status: 404 });
    expect((await store.getProject(created.id)).revision).toBe(restored.revision);
  });

  it("reports unreadable project files and skips them when listing", async () => {
    const directory = await makeDirectory();
    const store = deterministicStore(directory);
    const good = await store.createProject({ name: "Good" });
    const broken = "a".repeat(32);
    const stranger = "b".repeat(32);
    await writeFile(path.join(directory, `${broken}.json`), "{not json");
    await writeFile(path.join(directory, `${stranger}.json`), JSON.stringify({ ...good, id: "c".repeat(32) }));
    await writeFile(path.join(directory, "notes.txt"), "ignored");
    await expect(store.getProject(broken)).rejects.toMatchObject({ code: "STORE_CORRUPT", status: 500 });
    await expect(store.getProject(stranger)).rejects.toMatchObject({ code: "STORE_CORRUPT", status: 500, message: "Stored project identity mismatch" });
    await expect(store.addMarker(broken, { time_us: 1, label: "x" })).rejects.toMatchObject({ code: "STORE_CORRUPT" });
    expect(await store.listProjects()).toEqual([good]);
    expect(await new ProjectStore({ directory: path.join(directory, "missing") }).listProjects()).toEqual([]);
  });

  it("refuses to save a project over the size limit and keeps the last good revision", async () => {
    const directory = await makeDirectory();
    const store = deterministicStore(directory, { maxProjectBytes: 2048 });
    const project = await store.createProject({ name: "Small" });
    await expect(store.addMarker(project.id, { time_us: 1, label: "x".repeat(200) })).resolves.toMatchObject({ revision: 2 });
    const before = await store.getProject(project.id);
    const error = await store.addSrtSubtitles(project.id, Array.from({ length: 40 }, (_, index) => `${index + 1}\n00:00:${String(index).padStart(2, "0")},000 --> 00:00:${String(index).padStart(2, "0")},500\nline ${index}\n`).join("\n")).catch((reason) => reason);
    expect(error).toMatchObject({ code: "PROJECT_TOO_LARGE", status: 413 });
    expect(await store.getProject(project.id)).toEqual(before);
    expect((await readdir(directory)).filter((name) => name.endsWith(".tmp"))).toEqual([]);
  });

  it("cleans up the temporary file when writing or syncing fails", async () => {
    const directory = await makeDirectory();
    const store = deterministicStore(directory);
    const project = await store.createProject({ name: "Disk full" });
    const sample = await open(path.join(directory, "sample"), "w");
    const fileHandle = Object.getPrototypeOf(sample);
    await sample.close();
    await rm(path.join(directory, "sample"));
    const failure = Object.assign(new Error("no space left on device"), { code: "ENOSPC" });
    const attempts = [
      ["writeFile", () => store.addMarker(project.id, { time_us: 1, label: "write" })],
      ["sync", () => store.addMarker(project.id, { time_us: 2, label: "sync" })],
      ["sync", () => store.createProject({ name: "Never stored" })],
    ];
    for (const [method, attempt] of attempts) {
      const spy = vi.spyOn(fileHandle, method).mockRejectedValueOnce(failure);
      try {
        await expect(attempt()).rejects.toBe(failure);
      } finally {
        spy.mockRestore();
      }
    }
    expect(await readdir(directory)).toEqual([`${project.id}.json`]);
    expect(await store.getProject(project.id)).toEqual(project);
  });

  it("strips and checks long runs of whitespace in linear time", async () => {
    const store = deterministicStore(await makeDirectory());
    const { id } = await store.createProject({ name: "Whitespace" });
    const blank = " \t\n\u3000\u2028".repeat(40_000); // 20 万个空白
    const attempts = [
      [() => store.createProject({ name: `a${blank}a` }), "VALIDATION_ERROR"],
      [() => store.createProject({ name: blank }), "VALIDATION_ERROR"],
      [() => store.addMarker(id, { time_us: 0, label: `a${blank}a` }), "VALIDATION_ERROR"],
      [() => store.listProjects({ limit: `${blank}1${blank}x` }), "VALIDATION_ERROR"],
      [() => store.addSrtSubtitles(id, `x${"\n".repeat(200_000)}x`), "SRT_INVALID"],
      [() => store.addSrtSubtitles(id, `00:00:01,000${" ".repeat(200_000)}x --> 00:00:02,000\ncaption`), "SRT_INVALID"],
      [() => store.addSrtSubtitles(id, blank), "SRT_EMPTY"],
      // 先去空白再查大小（同原仓库），全是空白的超大 SRT 仍然报空
      [() => store.addSrtSubtitles(id, " ".repeat(5 * 1024 * 1024)), "SRT_EMPTY"],
    ];
    for (const [index, [attempt, code]] of attempts.entries()) {
      const started = performance.now();
      const error = await attempt().catch((reason) => reason);
      expect(error, `attempt ${index}`).toMatchObject({ code });
      // 只用来抓平方级回退：20 万到 500 万个空白按平方级处理要几十秒以上，线性实现平时 1–40ms。
      // 不卡 100ms 这种紧阈值——并行跑全量测试时同一段代码会被拖到 100–330ms（本机实测）。
      expect(performance.now() - started, `attempt ${index}`).toBeLessThan(2_000);
    }
    expect(await store.createProject({ name: `${blank}trimmed${blank}` })).toMatchObject({ name: "trimmed" });
  });

  it("rejects SRT bodies over 4 MB", async () => {
    const store = deterministicStore(await makeDirectory());
    const { id } = await store.createProject({ name: "Big subtitles" });
    const huge = `00:00:00,000 --> 00:00:01,000\n${"字".repeat(1_400_000)}`;
    await expect(store.addSrtSubtitles(id, huge)).rejects.toMatchObject({ code: "SRT_TOO_LARGE", status: 422 });
  });

  it("hands the rotation of phone videos through to the draft (real probe)", async () => {
    // media-rotation/rot90.mp4：编码 128x72、显示矩阵转 90 度，转正后和 1080x1920 画布同比例
    const roots = [await realpath(path.join(FIXTURES, "media-rotation"))];
    const store = deterministicStore(await makeDirectory(), { probe: undefined, mediaRoots: roots });
    const { id } = await store.createProject({ name: "竖拍", canvas: { width: 1080, height: 1920, fps: 30 } });
    const { tracks } = await store.createTrack(id, { track_type: "video" });
    const clip = path.join(roots[0], "rot90.mp4");
    const project = await store.addMediaSegment(id, tracks[0].id, { path: clip, timeline: { duration_us: 1_000_000 }, fit: "cover" });
    const media = await store.mediaInfos(project);
    expect(media).toEqual([expect.objectContaining({ path: clip, width: 128, height: 72, rotation: 90 })]);
    const content = JSON.parse(serializeDraft({ project, media, ...draftInputs(project) }));
    expect(content.materials.videos.map((item) => [item.width, item.height])).toEqual([[128, 72]]);
    expect(content.tracks[0].segments[0].clip.scale).toEqual({ x: 1, y: 1 });
  });

  it("notices media that became shorter than a clip when collecting draft inputs", async () => {
    let shrink = false;
    const store = deterministicStore(await makeDirectory(), {
      probe: async (raw, options) => {
        const info = await fakeProbe(raw, options);
        return shrink ? { ...info, duration_us: 500_000 } : info;
      },
    });
    const { id } = await store.createProject({ name: "Shrunk" });
    const { tracks } = await store.createTrack(id, { track_type: "video" });
    const clip = path.join(MEDIA_ROOT, "clip.avi");
    const project = await store.addMediaSegment(id, tracks[0].id, { path: clip, timeline: { duration_us: 1_000_000 } });
    expect(await store.mediaInfos(project)).toEqual([{ path: clip, ...MEDIA_INFO["clip.avi"] }]);
    shrink = true;
    await expect(store.mediaInfos(project)).rejects.toMatchObject({ code: "SOURCE_RANGE_INVALID", message: "Media changed since it was added" });
  });

  it("treats undefined fields as not given, like a JSON body would", async () => {
    const store = deterministicStore(await makeDirectory());
    const { id } = await store.createProject({ name: "Undefined", canvas: undefined });
    const { tracks } = await store.createTrack(id, { track_type: "text", name: undefined });
    const project = await store.addTextSegment(id, tracks[0].id, { text: "x", timeline: { duration_us: 10 }, color: undefined });
    const segment = project.tracks[0].segments[0];
    expect(segment.color).toBe("#FFFFFF");
    expect(await store.updateSegment(id, segment.id, { text: undefined, bold: undefined })).toEqual(project);
    expect(await store.updateProject(id, { name: undefined }, { expectRevision: undefined })).toEqual(project);
    await expect(store.moveSegment(id, segment.id, undefined)).rejects.toMatchObject({
      code: "VALIDATION_ERROR",
      details: { errors: [{ path: "start_us", type: "missing", message: "Field required" }] },
    });
  });

  it("passes the media roots to the probe", async () => {
    const calls = [];
    const store = deterministicStore(await makeDirectory(), {
      mediaRoots: ["/a", "/b"],
      maxMediaBytes: 10,
      probe: async (raw, options) => {
        calls.push([raw, options]);
        throw invalid("MEDIA_NOT_FOUND", "Media file does not exist");
      },
    });
    const { id } = await store.createProject({ name: "Roots" });
    const { tracks } = await store.createTrack(id, { track_type: "video" });
    await expect(store.addMediaSegment(id, tracks[0].id, { path: "/a/x.mp4", timeline: { duration_us: 1 } })).rejects.toMatchObject({ code: "MEDIA_NOT_FOUND" });
    expect(calls).toEqual([["/a/x.mp4", { roots: ["/a", "/b"], maxBytes: 10 }]]);
  });
});

describe("ProjectStore moves and copies clips across tracks", () => {
  /** 每种轨道各一条（视频两条）、各放一段，外加一组字幕。 */
  async function everyTrack() {
    const store = deterministicStore(await makeDirectory());
    const { id } = await store.createProject({ name: "Tracks" });
    const track = {};
    for (const [name, type] of [["main", "video"], ["pip", "video"], ["music", "audio"], ["music2", "audio"], ["title", "text"], ["note", "text"], ["sticker", "sticker"], ["sticker2", "sticker"], ["effect", "effect"], ["effect2", "effect"], ["filter", "filter"], ["filter2", "filter"]]) {
      track[name] = (await store.createTrack(id, { track_type: type, name })).tracks.at(-1).id;
    }
    const at = { timeline: { start_us: 0, duration_us: 1_000_000 } };
    await store.addMediaSegment(id, track.main, { path: path.join(MEDIA_ROOT, "clip-720p.mp4"), ...at });
    await store.addMediaSegment(id, track.main, { path: path.join(MEDIA_ROOT, "still.png"), timeline: { start_us: 1_000_000, duration_us: 1_000_000 } });
    await store.addMediaSegment(id, track.music, { path: path.join(MEDIA_ROOT, "tone.aac"), ...at });
    await store.addTextSegment(id, track.title, { text: "标题", ...at });
    await store.addStickerSegment(id, track.sticker, { resource_id: "7001", ...at });
    await store.addTrackEffectSegment(id, track.effect, { effect_type: "_1998", ...at });
    await store.addTrackFilterSegment(id, track.filter, { filter_type: "_1980", ...at });
    const project = await store.addSrtSubtitles(id, "1\n00:00:00,000 --> 00:00:01,000\n第一句");
    const clips = Object.fromEntries(project.tracks.flatMap((item) => item.segments.map((segment) => [`${item.name}:${segment.kind}`, segment.id])));
    const subtitles = project.tracks.find((item) => item.kind === "subtitle");
    return { store, id, track, clips, subtitles };
  }
  const where = (project, clipId) => {
    const track = project.tracks.find((item) => item.segments.some((segment) => segment.id === clipId));
    return track ? { track: track.id, segment: track.segments.find((segment) => segment.id === clipId) } : null;
  };

  it("moves each kind of clip onto another track of its type in one write, and refuses the others", async () => {
    const { store, id, track, clips, subtitles } = await everyTrack();
    const fits = [
      ["main:video", "pip"], ["main:image", "pip"], ["music:audio", "music2"], ["title:text", "note"],
      ["sticker:sticker", "sticker2"], ["effect:track_effect", "effect2"], ["filter:track_filter", "filter2"],
    ];
    for (const [index, [clip, to]] of fits.entries()) {
      const before = await store.getProject(id);
      const start = 5_000_000 + index * 2_000_000;
      const moved = await store.moveSegment(id, clips[clip], start, { trackId: track[to] });
      expect(moved.revision, clip).toBe(before.revision + 1);
      expect(where(moved, clips[clip]), clip).toMatchObject({ track: track[to], segment: { timeline: { start_us: start, duration_us: 1_000_000 } } });
      // 别的设置原样带过去
      const { timeline: _old, ...rest } = where(before, clips[clip]).segment;
      expect(where(moved, clips[clip]).segment).toMatchObject(rest);
    }
    const refused = [
      ["main:video", "music"], ["main:image", "title"], ["music:audio", "pip"], ["title:text", "sticker"],
      ["sticker:sticker", "title"], ["effect:track_effect", "filter"], ["filter:track_filter", "effect"],
    ];
    for (const [clip, to] of refused) {
      await expect(store.moveSegment(id, clips[clip], 30_000_000, { trackId: track[to] }), clip).rejects.toMatchObject({ code: "TRACK_TYPE_MISMATCH", status: 422 });
    }
    // 字幕轨上的不能挪走，别的也不能挪进字幕轨；同一条字幕轨上照旧能挪
    await expect(store.moveSegment(id, clips["Subtitles:text"], 0, { trackId: track.title })).rejects.toMatchObject({ code: "SUBTITLE_CLIP_LOCKED" });
    await expect(store.moveSegment(id, clips["title:text"], 30_000_000, { trackId: subtitles.id })).rejects.toMatchObject({ code: "SUBTITLE_TRACK_LOCKED" });
    expect(where(await store.moveSegment(id, clips["Subtitles:text"], 2_000_000, { trackId: subtitles.id }), clips["Subtitles:text"]).segment.timeline.start_us).toBe(2_000_000);
    // 目标轨道上重叠照旧报错；轨道不存在、版本对不上也是
    await store.addMediaSegment(id, track.main, { path: path.join(MEDIA_ROOT, "clip-720p.mp4"), timeline: { start_us: 0, duration_us: 1_000_000 } });
    await expect(store.moveSegment(id, clips["main:image"], 500_000, { trackId: track.main })).rejects.toMatchObject({ code: "TIMELINE_OVERLAP" });
    await expect(store.moveSegment(id, clips["title:text"], 0, { trackId: "nope" })).rejects.toMatchObject({ code: "TRACK_NOT_FOUND" });
    await expect(store.moveSegment(id, clips["title:text"], 0, { trackId: track.title, expectRevision: 1 })).rejects.toMatchObject({ code: "PROJECT_CONFLICT" });
    // 挪回原轨道：从画中画轨拿走，放进主画面轨
    const back = await store.moveSegment(id, clips["main:image"], 1_000_000, { trackId: track.main });
    expect(where(back, clips["main:image"]).track).toBe(track.main);
    expect(back.tracks.find((item) => item.id === track.pip).segments.map((segment) => segment.id)).toEqual([clips["main:video"]]);
  });

  it("copies a clip with everything on it under new ids, onto its own track or a matching one", async () => {
    const { store, id, track, clips, subtitles } = await everyTrack();
    const video = clips["main:video"];
    await store.addKeyframes(id, video, { keyframes: [{ property: "alpha", offset_us: 0, value: 0 }, { property: "alpha", offset_us: 500_000, value: 1 }] });
    await store.updateSegment(id, video, { transform: { scale_x: 1.5, rotation: 30 }, speed: 2, source: { start_us: 500_000, duration_us: 2_000_000 } });
    await store.addEffect(id, video, { effect_type: "_1998" });
    await store.addFilter(id, video, { filter_type: "_1980", intensity: 40 });
    await store.addAnimation(id, video, { animation_type: "渐显" });
    await store.addTransition(id, video, { transition_type: "叠化" });
    await store.addFade(id, video, { in_duration_us: 100_000, out_duration_us: 100_000 });
    await store.addMask(id, video, { mask_type: "圆形" });
    const original = where(await store.getProject(id), video).segment;

    const { project, segment } = await store.duplicateSegment(id, video, 3_000_000);
    expect(segment.id).not.toBe(video);
    const { id: _id, timeline: _timeline, ...settings } = original;
    expect(where(project, segment.id)).toEqual({ track: track.main, segment: { ...settings, id: segment.id, timeline: { start_us: 3_000_000, duration_us: 1_000_000 } } });
    // 深拷贝：改副本不影响原来那段
    await store.updateSegment(id, segment.id, { transform: { scale_x: 3 } });
    expect(where(await store.getProject(id), video).segment).toEqual(original);
    // 文字连样式一起复制到另一条文字轨
    const copied = await store.duplicateSegment(id, clips["title:text"], 0, { trackId: track.note });
    expect(where(copied.project, copied.segment.id)).toMatchObject({ track: track.note, segment: { kind: "text", text: "标题", font_size: 8 } });

    await expect(store.duplicateSegment(id, video, 500_000)).rejects.toMatchObject({ code: "TIMELINE_OVERLAP" });
    await expect(store.duplicateSegment(id, video, 9_000_000, { trackId: track.music })).rejects.toMatchObject({ code: "TRACK_TYPE_MISMATCH" });
    await expect(store.duplicateSegment(id, clips["Subtitles:text"], 9_000_000)).rejects.toMatchObject({ code: "SUBTITLE_CLIP_LOCKED" });
    await expect(store.duplicateSegment(id, clips["title:text"], 9_000_000, { trackId: subtitles.id })).rejects.toMatchObject({ code: "SUBTITLE_TRACK_LOCKED" });
    await expect(store.duplicateSegment(id, "nope", 0)).rejects.toMatchObject({ code: "SEGMENT_NOT_FOUND" });
    await expect(store.duplicateSegment(id, video, -1)).rejects.toMatchObject({ code: "VALIDATION_ERROR" });
  });
});

describe("ProjectStore keeps sped-up full-length clips inside the media", () => {
  // round(round(D/倍速)×倍速) = D+1 的组合（时长取 media-info.json）
  const cases = [
    ["clip-720p.mp4", "video", 1.8],
    ["fragmented.mp4", "video", 2.1],
    ["tone.aac", "audio", 1.2],
    ["tone.mp3", "audio", 3],
  ];
  const pyRound = (value) => {
    const floor = Math.floor(value);
    const rest = value - floor;
    return rest > 0.5 ? floor + 1 : rest < 0.5 ? floor : floor % 2 === 0 ? floor : floor + 1;
  };

  async function fullLengthClip(name, trackType, speed) {
    const store = deterministicStore(await makeDirectory());
    const { id } = await store.createProject({ name: "Speed" });
    const { tracks } = await store.createTrack(id, { track_type: trackType });
    const full = MEDIA_INFO[name].duration_us;
    const duration = pyRound(full / speed);
    expect(pyRound(duration * speed), "case should round past the media end").toBe(full + 1);
    const project = await store.addMediaSegment(id, tracks[0].id, {
      path: path.join(MEDIA_ROOT, name),
      timeline: { duration_us: duration },
      source: { duration_us: full },
      speed,
    });
    return { store, id, trackId: tracks[0].id, segment: project.tracks[0].segments[0], full, duration };
  }

  for (const [name, trackType, speed] of cases) {
    it(`moves a whole ${name} clip at ${speed}x without touching its source range`, async () => {
      const { store, id, segment, full, duration } = await fullLengthClip(name, trackType, speed);
      const moved = await store.moveSegment(id, segment.id, 1_000_000);
      expect(moved.tracks[0].segments[0]).toMatchObject({
        timeline: { start_us: 1_000_000, duration_us: duration },
        source: { start_us: 0, duration_us: full },
        speed,
      });
      // update_clip 只给开始时间时，工具层会带上原时长；同一时长、同一倍速也只是挪位置
      const updated = await store.updateSegment(id, segment.id, { timeline: { start_us: 2_000_000, duration_us: duration }, speed });
      expect(updated.tracks[0].segments[0]).toMatchObject({
        timeline: { start_us: 2_000_000, duration_us: duration },
        source: { start_us: 0, duration_us: full },
      });
    });
  }

  it("fits the source to the media when a clip is stretched back to full length", async () => {
    const { store, id, segment, full, duration } = await fullLengthClip("clip-720p.mp4", "video", 1.8);
    const shorter = await store.updateSegment(id, segment.id, { timeline: { start_us: 0, duration_us: 1_000_000 } });
    expect(shorter.tracks[0].segments[0].source).toEqual({ start_us: 0, duration_us: 1_800_000 });
    const stretched = await store.updateSegment(id, segment.id, { timeline: { start_us: 0, duration_us: duration } });
    expect(stretched.tracks[0].segments[0].source).toEqual({ start_us: 0, duration_us: full });
    // 真放不下还是照样报错
    await expect(store.updateSegment(id, segment.id, { timeline: { start_us: 0, duration_us: duration + 1 } }))
      .rejects.toMatchObject({ code: "SOURCE_RANGE_INVALID" });
  });

  it("fits the derived source to the media when a clip is added with only a duration and speed", async () => {
    const store = deterministicStore(await makeDirectory());
    const { id } = await store.createProject({ name: "Speed" });
    const { tracks } = await store.createTrack(id, { track_type: "audio" });
    const full = MEDIA_INFO["tone.aac"].duration_us;
    const duration = pyRound(full / 1.2);
    const project = await store.addMediaSegment(id, tracks[0].id, {
      path: path.join(MEDIA_ROOT, "tone.aac"),
      timeline: { duration_us: duration },
      speed: 1.2,
    });
    expect(project.tracks[0].segments[0]).toMatchObject({ source: { start_us: 0, duration_us: full }, timeline: { duration_us: duration } });
    await expect(store.addMediaSegment(id, tracks[0].id, {
      path: path.join(MEDIA_ROOT, "tone.aac"),
      timeline: { start_us: 5_000_000, duration_us: duration + 1 },
      speed: 1.2,
    })).rejects.toMatchObject({ code: "SOURCE_RANGE_INVALID" });
  });
});

describe("parseSrt", () => {
  it("reads Unicode digits like Python's \\d and int()", () => {
    expect(parseSrt("١\n٠٠:٠٠:٠١,٥٠٠ --> 00:00:02,000\nمرحبا")).toEqual([{ start_us: 1_500_000, end_us: 2_000_000, text: "مرحبا" }]);
  });

  it("takes index lines that Python's str.isdigit() accepts", () => {
    for (const index of ["²", "①", "⑴³", "12"]) {
      expect(parseSrt(`${index}\n00:00:01,000 --> 00:00:02,000\nhi`)).toEqual([{ start_us: 1_000_000, end_us: 2_000_000, text: "hi" }]);
    }
    // 时间行只认十进制数字；Unicode 15.0 之后新加的数字原仓库（Python 3.12）也不认
    expect(() => parseSrt("00:00:0²,000 --> 00:00:02,000\nhi")).toThrow("SRT timing is invalid");
    expect(() => parseSrt("00:00:0\u{1CCF1},000 --> 00:00:02,000\nhi")).toThrow("SRT timing is invalid");
    expect(() => parseSrt("\u{1CCF1}\n00:00:01,000 --> 00:00:02,000\nhi")).toThrow("Expected an SRT timing line");
  });
});

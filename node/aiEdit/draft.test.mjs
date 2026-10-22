import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { mkdir, mkdtemp, readdir, readFile, realpath, rm, stat, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

import {
  PLATFORM_PROFILES,
  animationCatalog,
  createDraftFolder,
  draftHashId,
  draftInputs,
  dumpDraftJson,
  editingOptions,
  parseDraftJson,
  serializeDraft,
} from "./draft.mjs";
import { dumps, floatRepr, pyFloat } from "./draft/pyjson.mjs";

// 夹具由 node/scripts/ai-edit/generate-draft-fixtures.py 用原仓库 JianYingAPI 生成，见该脚本文件头。
// 夹具是在 POSIX 上生成的：素材根目录写成 "/AI-Edit/Media"，路径都写成 "${MEDIA}/a/b" 这样的占位，
// 素材名表的键是 casefold 过的 "/ai-edit/media/…"，素材 id 也由这些路径算出。下面的 fill / materialNamesHere /
// fixtureMaterialIds 按当前平台换算成草稿里真正会写出的值，所以 Windows 上（CI 的 windows-latest）也是逐字节对照，
// 而不是跳过。
const FIXTURES = new URL("../fixtures/ai-edit/drafts/", import.meta.url);
const MEDIA_DIR = path.resolve(fileURLToPath(new URL("../fixtures/ai-edit/media/", import.meta.url)));
const index = JSON.parse(readFileSync(new URL("index.json", FIXTURES), "utf8"));
const fixtureText = (name) => readFileSync(new URL(name, FIXTURES), "utf8");
// POSIX 上还是 /AI-Edit/Media；Windows 上落在当前盘符下（如 D:\AI-Edit\Media），和 draft.mjs 里 path.resolve 的结果一致。
const MEDIA_ROOT = path.resolve(index.media_root);

const temporary = [];
afterEach(async () => {
  await Promise.all(temporary.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
});

async function tempDir() {
  const dir = await realpath(await mkdtemp(path.join(os.tmpdir(), "ai-edit-draft-")));
  temporary.push(dir);
  return dir;
}

/** 与 Python 侧一样的假 uuid（face0000…n），比较前两边都按首次出现的顺序重新编号。 */
function fakeIds() {
  let counter = 0;
  return () => {
    counter += 1;
    return `face${counter.toString(16).padStart(28, "0")}`;
  };
}

function normalizeIds(text) {
  const seen = new Map();
  return text.replace(/face[0-9a-f]{28}|FACE[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}/g, (token) => {
    const key = token.toLowerCase().replaceAll("-", "");
    if (!seen.has(key)) seen.set(key, `<uuid-${seen.size + 1}>`);
    return seen.get(key);
  });
}

/**
 * 把 "${NAME}/a/b" 换成当前平台上的真实路径。
 * 默认用 path.join 拼（Windows 上是反斜杠），对应 draft.mjs 用 path.resolve / path.join 写出的素材路径和草稿目录；
 * posix 里列出的占位改用 "/" 拼，对应动画资源路径（原仓库 as_posix()，Windows 上写成 C:/…）。
 * json 为真时按 JSON 字符串转义，用在草稿文本里；为假时原样返回，用在文件系统路径上。
 */
function fill(text, vars, { json = true, posix = [] } = {}) {
  return text.replace(/\$\{([A-Z]+)\}((?:\/[^"/]+)*)/g, (whole, name, tail) => {
    if (!Object.hasOwn(vars, name)) return whole;
    let value = path.join(vars[name], ...tail.split("/").filter(Boolean));
    if (posix.includes(name)) value = value.split(path.sep).join("/");
    return json ? JSON.stringify(value).slice(1, -1) : value;
  });
}

const fillObject = (value, vars) => JSON.parse(fill(JSON.stringify(value), vars));

// 期望的草稿文本里 WORK 只出现在本机动画资源路径上，这类路径一律用 "/"。
const EXPECTED_STYLE = { posix: ["WORK"] };

/** 素材名表的键是 casefold 过的绝对路径（夹具里是 "/ai-edit/media/…"），换到 media 下、按当前平台的写法。 */
function materialNamesHere(names, media) {
  const root = `${index.media_root.toLowerCase()}/`;
  return Object.fromEntries(Object.entries(names).map(([key, name]) => {
    if (!key.startsWith(root)) throw new Error(`material name key outside media_root: ${key}`);
    return [path.join(media, ...key.slice(root.length).split("/")).toLowerCase(), name];
  }));
}

/** 同 draft.mjs 的 cropKey：带裁剪的素材 id 在路径后面再接这一段。 */
const cropSuffix = (crop) => `\0crop\0${dumps(
  Object.fromEntries(Object.entries(crop).map(([key, value]) => [key, typeof value === "number" ? pyFloat(value) : value])),
  { sortKeys: true, separators: [",", ":"] },
)}`;

/**
 * 素材 id 由素材路径算出：draftHashId(casefold(路径)[+裁剪], "material")，夹具里按生成时的 POSIX 路径算。
 * 返回一个函数，把按当前路径算出的素材 id 换回夹具里的值再比较。POSIX 上前后一样，等于原样逐字节比较；
 * Windows 上素材落在盘符下，只有这些 id 跟着变，草稿写错的 id 不在表里，照样比不过。
 */
function fixtureMaterialIds(scenario, vars) {
  const suffixes = new Set([""]);
  for (const track of scenario.project.tracks ?? []) {
    for (const segment of track.segments ?? []) if (segment.crop) suffixes.add(cropSuffix(segment.crop));
  }
  const replacements = new Map();
  for (const info of scenario.media) {
    const generated = info.path.replaceAll("${MEDIA}", index.media_root).toLowerCase();
    const here = fill(info.path, vars, { json: false }).toLowerCase();
    for (const suffix of suffixes) {
      const id = draftHashId(here + suffix, "material");
      const original = draftHashId(generated + suffix, "material");
      if (id !== original) replacements.set(id, original);
    }
  }
  return (text) => text.replace(/\b[0-9a-f]{32}\b/g, (id) => replacements.get(id) ?? id);
}

async function prepare(scenario, { media = MEDIA_ROOT } = {}) {
  const work = scenario.packages.length || scenario.animation_roots.length ? await tempDir() : path.resolve("/unused-work");
  const vars = { MEDIA: media, WORK: work };
  for (const pkg of scenario.packages) {
    const folder = path.join(fill(pkg.root, vars, { json: false }), "animation_resources", pkg.effect_id, pkg.md5);
    await mkdir(folder, { recursive: true });
    await writeFile(path.join(folder, "config.json"), JSON.stringify(pkg.config));
  }
  const project = fillObject(scenario.project, vars);
  const mediaInfos = fillObject(scenario.media, vars);
  return {
    vars,
    project,
    media: mediaInfos,
    options: {
      project,
      media: mediaInfos,
      ...draftInputs(project),
      draftId: scenario.draft_id ?? undefined,
      animationResourceRoots: scenario.animation_roots.map((root) => fill(root, vars, { json: false })),
      newId: fakeIds(),
    },
  };
}

function thrown(callback) {
  try {
    callback();
  } catch (error) {
    return error;
  }
  throw new Error("expected an error");
}

const byName = (name) => index.scenarios.find((scenario) => scenario.name === name);

/** 把场景按 media 这个素材根目录导出一遍，和夹具逐字节对照（路径和由路径算出的素材 id 按当前平台换算）。 */
async function expectMatchesFixture(scenario, media = MEDIA_ROOT) {
  const { project, options, vars } = await prepare(scenario, { media });
  const inputs = draftInputs(project);
  expect(inputs.subtitleTracks.map(([track]) => track.id)).toEqual(scenario.inputs.subtitle_track_ids);
  for (const [track, segments] of inputs.subtitleTracks) expect(segments).toBe(track.segments);
  expect(inputs.materialNames).toEqual(materialNamesHere(scenario.inputs.material_names, media));

  const text = serializeDraft(options);
  const expected = fill(fixtureText(scenario.expected), vars, EXPECTED_STYLE);
  expect(normalizeIds(fixtureMaterialIds(scenario, vars)(text))).toBe(normalizeIds(expected));
  return text;
}

describe("draft serialization matches JianYingAPI", () => {
  for (const scenario of index.scenarios.filter((item) => item.expected)) {
    it(`${scenario.name}: ${scenario.description}`, async () => {
      await expectMatchesFixture(scenario);
    });
  }

  it("still matches when the media root is not the one the fixtures were made with (as on Windows)", async () => {
    // 夹具按 /AI-Edit/Media 生成，Windows 上素材根目录会变成 D:\AI-Edit\Media。这里换一个带空格和中文的根目录，
    // 让 POSIX 上也走一遍换算：路径、素材名表、由路径算出的素材 id 都得跟着换，否则对照不上。
    const media = path.resolve("/剪映 素材/AI Edit");
    for (const scenario of index.scenarios.filter((item) => item.expected)) {
      const text = await expectMatchesFixture(scenario, media);
      if (fixtureText(scenario.expected).includes("${MEDIA}")) {
        expect(text, scenario.name).toContain(JSON.stringify(media + path.sep).slice(1, -1));
        expect(text, scenario.name).not.toContain(index.media_root);
      }
    }
  });

  it("round-trips every fixture through parseDraftJson/dumpDraftJson byte for byte", () => {
    for (const scenario of index.scenarios.filter((item) => item.expected)) {
      const text = fixtureText(scenario.expected);
      expect(dumpDraftJson(parseDraftJson(text), { indent: 4 })).toBe(text);
    }
  });

  it("formats floats like Python repr and hashes ids like _id", () => {
    for (const [value, repr] of index.float_repr) expect(floatRepr(value)).toBe(repr);
    for (const { label, scope, id } of index.hash_ids) expect(draftHashId(label, scope)).toBe(id);
  });

  it("works with real random ids", async () => {
    const { options } = await prepare(byName("video-features"));
    const text = serializeDraft({ ...options, newId: undefined });
    expect(text).not.toMatch(/face0{20}/);
    expect(JSON.parse(text).materials.videos.length).toBeGreaterThan(0);
  });
});

describe("fixed bugs", () => {
  const segmentsOf = (content) => content.tracks.flatMap((track) => track.segments);

  it("cover/stretch fill the canvas when the source is wider than the canvas", async () => {
    const scenario = byName("fit-portrait");
    const { options } = await prepare(scenario);
    const fixed = segmentsOf(JSON.parse(serializeDraft(options)));
    const original = segmentsOf(JSON.parse(fixtureText(scenario.original)));
    // 1280x720 素材放进 1080x1920 画布：画布比 / 素材比 < 1
    const factor = (1080 / 1920) / (1280 / 720);
    expect(fixed[0].clip.scale).toEqual({ x: 1 / factor, y: 1 / factor });
    expect(original[0].clip.scale).toEqual({ x: 1, y: 1 });
    expect(fixed[1].clip.scale).toEqual({ x: 1, y: 1 / factor });
    expect(original[1].clip.scale).toEqual({ x: factor, y: 1 });
    // 素材比画布窄时（横屏画布上的竖屏素材）新旧结果相同
    const landscape = byName("fit-landscape");
    const { options: wide } = await prepare(landscape);
    const narrowFactor = (1920 / 1080) / (720 / 1280);
    const wideFixed = segmentsOf(JSON.parse(serializeDraft(wide)));
    const wideOriginal = segmentsOf(JSON.parse(fixtureText(landscape.original)));
    expect(wideFixed[0].clip.scale).toEqual({ x: narrowFactor, y: narrowFactor });
    expect(wideFixed.map((segment) => segment.clip)).toEqual(wideOriginal.map((segment) => segment.clip));
  });

  it("cover/stretch use the upright size of rotated phone videos; materials keep the coded size", async () => {
    // 手机竖拍常是编码 1280x720 + 显示矩阵转 90 度，剪映渲染时会转正：按转正后的 720x1280 算缩放
    const render = async (name, rotation, canvas = null) => {
      const { options } = await prepare(byName(name));
      if (canvas) options.project.canvas = { ...options.project.canvas, ...canvas };
      const info = options.media.find((item) => item.path.endsWith("clip-720p.mp4"));
      if (rotation !== null) info.rotation = rotation;
      const content = JSON.parse(serializeDraft(options));
      const material = content.materials.videos.find((item) => item.path.endsWith("clip-720p.mp4"));
      const clips = segmentsOf(content).filter((segment) => segment.material_id === material.id).map((segment) => segment.clip.scale);
      return { material, clips };
    };
    // 竖屏画布 1080x1920：转正后比例正好相同，cover/stretch 都是 1（剪映 Mac 11.6 实测 1.0 已铺满）
    const plain = await render("fit-portrait", null);
    const wide = (1080 / 1920) / (1280 / 720);
    expect(plain.clips.slice(0, 2)).toEqual([{ x: 1 / wide, y: 1 / wide }, { x: 1, y: 1 / wide }]);
    for (const rotation of [90, 270]) {
      const turned = await render("fit-portrait", rotation);
      expect(turned.clips.slice(0, 2)).toEqual([{ x: 1, y: 1 }, { x: 1, y: 1 }]);
      expect([turned.material.width, turned.material.height]).toEqual([1280, 720]);
    }
    for (const rotation of [0, 180]) {
      expect((await render("fit-portrait", rotation)).clips).toEqual(plain.clips);
    }
    // 横屏画布 1920x1080：没转的横屏素材正好铺满，转了 90 度的竖拍要放大或横向拉伸
    const landscapeCanvas = { width: 1920, height: 1080 };
    const upright = await render("fit-portrait", 0, landscapeCanvas);
    expect(upright.clips.slice(0, 2)).toEqual([{ x: 1, y: 1 }, { x: 1, y: 1 }]);
    const tall = (1920 / 1080) / (720 / 1280);
    const sideways = await render("fit-portrait", 90, landscapeCanvas);
    expect(sideways.clips.slice(0, 2)).toEqual([{ x: tall, y: tall }, { x: tall, y: 1 }]);
    expect([sideways.material.width, sideways.material.height]).toEqual([1280, 720]);
  });

  it("lists each material once and every clip points at it", async () => {
    const scenario = byName("same-material");
    const { options } = await prepare(scenario);
    const fixed = JSON.parse(serializeDraft(options));
    const original = JSON.parse(fixtureText(scenario.original));
    const identity = (material) => `${material.path}\0${JSON.stringify(material.crop ?? null)}`;
    for (const content of [fixed]) {
      const all = [...content.materials.videos, ...content.materials.audios];
      expect(new Set(all.map(identity)).size).toBe(all.length);
      const ids = new Set(all.map((material) => material.id));
      for (const segment of segmentsOf(content)) expect(ids.has(segment.material_id)).toBe(true);
    }
    expect(fixed.materials.videos).toHaveLength(4);
    expect(fixed.materials.audios).toHaveLength(1);
    expect(original.materials.videos.length).toBeGreaterThan(4);
    expect(original.materials.audios).toHaveLength(2);
  });
});

describe("errors", () => {
  // 原仓库里这几种是未处理的异常（HTTP 500），移植版给出结构化 422
  const ported = {
    "error-duplicate-intro": "DRAFT_FEATURE_INVALID",
    "error-image-too-long": "SOURCE_RANGE_INVALID",
    "error-subtitle-overlap": "TIMELINE_OVERLAP",
  };

  for (const scenario of index.scenarios.filter((item) => item.error && item.name !== "error-duplicate-track-name")) {
    it(`${scenario.name}: ${scenario.description}`, async () => {
      const { options } = await prepare(scenario);
      const error = thrown(() => serializeDraft(options));
      if (scenario.error.code) {
        expect(error).toMatchObject({ code: scenario.error.code, status: 422, message: scenario.error.message });
      } else {
        expect(error).toMatchObject({ code: ported[scenario.name], status: 422 });
        if (scenario.error.type === "ValueError") expect(error.message).toBe(scenario.error.message);
      }
    });
  }

  it("exports tracks that share a name (the original raised NameError)", async () => {
    const scenario = byName("error-duplicate-track-name");
    expect(scenario.error.type).toBe("NameError");
    const { options } = await prepare(scenario);
    const content = JSON.parse(serializeDraft(options));
    expect(content.tracks.map((track) => [track.type, track.name])).toEqual([
      ["video", "主轨"], ["video", "主轨"], ["text", "标题"],
    ]);
    expect(content.tracks[0].segments).toHaveLength(2);
  });

  it("rejects animation packages that leave the configured root", async () => {
    const scenario = byName("animation-resources");
    const { options, vars } = await prepare(scenario);
    const outside = await tempDir();
    const [pkg] = scenario.packages;
    const linked = path.join(fill(pkg.root, vars, { json: false }), "animation_resources", pkg.effect_id, pkg.md5);
    await rm(linked, { recursive: true });
    await mkdir(path.join(outside, "pkg"), { recursive: true });
    await writeFile(path.join(outside, "pkg", "config.json"), JSON.stringify(pkg.config));
    // Windows 上建目录符号链接要管理员或开发者模式，junction 不用；其他平台忽略这个参数
    await symlink(path.join(outside, "pkg"), linked, "junction");
    expect(thrown(() => serializeDraft(options))).toMatchObject({ code: "ANIMATION_RESOURCE_INVALID" });
  });

  it("rejects unknown platforms", async () => {
    const { options } = await prepare(byName("basic"));
    // 原型链上的键名也要是 422，不能变成 TypeError
    for (const platform of ["linux", "toString", "constructor", "__proto__", "hasOwnProperty", null]) {
      expect(thrown(() => serializeDraft({ ...options, platform }))).toMatchObject({ code: "DRAFT_PLATFORM_INVALID", status: 422 });
    }
  });
});

describe("platform profiles", () => {
  it("mac only swaps the platform blocks", async () => {
    const { options } = await prepare(byName("video-features"));
    const windows = JSON.parse(serializeDraft({ ...options, newId: fakeIds() }));
    const mac = JSON.parse(serializeDraft({ ...options, newId: fakeIds(), platform: "mac" }));
    expect(serializeDraft({ ...options, newId: fakeIds(), platform: "darwin" })).toBe(
      serializeDraft({ ...options, newId: fakeIds(), platform: "mac" }),
    );
    expect(mac.platform).toEqual(PLATFORM_PROFILES.mac.platform);
    expect(mac.last_modified_platform).toEqual(PLATFORM_PROFILES.mac.platform);
    expect(mac.platform.os).toBe("mac");
    expect(windows.platform).toEqual(PLATFORM_PROFILES.windows.platform);
    for (const content of [windows, mac]) {
      delete content.platform;
      delete content.last_modified_platform;
    }
    expect(mac).toEqual(windows);
  });

  it("names the main file per platform", () => {
    expect(PLATFORM_PROFILES.windows.contentFile).toBe("draft_content.json");
    expect(PLATFORM_PROFILES.mac.contentFile).toBe("draft_info.json");
  });
});

describe("createDraftFolder", () => {
  const sha256 = (text) => createHash("sha256").update(text).digest("hex");

  for (const expected of index.folder.cases) {
    it(`publishes the same folder as create_draft_folder (${expected.case})`, async () => {
      const { options, vars } = await prepare(byName("basic"));
      const root = await tempDir();
      const result = await createDraftFolder({ ...options, root, draftId: expected.draft_id, owned: expected.owned });
      const filled = { ...vars, DRAFTS: root };
      expect(result.directory).toBe(fill(expected.directory, filled, { json: false }));
      expect((await readdir(result.directory)).sort()).toEqual(expected.files);
      expect(await readdir(root)).toEqual([path.basename(result.directory)]);

      const content = await readFile(result.contentPath, "utf8");
      expect(result.content).toBe(content);
      const toFixtureIds = fixtureMaterialIds(byName("basic"), vars);
      expect(normalizeIds(toFixtureIds(content))).toBe(normalizeIds(fill(expected.content, filled)));
      expect(result.contentHash).toBe(sha256(content));
      const meta = await readFile(path.join(result.directory, "draft_meta_info.json"), "utf8");
      expect(meta).toBe(fill(expected.meta, filled));
      expect(result.size).toBe(Buffer.byteLength(content) + Buffer.byteLength(meta));
      // project.json 是工程快照：字段与原仓库一致，浮点写法跟随传入的工程对象
      const snapshot = await readFile(path.join(result.directory, "project.json"), "utf8");
      expect(JSON.parse(snapshot)).toEqual(JSON.parse(fill(expected.project_json, filled)));
      if (expected.owned) {
        expect(await readFile(path.join(result.directory, ".owned"), "utf8")).toBe(expected.owned_marker);
      }
    });
  }

  it("refuses an existing target and invalid ids like the original", async () => {
    const { options } = await prepare(byName("basic"));
    const root = await tempDir();
    await createDraftFolder({ ...options, root, draftId: "0123456789ABCDEF0123456789ABCDEF" });
    for (const expected of index.folder.errors) {
      const error = await createDraftFolder({ ...options, root, draftId: expected.draft_id }).catch((reason) => reason);
      expect(error).toMatchObject({ code: expected.code, status: expected.status });
    }
    // 失败后不留下临时目录和占位目录
    expect(await readdir(root)).toEqual(["0123456789ABCDEF0123456789ABCDEF"]);
  });

  for (const foreign of [[], ["foreign"]]) {
    it(`refuses when the target appears while staging (${foreign.length ? "non-empty" : "empty"})`, async () => {
      const { options } = await prepare(byName("basic"));
      const root = await tempDir();
      const draftId = "00000000000000000000000000000ABC";
      const target = path.join(root, draftId);
      // materialPath 在开头的存在检查之后、发布之前同步调用：在这里建好同名目录，确定地落在暂存期间
      let made = false;
      const materialPath = (info) => {
        if (!made) {
          made = true;
          mkdirSync(target);
          for (const name of foreign) writeFileSync(path.join(target, name), "x");
        }
        return path.resolve(info.path);
      };
      const error = await createDraftFolder({ ...options, root, draftId, materialPath }).catch((reason) => reason);
      expect(made).toBe(true);
      expect(error).toMatchObject({ code: "DRAFT_EXISTS", status: 409 });
      // 空目录也不能被 rename 覆盖掉；暂存和占位目录都清理干净
      expect(await readdir(root)).toEqual([draftId]);
      expect(await readdir(target)).toEqual(foreign);
    });
  }

  it("writes draft_info.json on mac and counts real material sizes", async () => {
    const scenario = byName("basic");
    const { options } = await prepare(scenario, { media: MEDIA_DIR });
    const root = await tempDir();
    const result = await createDraftFolder({ ...options, root, draftId: "ABCDEF0123456789ABCDEF0123456789", platform: "mac" });
    expect((await readdir(result.directory)).sort()).toEqual([".owned", "draft_info.json", "draft_meta_info.json", "project.json"]);
    expect(result.contentPath).toBe(path.join(result.directory, "draft_info.json"));
    const content = JSON.parse(result.content);
    expect(content.platform.os).toBe("mac");
    const meta = JSON.parse(await readFile(path.join(result.directory, "draft_meta_info.json"), "utf8"));
    let size = 0;
    for (const info of options.media) size += (await stat(info.path)).size;
    expect(meta.draft_timeline_materials_size_).toBe(size);
    expect(meta.draft_fold_path).toBe(result.directory);
    expect(meta.draft_root_path).toBe(root);
    expect(meta.tm_duration).toBe(content.duration);
    expect(content.materials.videos[0].path.startsWith(MEDIA_DIR + path.sep)).toBe(true);
  });

  it("names the folder after folderName when given and keeps meta in step", async () => {
    const { options } = await prepare(byName("basic"));
    const root = await tempDir();
    const draftId = "ABCDEF01-2345-6789-ABCD-EF0123456789";
    const plain = await createDraftFolder({ ...options, root: await tempDir(), draftId, platform: "mac" });
    const result = await createDraftFolder({ ...options, root, draftId, platform: "mac", folderName: "海边 小片 (2)" });
    expect(result.directory).toBe(path.join(root, "海边 小片 (2)"));
    expect(await readdir(root)).toEqual(["海边 小片 (2)"]);
    const meta = JSON.parse(await readFile(path.join(result.directory, "draft_meta_info.json"), "utf8"));
    expect(meta.draft_fold_path).toBe(result.directory);
    expect(meta.draft_root_path).toBe(root);
    expect(meta.draft_id).toBe(draftId);
    // 只换目录名，草稿内容（含 id）和按 id 命名时一样。
    expect(normalizeIds(result.content)).toBe(normalizeIds(plain.content));
    expect(JSON.parse(result.content).id).toBe(draftId);
    expect(path.basename(plain.directory)).toBe("ABCDEF0123456789ABCDEF0123456789");
    const again = await createDraftFolder({ ...options, root, draftId, folderName: "海边 小片 (2)" }).catch((reason) => reason);
    expect(again).toMatchObject({ code: "DRAFT_EXISTS", status: 409 });
    for (const folderName of ["", ".", "..", "a/b", "a\\b", "a\0b"]) {
      const error = await createDraftFolder({ ...options, root, draftId, folderName }).catch((reason) => reason);
      expect(error, JSON.stringify(folderName)).toMatchObject({ code: "PATH_UNSAFE", status: 422 });
    }
    expect(await readdir(root)).toEqual(["海边 小片 (2)"]);
  });

  it("lets the caller rewrite material paths without changing material ids", async () => {
    const { options } = await prepare(byName("same-material"));
    const plain = JSON.parse(serializeDraft({ ...options, newId: fakeIds() }));
    const moved = JSON.parse(serializeDraft({
      ...options,
      newId: fakeIds(),
      materialPath: (info) => `/Drafts/X/materials/${path.basename(info.path)}`,
    }));
    const all = (content) => [...content.materials.videos, ...content.materials.audios];
    expect(all(moved).map((item) => item.id)).toEqual(all(plain).map((item) => item.id));
    expect(all(moved).every((item) => item.path.startsWith("/Drafts/X/materials/"))).toBe(true);
  });
});

describe("catalog helpers", () => {
  it("editingOptions matches editing_options()", () => {
    expect(editingOptions()).toEqual(index.editing_options);
  });

  it("animationCatalog matches animation_catalog()", () => {
    for (const { args, result } of index.animation_catalog) {
      const { vip_only: vipOnly, ...rest } = args;
      expect(animationCatalog({ ...rest, ...(vipOnly === undefined ? {} : { vipOnly }) })).toEqual(result);
    }
  });
});

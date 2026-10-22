import { execFile, execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  cpSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BOX_PROFILES,
  cachedProbe,
  createDraftBox,
  discoverDraftBox,
  internals,
  isOwnedDraftDirectory,
  materialFileName,
  newIndexRecord,
  OWNER_MARKER,
  parseRegQueryOutput,
  readIniValue,
  utcTimestampText,
} from "./draftBox.mjs";
import { EditError } from "./errors.mjs";

// 默认的剪映进程检测、读注册表都起子进程：换成可观察的替身（默认照常调真的），用来确认它们是异步的、有缓存。
vi.mock("node:child_process", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, execFile: vi.fn(actual.execFile), execFileSync: vi.fn(actual.execFileSync) };
});

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(HERE, "..", "fixtures", "ai-edit");
const BOX_FIXTURES = path.join(FIXTURES, "draft-box");
// 同草稿箱代码用系统的 realpath：Windows CI 的临时目录是 8.3 短名（RUNNER~1），JS 版 realpathSync 不展开。
const MEDIA = realpathSync.native(path.join(FIXTURES, "media"));
const ORACLE = JSON.parse(readFileSync(path.join(BOX_FIXTURES, "oracle.json"), "utf8"));
const CASES = JSON.parse(readFileSync(path.join(BOX_FIXTURES, "index-cases.json"), "utf8"));
const MAC_INDEX_SAMPLE = readFileSync(path.join(BOX_FIXTURES, "mac-root_meta_info.json"), "utf8");
const HAS_REAL_DRAFT = existsSync(path.join(HERE, "draft.mjs"));
const PLATFORMS = ["windows", "mac"];
const FIXED_ID = "870F8B67-3D2F-441C-BDE0-760C8529A2DE";

const temporaryDirectories = [];
afterEach(async () => {
  vi.restoreAllMocks();
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })));
});

async function makeTemp() {
  const directory = realpathSync.native(await mkdtemp(path.join(tmpdir(), "draft-box-")));
  temporaryDirectories.push(directory);
  return directory;
}

function substitute(value, replacements) {
  if (typeof value === "string") {
    return Object.entries(replacements).reduce((text, [from, to]) => text.split(from).join(to), value);
  }
  if (Array.isArray(value)) return value.map((item) => substitute(item, replacements));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, substitute(item, replacements)]));
  }
  return value;
}

/**
 * 夹具里的路径写成 "${BASE}/drafts/x"（oracle 是在 Mac 上生成的）：换成本机路径，Windows 上分隔符换成反斜杠。
 * json 为 true 时目标是 JSON 文本，路径还要转义，否则反斜杠拼进去就成了非法转义。
 */
function localize(value, roots, { json = false } = {}) {
  if (typeof value === "string") {
    return value.replace(/\$\{(\w+)\}((?:(?!\$\{)[^"])*)/g, (whole, key, rest) => {
      if (!Object.hasOwn(roots, key)) return whole;
      const local = roots[key] + rest.split("/").join(path.sep);
      return json ? JSON.stringify(local).slice(1, -1) : local;
    });
  }
  if (Array.isArray(value)) return value.map((item) => localize(item, roots, { json }));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, localize(item, roots, { json })]));
  }
  return value;
}

function catchSync(callback) {
  try {
    callback();
  } catch (error) {
    return error;
  }
  throw new Error("expected an error");
}

async function catchAsync(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error("expected an error");
}

function expectEdit(error, code, status) {
  expect(error).toBeInstanceOf(EditError);
  expect({ code: error.code, status: error.status }).toEqual({ code, status });
}

function defaultDrafts(root) {
  return [
    {
      draft_id: "DRAFT_A",
      draft_name: "Alpha",
      draft_fold_path: path.join(root, "Alpha"),
      draft_root_path: root,
      tm_draft_create: 1_700_000_000_000_000,
      tm_draft_modified: 1_700_000_002_000_000,
      tm_duration: 2_500_000,
      draft_timeline_materials_size: 4096,
      draft_is_invisible: false,
      draft_is_cloud_temp_draft: false,
    },
    {
      draft_id: "DRAFT_B",
      draft_name: "Beta",
      draft_fold_path: path.join(root, "Beta"),
      draft_root_path: root,
      tm_draft_create: 1_700_000_000_000_000,
      tm_draft_modified: 1_700_000_002_000_000,
      tm_duration: 1_000_000,
      draft_timeline_materials_size: 1024,
    },
    {
      draft_id: "DRAFT_C",
      draft_name: "Gamma",
      draft_fold_path: path.join(root, "Gamma"),
      draft_root_path: root,
      tm_draft_create: 1_700_000_001_000_000,
      tm_draft_modified: 1_700_000_003_000_000,
      tm_duration: 0,
      draft_timeline_materials_size: 0,
    },
  ];
}

/** 同原仓库测试的 write_index：草稿根目录 + 旁边 index/ 目录里的索引。 */
function writeIndex(root, { indexParent, drafts, extra = {} } = {}) {
  mkdirSync(root, { recursive: true });
  const indexDirectory = indexParent ?? path.join(path.dirname(root), "index");
  mkdirSync(indexDirectory, { recursive: true });
  const indexPath = path.join(indexDirectory, "root_meta_info.json");
  writeFileSync(
    indexPath,
    JSON.stringify({ root_path: indexDirectory, all_draft_store: drafts ?? defaultDrafts(root), draft_ids: [], ...extra }),
  );
  return indexPath;
}

/** 原仓库测试的 box 夹具：Alpha（有主文件和 meta）、Beta、Gamma 三个草稿。 */
async function makeBox(platform, options = {}) {
  const base = await makeTemp();
  const root = path.join(base, "drafts");
  const indexPath = writeIndex(root);
  const mainFile = BOX_PROFILES[platform].mainFile;
  mkdirSync(path.join(root, "Alpha"));
  writeFileSync(path.join(root, "Alpha", mainFile), "{}");
  writeFileSync(path.join(root, "Alpha", "draft_meta_info.json"), "{}");
  mkdirSync(path.join(root, "Beta"));
  mkdirSync(path.join(root, "Gamma"));
  const box = createDraftBox({
    platform,
    indexPath,
    draftsRoot: root,
    draftsRootSource: "environment",
    isJianyingRunning: () => false,
    draftModule: fakeDraftModule(),
    ...options,
  });
  return { box, root, indexPath, base, mainFile };
}

// ---------------------------------------------------------------- 草稿模块替身

/**
 * draft.mjs（模块 A）的最小替身：按约定签名发布 <folderName 或 32 位 id>/ 目录，主文件内容只含草稿箱要读的字段，
 * 时间与时长的算法同原仓库（create_time = 工程创建时间的整秒，duration = 片段最晚结束点）。
 */
function fakeDraftModule({ honorMaterialPath = true } = {}) {
  return {
    draftInputs() {
      return { subtitleTracks: [], materialNames: {} };
    },
    async createDraftFolder({ project, root, draftId, media = [], owned = true, platform = "windows", materialPath = null, folderName = null }) {
      const folder = draftId.replace(/-/g, "").toUpperCase();
      const dashed = `${folder.slice(0, 8)}-${folder.slice(8, 12)}-${folder.slice(12, 16)}-${folder.slice(16, 20)}-${folder.slice(20)}`;
      const target = path.join(root, folderName ?? folder);
      if (existsSync(target)) throw new EditError("DRAFT_EXISTS", "Draft folder already exists", { status: 409 });
      const mainFile = platform === "mac" ? "draft_info.json" : "draft_content.json";
      const temporary = path.join(root, `.${folder}.fake.tmp`);
      mkdirSync(temporary);
      const segments = (project.tracks ?? []).flatMap((track) => track.segments ?? []);
      const duration = Math.max(0, ...segments.map((segment) => segment.timeline.start_us + segment.timeline.duration_us));
      const filePath = (info) => (honorMaterialPath && materialPath ? materialPath(info) : path.resolve(info.path));
      const content = {
        id: dashed,
        name: project.name,
        create_time: Math.floor(Date.parse(project.created_at) / 1000),
        update_time: Math.floor(Date.parse(project.updated_at) / 1000),
        duration,
        materials: {
          videos: media.filter((info) => info.kind !== "audio").map((info) => ({ path: filePath(info), material_name: path.basename(info.path) })),
          audios: media.filter((info) => info.kind === "audio").map((info) => ({ path: filePath(info), name: path.basename(info.path) })),
        },
        platform: { os: platform },
      };
      const text = JSON.stringify(content, null, 4);
      writeFileSync(path.join(temporary, mainFile), text);
      writeFileSync(
        path.join(temporary, "draft_meta_info.json"),
        JSON.stringify(
          {
            draft_id: dashed,
            draft_name: project.name,
            draft_fold_path: target,
            draft_root_path: root,
            draft_timeline_materials_size_: media.reduce((total, info) => total + (existsSync(info.path) ? statSync(info.path).size : 0), 0),
            tm_draft_create: content.create_time * 1_000_000,
            tm_draft_modified: content.update_time * 1_000_000,
            tm_duration: duration,
          },
          null,
          2,
        ),
      );
      writeFileSync(path.join(temporary, "project.json"), JSON.stringify(project, null, 2));
      if (owned) writeFileSync(path.join(temporary, ".owned"), String(project.id));
      renameSync(temporary, target);
      return {
        directory: target,
        contentPath: path.join(target, mainFile),
        content: text,
        contentHash: createHash("sha256").update(text).digest("hex"),
        size: text.length,
      };
    },
  };
}

function makeProject(overrides = {}) {
  return {
    name: "Native new draft",
    canvas: { width: 1920, height: 1080, fps: 30 },
    id: "0123456789abcdef0123456789abcdef",
    created_at: "2026-10-19T07:00:00.000000Z",
    updated_at: "2026-10-19T07:00:05.000000Z",
    revision: 3,
    tracks: [
      {
        id: "track-text",
        track_type: "text",
        name: "Text",
        mute: false,
        segments: [{ id: "segment-text", kind: "text", text: "Preserved text", timeline: { start_us: 0, duration_us: 1_000_000 } }],
      },
    ],
    markers: [],
    draft: null,
    ...overrides,
  };
}

/** 原仓库测试里的 IncompleteProject / Project：只有 name 和 id 的工程。 */
function bareProject(id, name) {
  return { id, name };
}

function writeOwnedBuild(target, draftId, { projectId, mainFile, content } = {}) {
  mkdirSync(target);
  writeFileSync(
    path.join(target, OWNER_MARKER),
    JSON.stringify({ draft_id: draftId, ...(projectId ? { project_id: projectId } : {}), generator: "jianying-ai-service" }),
  );
  for (const name of [mainFile, "draft_meta_info.json", "project.json"]) writeFileSync(path.join(target, name), "{}");
  return content;
}

const listNames = (directory) => readdirSync(directory).sort();

/** 草稿名一律等于文件夹名：主文件 name、meta 的 draft_name、索引条目的 draft_name 三处都是。 */
function expectNamedAfterFolder(directory, indexPath, draftId, mainFile) {
  const folder = path.basename(directory);
  expect(JSON.parse(readFileSync(path.join(directory, mainFile), "utf8")).name).toBe(folder);
  expect(JSON.parse(readFileSync(path.join(directory, "draft_meta_info.json"), "utf8")).draft_name).toBe(folder);
  const entry = JSON.parse(readFileSync(indexPath, "utf8")).all_draft_store.find((item) => item.draft_id === draftId);
  expect(entry.draft_name).toBe(folder);
  return folder;
}

// ---------------------------------------------------------------- 原仓库测试：读草稿箱

describe.each(PLATFORMS)("draft box reading (%s)", (platform) => {
  it("status and list are bounded with create support", async () => {
    const { box, root, mainFile } = await makeBox(platform);
    const status = await box.status();
    expect(status.status).toBe("available");
    expect(status.read_only).toBe(false);
    expect(status.operations.create).toBe("available");
    expect(status.operations.write_support).toBe("create_and_repair_owned");
    expect(status.operations.repair).toBe("owned_draft_only");
    expect(status.operations.edit).toBe("unsupported_encrypted");
    expect(status.draft_count).toBe(3);
    expect(status.timeline_content).toBe("unsupported_encrypted");
    expect(status.platform).toBe(platform);
    expect(status.jianyingRunning).toBe(false);

    const listed = await box.listDrafts({ limit: 2, offset: 1 });
    expect(listed.total).toBe(3);
    expect(listed.items.map((item) => item.id)).toEqual(["DRAFT_A", "DRAFT_B"]);
    expect((await box.getDraft("DRAFT_A")).draft.files_present).toEqual({ [mainFile]: true, "draft_meta_info.json": true });
    expectEdit(await catchAsync(box.listDrafts({ limit: 201 })), "DRAFT_BOX_PAGINATION_INVALID", 422);
    expect(await box.status()).toEqual(status);
    expectEdit(await catchAsync(box.getDraft(path.basename(root))), "DRAFT_BOX_NOT_FOUND", 404);
    expect(readFileSync(path.join(root, "Alpha", mainFile), "utf8")).toBe("{}");
  });

  it("sorts stably, searches and returns detail without raw index fields", async () => {
    const { box } = await makeBox(platform);
    const first = (await box.listDrafts()).items;
    const second = (await box.listDrafts()).items;
    expect(first.map((item) => item.id)).toEqual(["DRAFT_C", "DRAFT_A", "DRAFT_B"]);
    expect(first).toEqual(second);
    expect((await box.listDrafts({ query: "alpha" })).items.map((item) => item.id)).toEqual(["DRAFT_A"]);
    const detail = (await box.getDraft("DRAFT_A")).draft;
    expect(detail.name).toBe("Alpha");
    expect(detail.created_at).toBe("2023-11-15T06:13:20+08:00");
    expect(detail.duration_us).toBe(2_500_000);
    expect(detail.timeline_content).toEqual({ status: "unsupported", reason: "encrypted" });
    expect(detail).not.toHaveProperty("draft_is_cloud_temp_draft");
    expect(detail).not.toHaveProperty("cloud_user_id");
    // 原仓库 MCP 工具与 HTTP 共用同一服务：这里直接调同一对象。
    expect((await box.listDrafts({ query: "beta" })).items.map((item) => item.id)).toEqual(["DRAFT_B"]);
    expect((await box.getDraft("DRAFT_B")).draft.name).toBe("Beta");
  });

  it("rejects invalid ids and reports missing ones", async () => {
    const { box } = await makeBox(platform);
    expectEdit(await catchAsync(box.getDraft("NOT_FOUND")), "DRAFT_BOX_NOT_FOUND", 404);
    for (const draftId of ["..", "bad\0id", "<script>", "", 7, null]) {
      expectEdit(await catchAsync(box.getDraft(draftId)), "DRAFT_BOX_INVALID_ID", 422);
    }
  });

  it("reports unavailable, corrupt and out-of-bounds indexes", async () => {
    const base = await makeTemp();
    const root = path.join(base, "drafts");
    mkdirSync(root);
    // 索引所在目录都不存在：剪映没装或没打开过。
    const missing = createDraftBox({ platform, indexPath: path.join(base, "absent", "root_meta_info.json"), draftsRoot: root, isJianyingRunning: () => false });
    expect((await missing.status()).reason).toBe("index_not_found");
    expect((await missing.status()).status).toBe("unavailable");
    expectEdit(await catchAsync(missing.listDrafts()), "DRAFT_BOX_UNAVAILABLE", 404);

    const broken = path.join(base, "broken.json");
    writeFileSync(broken, "{");
    const brokenBox = createDraftBox({ platform, indexPath: broken, draftsRoot: root, isJianyingRunning: () => false });
    expectEdit(await catchAsync(brokenBox.status()), "DRAFT_BOX_INDEX_INVALID", 500);
    expectEdit(await catchAsync(brokenBox.listDrafts()), "DRAFT_BOX_INDEX_INVALID", 500);

    const outsideDraft = path.join(base, "outside", "DRAFT_OUT");
    mkdirSync(outsideDraft, { recursive: true });
    const index = writeIndex(root, {
      drafts: [{ draft_id: "DRAFT_OUT", draft_name: "Outside", draft_fold_path: outsideDraft, draft_root_path: root }],
    });
    const body = await createDraftBox({ platform, indexPath: index, draftsRoot: root, isJianyingRunning: () => false }).listDrafts();
    expect(body.items).toEqual([]);
    expect(body.index.skipped_count).toBe(1);
  });

  // Windows 上 chmod 只能设只读属性，文件照样读得到；root 什么都读得到。
  it.skipIf(process.platform === "win32" || process.getuid?.() === 0)("does not leak OS details when the index cannot be read", async () => {
    const base = await makeTemp();
    const index = path.join(base, "root_meta_info.json");
    writeFileSync(index, "{}");
    const root = path.join(base, "drafts");
    mkdirSync(root);
    chmodSync(index, 0o000);
    try {
      const box = createDraftBox({ platform, indexPath: index, draftsRoot: root, isJianyingRunning: () => false });
      const status = await box.status();
      expect(status.status).toBe("unavailable");
      expect(status.reason).toBe("index_permission_denied");
      const error = await catchAsync(box.listDrafts());
      expectEdit(error, "DRAFT_BOX_INDEX_UNREADABLE", 503);
      expect(JSON.stringify(error)).not.toContain(base);
      expect(error.message).not.toContain(base);
    } finally {
      chmodSync(index, 0o600);
    }
  });

  it("parses ISO timestamps and skips symlinked draft folders", async () => {
    const base = await makeTemp();
    const root = path.join(base, "drafts");
    const outside = path.join(base, "outside");
    mkdirSync(root);
    mkdirSync(outside);
    writeFileSync(path.join(outside, "draft_content.json"), "{}");
    symlinkSync(outside, path.join(root, "DRAFT_LINK"), "dir");
    const record = {
      draft_id: "DRAFT_LINK",
      draft_name: "Linked",
      draft_fold_path: path.join(root, "DRAFT_LINK"),
      draft_root_path: root,
      tm_draft_modified: "2026-10-18 22:34:42",
    };
    const index = writeIndex(root, { drafts: [record] });
    expect((await createDraftBox({ platform, indexPath: index, draftsRoot: root, isJianyingRunning: () => false }).listDrafts()).items).toEqual([]);

    unlinkSync(path.join(root, "DRAFT_LINK"));
    mkdirSync(path.join(root, "DRAFT_LINK"));
    const recordIndex = writeIndex(root, { drafts: [record] });
    const detail = (await createDraftBox({ platform, indexPath: recordIndex, draftsRoot: root, isJianyingRunning: () => false }).getDraft("DRAFT_LINK")).draft;
    expect(detail.modified_at).toBe("2026-10-18T22:34:42+08:00");
  });
});

// ---------------------------------------------------------------- 原仓库测试：新建与修复

function addNativeTimeline(target, mainFile) {
  const mainId = "4D2B6B0E-1C3A-4F5B-9D7E-2A1B3C4D5E6F";
  const timelines = path.join(target, "Timelines");
  const main = path.join(timelines, mainId, mainFile);
  const manifest = { main_timeline_id: mainId, timelines: [{ id: mainId }] };
  mkdirSync(path.dirname(main), { recursive: true });
  writeFileSync(main, "opaque native main timeline");
  writeFileSync(`${main}.bak`, "opaque native main timeline backup");
  writeFileSync(path.join(timelines, "project.json"), JSON.stringify(manifest));
  mkdirSync(path.join(timelines, "other-timeline"));
  writeFileSync(path.join(timelines, "other-timeline", mainFile), "keep other timeline");
  writeFileSync(path.join(path.dirname(main), "native-extra.bin"), "keep timeline auxiliary");
  return { mainId, timelines, main, manifest };
}

describe.each(PLATFORMS.flatMap((platform) => [[platform, false], [platform, true]]))(
  "create then repair in place (%s, native timeline: %s)",
  (platform, withNativeTimeline) => {
    it("writes, lists, survives failed repairs and repairs in place", async () => {
      const base = await makeTemp();
      const root = path.join(base, "drafts");
      const indexPath = writeIndex(root, { indexParent: path.join(base, "index") });
      const mainFile = BOX_PROFILES[platform].mainFile;
      mkdirSync(path.join(root, "Alpha"));
      writeFileSync(path.join(root, "Alpha", mainFile), "{}");
      writeFileSync(path.join(root, "Alpha", "draft_meta_info.json"), "{}");
      mkdirSync(path.join(root, "Beta"));
      mkdirSync(path.join(root, "Gamma"));
      const indexDocument = JSON.parse(readFileSync(indexPath, "utf8"));
      indexDocument.draft_ids = 0;
      indexDocument.untouched = { keep: ["all", "original", "fields"] };
      writeFileSync(indexPath, JSON.stringify(indexDocument));
      const originalBytes = readFileSync(indexPath);
      const beforeRecords = indexDocument.all_draft_store.map((item) => item.draft_id);

      const draftModule = fakeDraftModule();
      const box = createDraftBox({ platform, indexPath, draftsRoot: root, draftsRootSource: "test", isJianyingRunning: () => false, draftModule });
      const project = makeProject();
      const body = await box.createDraft({ project, media: [] });
      expect(body.status).toBe("written");
      expect(body.source).toBe("jianying_index");
      expect(body.read_only).toBe(false);
      expect(body.jianying_ui_verified).toBe(false);
      const draftId = body.id;
      expect(body.draftId).toBe(draftId);
      expect(draftId).toMatch(/^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$/);
      expect(body.name).toBe("Native new draft");
      expect(body.draftName).toBe("Native new draft");
      const target = body.path;
      // 文件夹按草稿名命名（剪映自己就这样），不再是去横线的草稿 id。
      expect(target).toBe(path.join(root, "Native new draft"));
      const content = JSON.parse(readFileSync(path.join(target, mainFile), "utf8"));
      const metadata = JSON.parse(readFileSync(path.join(target, "draft_meta_info.json"), "utf8"));
      expect(content.id).toBe(draftId);
      expect(metadata.draft_id).toBe(draftId);
      expect(content.name).toBe("Native new draft");
      expect(existsSync(path.join(target, "project.json"))).toBe(true);
      expect(existsSync(path.join(target, OWNER_MARKER))).toBe(true);
      expect(body.draft.draft_root_path).toBe(root);
      expect(body.draft.draft_fold_path).toBe(target);
      expect(body.draft.draft_json_file).toBe(path.join(target, mainFile));
      expect(body.draft.draft_cover).toBe(platform === "mac" ? path.join(target, "draft_cover.jpg") : "");
      expect(metadata.draft_fold_path).toBe(target);

      const updatedIndex = JSON.parse(readFileSync(indexPath, "utf8"));
      expect(updatedIndex.untouched).toEqual({ keep: ["all", "original", "fields"] });
      let added;
      if (platform === "mac") {
        // 剪映 Mac：新条目在最前，draft_ids 是只增不减的计数（原值和条目数取大再加一）。
        expect(updatedIndex.draft_ids).toBe(4);
        expect(updatedIndex.all_draft_store.slice(1).map((item) => item.draft_id)).toEqual(beforeRecords);
        added = updatedIndex.all_draft_store[0];
        expect(added.tm_draft_cloud_parent_entry_id).toBe(-1);
        expect(added).not.toHaveProperty("tm_draft_parent_entry_id");
      } else {
        expect(updatedIndex.draft_ids).toBe(0);
        expect(updatedIndex.all_draft_store.slice(0, -1).map((item) => item.draft_id)).toEqual(beforeRecords);
        added = updatedIndex.all_draft_store.at(-1);
        expect(added.tm_draft_parent_entry_id).toBe(-1);
      }
      expect(added.draft_id).toBe(draftId);
      expect(added.draft_name).toBe("Native new draft");
      expect(added.tm_draft_cloud_entry_id).toBe(-1);
      expect(added.tm_draft_cloud_space_id).toBe(-1);
      expect(added.tm_draft_cloud_user_id).toBe(-1);
      expect(added.draft_is_invisible).toBe(false);
      expect(added.streaming_edit_draft_ready).toBe(true);
      expect(Object.keys(added).filter((key) => !key.startsWith("tm_draft_cloud_"))).not.toContain("cloud_user_id");
      expect(readFileSync(indexPath).equals(originalBytes)).toBe(false);

      expect((await box.listDrafts({ query: "Native new draft" })).items.map((item) => item.id)).toEqual([draftId]);
      const detail = (await box.getDraft(draftId)).draft;
      expect(detail.name).toBe("Native new draft");
      expect(detail.files_present).toEqual({ [mainFile]: true, "draft_meta_info.json": true });
      expect((await box.status()).operations.write_support).toBe("create_and_repair_owned");

      const oldContent = readFileSync(path.join(target, mainFile));
      const auxiliary = path.join(target, "custom-extra.bin");
      writeFileSync(auxiliary, "keep auxiliary data");
      const native = withNativeTimeline ? addNativeTimeline(target, mainFile) : null;
      const indexWithExtra = JSON.parse(readFileSync(indexPath, "utf8"));
      indexWithExtra.all_draft_store.find((item) => item.draft_id === draftId).future_jianying_field = { keep: true };
      writeFileSync(indexPath, JSON.stringify(indexWithExtra));
      const oldIndex = readFileSync(indexPath);
      const oldCreatedAt = added.tm_draft_create;
      const renamed = makeProject({ name: "Native repaired draft", updated_at: "2026-10-19T08:00:00.000000Z", revision: 4 });

      const assertUntouched = () => {
        expect(readFileSync(path.join(target, mainFile)).equals(oldContent)).toBe(true);
        expect(readFileSync(auxiliary, "utf8")).toBe("keep auxiliary data");
        expect(readFileSync(indexPath).equals(oldIndex)).toBe(true);
        if (native) {
          expect(readFileSync(native.main, "utf8")).toBe("opaque native main timeline");
          expect(readFileSync(`${native.main}.bak`, "utf8")).toBe("opaque native main timeline backup");
          expect(existsSync(path.join(native.timelines, ".jianying_ai_repair_backups"))).toBe(false);
        }
        expect(readdirSync(root).filter((name) => name.endsWith(".repair") || name.endsWith(".backup"))).toEqual([]);
      };

      // 建草稿失败：原草稿、索引、原生时间线都不动，暂存目录清掉。
      const failedBuild = await catchAsync(
        box.repairDraft({
          project: renamed,
          draftId,
          builder: async () => {
            throw new EditError("DRAFT_BUILD_FAILED", "simulated build failure", { status: 500 });
          },
        }),
      );
      expectEdit(failedBuild, "DRAFT_BUILD_FAILED", 500);
      assertUntouched();

      // 写索引失败：已经换上去的新草稿要撤回，原目录原样恢复。
      const replace = vi.spyOn(internals, "atomicReplaceIndex").mockImplementation(() => {
        throw new EditError("DRAFT_BOX_INDEX_WRITE_FAILED", "simulated index failure", { status: 500 });
      });
      expectEdit(await catchAsync(box.repairDraft({ project: renamed, draftId })), "DRAFT_BOX_INDEX_WRITE_FAILED", 500);
      assertUntouched();
      replace.mockRestore();

      const repaired = await box.repairDraft({ project: renamed, draftId });
      expect(repaired.status).toBe("repaired");
      expect(repaired.id).toBe(draftId);
      expect(repaired.draftId).toBe(draftId);
      expect(repaired.path).toBe(target);
      expect(repaired.verification.draft_id_preserved).toBe(true);
      expect(repaired.verification.draft_path_preserved).toBe(true);
      const repairedContent = JSON.parse(readFileSync(path.join(target, mainFile), "utf8"));
      expect(repairedContent.id).toBe(draftId);
      // 草稿名跟文件夹走：工程改了名，文件夹不改，草稿名也还是文件夹名。
      expect(repairedContent.name).toBe("Native new draft");
      expect(repaired.draftName).toBe("Native new draft");
      expectNamedAfterFolder(target, indexPath, draftId, mainFile);
      const repairedMetadata = JSON.parse(readFileSync(path.join(target, "draft_meta_info.json"), "utf8"));
      expect(repairedMetadata.draft_fold_path).toBe(target);
      expect(repairedMetadata.draft_root_path).toBe(root);
      expect(readFileSync(auxiliary, "utf8")).toBe("keep auxiliary data");
      expect(repaired.verification.main_timeline_updated).toBe(withNativeTimeline);
      if (native) {
        expect(repaired.verification.main_timeline_path).toBe(path.relative(target, native.main));
        expect(JSON.parse(readFileSync(native.main, "utf8"))).toEqual({ ...repairedContent, id: native.mainId });
        expect(readFileSync(`${native.main}.bak`).equals(readFileSync(native.main))).toBe(true);
        expect(JSON.parse(readFileSync(path.join(native.timelines, "project.json"), "utf8"))).toEqual(native.manifest);
        expect(readFileSync(path.join(native.timelines, "other-timeline", mainFile), "utf8")).toBe("keep other timeline");
        expect(readFileSync(path.join(path.dirname(native.main), "native-extra.bin"), "utf8")).toBe("keep timeline auxiliary");
        const snapshots = readdirSync(path.join(native.timelines, ".jianying_ai_repair_backups"));
        expect(snapshots).toHaveLength(1);
        const snapshot = path.join(native.timelines, ".jianying_ai_repair_backups", snapshots[0]);
        expect(readFileSync(path.join(snapshot, mainFile), "utf8")).toBe("opaque native main timeline");
        expect(readFileSync(path.join(snapshot, `${mainFile}.bak`), "utf8")).toBe("opaque native main timeline backup");
        expect(JSON.parse(readFileSync(path.join(snapshot, "project.json"), "utf8"))).toEqual(native.manifest);
      } else {
        expect(repaired.verification.main_timeline_path).toBeNull();
        expect(existsSync(path.join(target, "Timelines"))).toBe(false);
      }
      const repairedIndex = JSON.parse(readFileSync(indexPath, "utf8"));
      const matches = repairedIndex.all_draft_store.filter((item) => item.draft_id === draftId);
      expect(matches).toHaveLength(1);
      expect(matches[0].draft_fold_path).toBe(target);
      expect(matches[0].draft_name).toBe("Native new draft");
      expect(matches[0].tm_draft_create).toBe(oldCreatedAt);
      expect(matches[0].future_jianying_field).toEqual({ keep: true });
      expect(repairedIndex.all_draft_store).toHaveLength(4);
      expect(readdirSync(root).filter((name) => name.startsWith("."))).toEqual([]);
    });
  },
);

describe.each(PLATFORMS)("draft box writes (%s)", (platform) => {
  const mainFile = BOX_PROFILES[platform].mainFile;

  it.each([
    [[]],
    [{ main_timeline_id: "../outside", timelines: [{ id: "../outside" }] }],
    [{ main_timeline_id: "870F8B67-3D2F-441C-BDE0-760C8529A2DE", timelines: [] }],
    [
      {
        main_timeline_id: "870F8B67-3D2F-441C-BDE0-760C8529A2DE",
        timelines: [{ id: "870F8B67-3D2F-441C-BDE0-760C8529A2DE" }, { id: "870F8B67-3D2F-441C-BDE0-760C8529A2DE" }],
      },
    ],
    [{ main_timeline_id: "870F8B67-3D2F-441C-BDE0-760C8529A2DE", timelines: [{ id: "870F8B67-3D2F-441C-BDE0-760C8529A2DE" }] }],
  ])("main timeline repair rejects invalid manifest %j", async (manifest) => {
    const base = await makeTemp();
    const source = path.join(base, "source");
    const staged = path.join(base, "staged");
    const manifestPath = path.join(source, "Timelines", "project.json");
    mkdirSync(path.dirname(manifestPath), { recursive: true });
    writeFileSync(manifestPath, JSON.stringify(manifest));
    const original = readFileSync(manifestPath);
    const error = catchSync(() => internals.stageMainTimelineRepair(source, staged, {}, "test", { mainFile }));
    expectEdit(error, "DRAFT_BOX_TIMELINE_INVALID", 409);
    expect(readFileSync(manifestPath).equals(original)).toBe(true);
    expect(existsSync(staged)).toBe(false);
  });

  it("main timeline repair refuses linked entries", async () => {
    const base = await makeTemp();
    const source = path.join(base, "source");
    const mainId = "870F8B67-3D2F-441C-BDE0-760C8529A2DE";
    mkdirSync(path.join(source, "Timelines", mainId), { recursive: true });
    writeFileSync(path.join(source, "Timelines", "project.json"), JSON.stringify({ main_timeline_id: mainId, timelines: [{ id: mainId }] }));
    writeFileSync(path.join(base, "elsewhere.json"), "{}");
    symlinkSync(path.join(base, "elsewhere.json"), path.join(source, "Timelines", mainId, mainFile));
    expectEdit(catchSync(() => internals.stageMainTimelineRepair(source, path.join(base, "staged"), {}, "t", { mainFile })), "DRAFT_BOX_TIMELINE_INVALID", 409);
  });

  it("repair rejects an indexed draft this service does not own", async () => {
    const base = await makeTemp();
    const draftId = "5B2D4E8A-0C1F-4A3B-9E6D-7F8091A2B3C4";
    const root = path.join(base, "drafts");
    const target = path.join(root, draftId.replace(/-/g, ""));
    const indexPath = writeIndex(root, {
      indexParent: path.join(base, "index"),
      drafts: [{ draft_id: draftId, draft_name: "Foreign draft", draft_fold_path: target }],
    });
    mkdirSync(target);
    writeFileSync(path.join(target, mainFile), "keep this content");
    const originalIndex = readFileSync(indexPath);
    const box = createDraftBox({ platform, indexPath, draftsRoot: root, isJianyingRunning: () => false, draftModule: fakeDraftModule() });
    const error = await catchAsync(box.repairDraft({ project: makeProject({ id: "unrelated-project" }), draftId }));
    expectEdit(error, "DRAFT_BOX_NOT_OWNED", 409);
    expect(readFileSync(path.join(target, mainFile), "utf8")).toBe("keep this content");
    expect(readFileSync(indexPath).equals(originalIndex)).toBe(true);
    expect(listNames(root)).toEqual([path.basename(target)]);
  });

  it("refuses to write while Jianying is running and on unsupported platforms", async () => {
    let running = true;
    const { box, root, indexPath } = await makeBox(platform, { isJianyingRunning: () => running });
    const indexBytes = readFileSync(indexPath);
    const mustNotBuild = vi.fn(async () => {
      throw new Error("project must not be built while app is running");
    });
    const error = await catchAsync(box.createDraft({ project: makeProject(), builder: mustNotBuild }));
    expectEdit(error, "DRAFT_BOX_APP_RUNNING", 409);
    expect(error.details.action).toBe("close_jianying_and_retry");
    expect(readFileSync(indexPath).equals(indexBytes)).toBe(true);
    expect(listNames(root)).toEqual(["Alpha", "Beta", "Gamma"]);
    const repair = await catchAsync(box.repairDraft({ project: makeProject(), draftId: FIXED_ID, builder: mustNotBuild }));
    expectEdit(repair, "DRAFT_BOX_APP_RUNNING", 409);
    expect(mustNotBuild).not.toHaveBeenCalled();

    running = false;
    const unsupported = createDraftBox({ platform: "linux", indexPath, draftsRoot: root, isJianyingRunning: () => false });
    const refused = await catchAsync(unsupported.createDraft({ project: makeProject(), builder: mustNotBuild }));
    expectEdit(refused, "DRAFT_BOX_WRITE_UNAVAILABLE", 503);
    expect(refused.details.reason).toBe("platform_unsupported");
    expect(await unsupported.status()).toMatchObject({ status: "unavailable", reason: "platform_unsupported" });
    expect(mustNotBuild).not.toHaveBeenCalled();
  });

  it("treats a failing or ambiguous process check as unknown", async () => {
    const failing = await makeBox(platform, {
      isJianyingRunning: () => {
        throw new Error("pgrep missing");
      },
    });
    expectEdit(await catchAsync(failing.box.createDraft({ project: makeProject() })), "DRAFT_BOX_PROCESS_CHECK_FAILED", 503);
    expect((await failing.box.status()).jianyingRunning).toBeNull();
    const ambiguous = await makeBox(platform, { isJianyingRunning: () => "maybe" });
    expectEdit(await catchAsync(ambiguous.box.createDraft({ project: makeProject() })), "DRAFT_BOX_PROCESS_CHECK_FAILED", 503);
    expect(listNames(ambiguous.root)).toEqual(["Alpha", "Beta", "Gamma"]);
  });

  it("awaits an asynchronous process check and skips the cache only right before publishing", async () => {
    let running = true;
    const fresh = [];
    const { box, root } = await makeBox(platform, {
      isJianyingRunning: async (options) => {
        fresh.push(options?.fresh === true);
        return running;
      },
    });
    expect((await box.status()).jianyingRunning).toBe(true);
    expectEdit(await catchAsync(box.createDraft({ project: makeProject() })), "DRAFT_BOX_APP_RUNNING", 409);
    expect(listNames(root)).toEqual(["Alpha", "Beta", "Gamma"]);
    running = false;
    fresh.length = 0;
    const created = await box.createDraft({ project: makeProject() });
    // 新建：只有写索引前那次不用缓存。
    expect(fresh).toEqual([false, false, false, true]);
    fresh.length = 0;
    await box.repairDraft({ project: makeProject(), draftId: created.draftId });
    // 修复：只有替换草稿目录前那次不用缓存（紧接着写索引前那次复用它）。
    expect(fresh).toEqual([false, false, false, true, false]);
  });

  it.runIf(HAS_REAL_DRAFT)("writes and repairs through a drafts root spelled differently from its real path", async ({ skip }) => {
    const base = await makeTemp();
    const real = path.join(base, "Drafts");
    const indexPath = writeIndex(real, { indexParent: path.join(base, "index"), drafts: [] });
    // 大小写不同的另一种写法（Windows 上同类的是 8.3 短名 RUNNER~1）：区分大小写的盘上没有这种别名。
    const alias = path.join(base, "DRAFTS");
    if (!existsSync(alias)) skip();
    // 用真的草稿模块：它按系统 realpath 拼目录，草稿箱也得用同一种，否则「是不是预留的目录」对不上。
    const box = createDraftBox({ platform, indexPath, draftsRoot: alias, isJianyingRunning: () => false });
    const created = await box.createDraft({ project: makeProject(), media: [mediaInfo("tone.mp3")] });
    expect(created.path).toBe(path.join(real, "Native new draft"));
    const entry = () => JSON.parse(readFileSync(indexPath, "utf8")).all_draft_store.find((item) => item.draft_id === created.draftId);
    expect(entry()).toMatchObject({ draft_fold_path: created.path, draft_root_path: real });
    const repaired = await box.repairDraft({ project: makeProject({ name: "改过" }), media: [mediaInfo("tone.mp3")], draftId: created.draftId });
    expect(repaired.path).toBe(created.path);
    expect(listNames(real)).toEqual(["Native new draft"]);
  });

  it("leaves an unowned build output alone and never falls back to another path", async () => {
    const { box, root, indexPath } = await makeBox(platform);
    const indexBytes = readFileSync(indexPath);
    const originalNames = listNames(root);
    mkdirSync(path.join(root, "drafts"));
    writeFileSync(path.join(root, "drafts", "foreign.txt"), "preserve malformed native folder");
    const seen = [];
    const error = await catchAsync(
      box.createDraft({
        project: bareProject("missing-project", "x"),
        builder: async ({ directory, draftId }) => {
          seen.push(draftId);
          mkdirSync(directory);
          writeFileSync(path.join(directory, "partial.txt"), "partial");
          return { project: bareProject("missing-project", "Incomplete builder result"), directory, content: { id: draftId } };
        },
      }),
    );
    expectEdit(error, "DRAFT_BOX_INVALID_DRAFT", 500);
    expect(error.message).toBe("Generated draft content has an invalid identity");
    expect(seen).toHaveLength(1);
    // 文件夹按草稿名「x」命名。
    expect(listNames(root)).toEqual([...originalNames, "drafts", "x"].sort());
    expect(readFileSync(path.join(root, "drafts", "foreign.txt"), "utf8")).toBe("preserve malformed native folder");
    expect(readFileSync(indexPath).equals(indexBytes)).toBe(true);
  });

  it("cleans the new folder when an owned build fails", async () => {
    const calls = [];
    let checks = 0;
    const { box, root, indexPath } = await makeBox(platform, {
      isJianyingRunning: () => {
        checks += 1;
        return false;
      },
    });
    const indexBytes = readFileSync(indexPath);
    mkdirSync(path.join(root, "drafts"));
    writeFileSync(path.join(root, "drafts", "foreign.txt"), "preserve");
    const error = await catchAsync(
      box.createDraft({
        project: bareProject("missing-project", "x"),
        builder: async ({ directory, draftId }) => {
          mkdirSync(directory);
          writeFileSync(path.join(directory, OWNER_MARKER), JSON.stringify({ draft_id: draftId, project_id: "missing-project", generator: "jianying-ai-service" }));
          calls.push(checks);
          throw new Error("stop after ownership marker creation");
        },
      }),
    );
    expect(error.message).toBe("stop after ownership marker creation");
    expect(listNames(root)).toEqual(["Alpha", "Beta", "Gamma", "drafts"]);
    expect(readFileSync(path.join(root, "drafts", "foreign.txt"), "utf8")).toBe("preserve");
    expect(readFileSync(indexPath).equals(indexBytes)).toBe(true);
    // 建草稿之前已经检查过三次剪映是否在运行。
    expect(calls).toEqual([3]);
  });

  it("keeps a published folder whose marker has no project when the index write fails", async () => {
    const { box, root, indexPath } = await makeBox(platform);
    const indexBytes = readFileSync(indexPath);
    vi.spyOn(internals, "atomicReplaceIndex").mockImplementation(() => {
      throw new EditError("DRAFT_BOX_INDEX_WRITE_FAILED", "simulated index failure", { status: 500 });
    });
    const error = await catchAsync(
      box.createDraft({
        project: bareProject("project", "Published before index failure"),
        builder: async ({ project, directory, draftId }) => {
          writeOwnedBuild(directory, draftId, { mainFile });
          const content = {
            id: draftId,
            name: project.name,
            create_time: 1_700_000_000,
            update_time: 1_700_000_000,
            duration: 0,
            materials: {},
          };
          return { project, directory, content };
        },
      }),
    );
    expectEdit(error, "DRAFT_BOX_INDEX_WRITE_FAILED", 500);
    const published = listNames(root).filter((name) => !["Alpha", "Beta", "Gamma"].includes(name));
    expect(published).toHaveLength(1);
    expect(existsSync(path.join(root, published[0], OWNER_MARKER))).toBe(true);
    expect(readFileSync(indexPath).equals(indexBytes)).toBe(true);
  });

  it("removes only the new owned folder when the index changes during the write", async () => {
    const base = await makeTemp();
    const root = path.join(base, "drafts");
    const indexPath = writeIndex(root, { indexParent: path.join(base, "index") });
    const index = JSON.parse(readFileSync(indexPath, "utf8"));
    index.draft_ids = 0;
    writeFileSync(indexPath, JSON.stringify(index, null, 4));
    for (const name of ["Alpha", "Beta", "Gamma"]) mkdirSync(path.join(root, name));
    mkdirSync(path.join(root, "DO_NOT_REMOVE"));
    writeFileSync(path.join(root, "DO_NOT_REMOVE", "foreign.txt"), "foreign");
    const originalNames = ["Alpha", "Beta", "DO_NOT_REMOVE", "Gamma"];
    expect(listNames(root)).toEqual(originalNames);

    let changes = 0;
    const conflictAfterWrite = () => {
      if (changes === 3) {
        const changed = JSON.parse(readFileSync(indexPath, "utf8"));
        changed.all_draft_store[0].draft_name = "Changed by Jianying";
        changed.draft_ids = 0;
        const competing = path.join(path.dirname(indexPath), ".root_meta_info.json.competing");
        writeFileSync(competing, JSON.stringify(changed, null, 2));
        renameSync(competing, indexPath);
      }
      changes += 1;
      return false;
    };
    const box = createDraftBox({ platform, indexPath, draftsRoot: root, isJianyingRunning: conflictAfterWrite, draftModule: fakeDraftModule() });
    const error = await catchAsync(box.createDraft({ project: makeProject({ name: "Conflict" }) }));
    expectEdit(error, "DRAFT_BOX_INDEX_CONFLICT", 409);
    expect(error.details).toEqual({ action: "retry" });
    const rawIndex = readFileSync(indexPath);
    const current = JSON.parse(rawIndex.toString("utf8"));
    expect(current.draft_ids).toBe(0);
    expect(current.all_draft_store[0].draft_name).toBe("Changed by Jianying");
    expect(current.all_draft_store).toHaveLength(3);
    expect(readFileSync(path.join(root, "DO_NOT_REMOVE", "foreign.txt"), "utf8")).toBe("foreign");
    expect(listNames(root)).toEqual(originalNames);
    expect(readFileSync(indexPath).equals(rawIndex)).toBe(true);
    expect(readdirSync(path.dirname(indexPath)).filter((name) => name.endsWith(".tmp") || name.endsWith(".competing"))).toEqual([]);
  });

  it("does not create folders for a missing project or a corrupt index", async () => {
    const { box, root, indexPath } = await makeBox(platform);
    expectEdit(await catchAsync(box.createDraft({ project: null })), "PROJECT_NOT_FOUND", 404);
    expect(readdirSync(root)).toHaveLength(3);
    writeFileSync(indexPath, "{");
    expectEdit(await catchAsync(box.createDraft({ project: makeProject({ name: "Corrupt index" }) })), "DRAFT_BOX_INDEX_INVALID", 500);
    expect(readdirSync(root)).toHaveLength(3);
    expect(readFileSync(indexPath, "utf8")).toBe("{");
  });

  it("creates and repairs through the same write path", async () => {
    const base = await makeTemp();
    const root = path.join(base, "drafts");
    const indexPath = writeIndex(root, { indexParent: path.join(base, "index") });
    for (const name of ["Alpha", "Beta", "Gamma"]) mkdirSync(path.join(root, name));
    const box = createDraftBox({ platform, indexPath, draftsRoot: root, isJianyingRunning: () => false, draftModule: fakeDraftModule() });
    const project = makeProject({ name: "MCP draft box" });
    const body = await box.createDraft({ project });
    expect(body.ok).toBe(true);
    expect(body.status).toBe("written");
    expect(body.draft.draft_name).toBe("MCP draft box");
    expect(body.jianying_ui_verified).toBe(false);
    expect((await box.getDraft(body.id)).draft.name).toBe("MCP draft box");
    const repaired = await box.repairDraft({ project, draftId: body.id });
    expect(repaired.status).toBe("repaired");
    expect(repaired.id).toBe(body.id);
    expect(repaired.path).toBe(body.path);
  });

  it("keeps other drafts' 64-bit cloud ids and 1.0-style numbers byte for byte", async () => {
    const base = await makeTemp();
    const root = path.join(base, "drafts");
    const indexDirectory = path.join(base, "index");
    mkdirSync(root, { recursive: true });
    mkdirSync(indexDirectory);
    const indexPath = path.join(indexDirectory, "root_meta_info.json");
    const foreign =
      `{"draft_id":"CLOUD_OTHER","draft_name":"别人的云草稿","draft_fold_path":${JSON.stringify(path.join(root, "CloudOther"))},` +
      '"tm_draft_cloud_user_id":7234567890123456789,"tm_draft_cloud_entry_id":7345678901234567891,' +
      '"tm_draft_cloud_space_id":7012345678901234567,"draft_timeline_materials_size":1.0,"tiny":1e-07,"huge":1e+22,' +
      '"negative_zero":-0.0,"nested":{"10":[1.0],"2":{}}}';
    writeFileSync(indexPath, `{"root_path":${JSON.stringify(indexDirectory)},"all_draft_store":[${foreign}],"draft_ids":1,"extra":[2.5e6,1E+2,-0]}`);
    const box = createDraftBox({ platform, indexPath, draftsRoot: root, isJianyingRunning: () => false, draftModule: fakeDraftModule() });
    const created = await box.createDraft({ project: makeProject() });
    const afterCreate = readFileSync(indexPath, "utf8");
    expect(afterCreate).toContain(foreign);
    // 不是 Python 输出形式的数，同原仓库一样规范化。
    expect(afterCreate).toContain('"extra":[2500000.0,100.0,0]');
    expect(JSON.parse(afterCreate).draft_ids).toBe(platform === "mac" ? 2 : 1);
    await box.repairDraft({ project: makeProject({ name: "Renamed" }), draftId: created.draftId });
    const afterRepair = readFileSync(indexPath, "utf8");
    expect(afterRepair).toContain(foreign);
    expect(afterRepair).toContain('"extra":[2500000.0,100.0,0]');
    expect(JSON.parse(afterRepair).all_draft_store.find((item) => item.draft_id === created.draftId).draft_name).toBe("Native new draft");
  });

  it("notices a competing change that only touches a 64-bit id", async () => {
    const { indexPath, root } = await makeBox(platform);
    writeFileSync(indexPath, readFileSync(indexPath, "utf8").replace('"draft_ids":[]', '"draft_ids":[],"cloud":7234567890123456789'));
    let checks = 0;
    const box = createDraftBox({
      platform,
      indexPath,
      draftsRoot: root,
      isJianyingRunning: () => {
        checks += 1;
        // 第 4 次检查在写索引前的比对之前；改动后的值用 JSON.parse 读出来和原来一样。
        if (checks === 4) writeFileSync(indexPath, readFileSync(indexPath, "utf8").replace("7234567890123456789", "7234567890123456788"));
        return false;
      },
      draftModule: fakeDraftModule(),
    });
    expectEdit(await catchAsync(box.createDraft({ project: makeProject() })), "DRAFT_BOX_INDEX_CONFLICT", 409);
    expect(readFileSync(indexPath, "utf8")).toContain('"cloud":7234567890123456788');
    expect(listNames(root)).toEqual(["Alpha", "Beta", "Gamma"]);
  });

  it("names the folder after draftName, the draft after the folder, and keeps the project otherwise unchanged", async () => {
    const { box, indexPath } = await makeBox(platform);
    const project = makeProject({ name: "工程名" });
    const body = await box.createDraft({ project, draftName: "  会话标题  " });
    expect(path.basename(body.path)).toBe("会话标题");
    expect(body.draftName).toBe("会话标题");
    const snapshot = JSON.parse(readFileSync(path.join(body.path, "project.json"), "utf8"));
    expect(snapshot.name).toBe("会话标题");
    expect(project.name).toBe("工程名");
    // 修复时文件夹不改名，草稿名仍是文件夹名，不跟工程名或 draftName 走。
    const repaired = await box.repairDraft({ project, draftId: body.draftId });
    expect(repaired.draftName).toBe("会话标题");
    expect((await box.repairDraft({ project, draftId: body.draftId, draftName: "别的名字" })).draftName).toBe("会话标题");
    expectNamedAfterFolder(body.path, indexPath, body.draftId, BOX_PROFILES[platform].mainFile);
  });

  it("rejects a builder whose draft name is not the folder name", async () => {
    const { box, root, indexPath } = await makeBox(platform);
    const indexBytes = readFileSync(indexPath);
    const error = await catchAsync(
      box.createDraft({
        project: bareProject("project", "文件夹名"),
        builder: async ({ directory, draftId }) => {
          writeOwnedBuild(directory, draftId, { projectId: "project", mainFile });
          const content = { id: draftId, name: "别的名字", create_time: 1_700_000_000, update_time: 1_700_000_000, duration: 0, materials: {} };
          return { project: bareProject("project", "别的名字"), directory, content };
        },
      }),
    );
    expectEdit(error, "DRAFT_BOX_INVALID_DRAFT", 500);
    expect(readFileSync(indexPath).equals(indexBytes)).toBe(true);
    expect(listNames(root)).toEqual(["Alpha", "Beta", "Gamma"]);
  });

  it("repair reports a project mismatch, missing drafts and non-UUID ids", async () => {
    const { box } = await makeBox(platform);
    const created = await box.createDraft({ project: makeProject() });
    expectEdit(
      await catchAsync(box.repairDraft({ project: makeProject({ id: "another-project" }), draftId: created.draftId })),
      "DRAFT_BOX_PROJECT_MISMATCH",
      409,
    );
    expectEdit(await catchAsync(box.repairDraft({ project: makeProject(), draftId: FIXED_ID })), "DRAFT_BOX_NOT_FOUND", 404);
    expectEdit(await catchAsync(box.repairDraft({ project: makeProject(), draftId: "DRAFT_A" })), "DRAFT_BOX_INVALID_ID", 422);
    // 索引里同一个 id 出现两次时拒绝修复。
  });

  it("repair refuses an ambiguous index identity", async () => {
    const { box, indexPath } = await makeBox(platform);
    const created = await box.createDraft({ project: makeProject() });
    const index = JSON.parse(readFileSync(indexPath, "utf8"));
    index.all_draft_store.push({ ...index.all_draft_store.find((item) => item.draft_id === created.draftId), draft_id: created.draftId.toLowerCase() });
    writeFileSync(indexPath, JSON.stringify(index));
    expectEdit(await catchAsync(box.repairDraft({ project: makeProject(), draftId: created.draftId })), "DRAFT_BOX_INDEX_INVALID", 500);
  });

  it("repair detects an index changed by Jianying and leaves everything in place", async () => {
    let calls = 0;
    let poke = null;
    const { box, indexPath } = await makeBox(platform, {
      isJianyingRunning: () => {
        calls += 1;
        poke?.(calls);
        return false;
      },
    });
    const created = await box.createDraft({ project: makeProject() });
    const before = readFileSync(path.join(created.path, BOX_PROFILES[platform].mainFile));
    poke = (count) => {
      // 修复里第 3 次检查在比对索引之前：此时剪映改了索引。
      if (count === calls && poke.armed === count) {
        const changed = JSON.parse(readFileSync(indexPath, "utf8"));
        changed.all_draft_store[0].draft_name = "Touched";
        writeFileSync(indexPath, JSON.stringify(changed));
      }
    };
    poke.armed = calls + 3;
    const error = await catchAsync(box.repairDraft({ project: makeProject({ name: "Second" }), draftId: created.draftId }));
    expectEdit(error, "DRAFT_BOX_INDEX_CONFLICT", 409);
    expect(readFileSync(path.join(created.path, BOX_PROFILES[platform].mainFile)).equals(before)).toBe(true);
  });

  it("reports an incomplete rollback when the index cannot be restored", async () => {
    const { box, indexPath, root } = await makeBox(platform);
    const created = await box.createDraft({ project: makeProject() });
    const before = readFileSync(indexPath);
    // 新索引写进去了但校验前就失败，而且恢复原索引时也写不进去。
    vi.spyOn(internals, "atomicReplaceIndex").mockImplementation((target, encoded) => {
      if (!readFileSync(target).equals(before)) throw new EditError("DRAFT_BOX_INDEX_WRITE_FAILED", "cannot restore", { status: 500 });
      writeFileSync(target, encoded);
      throw new EditError("DRAFT_BOX_INDEX_WRITE_FAILED", "verify failed", { status: 500 });
    });
    const error = await catchAsync(box.repairDraft({ project: makeProject({ name: "Second" }), draftId: created.draftId }));
    expectEdit(error, "DRAFT_BOX_REPAIR_ROLLBACK_FAILED", 500);
    expect(error.details).toMatchObject({ index_restore_required: true, draft_restore_required: false, index_path: indexPath });
    // 草稿目录已经恢复成原来的。
    expect(JSON.parse(readFileSync(path.join(created.path, BOX_PROFILES[platform].mainFile), "utf8")).name).toBe("Native new draft");
    expect(readdirSync(root).filter((name) => name.startsWith("."))).toEqual([]);
  });

  it("serializes concurrent writes so both drafts reach the index", async () => {
    const { box, indexPath } = await makeBox(platform);
    const [first, second] = await Promise.all([
      box.createDraft({ project: makeProject({ id: "a".repeat(32), name: "First" }) }),
      box.createDraft({ project: makeProject({ id: "b".repeat(32), name: "Second" }) }),
    ]);
    const ids = JSON.parse(readFileSync(indexPath, "utf8")).all_draft_store.map((item) => item.draft_id);
    expect(ids).toEqual(expect.arrayContaining([first.draftId, second.draftId]));
    expect(ids).toHaveLength(5);
  });

  it("collides instead of overwriting an existing index identity", async () => {
    const { box, root, indexPath } = await makeBox(platform, { newDraftId: () => FIXED_ID });
    const index = JSON.parse(readFileSync(indexPath, "utf8"));
    index.all_draft_store.push({ draft_id: FIXED_ID.toLowerCase(), draft_name: "Taken", draft_fold_path: path.join(root, "Taken") });
    writeFileSync(indexPath, JSON.stringify(index));
    expectEdit(await catchAsync(box.createDraft({ project: makeProject() })), "DRAFT_BOX_COLLISION", 409);
    expect(listNames(root)).toEqual(["Alpha", "Beta", "Gamma"]);
  });

  it("names the new folder after the draft and steps around existing folders and index entries", async () => {
    const { box, root, indexPath } = await makeBox(platform);
    // 已有同名文件夹（大小写不同也算）、索引里登记过但文件夹已经不在的名字，都要让开；隐藏的暂存目录不算重名。
    mkdirSync(path.join(root, "海边 Trip"));
    writeFileSync(path.join(root, "海边 Trip", "foreign.txt"), "keep");
    const index = JSON.parse(readFileSync(indexPath, "utf8"));
    index.all_draft_store.push({ draft_id: "GONE", draft_name: "Gone", draft_fold_path: `${path.join(root, "海边 trip (2)")}${path.sep}` });
    writeFileSync(indexPath, JSON.stringify(index));
    mkdirSync(path.join(root, ".0123456789abcdef.repair"));
    const first = await box.createDraft({ project: makeProject({ name: "海边 Trip" }) });
    expect(path.basename(first.path)).toBe("海边 Trip (3)");
    expect(readFileSync(path.join(root, "海边 Trip", "foreign.txt"), "utf8")).toBe("keep");
    const second = await box.createDraft({ project: makeProject({ id: "c".repeat(32), name: "海边 Trip" }) });
    expect(path.basename(second.path)).toBe("海边 Trip (4)");
    // 草稿名等于文件夹名（带序号），剪映打开保存后也不会再变。
    expect([first.draftName, second.draftName]).toEqual(["海边 Trip (3)", "海边 Trip (4)"]);
    for (const created of [first, second]) expectNamedAfterFolder(created.path, indexPath, created.draftId, mainFile);
    const entries = JSON.parse(readFileSync(indexPath, "utf8")).all_draft_store;
    for (const created of [first, second]) {
      const entry = entries.find((item) => item.draft_id === created.draftId);
      expect(entry.draft_fold_path).toBe(created.path);
      expect(entry.draft_json_file).toBe(path.join(created.path, mainFile));
      expect(JSON.parse(readFileSync(path.join(created.path, "draft_meta_info.json"), "utf8")).draft_fold_path).toBe(created.path);
    }
  });

  it("replaces characters Windows and macOS refuse and keeps names short", async () => {
    const { box, root, indexPath } = await makeBox(platform);
    const unsafeName = ` .a/b\\c:d*e?f"g<h>i|j\tk\u0001. `;
    const unsafe = await box.createDraft({ project: makeProject({ name: unsafeName }) });
    expect(path.basename(unsafe.path)).toBe("a_b_c_d_e_f_g_h_i_j_k_");
    // 草稿名跟着用替换后的文件夹名。
    expect(unsafe.draftName).toBe("a_b_c_d_e_f_g_h_i_j_k_");
    const reserved = await box.createDraft({ project: makeProject({ name: "CON.mp4" }) });
    expect(path.basename(reserved.path)).toBe("_CON.mp4");
    // 只有句点和空格的名字用默认名。
    const dots = await box.createDraft({ project: makeProject({ name: "..." }) });
    expect(path.basename(dots.path)).toBe("AI 剪辑");
    // 49 个汉字 147 字节，再放一个 4 字节的表情就超了：整字截断，不留半个字符。
    const long = await box.createDraft({ project: makeProject({ name: "剪".repeat(49) + "😀".repeat(20) }) });
    expect(path.basename(long.path)).toBe("剪".repeat(49));
    const again = await box.createDraft({ project: makeProject({ name: "剪".repeat(49) + "😀".repeat(20) }) });
    expect(path.basename(again.path)).toBe(`${"剪".repeat(48)} (2)`);
    const emoji = await box.createDraft({ project: makeProject({ name: `ab${"😀".repeat(40)}` }) });
    expect(path.basename(emoji.path)).toBe(`ab${"😀".repeat(37)}`);
    for (const created of [long, again, emoji]) {
      const folder = path.basename(created.path);
      expect(Buffer.byteLength(folder)).toBeLessThanOrEqual(150);
      expect(folder.isWellFormed()).toBe(true);
      expect(existsSync(created.path)).toBe(true);
    }
    for (const created of [unsafe, reserved, dots, long, again, emoji]) {
      expect(path.dirname(created.path)).toBe(root);
      expect(created.draftName).toBe(path.basename(created.path));
      expectNamedAfterFolder(created.path, indexPath, created.draftId, mainFile);
      expect((await box.repairDraft({ project: makeProject({ name: "改名" }), draftId: created.draftId })).path).toBe(created.path);
    }
  });

  it("fingerprints a draft so a later export can tell it was opened or changed in Jianying", async () => {
    const { box, indexPath, mainFile } = await makeBox(platform);
    const created = await box.createDraft({ project: makeProject() });
    const first = await box.fingerprint(created.draftId);
    expect(typeof first).toBe("string");
    // 什么都没动：再算一次一样；修复后（我们自己写的）会变，记新的就行
    expect(await box.fingerprint(created.draftId)).toBe(first);
    await box.repairDraft({ project: makeProject(), draftId: created.draftId });
    const exported = await box.fingerprint(created.draftId);
    // 剪映保存：主文件变了
    const main = path.join(created.path, mainFile);
    writeFileSync(main, `${readFileSync(main, "utf8")} `);
    const saved = await box.fingerprint(created.draftId);
    expect(saved).not.toBe(exported);
    // 剪映第一次保存后改读 Timelines 里的主时间线
    const mainId = "4D2B6B0E-1C3A-4F5B-9D7E-2A1B3C4D5E6F";
    mkdirSync(path.join(created.path, "Timelines", mainId), { recursive: true });
    writeFileSync(path.join(created.path, "Timelines", "project.json"), JSON.stringify({ main_timeline_id: mainId, timelines: [{ id: mainId }] }));
    writeFileSync(path.join(created.path, "Timelines", mainId, mainFile), "native");
    const native = await box.fingerprint(created.draftId);
    expect(native).not.toBe(saved);
    writeFileSync(path.join(created.path, "Timelines", mainId, mainFile), "native, saved again");
    expect(await box.fingerprint(created.draftId)).not.toBe(native);
    // 只在索引里改了修改时间也算
    const before = await box.fingerprint(created.draftId);
    const index = JSON.parse(readFileSync(indexPath, "utf8"));
    index.all_draft_store.find((item) => item.draft_id === created.draftId).tm_draft_modified += 5_000_000;
    writeFileSync(indexPath, JSON.stringify(index));
    expect(await box.fingerprint(created.draftId)).not.toBe(before);
    // 草稿被删了
    rmSync(created.path, { recursive: true, force: true });
    expectEdit(await catchAsync(box.fingerprint(created.draftId)), "DRAFT_BOX_NOT_FOUND", 404);
  });

  it("writes the material size from draft_meta_info.json into the index", async () => {
    const { box, indexPath } = await makeBox(platform, { draftModule: undefined, copyMaterial: async (source, destination) => writeFileSync(destination, readFileSync(source), { flag: "wx" }) });
    const media = [mediaInfo("clip-720p.mp4"), mediaInfo("tone.mp3")];
    const expected = media.reduce((total, info) => total + statSync(info.path).size, 0);
    const created = await box.createDraft({ project: makeProject(), media });
    const meta = JSON.parse(readFileSync(path.join(created.path, "draft_meta_info.json"), "utf8"));
    expect(meta.draft_timeline_materials_size_).toBe(expected);
    const entry = () => JSON.parse(readFileSync(indexPath, "utf8")).all_draft_store.find((item) => item.draft_id === created.draftId);
    expect(expected).toBeGreaterThan(0);
    expect(created.draft.draft_timeline_materials_size).toBe(expected);
    expect(entry().draft_timeline_materials_size).toBe(expected);
    expect((await box.getDraft(created.draftId)).draft.material_size_bytes).toBe(expected);
    const repaired = await box.repairDraft({ project: makeProject(), media: media.slice(1), draftId: created.draftId });
    const after = statSync(media[1].path).size;
    expect(repaired.draft.draft_timeline_materials_size).toBe(after);
    expect(entry().draft_timeline_materials_size).toBe(after);
    expect(JSON.parse(readFileSync(path.join(created.path, "draft_meta_info.json"), "utf8")).draft_timeline_materials_size_).toBe(after);
  });

  it("repairs a draft made earlier with an id-named folder and keeps that folder", async () => {
    const { box, root, indexPath } = await makeBox(platform);
    const created = await box.createDraft({ project: makeProject() });
    // 早先的版本按去横线的 id 建文件夹：把这份草稿改成那种布局。
    const legacy = path.join(root, created.draftId.replace(/-/g, ""));
    renameSync(created.path, legacy);
    const index = JSON.parse(readFileSync(indexPath, "utf8"));
    const entry = index.all_draft_store.find((item) => item.draft_id === created.draftId);
    entry.draft_fold_path = legacy;
    entry.draft_json_file = path.join(legacy, mainFile);
    writeFileSync(indexPath, JSON.stringify(index));
    const repaired = await box.repairDraft({ project: makeProject({ name: "Legacy repaired" }), draftId: created.draftId });
    expect(repaired.path).toBe(legacy);
    expect(listNames(root)).toEqual(["Alpha", "Beta", "Gamma", path.basename(legacy)].sort());
    // 旧草稿的文件夹名就是 id，草稿名也跟着是它。
    expect(repaired.draftName).toBe(path.basename(legacy));
    expectNamedAfterFolder(legacy, indexPath, created.draftId, mainFile);
    expect(JSON.parse(readFileSync(path.join(legacy, "draft_meta_info.json"), "utf8")).draft_fold_path).toBe(legacy);
    const after = JSON.parse(readFileSync(indexPath, "utf8")).all_draft_store.find((item) => item.draft_id === created.draftId);
    expect(after).toMatchObject({ draft_fold_path: legacy, draft_json_file: path.join(legacy, mainFile), draft_name: path.basename(legacy) });
  });

  it("repairs by the indexed folder and does not rename it when the draft name changes", async () => {
    const { box, root, indexPath } = await makeBox(platform);
    const created = await box.createDraft({ project: makeProject({ name: "第一版" }) });
    const repaired = await box.repairDraft({ project: makeProject({ name: "第二版" }), draftId: created.draftId });
    expect(repaired.path).toBe(path.join(root, "第一版"));
    expect(repaired.draftName).toBe("第一版");
    expect(listNames(root)).toEqual(["Alpha", "Beta", "Gamma", "第一版"]);
    const entry = JSON.parse(readFileSync(indexPath, "utf8")).all_draft_store.find((item) => item.draft_id === created.draftId);
    expect(entry).toMatchObject({ draft_fold_path: created.path, draft_name: "第一版" });
    expectNamedAfterFolder(created.path, indexPath, created.draftId, mainFile);
    // 剪映在名字里没改过的情况下把文件夹改了名：索引没跟着改就找不到，拒绝修复而不是另找。
    renameSync(created.path, path.join(root, "挪走了"));
    expectEdit(await catchAsync(box.repairDraft({ project: makeProject(), draftId: created.draftId })), "DRAFT_BOX_NOT_OWNED", 409);
  });

  it("refuses to repair when the indexed folder leaves the draft root or is a link", async () => {
    const { box, root, indexPath, base } = await makeBox(platform);
    const created = await box.createDraft({ project: makeProject() });
    const original = readFileSync(indexPath, "utf8");
    const pointTo = (folder) => {
      const index = JSON.parse(original);
      const entry = index.all_draft_store.find((item) => item.draft_id === created.draftId);
      entry.draft_fold_path = folder;
      writeFileSync(indexPath, JSON.stringify(index));
    };
    const refused = async (folder) => {
      pointTo(folder);
      const before = readFileSync(indexPath);
      expectEdit(await catchAsync(box.repairDraft({ project: makeProject({ name: "不该写" }), draftId: created.draftId })), "DRAFT_BOX_NOT_OWNED", 409);
      expect(readFileSync(indexPath).equals(before)).toBe(true);
    };
    // 根目录外一份带着同样所有权标记的拷贝。
    const outside = path.join(base, "outside", "Native new draft");
    mkdirSync(path.dirname(outside));
    cpSync(created.path, outside, { recursive: true });
    await refused(outside);
    // 根目录下指向那份拷贝的符号链接。
    symlinkSync(outside, path.join(root, "link"), "dir");
    await refused(path.join(root, "link"));
    // 父目录里的符号链接、..、草稿根目录本身、隐藏目录、空值。
    symlinkSync(path.dirname(outside), path.join(root, "nested"), "dir");
    await refused(path.join(root, "nested", "Native new draft"));
    await refused(`${root}/..`);
    await refused(`${root}/../outside/Native new draft`);
    await refused(root);
    mkdirSync(path.join(root, ".hidden"));
    cpSync(created.path, path.join(root, ".hidden", "x"), { recursive: true });
    await refused(path.join(root, ".hidden"));
    await refused("");
    // 根目录外那份拷贝一点没动。
    expect(JSON.parse(readFileSync(path.join(outside, mainFile), "utf8")).name).toBe("Native new draft");
    expect(JSON.parse(readFileSync(path.join(outside, "draft_meta_info.json"), "utf8")).draft_fold_path).toBe(created.path);
    pointTo(created.path);
    expect((await box.repairDraft({ project: makeProject({ name: "可以写" }), draftId: created.draftId })).path).toBe(created.path);
  });
});

describe("ownership helpers", () => {
  it("cleanup keeps a foreign folder", async () => {
    // 原仓库还要求目录名等于草稿 id；现在文件夹按草稿名命名，身份只看所有权标记。
    const base = await makeTemp();
    const foreign = path.join(base, "ABC");
    mkdirSync(foreign);
    const marker = path.join(foreign, OWNER_MARKER);
    writeFileSync(marker, JSON.stringify({ draft_id: "OTHER", generator: "jianying-ai-service" }));
    internals.removeOwnedDirectory(foreign, "ABC");
    expect(existsSync(marker)).toBe(true);
    writeFileSync(path.join(foreign, "foreign_file.txt"), "leave alone");
    writeFileSync(marker, JSON.stringify({ draft_id: "ABC", project_id: "p", generator: "someone-else" }));
    internals.removeOwnedDirectory(foreign, "ABC");
    writeFileSync(marker, JSON.stringify({ draft_id: "ABC", project_id: "p", generator: "jianying-ai-service" }));
    internals.removeOwnedDirectory(foreign, "ABC", { projectId: "q" });
    symlinkSync(foreign, path.join(base, "link"), "dir");
    internals.removeOwnedDirectory(path.join(base, "link"), "ABC", { projectId: "p" });
    expect(readFileSync(path.join(foreign, "foreign_file.txt"), "utf8")).toBe("leave alone");
    internals.removeOwnedDirectory(foreign, "ABC", { projectId: "p" });
    expect(existsSync(foreign)).toBe(false);
  });

  it("matches markers by id, generator and project", async () => {
    const base = await makeTemp();
    const folder = path.join(base, "随便起的草稿名");
    mkdirSync(folder);
    expect(isOwnedDraftDirectory(folder, FIXED_ID, null)).toBe(true);
    expect(isOwnedDraftDirectory(folder, FIXED_ID, null, { ownerRequired: true })).toBe(false);
    writeFileSync(path.join(folder, OWNER_MARKER), JSON.stringify({ draft_id: FIXED_ID.toLowerCase(), project_id: "p", generator: "jianying-ai-service" }));
    expect(isOwnedDraftDirectory(folder, FIXED_ID, "p", { ownerRequired: true })).toBe(true);
    expect(isOwnedDraftDirectory(folder, FIXED_ID, "q", { ownerRequired: true })).toBe(false);
    writeFileSync(path.join(folder, OWNER_MARKER), JSON.stringify({ draft_id: FIXED_ID, project_id: "p", generator: "someone-else" }));
    expect(isOwnedDraftDirectory(folder, FIXED_ID, "p", { ownerRequired: true })).toBe(false);
    writeFileSync(path.join(folder, OWNER_MARKER), "not json");
    expect(isOwnedDraftDirectory(folder, FIXED_ID, null)).toBe(false);
  });
});

// ---------------------------------------------------------------- 对照基准（原仓库实际输出）

describe("oracle: original DraftBoxService outputs", () => {
  async function oracleLayout() {
    const base = await makeTemp();
    for (const directory of CASES.layout.dirs) mkdirSync(path.join(base, directory), { recursive: true });
    for (const [name, text] of Object.entries(CASES.layout.files)) writeFileSync(path.join(base, name), text);
    for (const [name, target] of Object.entries(CASES.layout.symlinks)) symlinkSync(target, path.join(base, name), "dir");
    const replacements = { "${BASE}": base, "${LONG_NAME}": "x".repeat(201), "${LONG_QUERY}": "q".repeat(201) };
    const indexPath = path.join(base, "index", "root_meta_info.json");
    writeFileSync(indexPath, JSON.stringify(substitute(CASES.index, replacements)));
    const box = createDraftBox({
      platform: "windows",
      indexPath,
      draftsRoot: path.join(base, "drafts"),
      draftsRootSource: "environment",
      isJianyingRunning: () => false,
    });
    return { base, box, replacements };
  }

  async function capture(callback) {
    try {
      return { result: await callback() };
    } catch (error) {
      if (!(error instanceof EditError)) throw error;
      return { error: { code: error.code, status: error.status } };
    }
  }

  it("status matches", async () => {
    const { base, box } = await oracleLayout();
    expect((await capture(() => box.status())).result).toMatchObject(ORACLE.status.result);
    const missing = createDraftBox({
      platform: "windows",
      indexPath: path.join(base, "absent", "root_meta_info.json"),
      draftsRoot: path.join(base, "drafts"),
      isJianyingRunning: () => false,
    });
    expect(await missing.status()).toMatchObject(ORACLE.status_missing_index.result);
  });

  it("list results, order, skips and errors match", async () => {
    const { base, box, replacements } = await oracleLayout();
    for (const [index, query] of CASES.lists.entries()) {
      const actual = await capture(() => box.listDrafts(substitute(query, replacements)));
      expect(actual, JSON.stringify(query)).toEqual(localize(ORACLE.lists[index], { BASE: base }));
    }
  });

  it("get results and errors match", async () => {
    const { base, box } = await oracleLayout();
    for (const [index, draftId] of CASES.gets.entries()) {
      expect(await capture(() => box.getDraft(draftId)), draftId).toEqual(localize(ORACLE.gets[index], { BASE: base }));
    }
  });

  it("timestamp conversion matches Python datetime", () => {
    expect(CASES.timestamps.map((value) => utcTimestampText(value))).toEqual(ORACLE.timestamps);
  });

  it("rewrites third-party index text exactly like Python json.dumps(json.loads(raw))", () => {
    for (const { raw, python } of ORACLE.index_round_trips) {
      let actual;
      try {
        actual = internals.dumpIndexTree(internals.parseIndexTree(raw));
      } catch {
        actual = null;
      }
      expect(actual, raw.slice(0, 80)).toBe(python);
    }
  });

  it("case folding matches str.casefold", () => {
    for (const [code, folded] of Object.entries(ORACLE.casefold.chars)) {
      expect(internals.casefold(String.fromCodePoint(Number.parseInt(code, 16))), code).toBe(folded);
    }
    expect(ORACLE.casefold.strings.map((value) => internals.casefold(value))).toEqual(ORACLE.casefold.folded);
  });

  it("new index records match, including key order", async () => {
    // Windows 上 path.join("/oracle-base", …) 得到 \oracle-base\…，用 resolve 出的绝对路径当根。
    const base = path.resolve("/oracle-base");
    for (const [index, { content }] of CASES.records.entries()) {
      const expected = localize(ORACLE.records[index], { BASE: base });
      const directory = path.join(base, "drafts", FIXED_ID.replace(/-/g, ""));
      const actual = await capture(() => newIndexRecord(FIXED_ID, "Record sample", directory, path.join(base, "drafts"), content));
      expect(actual).toEqual(expected);
      if (expected.result) expect(Object.keys(actual.result)).toEqual(Object.keys(expected.result));
    }
  });

  it("Mac index records carry exactly the fields of a real Jianying 11.6 entry", () => {
    const sample = JSON.parse(MAC_INDEX_SAMPLE);
    expect(sample.draft_ids).toBe(sample.all_draft_store.length);
    const real = sample.all_draft_store[0];
    const record = newIndexRecord(FIXED_ID, "样例", "/r/X", "/r", { create_time: 1, update_time: 2, duration: 3 }, BOX_PROFILES.mac);
    expect(Object.keys(record)).toEqual(Object.keys(real));
    for (const [key, value] of Object.entries(real)) {
      if (key.startsWith("tm_draft_cloud") || key.startsWith("cloud_") || key.startsWith("pippit") || key.startsWith("draft_is_") || key.startsWith("draft_cloud")) {
        expect(record[key], key).toEqual(value);
      }
    }
    expect(record.draft_json_file).toBe(path.join("/r/X", "draft_info.json"));
  });
});

describe.each(PLATFORMS)("oracle: create → repair flow of the original service (%s)", (platform) => {
  const draftModules = [["fake draft module", () => fakeDraftModule()]];
  if (HAS_REAL_DRAFT) draftModules.push(["real draft.mjs", () => undefined]);

  it.each(draftModules)("matches with %s", async (_label, makeModule) => {
    const flow = ORACLE.flow;
    const base = await makeTemp();
    const root = path.join(base, "drafts");
    mkdirSync(root);
    mkdirSync(path.join(base, "index"));
    const indexPath = path.join(base, "index", "root_meta_info.json");
    const roots = { MEDIA, BASE: base };
    // 起始索引里有别人的云草稿条目（64 位 id、1.0 一类写法），原仓库整份往返后它逐字节不变。
    writeFileSync(indexPath, localize(flow.index_before, roots, { json: true }));
    const projectV1 = localize(flow.project_v1, roots);
    const projectV2 = localize(flow.project_v2, roots);
    const media = localize(flow.media, roots);
    const box = createDraftBox({
      platform,
      indexPath,
      draftsRoot: root,
      draftsRootSource: "test",
      isJianyingRunning: () => false,
      draftModule: makeModule(),
      newDraftId: () => flow.draft_id,
    });
    const mainFile = BOX_PROFILES[platform].mainFile;
    // 与原仓库的有意差异：①新草稿的文件夹按草稿名命名（原仓库是去横线的 id）；
    // ②索引条目的素材大小取 draft_meta_info.json 的 draft_timeline_materials_size_（原仓库总是 0）。
    const idFolder = flow.draft_id.replace(/-/g, "");
    const folderName = projectV1.name;
    // ③草稿名一律等于文件夹名：修复时工程改了名（原仓库草稿名跟着变），这里仍是建草稿时的文件夹名。
    expect(projectV2.name).not.toBe(folderName);
    const repairedName = (value) => substitute(value, { [projectV2.name]: folderName });
    const materialsSize = flow.meta_after_create.draft_timeline_materials_size_;
    expect(materialsSize).toBeGreaterThan(0);
    expect(flow.meta_after_repair.draft_timeline_materials_size_).toBe(materialsSize);
    const forPlatform = (value) => {
      const renamed = substitute(value, { [idFolder]: folderName });
      return platform === "mac" ? substitute(renamed, { "/draft_content.json": `/${mainFile}` }) : renamed;
    };
    // 先在 oracle 的 "/" 写法上改文件夹名和主文件名，再换成本机路径；json：目标是索引原文。
    const expected = (value, options) => localize(forPlatform(value), roots, options);
    const macRecord = (record) => {
      if (record.draft_id !== flow.draft_id) return record;
      const sized = { ...record, draft_timeline_materials_size: materialsSize };
      if (platform !== "mac") return sized;
      // Mac 条目同剪映 11.6：draft_cover 指向草稿目录里的封面，父条目字段换名。
      const { tm_draft_parent_entry_id: parent, ...rest } = sized;
      const mac = { ...rest, tm_draft_cloud_parent_entry_id: parent, draft_cover: path.join(rest.draft_fold_path, "draft_cover.jpg") };
      return Object.fromEntries(Object.entries(mac).sort(([a], [b]) => (a < b ? -1 : 1)));
    };
    // Mac 的新条目插在最前、draft_ids 记条目数；Windows 同原仓库追加在末尾、draft_ids 不动。
    const expectedIndexDocument = (document) => {
      const store = document.all_draft_store.map(macRecord);
      if (platform !== "mac") return { ...document, all_draft_store: store };
      const ours = store.filter((record) => record.draft_id === flow.draft_id);
      return { ...document, all_draft_store: [...ours, ...store.filter((record) => record.draft_id !== flow.draft_id)], draft_ids: store.length };
    };
    const expectRawIndex = (expectedRaw) => {
      const raw = readFileSync(indexPath, "utf8");
      const zero = '"draft_timeline_materials_size":0,';
      expect(expectedRaw.split(zero)).toHaveLength(2);
      if (platform === "windows") expect(raw).toBe(expectedRaw.replace(zero, `"draft_timeline_materials_size":${materialsSize},`));
      expect(raw).toContain(localize(flow.foreign_entry, roots, { json: true }));
      expect(raw).toContain(`"untouched":${flow.untouched}`);
    };

    const created = await box.createDraft({ project: projectV1, media });
    const expectedCreated = expected(flow.created);
    expect(created).toMatchObject({ ...expectedCreated, draft: macRecord(expectedCreated.draft), record: macRecord(expectedCreated.record) });
    const indexAfterCreate = JSON.parse(readFileSync(indexPath, "utf8"));
    expect(indexAfterCreate).toEqual(expectedIndexDocument(expected(flow.index_after_create)));
    expectRawIndex(expected(flow.index_raw_after_create, { json: true }));
    const target = created.path;
    expect(target).toBe(path.join(root, "原生草稿 Native"));
    expect(created.draft.draft_timeline_materials_size).toBe(materialsSize);
    expect(JSON.parse(readFileSync(path.join(target, OWNER_MARKER), "utf8"))).toEqual(expected(flow.owner));
    const meta = JSON.parse(readFileSync(path.join(target, "draft_meta_info.json"), "utf8"));
    expect(meta).toEqual(expected(flow.meta_after_create));
    // 统一成 oracle 的 "/" 写法（Windows 上列出来是 Timelines\…）。
    const files = (directory) =>
      readdirSync(directory, { recursive: true })
        .map((name) => String(name).split(path.sep).join("/"))
        .filter((name) => !name.startsWith("materials/"))
        .sort();
    const macFiles = (list) => (platform === "mac" ? [...list.map((name) => name.replace("draft_content.json", mainFile)), "materials"].sort() : list);
    expect(files(target)).toEqual(macFiles(flow.files_after_create));

    writeFileSync(path.join(target, "custom-extra.bin"), "keep auxiliary data");
    const mainTimeline = path.join(target, "Timelines", flow.main_timeline_id, mainFile);
    mkdirSync(path.dirname(mainTimeline), { recursive: true });
    writeFileSync(mainTimeline, "opaque native main timeline");
    writeFileSync(`${mainTimeline}.bak`, "opaque native main timeline backup");
    writeFileSync(
      path.join(target, "Timelines", "project.json"),
      JSON.stringify({ main_timeline_id: flow.main_timeline_id, timelines: [{ id: flow.main_timeline_id }] }),
    );

    const repaired = await box.repairDraft({ project: projectV2, media, draftId: created.draftId });
    const expectedRepaired = repairedName(expected(flow.repaired));
    expect(repaired.draftName).toBe(folderName);
    expect(repaired.name).toBe(folderName);
    expect(repaired).toMatchObject({
      ...expectedRepaired,
      draft: macRecord(expectedRepaired.draft),
      record: macRecord(expectedRepaired.record),
      verification: { ...expectedRepaired.verification, main_timeline_path: path.join("Timelines", flow.main_timeline_id, mainFile) },
    });
    const indexAfterRepair = JSON.parse(readFileSync(indexPath, "utf8"));
    expect(indexAfterRepair).toEqual(expectedIndexDocument(repairedName(expected(flow.index_after_repair))));
    expectRawIndex(repairedName(expected(flow.index_raw_after_repair, { json: true })));
    expect(JSON.parse(readFileSync(path.join(target, "draft_meta_info.json"), "utf8"))).toEqual(repairedName(expected(flow.meta_after_repair)));
    const [token] = readdirSync(path.join(target, "Timelines", ".jianying_ai_repair_backups"));
    const afterRepair = files(target).map((name) => name.split(token).join("${TOKEN}"));
    expect(afterRepair).toEqual(macFiles(flow.files_after_repair).map((name) => (platform === "mac" ? name.replace("draft_content.json.bak", `${mainFile}.bak`) : name)).sort());
    const repairedContent = JSON.parse(readFileSync(path.join(target, mainFile), "utf8"));
    expect(repairedContent.name).toBe(folderName);
    expectNamedAfterFolder(target, indexPath, flow.draft_id, mainFile);
    expect(JSON.parse(readFileSync(mainTimeline, "utf8"))).toEqual({ ...repairedContent, id: flow.main_timeline_id });
    expect(flow.timeline_matches_content).toBe(true);
    expect(readFileSync(`${mainTimeline}.bak`).equals(readFileSync(mainTimeline))).toBe(flow.timeline_backup_matches_main);
    expect(listNames(root)).toEqual(expected(flow.root_entries_after_repair));
    expect(repaired.path).toBe(target);
  });
});

// ---------------------------------------------------------------- Mac 专有

async function makeMacHome({ globalSetting } = {}) {
  const home = await makeTemp();
  const root = path.join(home, "Movies", "JianyingPro", "User Data", "Projects", "com.lveditor.draft");
  mkdirSync(root, { recursive: true });
  if (globalSetting !== undefined) {
    const config = path.join(home, "Movies", "JianyingPro", "User Data", "Config");
    mkdirSync(config, { recursive: true });
    writeFileSync(path.join(config, "globalSetting"), globalSetting);
  }
  return { home, root };
}

function macBox(home, options = {}) {
  return createDraftBox({ platform: "darwin", home, env: {}, isJianyingRunning: () => false, draftModule: fakeDraftModule(), ...options });
}

function mediaInfo(name) {
  const info = JSON.parse(readFileSync(path.join(FIXTURES, "media-info.json"), "utf8"))[name];
  return { path: path.join(MEDIA, name), ...info };
}

describe("Mac draft box", () => {
  it("finds the default draft folder with the index inside it", async () => {
    const { home, root } = await makeMacHome();
    expect(discoverDraftBox({ platform: "darwin", home, env: {} })).toMatchObject({
      platform: "mac",
      draftsRoot: root,
      indexPath: path.join(root, "root_meta_info.json"),
      draftsRootSource: "default",
      reason: null,
    });
    expect(discoverDraftBox({ platform: "darwin", home: "", env: {} }).reason).toBe("home_unavailable");
  });

  it("follows the custom draft path in globalSetting, and the environment over both", async () => {
    const custom = await makeTemp();
    // QSettings 把值里的反斜杠写成 \\（Windows 上的临时目录带反斜杠）。
    const { home } = await makeMacHome({
      globalSetting: `[General]\ncurrentCachePath=/elsewhere\ncurrentCustomDraftPath=${custom.replaceAll("\\", "\\\\")}\n\n[Other]\ncurrentCustomDraftPath=/wrong\n`,
    });
    expect(discoverDraftBox({ platform: "darwin", home, env: {} })).toMatchObject({
      draftsRoot: custom,
      indexPath: path.join(custom, "root_meta_info.json"),
      draftsRootSource: "global_setting",
    });
    const override = await makeTemp();
    expect(discoverDraftBox({ platform: "darwin", home, env: { JY_JIANYING_DRAFTS_ROOT: ` ${override} ` } })).toMatchObject({
      draftsRoot: override,
      indexPath: path.join(override, "root_meta_info.json"),
      draftsRootSource: "environment",
    });
    const empty = await makeMacHome({ globalSetting: "[General]\ncurrentCustomDraftPath=\n" });
    expect(discoverDraftBox({ platform: "darwin", home: empty.home, env: {} }).draftsRootSource).toBe("default");
  });

  it("reads Qt INI values with quotes and escapes", () => {
    expect(readIniValue("[General]\nkey=\"/a b/\\\"c\\\"\"\n", "General", "key")).toBe("/a b/\"c\"");
    expect(readIniValue("[General]\nkey=/Users/\\x8349\\x7a3f\n", "General", "key")).toBe("/Users/草稿");
    expect(readIniValue("[general]\r\nkey = value \r\n", "General", "key")).toBe("value");
    expect(readIniValue("[Other]\nkey=x\n", "General", "key")).toBeNull();
  });

  it("creates a valid index when Jianying has not made a draft yet", async () => {
    const { home, root } = await makeMacHome();
    const box = macBox(home, { newDraftId: () => FIXED_ID });
    expect(await box.status()).toMatchObject({ status: "available", index_exists: false, draft_count: 0, platform: "mac" });
    expect((await box.listDrafts()).items).toEqual([]);
    const created = await box.createDraft({ project: makeProject({ name: "第一份" }) });
    const index = JSON.parse(readFileSync(path.join(root, "root_meta_info.json"), "utf8"));
    expect(Object.keys(index)).toEqual(["all_draft_store", "draft_ids", "root_path"]);
    expect(index.root_path).toBe(root);
    expect(index.draft_ids).toBe(1);
    const sample = JSON.parse(MAC_INDEX_SAMPLE).all_draft_store[0];
    expect(Object.keys(index.all_draft_store[0])).toEqual(Object.keys(sample));
    expect(index.all_draft_store[0]).toMatchObject({
      draft_id: FIXED_ID,
      draft_name: "第一份",
      draft_fold_path: created.path,
      draft_root_path: root,
      draft_json_file: path.join(created.path, "draft_info.json"),
    });
    expect(existsSync(path.join(created.path, "draft_info.json"))).toBe(true);
    expect(existsSync(path.join(created.path, "draft_content.json"))).toBe(false);
    // Windows 上 stat 只反映只读属性（可写文件一律是 0o666），权限位没法断言。
    if (process.platform !== "win32") expect(statSync(path.join(root, "root_meta_info.json")).mode & 0o777).toBe(0o644);

    const second = await createDraftBox({ platform: "darwin", home, env: {}, isJianyingRunning: () => false, draftModule: fakeDraftModule() }).createDraft({
      project: makeProject({ id: "f".repeat(32), name: "第二份" }),
    });
    const after = JSON.parse(readFileSync(path.join(root, "root_meta_info.json"), "utf8"));
    expect(after.draft_ids).toBe(2);
    expect(after.all_draft_store.map((item) => item.draft_id)).toEqual([second.draftId, FIXED_ID]);
    expect((await box.listDrafts()).total).toBe(2);
  });

  it("appends to a real Jianying 11.6 index without touching its entry", async () => {
    const { home, root } = await makeMacHome();
    const sampleText = localize(MAC_INDEX_SAMPLE, { ROOT: root }, { json: true });
    writeFileSync(path.join(root, "root_meta_info.json"), sampleText);
    mkdirSync(path.join(root, "10月19日"));
    writeFileSync(path.join(root, "10月19日", "draft_info.json"), "encrypted");
    const box = macBox(home);
    expect((await box.listDrafts()).items.map((item) => item.name)).toEqual(["10月19日"]);
    const created = await box.createDraft({ project: makeProject() });
    const index = JSON.parse(readFileSync(path.join(root, "root_meta_info.json"), "utf8"));
    expect(index.draft_ids).toBe(2);
    expect(index.all_draft_store[0].draft_id).toBe(created.draftId);
    expect(index.all_draft_store[1]).toEqual(JSON.parse(sampleText).all_draft_store[0]);
    expect((await box.listDrafts()).items.map((item) => item.id)).toEqual(expect.arrayContaining([created.draftId, "D00316B3-C721-4792-86C2-33F289473FB1"]));
  });

  it.each([
    ["through materialPath", true],
    ["by rewriting paths afterwards", false],
  ])("clones materials into the draft and rewrites their paths (%s)", async (_label, honorMaterialPath) => {
    const { home, root } = await makeMacHome();
    const library = await makeTemp();
    for (const name of ["clip-720p.mp4", "still.png", "tone.mp3"]) {
      writeFileSync(path.join(library, name), readFileSync(path.join(MEDIA, name)));
    }
    const media = ["clip-720p.mp4", "still.png", "tone.mp3"].map((name) => ({ ...mediaInfo(name), path: path.join(library, name) }));
    const copies = [];
    const box = macBox(home, {
      draftModule: fakeDraftModule({ honorMaterialPath }),
      copyMaterial: async (source, destination) => {
        copies.push(source);
        writeFileSync(destination, readFileSync(source), { flag: "wx" });
      },
    });
    // 同一个文件出现两次只拷一份。
    const created = await box.createDraft({ project: makeProject(), media: [...media, media[0]] });
    const materials = path.join(created.path, "materials");
    const names = media.map((info) => materialFileName(realpathSync.native(info.path)));
    expect(listNames(materials)).toEqual([...names].sort());
    expect(copies).toHaveLength(3);
    for (const [index, info] of media.entries()) {
      expect(readFileSync(path.join(materials, names[index])).equals(readFileSync(info.path))).toBe(true);
    }
    expect(names[0]).toMatch(/^[0-9a-f]{16}\.mp4$/);
    const content = JSON.parse(readFileSync(path.join(created.path, "draft_info.json"), "utf8"));
    const written = [...content.materials.videos, ...content.materials.audios].map((item) => item.path);
    expect(written).toEqual(expect.arrayContaining(names.map((name) => path.join(created.path, "materials", name))));
    expect(JSON.stringify(content)).not.toContain(library);
    expect(content.materials.videos[0].material_name).toBe("clip-720p.mp4");

    // 素材库删了文件，草稿照样完整；修复时重新拷，路径写成发布后的位置而不是暂存目录。
    const created2 = await box.repairDraft({ project: makeProject({ name: "改过" }), media, draftId: created.draftId });
    const repairedContent = JSON.parse(readFileSync(path.join(created2.path, "draft_info.json"), "utf8"));
    for (const item of [...repairedContent.materials.videos, ...repairedContent.materials.audios]) {
      expect(path.dirname(item.path)).toBe(path.join(created.path, "materials"));
      expect(existsSync(item.path)).toBe(true);
    }
    expect(JSON.stringify(repairedContent)).not.toContain(".repair");
    expect(listNames(materials)).toEqual([...names].sort());
    expect(readdirSync(root).filter((name) => name.startsWith("."))).toEqual([]);
  });

  it("clones with the real copy routine and keeps the source untouched", async () => {
    const { home } = await makeMacHome();
    const box = macBox(home);
    const info = mediaInfo("still.png");
    const before = readFileSync(info.path);
    const created = await box.createDraft({ project: makeProject(), media: [info] });
    const cloned = path.join(created.path, "materials", materialFileName(info.path));
    expect(readFileSync(cloned).equals(before)).toBe(true);
    expect(readFileSync(info.path).equals(before)).toBe(true);
  });

  it("rejects missing material files before creating anything", async () => {
    const { home, root } = await makeMacHome();
    const box = macBox(home);
    const error = await catchAsync(box.createDraft({ project: makeProject(), media: [{ ...mediaInfo("still.png"), path: path.join(root, "gone.png") }] }));
    expectEdit(error, "MEDIA_NOT_FOUND", 422);
    expect(readdirSync(root)).toEqual([]);
  });

  it("refuses to write while VideoFusion-macOS is running", async () => {
    const { home, root } = await makeMacHome();
    let running = false;
    const box = macBox(home, { isJianyingRunning: () => running });
    const created = await box.createDraft({ project: makeProject() });
    const indexBefore = readFileSync(path.join(root, "root_meta_info.json"));
    running = true;
    expect((await box.status()).jianyingRunning).toBe(true);
    expectEdit(await catchAsync(box.createDraft({ project: makeProject() })), "DRAFT_BOX_APP_RUNNING", 409);
    expectEdit(await catchAsync(box.repairDraft({ project: makeProject(), draftId: created.draftId })), "DRAFT_BOX_APP_RUNNING", 409);
    expect(readFileSync(path.join(root, "root_meta_info.json")).equals(indexBefore)).toBe(true);
    expect(readdirSync(root).sort()).toEqual(["Native new draft", "root_meta_info.json"].sort());
  });

  it("reports unavailable when Jianying never created its draft folder", async () => {
    const home = await makeTemp();
    const box = macBox(home);
    expect(await box.status()).toMatchObject({ status: "unavailable", reason: "index_not_found" });
    expectEdit(await catchAsync(box.createDraft({ project: makeProject() })), "DRAFT_BOX_WRITE_UNAVAILABLE", 503);
  });
});

// ---------------------------------------------------------------- 剪映进程检测与注册表（不卡主进程）

describe("Jianying process check", () => {
  /** 换掉 execFile：按程序名回结果，记下起过哪些子进程。 */
  function fakeExecFile(respond) {
    const spawned = [];
    vi.mocked(execFile).mockImplementation((file, args, options, callback) => {
      const name = path.basename(String(file).replaceAll("\\", "/"));
      spawned.push(name);
      setTimeout(() => {
        const [error, stdout] = respond(name);
        callback(error, stdout, "");
      }, 5);
      return {};
    });
    return spawned;
  }

  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(execFile).mockReset();
    vi.mocked(execFileSync).mockClear();
  });

  /** 定住时钟：缓存期内的几次检查一定复用，机器慢也不影响计数。 */
  const freezeClock = () => vi.useFakeTimers({ toFake: ["Date"] });

  it("caches a probe briefly, shares a running probe and never caches a failure", async () => {
    let clock = 0;
    let calls = 0;
    let fail = false;
    const probe = cachedProbe(
      async () => {
        calls += 1;
        if (fail) throw new Error("probe failed");
        return calls;
      },
      { ttlMs: 1_000, now: () => clock },
    );
    expect(await Promise.all([probe(), probe(), probe({ fresh: true })])).toEqual([1, 1, 1]);
    clock = 999;
    expect(await probe()).toBe(1);
    expect(await probe({ fresh: true })).toBe(2);
    clock = 2_000;
    expect(await probe()).toBe(3);
    fail = true;
    expect(await catchAsync(probe({ fresh: true }))).toMatchObject({ message: "probe failed" });
    expect(await catchAsync(probe({ fresh: true }))).toMatchObject({ message: "probe failed" });
    expect(calls).toBe(5);
  });

  it("runs pgrep without blocking and shares one run between a status poll and an export", async () => {
    const { home } = await makeMacHome();
    // pgrep 没找到进程时退出码是 1。
    freezeClock();
    const spawned = fakeExecFile(() => [Object.assign(new Error("exit 1"), { code: 1 }), ""]);
    const box = createDraftBox({ platform: "darwin", home, env: {}, draftModule: fakeDraftModule() });
    const polls = [box.status(), box.status()];
    expect(polls[0]).toBeInstanceOf(Promise);
    expect((await Promise.all(polls)).map((status) => status.jianyingRunning)).toEqual([false, false]);
    expect(spawned).toEqual(["pgrep"]);
    const created = await box.createDraft({ project: makeProject() });
    // 新建里四次检查：缓存期内的三次复用，写索引前那次重新跑。
    expect(spawned).toEqual(["pgrep", "pgrep"]);
    await box.repairDraft({ project: makeProject(), draftId: created.draftId });
    expect(spawned).toHaveLength(3);
    expect(execFileSync).not.toHaveBeenCalled();
  });

  it("reports a running Jianying from pgrep and refuses to write", async () => {
    const { home } = await makeMacHome();
    fakeExecFile(() => [null, "4242\n"]);
    const box = createDraftBox({ platform: "darwin", home, env: {}, draftModule: fakeDraftModule() });
    expect((await box.status()).jianyingRunning).toBe(true);
    expectEdit(await catchAsync(box.createDraft({ project: makeProject() })), "DRAFT_BOX_APP_RUNNING", 409);
  });

  it("reads the Windows registry and tasklist without blocking and caches them briefly", async () => {
    const local = await makeTemp();
    const root = path.join(local, "Drafts");
    mkdirSync(path.join(root, "One"), { recursive: true });
    writeIndex(root, {
      indexParent: path.join(local, "JianyingPro", "User Data", "Projects", "com.lveditor.draft"),
      drafts: [{ draft_id: "ONE", draft_name: "One", draft_fold_path: path.join(root, "One"), draft_root_path: root }],
    });
    freezeClock();
    const spawned = fakeExecFile((name) => {
      if (name === "reg.exe") {
        return [null, Buffer.from(`\r\nHKEY_CURRENT_USER\\Software\r\n    currentCustomDraftPath    REG_SZ    ${root}\r\n\r\n`, "utf8")];
      }
      return [null, "INFO: No tasks are running which match the specified criteria.\r\n"];
    });
    const box = createDraftBox({ platform: "win32", env: { LOCALAPPDATA: local, SystemRoot: "C:\\Windows" } });
    const first = await box.status();
    expect(first).toMatchObject({ status: "available", drafts_root_source: "registry", draft_count: 1, jianyingRunning: false });
    expect([...spawned].sort()).toEqual(["reg.exe", "tasklist.exe"]);
    expect((await box.listDrafts()).items.map((item) => item.id)).toEqual(["ONE"]);
    expect(await box.status()).toEqual(first);
    expect(spawned).toHaveLength(2);
    expect(execFileSync).not.toHaveBeenCalled();
  });

  it("treats a failed registry read like discovery does", async () => {
    const local = await makeTemp();
    fakeExecFile(() => [Object.assign(new Error("access denied"), { code: 5 }), ""]);
    const box = createDraftBox({ platform: "win32", env: { LOCALAPPDATA: local }, isJianyingRunning: () => false });
    expect(await box.status()).toMatchObject({ status: "unavailable", reason: "drafts_root_unavailable" });
  });
});

// ---------------------------------------------------------------- Windows 发现

describe("Windows discovery", () => {
  it("keeps the environment root and the registry separate", async () => {
    const local = await makeTemp();
    const root = path.join(local, "custom");
    const discovery = discoverDraftBox({
      platform: "win32",
      env: { LOCALAPPDATA: local, JY_JIANYING_DRAFTS_ROOT: root },
      readRegistryDraftRoot: () => "ignored",
    });
    expect(discovery.indexPath).toBe(path.join(local, "JianyingPro", "User Data", "Projects", "com.lveditor.draft", "root_meta_info.json"));
    expect(discovery.draftsRoot).toBe(root);
    expect(discovery.draftsRootSource).toBe("environment");
  });

  it("uses the registry, falls back to the index folder only when the value is absent", async () => {
    const local = await makeTemp();
    const indexDirectory = path.join(local, "JianyingPro", "User Data", "Projects", "com.lveditor.draft");
    expect(discoverDraftBox({ platform: "win32", env: { LOCALAPPDATA: local }, readRegistryDraftRoot: () => "D:\\剪映草稿 " })).toMatchObject({
      draftsRoot: "D:\\剪映草稿",
      draftsRootSource: "registry",
    });
    expect(discoverDraftBox({ platform: "win32", env: { LOCALAPPDATA: local }, readRegistryDraftRoot: () => null }).reason).toBe("drafts_root_unavailable");
    mkdirSync(indexDirectory, { recursive: true });
    expect(discoverDraftBox({ platform: "win32", env: { LOCALAPPDATA: local }, readRegistryDraftRoot: () => null })).toMatchObject({
      draftsRoot: indexDirectory,
      draftsRootSource: "default",
      reason: null,
    });
    expect(
      discoverDraftBox({
        platform: "win32",
        env: { LOCALAPPDATA: local },
        readRegistryDraftRoot: () => {
          throw new Error("reg.exe failed");
        },
      }),
    ).toMatchObject({ draftsRoot: null, reason: "drafts_root_unavailable" });
    expect(discoverDraftBox({ platform: "win32", env: {} }).reason).toBe("local_app_data_unavailable");
  });

  it("parses reg query output in UTF-8 and GBK", () => {
    const output = "\r\nHKEY_CURRENT_USER\\Software\\ByteDance\\JianyingPro\\GlobalSettings\\History\r\n    currentCustomDraftPath    REG_SZ    D:\\剪映草稿\\JianyingPro Drafts\r\n\r\n";
    expect(parseRegQueryOutput(Buffer.from(output, "utf8"))).toBe("D:\\剪映草稿\\JianyingPro Drafts");
    // 「剪映草稿」的 GBK 编码。
    const gbk = Buffer.concat([
      Buffer.from("    currentCustomDraftPath    REG_SZ    D:\\", "latin1"),
      Buffer.from([0xbc, 0xf4, 0xd3, 0xb3, 0xb2, 0xdd, 0xb8, 0xe5]),
      Buffer.from("\r\n", "latin1"),
    ]);
    expect(parseRegQueryOutput(gbk)).toBe("D:\\剪映草稿");
    expect(parseRegQueryOutput("    currentCustomDraftPath    REG_EXPAND_SZ    %USERPROFILE%\\x\r\n")).toBeNull();
    expect(parseRegQueryOutput("ERROR: The system was unable to find the specified registry key or value.")).toBeNull();
  });

  it("lists drafts from a Windows layout found through LOCALAPPDATA", async () => {
    const local = await makeTemp();
    const indexDirectory = path.join(local, "JianyingPro", "User Data", "Projects", "com.lveditor.draft");
    const root = path.join(local, "Drafts");
    mkdirSync(path.join(root, "One"), { recursive: true });
    writeIndex(root, {
      indexParent: indexDirectory,
      drafts: [{ draft_id: "ONE", draft_name: "One", draft_fold_path: path.join(root, "One"), draft_root_path: root }],
    });
    const box = createDraftBox({ platform: "win32", env: { LOCALAPPDATA: local }, readRegistryDraftRoot: () => root, isJianyingRunning: () => false });
    expect(await box.status()).toMatchObject({ status: "available", drafts_root_source: "registry", draft_count: 1 });
    expect((await box.listDrafts()).items.map((item) => item.id)).toEqual(["ONE"]);
  });
});

describe("Mac draft_ids counter", () => {
  // 剪映 Mac 11.6 真机：建过 8 份、删到只剩 1 份，索引里 draft_ids 仍是 8——它是只增不减的计数，不是条目数
  it("keeps counting up from the stored value instead of resetting to the entry count", async () => {
    const base = await makeTemp();
    {
      const root = path.join(base, "drafts");
      mkdirSync(root);
      const indexPath = path.join(root, "root_meta_info.json");
      const kept = { draft_id: "11111111-1111-1111-1111-111111111111", draft_name: "留下的", draft_fold_path: path.join(root, "留下的") };
      writeFileSync(indexPath, JSON.stringify({ all_draft_store: [kept], draft_ids: 8, root_path: root }));
      const box = createDraftBox({ platform: "darwin", indexPath, draftsRoot: root, isJianyingRunning: () => false, draftModule: fakeDraftModule() });
      await box.createDraft({ project: makeProject() });
      expect(JSON.parse(readFileSync(indexPath, "utf8")).draft_ids).toBe(9);
    }
  });
});

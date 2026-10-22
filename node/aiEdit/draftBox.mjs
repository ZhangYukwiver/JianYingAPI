// 剪映草稿箱：读剪映本地草稿索引 root_meta_info.json，新建本服务生成的草稿、原位修复本服务拥有的草稿。
// 移植自 pyJianYingDraft（Apache-2.0，见 node/licenses）与 JianYingAPI 的 draft_box.py，
// 校验、所有权、冲突检测与失败回滚的语义逐条对齐原仓库；在此之上补了 Mac 版剪映。
// Windows 与 Mac 的差异（索引位置、主文件名、索引条目字段、素材是否拷进草稿）集中在 BOX_PROFILES。
// 与原仓库的有意差异：新草稿的文件夹按草稿名命名（剪映自己就是这样，打开保存后还会把显示名改成文件夹名），
// 原位修复按索引登记的 draft_fold_path 找目录，不再从草稿 id 推算。

import { execFile, execFileSync } from "node:child_process";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import {
  accessSync,
  closeSync,
  constants as fsConstants,
  cpSync,
  copyFileSync,
  fsyncSync,
  linkSync,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  readSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { copyFile, mkdir, realpath, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { isDeepStrictEqual } from "node:util";

import { EditError } from "./errors.mjs";

export const INDEX_FILE = "root_meta_info.json";
export const REGISTRY_KEY = "HKCU\\Software\\ByteDance\\JianyingPro\\GlobalSettings\\History";
export const REGISTRY_VALUE = "currentCustomDraftPath";
export const MAX_INDEX_BYTES = 16 * 1024 * 1024;
export const OWNER_MARKER = ".jianying_ai_owned.json";
export const MATERIALS_DIRECTORY = "materials";
export const DRAFT_ID = /^[A-Za-z0-9_-]{1,128}$/;
export const DRAFT_UUID = /^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$/;

const WINDOWS_INDEX_PARTS = ["JianyingPro", "User Data", "Projects", "com.lveditor.draft"];
const MAC_DRAFTS_PARTS = ["Movies", "JianyingPro", "User Data", "Projects", "com.lveditor.draft"];
const MAC_GLOBAL_SETTING_PARTS = ["Movies", "JianyingPro", "User Data", "Config", "globalSetting"];
const OWNER_GENERATOR = "jianying-ai-service";
const REPAIR_BACKUPS = ".jianying_ai_repair_backups";
const MAX_TIMELINE_MANIFEST_BYTES = 1024 * 1024;
export const DEFAULT_FOLDER_NAME = "AI 剪辑";
// 名字本身最多 150 字节，留出「 (9999)」和 Windows 路径的余量（APFS 单层上限 255 字节）。
const MAX_FOLDER_BYTES = 150;
const MAX_FOLDER_SUFFIX = 9999;
// 修复时暂存目录里那一层用短名字，照顾 Windows 的路径长度。
const STAGED_FOLDER = "draft";
const UNSAFE_FOLDER_CHARACTERS = /[/\\:*?"<>|\u0000-\u001f\u007f]/g;
const FOLDER_EDGES = /^[\s.]+|[\s.]+$/gu;
const WINDOWS_RESERVED_NAMES = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i;
const GRAPHEMES = new Intl.Segmenter(undefined, { granularity: "grapheme" });
// +08:00 下能表示的最大秒数（9999-12-31 23:59:59 本地时间），超出时原仓库的 fromtimestamp 会溢出返回 None。
const MAX_LOCAL_SECONDS = 253402271999;
const CHINA_OFFSET_SECONDS = 8 * 3600;
// 路径比较照 Python 的 os.path.normcase：只在宿主是 Windows 时忽略大小写。
const CASE_INSENSITIVE_PATHS = process.platform === "win32";
const BASE_GENERATED = ["draft_content.json", "draft_meta_info.json", "project.json", "draft_cover.jpg", ".owned", OWNER_MARKER];

export const BOX_PROFILES = {
  windows: {
    mainFile: "draft_content.json",
    generatedEntries: new Set(BASE_GENERATED),
    newRecordsFirst: false,
    countDraftIds: false,
    cloneMaterials: false,
    parentEntryField: "tm_draft_parent_entry_id",
    // 原仓库写空串；Windows 版剪映照样能列出。
    coverInFolder: false,
  },
  // 剪映 Mac 11.6 实测：主文件叫 draft_info.json，索引就在草稿根目录里，新条目插在最前且 draft_ids 是只增不减的计数，
  // 条目字段用 tm_draft_cloud_parent_entry_id。素材拷进草稿自己的 materials/，素材库删了草稿也不坏。
  mac: {
    mainFile: "draft_info.json",
    generatedEntries: new Set([...BASE_GENERATED, "draft_info.json", MATERIALS_DIRECTORY]),
    newRecordsFirst: true,
    countDraftIds: true,
    cloneMaterials: true,
    parentEntryField: "tm_draft_cloud_parent_entry_id",
    // 剪映 Mac 11.6 的条目：draft_cover 指向 <草稿目录>/draft_cover.jpg（剪映保存时自己生成这张图）。
    coverInFolder: true,
  },
};

const OPERATIONS = {
  status: "available",
  list: "available",
  get: "available",
  create: "available",
  repair: "owned_draft_only",
  edit: "unsupported_encrypted",
  delete: "unsupported",
  write_support: "create_and_repair_owned",
  open: "unsupported",
  export_video: "unsupported",
};

export function normalizePlatform(value) {
  const raw = String(value ?? "").toLowerCase();
  if (raw === "darwin" || raw === "mac" || raw === "macos") return "mac";
  if (raw === "win32" || raw === "windows") return "windows";
  return null;
}

// ---------------------------------------------------------------- 发现草稿箱

/** 找剪映的草稿根目录与索引位置。不读写索引本身。 */
export function discoverDraftBox({
  platform = process.platform,
  env = process.env,
  home = os.homedir(),
  readRegistryDraftRoot,
} = {}) {
  const kind = normalizePlatform(platform);
  const explicitRoot = String(env?.JY_JIANYING_DRAFTS_ROOT ?? "").trim();
  if (kind === "windows") {
    const localAppData = env?.LOCALAPPDATA;
    const indexDirectory = localAppData ? path.join(localAppData, ...WINDOWS_INDEX_PARTS) : null;
    let draftsRoot = null;
    let draftsRootSource = null;
    if (explicitRoot) {
      draftsRoot = explicitRoot;
      draftsRootSource = "environment";
    } else {
      let value;
      let failed = false;
      try {
        value = (readRegistryDraftRoot ?? (() => readWindowsRegistryDraftRoot(env)))();
      } catch {
        failed = true;
      }
      if (typeof value === "string" && value.trim()) {
        draftsRoot = value.trim();
        draftsRootSource = "registry";
      } else if (!failed && indexDirectory && isDirectory(indexDirectory)) {
        // 没改过草稿位置时注册表里没有这一项，剪映用的就是索引所在的默认目录。
        draftsRoot = indexDirectory;
        draftsRootSource = "default";
      }
    }
    return {
      platform: kind,
      indexPath: indexDirectory ? path.join(indexDirectory, INDEX_FILE) : null,
      draftsRoot,
      draftsRootSource,
      indexSource: "local_app_data",
      reason: !indexDirectory ? "local_app_data_unavailable" : !draftsRoot ? "drafts_root_unavailable" : null,
    };
  }
  if (kind === "mac") {
    if (!home) {
      return { platform: kind, indexPath: null, draftsRoot: null, draftsRootSource: null, indexSource: "drafts_root", reason: "home_unavailable" };
    }
    let draftsRoot;
    let draftsRootSource;
    if (explicitRoot) {
      draftsRoot = explicitRoot;
      draftsRootSource = "environment";
    } else {
      const custom = readMacCustomDraftRoot(home);
      draftsRoot = custom ?? path.join(home, ...MAC_DRAFTS_PARTS);
      draftsRootSource = custom ? "global_setting" : "default";
    }
    return { platform: kind, indexPath: path.join(draftsRoot, INDEX_FILE), draftsRoot, draftsRootSource, indexSource: "drafts_root", reason: null };
  }
  return { platform: null, indexPath: null, draftsRoot: null, draftsRootSource: null, indexSource: null, reason: "platform_unsupported" };
}

function registryCommand(env) {
  return path.join(env?.SystemRoot || env?.SYSTEMROOT || "C:\\Windows", "System32", "reg.exe");
}

/** discoverDraftBox 单独调用时的默认读法（同步）。草稿箱服务里走下面的异步版，不卡采集器所在的主进程。 */
function readWindowsRegistryDraftRoot(env) {
  let output;
  try {
    output = execFileSync(registryCommand(env), ["query", REGISTRY_KEY, "/v", REGISTRY_VALUE], { windowsHide: true, timeout: 10_000, stdio: ["ignore", "pipe", "ignore"] });
  } catch (error) {
    // reg query 找不到键或值时退出码是 1。
    if (error?.status === 1) return null;
    throw error;
  }
  return parseRegQueryOutput(output);
}

async function readWindowsRegistryDraftRootAsync(env) {
  let output;
  try {
    output = await runFile(registryCommand(env), ["query", REGISTRY_KEY, "/v", REGISTRY_VALUE], { windowsHide: true, timeout: 10_000, encoding: "buffer" });
  } catch (error) {
    if (error?.code === 1) return null;
    throw error;
  }
  return parseRegQueryOutput(output);
}

/** 解析 `reg query ... /v currentCustomDraftPath` 的输出。中文系统下输出是本地代码页，先按 UTF-8、不行再按 GBK 解。 */
export function parseRegQueryOutput(output) {
  const buffer = Buffer.isBuffer(output) ? output : Buffer.from(String(output ?? ""), "utf8");
  const candidates = [];
  for (const encoding of ["utf-8", "gbk"]) {
    try {
      candidates.push(new TextDecoder(encoding, { fatal: true }).decode(buffer));
    } catch {
      // 这台机器没有这个解码器，或字节不是这个编码。
    }
  }
  for (const text of candidates) {
    for (const line of text.split(/\r?\n/)) {
      const match = /^\s*currentCustomDraftPath\s+(REG_\w+)\s+(.*)$/i.exec(line);
      if (!match) continue;
      // 原仓库只认 REG_SZ。
      if (match[1].toUpperCase() !== "REG_SZ") return null;
      const value = match[2].trim();
      return value || null;
    }
  }
  return null;
}

function readMacCustomDraftRoot(home) {
  let text;
  try {
    text = readFileSync(path.join(home, ...MAC_GLOBAL_SETTING_PARTS), "utf8");
  } catch {
    return null;
  }
  const value = readIniValue(text, "General", REGISTRY_VALUE);
  return value && value.trim() ? value.trim() : null;
}

/** 读 Qt QSettings 写的 INI（剪映 Mac 的 globalSetting）里某一段的某个键。 */
export function readIniValue(text, section, key) {
  let current = null;
  for (const rawLine of String(text).split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith(";") || line.startsWith("#")) continue;
    const header = /^\[(.*)\]$/.exec(line);
    if (header) {
      current = header[1].trim();
      continue;
    }
    if (current?.toLowerCase() !== section.toLowerCase()) continue;
    const separator = line.indexOf("=");
    if (separator < 0 || line.slice(0, separator).trim() !== key) continue;
    return decodeQtIniValue(line.slice(separator + 1).trim());
  }
  return null;
}

function decodeQtIniValue(raw) {
  let value = raw;
  if (value.length >= 2 && value.startsWith("\"") && value.endsWith("\"")) value = value.slice(1, -1);
  return value.replace(/\\(x[0-9A-Fa-f]{1,4}|.)/g, (_, escape) => {
    if (escape[0] === "x" && escape.length > 1) return String.fromCharCode(Number.parseInt(escape.slice(1), 16));
    return { n: "\n", r: "\r", t: "\t", "0": "\0" }[escape] ?? escape;
  });
}

// 剪映进程检测和读注册表都要起子进程（pgrep / tasklist / reg，几十到上百毫秒）。采集器跑在 Electron 主进程里，
// 同步等子进程会让整个应用跟着卡，所以一律异步，结果再缓存一小会儿：页面轮询状态、一次导出里的几次检查共用一次。
const PROCESS_CHECK_TTL_MS = 1_500;
const REGISTRY_TTL_MS = 2_000;

/** 异步跑一个程序，返回 stdout；退出码非 0 时 error.code 是退出码。 */
function runFile(file, args, options) {
  return new Promise((resolve, reject) => {
    execFile(file, args, options, (error, stdout) => (error ? reject(error) : resolve(stdout)));
  });
}

/**
 * 把异步探测包成「短时缓存 + 同一时刻只跑一个」：缓存期内直接用上次的结果；fresh 时不用缓存（写入前那次），
 * 但正在跑的那次照样共用。探测失败不缓存，下次重试。
 */
export function cachedProbe(probe, { ttlMs = PROCESS_CHECK_TTL_MS, now = () => Date.now() } = {}) {
  let cached = null;
  let pending = null;
  return ({ fresh = false } = {}) => {
    if (!fresh && cached && now() - cached.at < ttlMs) return Promise.resolve(cached.value);
    if (pending) return pending;
    const startedAt = now();
    pending = Promise.resolve()
      .then(probe)
      .then((value) => {
        cached = { at: startedAt, value };
        return value;
      })
      .finally(() => {
        pending = null;
      });
    return pending;
  };
}

function defaultRunningCheck(kind, env) {
  if (kind === "mac") {
    return cachedProbe(async () => {
      try {
        await runFile("/usr/bin/pgrep", ["-x", "VideoFusion-macOS"], { timeout: 5_000 });
        return true;
      } catch (error) {
        // pgrep 没找到进程时退出码是 1。
        if (error?.code === 1) return false;
        throw error;
      }
    });
  }
  if (kind === "windows") {
    return cachedProbe(async () => {
      const tasklist = path.join(env?.SystemRoot || env?.SYSTEMROOT || "C:\\Windows", "System32", "tasklist.exe");
      const output = await runFile(tasklist, ["/FI", "IMAGENAME eq JianyingPro.exe", "/FO", "CSV", "/NH"], {
        windowsHide: true,
        timeout: 10_000,
        encoding: "latin1",
      });
      return /^"jianyingpro\.exe"/im.test(output);
    });
  }
  return () => false;
}

// ---------------------------------------------------------------- 草稿箱服务

/**
 * 剪映草稿箱。platform/env/home 决定去哪找草稿箱；isJianyingRunning 检测剪映是否在运行，返回布尔或 Promise<布尔>，
 * 会收到 { fresh }（写入前那次为 true；默认检测据此跳过缓存）。
 * 测试或特殊部署可直接给 indexPath/draftsRoot（同原仓库 DraftBoxService 的构造参数），
 * draftModule 换掉草稿序列化模块，projectBuilder 换掉整个建草稿步骤，newDraftId 固定新草稿 id。
 */
export function createDraftBox({
  platform = process.platform,
  env = process.env,
  home = os.homedir(),
  isJianyingRunning,
  readRegistryDraftRoot,
  indexPath: explicitIndexPath,
  draftsRoot: explicitDraftsRoot,
  draftsRootSource: explicitDraftsRootSource,
  projectBuilder,
  draftModule,
  newDraftId = () => randomUUID().toUpperCase(),
  animationResourceRoots = [],
  copyMaterial = cloneOrCopyFile,
} = {}) {
  const kind = normalizePlatform(platform);
  const profile = BOX_PROFILES[kind] ?? BOX_PROFILES.windows;
  const runningCheck = isJianyingRunning ?? defaultRunningCheck(kind, env);
  const registryLookup =
    kind === "windows" && !readRegistryDraftRoot ? cachedProbe(() => readWindowsRegistryDraftRootAsync(env), { ttlMs: REGISTRY_TTL_MS }) : null;
  let tail = Promise.resolve();

  /** Windows 上异步读一次注册表（短时缓存），交给 discoverDraftBox 的是现成结果，不在主进程里同步起 reg.exe。 */
  async function registryReader() {
    if (readRegistryDraftRoot || !registryLookup) return readRegistryDraftRoot;
    // 环境变量指定了草稿根目录时 discoverDraftBox 不读注册表。
    if (String(env?.JY_JIANYING_DRAFTS_ROOT ?? "").trim()) return undefined;
    try {
      const value = await registryLookup();
      return () => value;
    } catch (error) {
      return () => {
        throw error;
      };
    }
  }

  // 每次操作重新找：剪映改了草稿位置、或采集器开着时才装上剪映都能跟上。
  async function currentPaths() {
    const explicit = explicitIndexPath !== undefined && explicitDraftsRoot !== undefined;
    const found = !kind
      ? { indexPath: null, draftsRoot: null, draftsRootSource: null, reason: "platform_unsupported" }
      : explicit
        ? { indexPath: null, draftsRoot: null, draftsRootSource: null, reason: null }
        : discoverDraftBox({ platform: kind, env, home, readRegistryDraftRoot: await registryReader() });
    const indexPath = explicitIndexPath !== undefined ? explicitIndexPath : found.indexPath;
    const draftsRoot = explicitDraftsRoot !== undefined ? explicitDraftsRoot : found.draftsRoot;
    return {
      indexPath: indexPath == null ? null : String(indexPath),
      draftsRoot: draftsRoot == null ? null : String(draftsRoot),
      draftsRootSource: explicitDraftsRootSource !== undefined ? explicitDraftsRootSource : explicitDraftsRoot !== undefined ? null : found.draftsRootSource,
      reason: found.reason,
    };
  }

  function exclusive(task) {
    const run = tail.then(task);
    tail = run.then(noop, noop);
    return run;
  }

  async function runningState() {
    try {
      const running = await runningCheck({ fresh: false });
      return typeof running === "boolean" ? running : null;
    } catch {
      return null;
    }
  }

  /** fresh：写入前最后那次检查不用缓存的结果。 */
  async function checkStopped({ fresh = false, message = "Close Jianying before changing a draft" } = {}) {
    let running;
    try {
      running = await runningCheck({ fresh });
    } catch {
      throw new EditError("DRAFT_BOX_PROCESS_CHECK_FAILED", "Could not determine whether Jianying is running", { status: 503 });
    }
    if (typeof running !== "boolean") {
      throw new EditError("DRAFT_BOX_PROCESS_CHECK_FAILED", "Could not determine whether Jianying is running", { status: 503 });
    }
    if (running) {
      throw new EditError("DRAFT_BOX_APP_RUNNING", message, { status: 409, details: { action: "close_jianying_and_retry" } });
    }
  }

  function requireWritablePlatform(action) {
    if (!kind) {
      throw new EditError("DRAFT_BOX_WRITE_UNAVAILABLE", `Native draft ${action} is only available on Windows and macOS`, {
        status: 503,
        details: { reason: "platform_unsupported" },
      });
    }
  }

  function availability(paths) {
    if (!paths.indexPath) return [paths.reason === "home_unavailable" ? "home_unavailable" : "local_app_data_unavailable", null];
    if (!paths.draftsRoot) return ["drafts_root_unavailable", null];
    if (regularFile(paths.indexPath) === null) {
      return indexCreatable(paths) ? [null, null] : ["index_not_found", null];
    }
    try {
      const descriptor = openSync(paths.indexPath, "r");
      try {
        readSync(descriptor, Buffer.alloc(1), 0, 1, null);
      } finally {
        closeSync(descriptor);
      }
    } catch {
      return ["index_permission_denied", new EditError("DRAFT_BOX_INDEX_UNREADABLE", "Jianying draft index cannot be read", { status: 503 })];
    }
    return [null, null];
  }

  function loadRecords(paths, readError = null) {
    if (!paths.indexPath) throw new EditError("DRAFT_BOX_UNAVAILABLE", "Jianying draft index is unavailable", { status: 404 });
    if (!paths.draftsRoot) throw new EditError("DRAFT_BOX_UNAVAILABLE", "Jianying draft root is unavailable", { status: 404 });
    const index = regularFile(paths.indexPath);
    if (index === null) {
      // 剪映装好、还没建过草稿时没有索引：当成空草稿箱，第一次写入时再建。
      if (indexCreatable(paths)) return { records: [], skipped: 0, indexExists: false };
      throw new EditError("DRAFT_BOX_UNAVAILABLE", "Jianying draft index is unavailable", { status: 404 });
    }
    let raw;
    try {
      raw = readLimited(index, MAX_INDEX_BYTES + 1);
    } catch {
      throw readError ?? new EditError("DRAFT_BOX_INDEX_UNREADABLE", "Jianying draft index cannot be read", { status: 503 });
    }
    const { document } = parseIndexDocument(raw);
    const indexRoot = normalizeRoot(document.root_path, env);
    const indexDirectory = normalizeRoot(path.dirname(index), env);
    const draftRoot = normalizeRoot(paths.draftsRoot, env);
    if (indexRoot === null || indexDirectory === null || draftRoot === null || !samePath(indexRoot, indexDirectory)) {
      throw new EditError("DRAFT_BOX_INDEX_INVALID", "Jianying draft index root is invalid", { status: 500 });
    }
    const records = new Map();
    let skipped = 0;
    for (const item of document.all_draft_store) {
      const record = indexRecord(item, draftRoot, profile, env);
      const key = record ? casefold(record.draftId) : null;
      if (record === null || records.has(key)) {
        skipped += 1;
        continue;
      }
      records.set(key, record);
    }
    const ordered = [...records.values()].sort(compareRecords);
    return { records: ordered, skipped, indexExists: true };
  }

  async function status() {
    const [paths, jianyingRunning] = await Promise.all([currentPaths(), kind ? runningState() : false]);
    const base = {
      ok: true,
      platform: kind ?? String(platform),
      draftsRoot: paths.draftsRoot,
      indexPath: paths.indexPath,
      jianyingRunning,
    };
    const unavailable = (reason) => ({
      ...base,
      status: "unavailable",
      reason,
      read_only: true,
      timeline_content: "unsupported_encrypted",
    });
    if (!kind) return unavailable("platform_unsupported");
    const [reason, error] = availability(paths);
    if (reason) return unavailable(reason);
    if (error) throw error;
    const { records, skipped, indexExists } = loadRecords(paths, error);
    return {
      ...base,
      status: "available",
      reason: null,
      read_only: false,
      draft_count: records.length,
      skipped_count: skipped,
      index_source: "jianying_index",
      index_exists: indexExists,
      drafts_root_source: paths.draftsRootSource,
      timeline_content: "unsupported_encrypted",
      operations: { ...OPERATIONS },
    };
  }

  async function listDrafts({ limit = 50, offset = 0, query = null } = {}) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 200 || !Number.isInteger(offset) || offset < 0 || offset > 10_000) {
      throw new EditError("DRAFT_BOX_PAGINATION_INVALID", "Draft box pagination is out of range", { status: 422 });
    }
    if (query != null && typeof query !== "string") {
      throw new EditError("DRAFT_BOX_QUERY_INVALID", "Draft box query is invalid", { status: 422 });
    }
    const normalizedQuery = casefold(query || "");
    if ([...normalizedQuery].length > 200) {
      throw new EditError("DRAFT_BOX_QUERY_INVALID", "Draft box query is too long", { status: 422 });
    }
    const { records, skipped } = loadRecords(await currentPaths());
    const selected = records.filter(
      (record) => casefold(record.name).includes(normalizedQuery) || casefold(record.draftId).includes(normalizedQuery),
    );
    return {
      ok: true,
      items: selected.slice(offset, offset + limit).map(publicRecord),
      total: selected.length,
      limit,
      offset,
      query: query || "",
      index: { source: "jianying_index", draft_count: records.length, skipped_count: skipped },
    };
  }

  async function getDraft(draftId) {
    if (typeof draftId !== "string" || !DRAFT_ID.test(draftId)) {
      throw new EditError("DRAFT_BOX_INVALID_ID", "Draft ID is invalid", { status: 422 });
    }
    const { records, skipped } = loadRecords(await currentPaths());
    const record = records.find((item) => casefold(item.draftId) === casefold(draftId));
    if (!record) throw new EditError("DRAFT_BOX_NOT_FOUND", "Draft was not found", { status: 404 });
    return {
      ok: true,
      draft: {
        ...publicRecord(record),
        index: { source: "jianying_index", draft_count: records.length, skipped_count: skipped },
        timeline_content: { status: "unsupported", reason: "encrypted" },
      },
    };
  }

  /** 原仓库 _writable_index：路径都在、索引能读能写（索引缺失但目录在时允许新建）。 */
  function writableIndex(paths) {
    if (!paths.indexPath || !paths.draftsRoot) {
      throw new EditError("DRAFT_BOX_WRITE_UNAVAILABLE", "Jianying draft box paths are unavailable", { status: 503 });
    }
    let indexPath = regularFile(paths.indexPath);
    const missing = indexPath === null;
    if (missing && lexists(paths.indexPath)) {
      throw new EditError("DRAFT_BOX_WRITE_UNAVAILABLE", "Jianying draft index is unavailable", { status: 503 });
    }
    let indexDirectory;
    let root;
    try {
      indexDirectory = realpathOf(path.dirname(indexPath ?? path.resolve(paths.indexPath)));
      root = realpathOf(paths.draftsRoot);
    } catch {
      throw new EditError("DRAFT_BOX_WRITE_UNAVAILABLE", "Jianying draft paths are unavailable", { status: 503 });
    }
    if (!isDirectory(indexDirectory) || isSymlink(indexDirectory) || !isDirectory(root) || isSymlink(root)) {
      throw new EditError("DRAFT_BOX_WRITE_UNAVAILABLE", "Jianying draft paths are unavailable", { status: 503 });
    }
    if (missing) indexPath = path.join(indexDirectory, path.basename(paths.indexPath));
    readIndexSnapshot(indexPath, indexDirectory, env);
    try {
      if (!missing) accessSync(indexPath, fsConstants.R_OK | fsConstants.W_OK);
      const probe = path.join(indexDirectory, `.jianying-write-probe-${randomBytes(8).toString("hex")}`);
      closeSync(openSync(probe, "wx", 0o600));
      unlinkSync(probe);
    } catch {
      throw new EditError("DRAFT_BOX_WRITE_UNAVAILABLE", "Jianying draft index is not writable", { status: 503 });
    }
    return { root, indexPath, indexDirectory };
  }

  /** 默认的建草稿步骤：等价于原仓库 ProjectService.build_draft，Mac 上再把素材拷进草稿。 */
  async function buildDraft({ project, media, directory, finalDirectory, draftId }) {
    if (!isPlainObject(project) || typeof project.id !== "string") {
      throw new EditError("PROJECT_NOT_FOUND", "Project was not found", { status: 404 });
    }
    const draft = draftModule ?? (await import("./draft.mjs"));
    const { subtitleTracks, materialNames } = await draft.draftInputs(project);
    const plan = profile.cloneMaterials ? await planMaterials(media, finalDirectory) : null;
    const result = await draft.createDraftFolder({
      project,
      root: path.dirname(directory),
      folderName: path.basename(directory),
      draftId,
      media,
      subtitleTracks,
      materialNames,
      owned: true,
      platform: kind,
      animationResourceRoots,
      ...(plan ? { materialPath: (info) => plan.replacements.get(String(info?.path)) ?? path.resolve(String(info?.path)) } : {}),
    });
    let contentText = toText(result.content);
    const marker = path.join(result.directory, OWNER_MARKER);
    if (lexists(marker)) {
      throw new EditError("DRAFT_EXISTS", "Draft ownership marker already exists", { status: 409 });
    }
    atomicWrite(
      marker,
      JSON.stringify({ draft_id: draftId.toUpperCase(), project_id: project.id, generator: OWNER_GENERATOR }),
      { replace: false },
    );
    if (plan?.sources.size) {
      contentText = await copyPlannedMaterials({
        plan,
        directory: result.directory,
        contentPath: result.contentPath ?? path.join(result.directory, profile.mainFile),
        contentText,
        copyMaterial,
      });
    }
    return { project, directory: result.directory, contentText, content: JSON.parse(contentText) };
  }

  function withFolderName(project, directory) {
    return isPlainObject(project) ? { ...project, name: path.basename(directory) } : project;
  }

  function draftProject(project, draftName) {
    if (typeof draftName !== "string" || !isPlainObject(project)) return project;
    const name = [...draftName.trim()].slice(0, 200).join("");
    return name ? { ...project, name } : project;
  }

  async function createDraft({ project, media = [], draftName, builder } = {}) {
    const build = builder ?? projectBuilder ?? buildDraft;
    requireWritablePlatform("creation");
    await checkStopped({ message: "Close Jianying before adding a draft" });
    const projectId = typeof project?.id === "string" ? project.id : null;
    const requested = draftProject(project, draftName);
    return exclusive(async () => {
      const { root, indexPath, indexDirectory } = writableIndex(await currentPaths());
      const snapshot = readIndexSnapshot(indexPath, indexDirectory, env);
      await checkStopped();
      const draftId = String(newDraftId()).toUpperCase();
      // 同剪映：文件夹按草稿名命名，和已有文件夹或索引里登记的文件夹重名时加「 (2)」「 (3)」…
      const target = path.join(root, availableFolderName(root, requested?.name, snapshot?.document));
      await checkStopped();
      if (snapshot && findIndexRecord(snapshot.document, draftId)) {
        throw new EditError("DRAFT_BOX_COLLISION", "Draft identity is already in the index", { status: 409 });
      }
      let built;
      try {
        // 草稿名取文件夹名：剪映打开保存后会把显示名改成文件夹名，两边一开始就一致，免得加了序号或替换过字符后名字变来变去
        built = await build({ project: withFolderName(requested, target), media, directory: target, finalDirectory: target, draftId });
      } catch (error) {
        const marker = readOwner(target);
        if (marker !== null && projectId !== null && marker.project_id === projectId) {
          internals.removeOwnedDirectory(target, draftId, { projectId });
        }
        throw error;
      }
      const builtProject = built?.project;
      const projectIdentity = typeof builtProject?.id === "string" ? builtProject.id : null;
      const createdDirectory = isOwnedDraftDirectory(target, draftId, projectIdentity);
      let record;
      try {
        if (!isDirectory(target) || isSymlink(target)) {
          throw new EditError("DRAFT_BOX_INVALID_DRAFT", "Drafted directory identity does not match its request", { status: 500 });
        }
        realpathOf(target);
        if (!samePath(built.directory, target)) {
          throw new EditError("DRAFT_BOX_INVALID_DRAFT", "Drafted directory is outside its reserved path", { status: 500 });
        }
        validateBuiltDraft(built.directory, draftId, built.content, builtProject, profile, path.basename(target));
        const materialsSize = metadataMaterialsSize(readMetadata(built.directory));
        record = newIndexRecord(draftId, builtProject.name, built.directory, root, built.content, profile, { materialsSize });
        const updated = withNewRecord(snapshot?.tree ?? null, record, indexDirectory, profile);
        const encoded = Buffer.from(dumpIndexTree(updated), "utf8");
        if (encoded.length > MAX_INDEX_BYTES) {
          throw new EditError("DRAFT_BOX_INDEX_INVALID", "Updated Jianying draft index exceeds its limit", { status: 500 });
        }
        await checkStopped({ fresh: true });
        const current = readIndexSnapshot(indexPath, indexDirectory, env);
        if (!sameDocument(current, snapshot)) {
          throw new EditError("DRAFT_BOX_INDEX_CONFLICT", "Jianying draft index changed during the write", {
            status: 409,
            details: { action: "retry" },
          });
        }
        internals.atomicReplaceIndex(indexPath, encoded, { create: snapshot === null });
      } catch (error) {
        if (createdDirectory) internals.removeOwnedDirectory(target, draftId, { projectId: projectIdentity });
        throw error;
      }
      if (regularFile(indexPath) === null) {
        throw new EditError("DRAFT_BOX_INDEX_WRITE_FAILED", "Draft was published but its index entry could not be verified", { status: 500 });
      }
      readIndexSnapshot(indexPath, indexDirectory, env);
      return {
        ok: true,
        status: "written",
        draftId,
        draftName: record.draft_name,
        path: String(built.directory),
        id: draftId,
        name: record.draft_name,
        draft: { ...record },
        record: { ...record },
        source: "jianying_index",
        read_only: false,
        jianying_ui_verified: false,
        verification: {
          files_and_index_written: true,
          jianying_ui_verified: false,
          note: "Generated JSON files and the index entry were written; Jianying UI compatibility was not verified.",
        },
      };
    });
  }

  async function repairDraft({ project, media = [], draftId, draftName, builder } = {}) {
    if (typeof draftId !== "string" || !DRAFT_UUID.test(draftId)) {
      throw new EditError("DRAFT_BOX_INVALID_ID", "Only UUID drafts can be repaired", { status: 422 });
    }
    const build = builder ?? projectBuilder ?? buildDraft;
    requireWritablePlatform("repair");
    await checkStopped();
    const projectId = typeof project?.id === "string" ? project.id : null;
    const requested = draftProject(project, draftName);
    return exclusive(async () => {
      const { root, indexPath, indexDirectory } = writableIndex(await currentPaths());
      const original = readIndexSnapshot(indexPath, indexDirectory, env);
      await checkStopped();
      const indexed = original ? findIndexRecord(original.document, draftId) : null;
      if (!indexed) throw new EditError("DRAFT_BOX_NOT_FOUND", "Draft was not found", { status: 404 });
      const matching = original.document.all_draft_store.filter(
        (item) => isPlainObject(item) && typeof item.draft_id === "string" && casefold(item.draft_id) === casefold(draftId),
      );
      if (matching.length !== 1) {
        throw new EditError("DRAFT_BOX_INDEX_INVALID", "Draft identity is ambiguous in the Jianying index", { status: 500 });
      }
      const normalizedId = indexed.draft_id;
      const notOwned = () =>
        new EditError("DRAFT_BOX_NOT_OWNED", "Draft path is not a repairable service-owned directory", { status: 409 });
      // 以索引登记的文件夹为准（新草稿按草稿名命名，早先的按 id 命名），文件夹名修复时不动。
      const target = indexedFolderPath(indexed.draft_fold_path, root);
      if (target === null || isSymlink(target) || !isDirectory(target)) {
        throw notOwned();
      }
      try {
        if (!samePath(path.dirname(realpathOf(target)), root)) throw new Error("draft path is outside its root");
      } catch {
        throw notOwned();
      }
      const owner = readOwner(target);
      if (!isOwnedDraftDirectory(target, normalizedId, projectId, { ownerRequired: true })) {
        const code = owner !== null && owner.project_id !== projectId ? "DRAFT_BOX_PROJECT_MISMATCH" : "DRAFT_BOX_NOT_OWNED";
        throw new EditError(code, "Draft is not owned by the requested service project", { status: 409 });
      }

      const token = randomBytes(8).toString("hex");
      // 临时路径尽量短，照顾 Windows 的老路径长度上限。
      const stagingRoot = path.join(root, `.${token}.repair`);
      const backup = path.join(root, `.${token}.backup`);
      let preservedEntries = readdirSync(target).filter((name) => !profile.generatedEntries.has(name));
      try {
        mkdirSync(stagingRoot, { mode: 0o700 });
      } catch (error) {
        if (error?.code === "EEXIST") {
          throw new EditError("DRAFT_BOX_REPAIR_CONFLICT", "Draft repair staging path already exists", { status: 409 });
        }
        throw error;
      }

      const staged = path.join(stagingRoot, STAGED_FOLDER);
      let originalMoved = false;
      let indexWriteStarted = false;
      let retainStaging = false;
      let mainTimelinePath = null;
      let encoded = null;
      let updated = null;
      let updatedRecord;
      try {
        const built = await build({ project: withFolderName(requested, target), media, directory: staged, finalDirectory: target, draftId: normalizedId });
        const builtProject = built?.project;
        const stagedOwned = isOwnedDraftDirectory(staged, normalizedId, projectId, { ownerRequired: true });
        if (!stagedOwned || !samePath(built.directory, staged) || isSymlink(staged) || !isDirectory(staged)) {
          throw new EditError("DRAFT_BOX_INVALID_DRAFT", "Generated repair draft does not match its reserved path", { status: 500 });
        }
        realpathOf(staged);
        validateBuiltDraft(staged, normalizedId, built.content, builtProject, profile, path.basename(target));
        const metadataPath = path.join(staged, "draft_meta_info.json");
        const metadata = relocateStrings(JSON.parse(readFileSync(metadataPath, "utf8")), staged, target);
        metadata.draft_fold_path = target;
        metadata.draft_root_path = root;
        if (Object.hasOwn(metadata, "draft_name")) metadata.draft_name = builtProject.name;
        atomicWrite(metadataPath, JSON.stringify(metadata, null, 2), { replace: true });
        mainTimelinePath = internals.stageMainTimelineRepair(target, staged, built.content, token, {
          mainFile: profile.mainFile,
          contentText: built.contentText,
        });
        if (mainTimelinePath !== null) {
          preservedEntries = preservedEntries.filter((name) => casefold(name) !== "timelines");
        }
        const fresh = newIndexRecord(normalizedId, builtProject.name, target, root, built.content, profile, {
          materialsSize: metadataMaterialsSize(metadata),
        });
        // 同原仓库 {**indexed_record, **fresh_record}：在无损树上合并，别的字段（含大整数）原样留下。
        const position = original.document.all_draft_store.indexOf(indexed);
        const store = original.tree.get("all_draft_store");
        const indexedTree = store[position];
        const mergedRecord = new Map(indexedTree);
        for (const [key, value] of Object.entries(fresh)) mergedRecord.set(key, toIndexTree(value));
        if (indexedTree.has("tm_draft_create")) mergedRecord.set("tm_draft_create", indexedTree.get("tm_draft_create"));
        updatedRecord = plainFromTree(mergedRecord);
        updated = new Map(original.tree);
        updated.set(
          "all_draft_store",
          store.map((item, itemIndex) => (itemIndex === position ? mergedRecord : item)),
        );
        encoded = Buffer.from(dumpIndexTree(updated), "utf8");
        if (encoded.length > MAX_INDEX_BYTES) {
          throw new EditError("DRAFT_BOX_INDEX_INVALID", "Updated Jianying draft index exceeds its limit", { status: 500 });
        }

        await checkStopped();
        const current = readIndexSnapshot(indexPath, indexDirectory, env);
        if (!current || current.identity !== original.identity || !current.raw.equals(original.raw)) {
          throw new EditError("DRAFT_BOX_INDEX_CONFLICT", "Jianying draft index changed during the repair", {
            status: 409,
            details: { action: "retry" },
          });
        }
        if (!isOwnedDraftDirectory(target, normalizedId, projectId, { ownerRequired: true })) {
          throw new EditError("DRAFT_BOX_NOT_OWNED", "Draft ownership changed during the repair", { status: 409 });
        }
        await checkStopped({ fresh: true });
        renameSync(target, backup);
        originalMoved = true;
        for (const name of preservedEntries) {
          const stagedEntry = path.join(staged, name);
          if (lexists(stagedEntry)) {
            throw new EditError("DRAFT_BOX_REPAIR_CONFLICT", "Generated draft conflicts with an existing auxiliary file", { status: 409 });
          }
          renameSync(path.join(backup, name), stagedEntry);
        }
        renameSync(staged, target);
        await checkStopped();
        indexWriteStarted = true;
        internals.atomicReplaceIndex(indexPath, encoded);
        const verified = readIndexSnapshot(indexPath, indexDirectory, env);
        if (!verified || !verified.raw.equals(encoded) || !isDeepStrictEqual(verified.tree, updated)) {
          throw new EditError("DRAFT_BOX_INDEX_WRITE_FAILED", "Repaired draft index entry could not be verified", { status: 500 });
        }
      } catch (error) {
        let indexRollbackFailed = false;
        if (indexWriteStarted) {
          try {
            const current = readIndexSnapshot(indexPath, indexDirectory, env);
            if (current && current.raw.equals(encoded)) internals.atomicReplaceIndex(indexPath, original.raw);
            else if (!current || !current.raw.equals(original.raw)) indexRollbackFailed = true;
          } catch {
            indexRollbackFailed = true;
          }
        }
        let directoryRollbackFailed = false;
        if (originalMoved) {
          if (isOwnedDraftDirectory(target, normalizedId, projectId, { ownerRequired: true })) {
            try {
              renameSync(target, staged);
            } catch {
              // 下面按目标是否还在判断能不能恢复。
            }
          }
          for (const name of preservedEntries) {
            const stagedEntry = path.join(staged, name);
            const backupEntry = path.join(backup, name);
            if (lexists(stagedEntry) && !lexists(backupEntry)) {
              try {
                renameSync(stagedEntry, backupEntry);
              } catch {
                directoryRollbackFailed = true;
              }
            }
            if (lexists(stagedEntry) && !lexists(backupEntry)) directoryRollbackFailed = true;
          }
          try {
            if (!lexists(target)) {
              if (pathExists(backup) && !isSymlink(backup)) {
                renameSync(backup, target);
                originalMoved = false;
              } else {
                directoryRollbackFailed = true;
              }
            } else {
              directoryRollbackFailed = true;
            }
          } catch {
            directoryRollbackFailed = true;
          }
        }
        if (indexRollbackFailed || directoryRollbackFailed) {
          retainStaging = directoryRollbackFailed;
          const details = { index_restore_required: indexRollbackFailed, draft_restore_required: directoryRollbackFailed };
          if (indexRollbackFailed) details.index_path = indexPath;
          if (lexists(backup)) details.backup_path = backup;
          if (lexists(stagingRoot)) details.staging_path = stagingRoot;
          const failure = new EditError("DRAFT_BOX_REPAIR_ROLLBACK_FAILED", "Repair failed and automatic recovery was incomplete", {
            status: 500,
            details,
          });
          failure.cause = error;
          throw failure;
        }
        if (isOsError(error)) {
          const failure = new EditError("DRAFT_BOX_REPAIR_FAILED", "Could not replace the owned Jianying draft", { status: 500 });
          failure.cause = error;
          throw failure;
        }
        throw error;
      } finally {
        if (!retainStaging) {
          try {
            if (samePath(realpathOf(stagingRoot), stagingRoot)) rmSync(stagingRoot, { recursive: true, force: true });
          } catch {
            // 清理临时目录失败不影响结果。
          }
        }
      }
      try {
        rmSync(backup, { recursive: true, force: true });
      } catch {
        // 备份删不掉只是多占点空间。
      }
      return {
        ok: true,
        status: "repaired",
        draftId: normalizedId,
        draftName: updatedRecord.draft_name,
        path: target,
        id: normalizedId,
        name: updatedRecord.draft_name,
        draft: { ...updatedRecord },
        record: { ...updatedRecord },
        source: "jianying_index",
        read_only: false,
        jianying_ui_verified: false,
        verification: {
          files_and_index_written: true,
          draft_id_preserved: true,
          draft_path_preserved: true,
          main_timeline_updated: mainTimelinePath !== null,
          main_timeline_path: mainTimelinePath,
          jianying_ui_verified: false,
          note: "The service-owned draft was rebuilt and replaced in place; Jianying UI compatibility was not verified.",
        },
      };
    });
  }

  /**
   * 草稿的「指纹」：主文件、draft_meta_info.json、主时间线（Timelines 里那份，有才算）的大小和修改时间，加上索引里的修改时间。
   * 导出后记下来，再导出前对一下：变了就是用户在剪映里打开或改过。草稿不在了照 getDraft 抛 DRAFT_BOX_NOT_FOUND。
   */
  async function fingerprint(draftId) {
    const { draft } = await getDraft(draftId);
    const timelines = path.join(draft.path, "Timelines");
    let mainId = draftId;
    try {
      const manifest = path.join(timelines, "project.json");
      if (statSync(manifest).size <= MAX_TIMELINE_MANIFEST_BYTES) {
        const value = JSON.parse(readFileSync(manifest, "utf8"))?.main_timeline_id;
        if (typeof value === "string" && DRAFT_UUID.test(value)) mainId = value;
      }
    } catch { /* 剪映还没存过，没有 Timelines */ }
    const files = [profile.mainFile, "draft_meta_info.json", path.join("Timelines", "project.json"), path.join("Timelines", mainId, profile.mainFile)];
    const parts = files.map((name) => {
      try {
        const info = statSync(path.join(draft.path, name));
        return `${name}:${info.size}:${info.mtimeMs}`;
      } catch {
        return `${name}:-`;
      }
    });
    return [...parts, `index:${draft.modified_at ?? "-"}`].join("|");
  }

  return {
    platform: kind,
    profile,
    paths: currentPaths,
    status,
    listDrafts,
    getDraft,
    createDraft,
    repairDraft,
    fingerprint,
  };
}

// ---------------------------------------------------------------- 索引读写

/**
 * 解析索引。document 是普通 JS 对象，只用来读；tree 是无损版本，写回索引只用它：
 * 别的草稿条目里可能有超过 2^53 的云端 id、写成 1.0 的数，JSON.parse 再 JSON.stringify 会悄悄改掉它们。
 */
function parseIndexDocument(raw) {
  let document;
  let tree;
  try {
    // utf-8-sig：去掉开头的 BOM；非法 UTF-8 直接算索引坏了。
    const text = new TextDecoder("utf-8", { fatal: true }).decode(raw);
    document = JSON.parse(text);
    tree = parseIndexTree(text);
  } catch {
    throw new EditError("DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", { status: 500 });
  }
  if (!isPlainObject(document)) {
    throw new EditError("DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", { status: 500 });
  }
  if (typeof document.root_path !== "string" || !document.root_path.trim() || !Array.isArray(document.all_draft_store)) {
    throw new EditError("DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", { status: 500 });
  }
  return { document, tree };
}

/** 读一次索引：身份（dev/ino/大小/时间）、原始字节、解析后的文档。文件不存在时返回 null。 */
function readIndexSnapshot(indexPath, indexDirectory, env) {
  let stats;
  let raw;
  try {
    stats = statSync(indexPath, { bigint: true });
    raw = readLimited(indexPath, MAX_INDEX_BYTES + 1);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw new EditError("DRAFT_BOX_INDEX_UNREADABLE", "Jianying draft index cannot be read", { status: 503 });
  }
  if (raw.length > MAX_INDEX_BYTES) {
    throw new EditError("DRAFT_BOX_INDEX_INVALID", "Jianying draft index is invalid", { status: 500 });
  }
  const { document, tree } = parseIndexDocument(raw);
  const root = normalizeRoot(document.root_path, env);
  const directory = normalizeRoot(indexDirectory, env);
  if (root === null || directory === null || !samePath(root, directory)) {
    throw new EditError("DRAFT_BOX_INDEX_INVALID", "Jianying draft index root is invalid", { status: 500 });
  }
  const identity = [stats.dev, stats.ino, stats.size, stats.mtimeNs, stats.ctimeNs].join(":");
  return { identity, raw, document, tree };
}

/** 同原仓库的 current != index：按内容比较（无损树，键序无关）。 */
function sameDocument(current, snapshot) {
  if (current === null || snapshot === null) return current === snapshot;
  return isDeepStrictEqual(current.tree, snapshot.tree);
}

function findIndexRecord(document, draftId) {
  for (const item of document.all_draft_store) {
    if (isPlainObject(item) && typeof item.draft_id === "string" && casefold(item.draft_id) === casefold(draftId)) return item;
  }
  return null;
}

/** 在无损树上加一条新条目；其余内容原样保留。 */
function withNewRecord(tree, record, indexDirectory, profile) {
  const entry = toIndexTree(record);
  if (tree === null) {
    return toIndexTree({ all_draft_store: [entry], draft_ids: 1, root_path: indexDirectory });
  }
  const current = tree.get("all_draft_store");
  const store = profile.newRecordsFirst ? [entry, ...current] : [...current, entry];
  const updated = new Map(tree);
  updated.set("all_draft_store", store);
  const count = updated.get("draft_ids");
  if (profile.countDraftIds && (count === undefined || (count instanceof IndexNumber && /^-?\d+$/.test(count.text)))) {
    // 剪映 Mac 的 draft_ids 是只增不减的计数（删了草稿也不回退，实测 8 份删到剩 1 份仍是 8），新建一份就在原值上加一
    const previous = count === undefined ? 0 : Number(count.text);
    updated.set("draft_ids", toIndexTree(Math.max(Number.isSafeInteger(previous) ? previous : 0, current.length) + 1));
  }
  return updated;
}

/** 原仓库 _atomic_replace_index：临时文件写好落盘再换上去；create 时不覆盖别人刚建的索引。 */
function atomicReplaceIndex(indexPath, encoded, { create = false } = {}) {
  const temporary = path.join(path.dirname(indexPath), `.${path.basename(indexPath)}.${randomBytes(8).toString("hex")}.tmp`);
  try {
    let mode = 0o644;
    try {
      mode = statSync(indexPath).mode & 0o777;
    } catch {
      // 新建索引用默认权限。
    }
    writeFileDurably(temporary, encoded, mode);
    if (create) linkExclusive(temporary, indexPath);
    else renameSync(temporary, indexPath);
  } catch (error) {
    if (create && error?.code === "EEXIST") {
      throw new EditError("DRAFT_BOX_INDEX_CONFLICT", "Jianying draft index changed during the write", {
        status: 409,
        details: { action: "retry" },
      });
    }
    throw new EditError("DRAFT_BOX_INDEX_WRITE_FAILED", "Jianying draft index could not be replaced", { status: 500 });
  } finally {
    try {
      unlinkSync(temporary);
    } catch {
      // 换上去之后临时名已经不在了。
    }
  }
}

// ---------------------------------------------------------------- 索引的无损读写

/** 索引里的数字：text 是 Python json.dumps 写出来的样子（整数照原文，小数照 repr）；null = 溢出成 inf。 */
class IndexNumber {
  constructor(text) {
    this.text = text;
  }
}

// Python 3.12 的 int() 默认最多 4300 位，json.loads 遇到更长的整数会报错。
const MAX_INT_DIGITS = 4300;
const JSON_NUMBER = /-?(?:0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/y;
const JSON_WHITESPACE = /[ \t\n\r]*/y;

/**
 * 同 Python json.loads 的结果结构：对象用 Map（键序同 Python dict：第一次出现的位置、最后一次的值），
 * 数字用 IndexNumber 留住原值。只在 JSON.parse 已经通过之后调用，所以不重复做语法报错。
 */
function parseIndexTree(text) {
  let index = 0;
  const skip = () => {
    JSON_WHITESPACE.lastIndex = index;
    JSON_WHITESPACE.exec(text);
    index = JSON_WHITESPACE.lastIndex;
  };
  const string = () => {
    const start = index;
    index += 1;
    for (;;) {
      const quote = text.indexOf('"', index);
      if (quote < 0) throw new SyntaxError("Unterminated string");
      let backslashes = 0;
      while (text[quote - 1 - backslashes] === "\\") backslashes += 1;
      index = quote + 1;
      if (backslashes % 2 === 0) return JSON.parse(text.slice(start, index));
    }
  };
  const value = () => {
    skip();
    const char = text[index];
    if (char === "{") {
      index += 1;
      const map = new Map();
      skip();
      if (text[index] === "}") {
        index += 1;
        return map;
      }
      for (;;) {
        skip();
        const key = string();
        skip();
        index += 1;
        map.set(key, value());
        skip();
        if (text[index++] === "}") return map;
      }
    }
    if (char === "[") {
      index += 1;
      const list = [];
      skip();
      if (text[index] === "]") {
        index += 1;
        return list;
      }
      for (;;) {
        list.push(value());
        skip();
        if (text[index++] === "]") return list;
      }
    }
    if (char === '"') return string();
    if (char === "t" || char === "n") {
      index += 4;
      return char === "t" ? true : null;
    }
    if (char === "f") {
      index += 5;
      return false;
    }
    JSON_NUMBER.lastIndex = index;
    const match = JSON_NUMBER.exec(text);
    if (!match) throw new SyntaxError("Invalid JSON value");
    index = JSON_NUMBER.lastIndex;
    const [token, fraction, exponent] = match;
    if (fraction !== undefined || exponent !== undefined) return new IndexNumber(pythonFloatRepr(Number(token)));
    if (token.replace("-", "").length > MAX_INT_DIGITS) throw new RangeError("Integer has too many digits");
    return new IndexNumber(token === "-0" ? "0" : token);
  };
  const result = value();
  skip();
  if (index !== text.length) throw new SyntaxError("Unexpected trailing data");
  return result;
}

/** Python repr(float)：最短往返的数字，小数点位置不在 (-4, 16] 时写成科学计数法，整数值补 .0。 */
export function pythonFloatRepr(number) {
  if (!Number.isFinite(number)) return null;
  if (number === 0) return Object.is(number, -0) ? "-0.0" : "0.0";
  const [mantissa, exponentText] = number.toExponential().split("e");
  const digits = mantissa.replace("-", "").replace(".", "");
  const point = Number(exponentText) + 1;
  let body;
  if (point <= -4 || point > 16) {
    const exponent = point - 1;
    const fraction = digits.length > 1 ? `.${digits.slice(1)}` : "";
    body = `${digits[0]}${fraction}e${exponent < 0 ? "-" : "+"}${String(Math.abs(exponent)).padStart(2, "0")}`;
  } else if (point <= 0) {
    body = `0.${"0".repeat(-point)}${digits}`;
  } else if (point >= digits.length) {
    body = `${digits}${"0".repeat(point - digits.length)}.0`;
  } else {
    body = `${digits.slice(0, point)}.${digits.slice(point)}`;
  }
  return number < 0 ? `-${body}` : body;
}

/** 本服务新写的值（新索引条目）转成同样的树；整数按 Python int 写，别的数按 repr(float)。 */
function toIndexTree(value) {
  if (typeof value === "number") {
    return new IndexNumber(Number.isInteger(value) ? BigInt(value).toString() : pythonFloatRepr(value));
  }
  if (Array.isArray(value)) return value.map(toIndexTree);
  if (isPlainObject(value) && !(value instanceof IndexNumber) && !(value instanceof Map)) {
    return new Map(Object.entries(value).map(([key, item]) => [key, toIndexTree(item)]));
  }
  return value;
}

/** json.dumps(tree, ensure_ascii=False, separators=(",", ":"), allow_nan=False)。 */
function dumpIndexTree(value) {
  if (value === null) return "null";
  if (value === true) return "true";
  if (value === false) return "false";
  if (typeof value === "string") return JSON.stringify(value);
  if (value instanceof IndexNumber) {
    if (value.text === null) {
      throw new EditError("DRAFT_BOX_INDEX_INVALID", "Jianying draft index has an out-of-range number", { status: 500 });
    }
    return value.text;
  }
  if (Array.isArray(value)) return `[${value.map(dumpIndexTree).join(",")}]`;
  if (value instanceof Map) {
    return `{${[...value].map(([key, item]) => `${JSON.stringify(key)}:${dumpIndexTree(item)}`).join(",")}}`;
  }
  if (typeof value === "number" || isPlainObject(value)) return dumpIndexTree(toIndexTree(value));
  throw new TypeError("Unsupported index value");
}

/** 返回给调用方的条目：树转回普通对象（数字取近似值，只用于展示）。 */
function plainFromTree(value) {
  if (value instanceof IndexNumber) return value.text === null ? null : Number(value.text);
  if (Array.isArray(value)) return value.map(plainFromTree);
  if (value instanceof Map) {
    const object = {};
    for (const [key, item] of value) {
      Object.defineProperty(object, key, { value: plainFromTree(item), enumerable: true, writable: true, configurable: true });
    }
    return object;
  }
  return value;
}

// ---------------------------------------------------------------- 索引条目

function indexRecord(item, draftRoot, profile, env) {
  if (!isPlainObject(item)) return null;
  const draftId = item.draft_id;
  const name = item.draft_name;
  const folder = item.draft_fold_path;
  if (
    typeof draftId !== "string" ||
    !DRAFT_ID.test(draftId) ||
    typeof name !== "string" ||
    !name ||
    [...name].length > 200 ||
    typeof folder !== "string" ||
    !folder.trim()
  ) {
    return null;
  }
  const itemRoot = item.draft_root_path ?? null;
  if (itemRoot !== null && typeof itemRoot !== "string") return null;
  let resolved;
  try {
    if (folder.includes("\0")) return null;
    const candidate = path.resolve(folder);
    if (isSymlink(candidate)) return null;
    resolved = resolveLoose(candidate);
  } catch {
    return null;
  }
  if (!isRelativeTo(resolved, draftRoot) || samePath(resolved, draftRoot)) return null;
  if (itemRoot !== null) {
    const normalizedItemRoot = normalizeRoot(itemRoot, env);
    if (normalizedItemRoot === null || !samePath(normalizedItemRoot, draftRoot)) return null;
  }
  if (isSymlink(resolved) || !isDirectory(resolved)) return null;
  if (item.draft_is_invisible === true) return null;
  const files = {};
  for (const filename of [profile.mainFile, "draft_meta_info.json"]) {
    const candidate = path.join(resolved, filename);
    files[filename] = isFile(candidate) && !isSymlink(candidate);
  }
  if (item.draft_is_cloud_temp_draft === true) return null;
  const created = utcTimestamp(item.tm_draft_create);
  const modified = utcTimestamp(item.tm_draft_modified);
  return {
    draftId,
    name,
    path: resolved,
    createdAt: created?.text ?? null,
    modifiedAt: modified?.text ?? null,
    createdSort: created?.sort ?? 0,
    modifiedSort: modified?.sort ?? 0,
    durationUs: nonNegativeInt(item.tm_duration),
    materialSizeBytes: nonNegativeInt(item.draft_timeline_materials_size),
    filesPresent: files,
  };
}

function compareRecords(first, second) {
  const firstMissing = first.modifiedAt === null ? 1 : 0;
  const secondMissing = second.modifiedAt === null ? 1 : 0;
  if (firstMissing !== secondMissing) return firstMissing - secondMissing;
  if (first.modifiedSort !== second.modifiedSort) return second.modifiedSort - first.modifiedSort;
  if (first.createdSort !== second.createdSort) return second.createdSort - first.createdSort;
  for (const key of ["name", "draftId"]) {
    const left = casefold(first[key]);
    const right = casefold(second[key]);
    if (left !== right) return left < right ? -1 : 1;
  }
  return 0;
}

function publicRecord(record) {
  return {
    id: record.draftId,
    name: record.name,
    path: record.path,
    created_at: record.createdAt,
    modified_at: record.modifiedAt,
    duration_us: record.durationUs,
    material_size_bytes: record.materialSizeBytes,
    files_present: { ...record.filesPresent },
    source: "jianying_index",
    read_only: true,
  };
}

/**
 * 原仓库 _new_index_record：按生成的草稿内容拼一条剪映索引条目（键按字母序，与剪映一致）。
 * materialsSize 是 draft_meta_info.json 里的 draft_timeline_materials_size_（素材文件大小之和）：
 * 原仓库从 materials[].material_size 求和，而草稿里不写这个字段，剪映列表就一直显示 0.0B；没给时才照原仓库算。
 */
export function newIndexRecord(draftId, name, directory, root, content, profile = BOX_PROFILES.windows, { materialsSize = null } = {}) {
  const created = content?.create_time;
  const modified = content?.update_time;
  const duration = content?.duration;
  if (!isNonNegativeInteger(created) || !isNonNegativeInteger(modified) || !isNonNegativeInteger(duration)) {
    throw new EditError("DRAFT_BOX_INVALID_DRAFT", "Generated draft timestamps or duration are invalid", { status: 500 });
  }
  const record = {
    cloud_draft_cover: false,
    cloud_draft_sync: false,
    draft_cloud_last_action_download: false,
    draft_cloud_purchase_info: "",
    draft_cloud_template_id: "",
    draft_cloud_tutorial_info: "",
    draft_cloud_videocut_purchase_info: "",
    draft_cover: profile.coverInFolder ? path.join(String(directory), "draft_cover.jpg") : "",
    draft_fold_path: String(directory),
    draft_id: draftId,
    draft_is_ai_shorts: false,
    draft_is_cloud_temp_draft: false,
    draft_is_infinite_canvas_draft: false,
    draft_is_invisible: false,
    draft_is_pippit_draft: false,
    draft_is_web_article_video: false,
    draft_json_file: path.join(String(directory), profile.mainFile),
    draft_name: name,
    draft_new_version: "",
    draft_root_path: String(root),
    draft_timeline_materials_size: isNonNegativeInteger(materialsSize) ? materialsSize : materialSize(content),
    draft_type: "",
    draft_web_article_video_enter_from: "",
    pippit_avatar_url: "",
    pippit_extra_info: "",
    pippit_id: "",
    pippit_user_name: "",
    streaming_edit_draft_ready: true,
    tm_draft_cloud_completed: "",
    tm_draft_cloud_entry_id: -1,
    tm_draft_cloud_modified: 0,
    tm_draft_cloud_space_id: -1,
    tm_draft_cloud_user_id: -1,
    tm_draft_create: created * 1_000_000,
    tm_draft_modified: Math.max(modified, created) * 1_000_000,
    [profile.parentEntryField]: -1,
    tm_draft_removed: 0,
    tm_duration: duration,
  };
  return Object.fromEntries(Object.entries(record).sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0)));
}

function materialSize(content) {
  const materials = content.materials === undefined ? {} : content.materials;
  if (!isPlainObject(materials)) return 0;
  let total = 0;
  for (const key of ["videos", "audios"]) {
    const list = materials[key] === undefined ? [] : materials[key];
    let items;
    if (Array.isArray(list)) items = list;
    else if (typeof list === "string" || isPlainObject(list)) items = [];
    else return 0;
    for (const material of items) {
      if (!isPlainObject(material)) continue;
      const size = material.material_size === undefined ? 0 : material.material_size;
      if (typeof size === "boolean") total += Number(size);
      else if (typeof size === "number") total += size;
      else return 0;
    }
  }
  return Number.isInteger(total) && total >= 0 ? total : 0;
}

function readMetadata(directory) {
  try {
    const metadataPath = path.join(directory, "draft_meta_info.json");
    if (isSymlink(metadataPath) || !isFile(metadataPath)) return null;
    return JSON.parse(readLimited(metadataPath, MAX_INDEX_BYTES).toString("utf8"));
  } catch {
    return null;
  }
}

function metadataMaterialsSize(metadata) {
  const size = isPlainObject(metadata) ? metadata.draft_timeline_materials_size_ : undefined;
  return Number.isSafeInteger(size) && size >= 0 ? size : null;
}

/** folderName：草稿名必须等于文件夹名（剪映打开保存后会把显示名改成文件夹名），换了建草稿步骤也一样。 */
function validateBuiltDraft(directory, draftId, content, project, profile, folderName) {
  const name = project?.name;
  if (
    !isPlainObject(content) ||
    typeof content.id !== "string" ||
    !DRAFT_UUID.test(content.id) ||
    casefold(content.id) !== casefold(draftId) ||
    typeof name !== "string" ||
    !name.trim() ||
    [...name].length > 200 ||
    content.name !== name ||
    name !== folderName
  ) {
    throw new EditError("DRAFT_BOX_INVALID_DRAFT", "Generated draft content has an invalid identity", { status: 500 });
  }
  for (const filename of [profile.mainFile, "draft_meta_info.json", "project.json"]) {
    const candidate = path.join(directory, filename);
    if (isSymlink(candidate) || !isFile(candidate)) {
      throw new EditError("DRAFT_BOX_INVALID_DRAFT", "Generated draft is incomplete", { status: 500 });
    }
  }
}

// ---------------------------------------------------------------- 时间戳

/** 原仓库 _utc_timestamp：索引里的时间（微秒/毫秒整数或 ISO 字符串）转成 +08:00 的 ISO 文本。 */
export function utcTimestampText(value) {
  return utcTimestamp(value)?.text ?? null;
}

function utcTimestamp(value) {
  if (typeof value === "string") return isoTimestamp(value);
  let timestamp = 0;
  if (typeof value === "number" && value > 0 && value < 2 ** 63) timestamp = Math.trunc(value);
  if (timestamp === 0) return null;
  // 照 Python datetime.fromtimestamp：先做浮点除法，再把小数部分按银行家舍入到微秒。
  const seconds = timestamp / (timestamp >= 1e14 ? 1_000_000 : 1000);
  let whole = Math.trunc(seconds);
  let micros = roundHalfEven((seconds - whole) * 1e6);
  if (micros >= 1e6) {
    micros -= 1e6;
    whole += 1;
  }
  if (whole > MAX_LOCAL_SECONDS) return null;
  return formatChinaTime(whole, micros);
}

/**
 * 原仓库 datetime.fromisoformat(raw.replace("Z", "+00:00"))，再 astimezone(+08:00)。
 * 逐字节照搬 CPython 3.12 的 C 实现（_datetimemodule.c），包括它的各种宽松写法：
 * 周日期（2026-W42-1）、任意字符作日期与时间的分隔符、时区里的秒和小数秒、秒后只有小数点等。
 */
function isoTimestamp(raw) {
  const parsed = pythonFromIsoFormat(raw.replace(/Z/g, "+00:00"));
  if (parsed === null) return null;
  const { year, month, day, hour, minute, second, microsecond, offsetSeconds, offsetMicros } = parsed;
  // 没写时区的按 +08:00；换算时先减偏移得 UTC，再加 8 小时，两步都要落在 1..9999 年（否则 Python 报 OverflowError）。
  let utcSeconds = daysFromCivil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second - (offsetSeconds ?? CHINA_OFFSET_SECONDS);
  let micros = microsecond - (offsetMicros ?? 0);
  utcSeconds += Math.floor(micros / 1e6);
  micros -= Math.floor(micros / 1e6) * 1e6;
  for (const seconds of [utcSeconds, utcSeconds + CHINA_OFFSET_SECONDS]) {
    const [civilYear] = civilFromDays(Math.floor(seconds / 86400));
    if (civilYear < 1 || civilYear > 9999) return null;
  }
  return formatChinaTime(utcSeconds, micros);
}

/** CPython 3.12 datetime_fromisoformat；成功返回各字段（offsetSeconds 为 null 表示没写时区），失败返回 null。 */
function pythonFromIsoFormat(text) {
  const points = [...text];
  if (points.length < 7) return null;
  // C 实现只允许分隔符位置（7/8/10）上出现代理字符，并把它换成 T；别处的孤立代理会在转 UTF-8 时失败。
  for (const position of [7, 8, 10]) {
    if (position > points.length) break;
    if (/^[\uD800-\uDFFF]$/.test(points[position] ?? "")) {
      points[position] = "T";
      break;
    }
  }
  const clean = points.join("");
  if (!clean.isWellFormed()) return null;
  const bytes = Buffer.from(clean, "utf8");
  const length = bytes.length;
  const separator = isoSeparator(bytes, length);
  if (separator < 0) return null;
  const date = isoDate(bytes, separator);
  if (date === null) return null;
  let time = { values: [0, 0, 0, 0], offsetSeconds: null, offsetMicros: null };
  if (length > separator) {
    // 分隔符可以是任意一个字符（按 UTF-8 首字节判断它占几个字节）。
    const lead = bytes[separator];
    const width = (lead & 0x80) === 0 ? 1 : (lead & 0xf0) === 0xe0 ? 3 : (lead & 0xf0) === 0xf0 ? 4 : 2;
    time = isoTime(bytes, separator + width, length);
    if (time === null) return null;
  }
  const [year, month, day] = date;
  const [hour, minute, second, microsecond] = time.values;
  if (year < 1 || year > 9999 || month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) return null;
  if (hour > 23 || minute > 59 || second > 59) return null;
  let { offsetSeconds, offsetMicros } = time;
  if (offsetSeconds === 0) {
    // C 里偏移秒数为 0 时直接用 UTC，偏移里的小数秒被丢掉。
    offsetMicros = 0;
  } else if (offsetSeconds !== null) {
    const total = offsetSeconds * 1e6 + offsetMicros;
    if (total <= -86400e6 || total >= 86400e6) return null;
  }
  return { year, month, day, hour, minute, second, microsecond, offsetSeconds, offsetMicros };
}

const byteAt = (bytes, index) => bytes[index] ?? 0;

function isoDigits(bytes, start, count) {
  let value = 0;
  for (let index = 0; index < count; index += 1) {
    const digit = byteAt(bytes, start + index) - 0x30;
    if (digit < 0 || digit > 9) return null;
    value = value * 10 + digit;
  }
  return value;
}

/** _find_isoformat_datetime_separator */
function isoSeparator(bytes, length) {
  if (length === 7) return 7;
  const isDigit = (index) => byteAt(bytes, index) >= 0x30 && byteAt(bytes, index) <= 0x39;
  if (bytes[4] === 0x2d) {
    if (bytes[5] !== 0x57) return 10;
    if (length > 8 && bytes[8] === 0x2d) {
      if (length === 9) return -1;
      return length > 10 && isDigit(10) ? 8 : 10;
    }
    return 8;
  }
  if (bytes[4] !== 0x57) return 8;
  let index = 7;
  while (index < length && isDigit(index)) index += 1;
  if (index < 9) return index;
  return index % 2 === 0 ? 7 : 8;
}

/** parse_isoformat_date：YYYY-MM-DD、YYYYMMDD、YYYY-Www[-D]、YYYYWww[D]。 */
function isoDate(bytes, separator) {
  const year = isoDigits(bytes, 0, 4);
  if (year === null) return null;
  let position = 4;
  const dashed = bytes[position] === 0x2d;
  if (dashed) position += 1;
  if (bytes[position] === 0x57) {
    const week = isoDigits(bytes, position + 1, 2);
    if (week === null) return null;
    position += 3;
    let weekday = 1;
    if (position < separator) {
      if (dashed && bytes[position++] !== 0x2d) return null;
      weekday = isoDigits(bytes, position, 1);
      if (weekday === null) return null;
    }
    return isoWeekDate(year, week, weekday);
  }
  const month = isoDigits(bytes, position, 2);
  if (month === null) return null;
  position += 2;
  if (dashed && bytes[position++] !== 0x2d) return null;
  const day = isoDigits(bytes, position, 2);
  return day === null ? null : [year, month, day];
}

/** iso_to_ymd：ISO 周日期换成公历日期。 */
function isoWeekDate(year, week, weekday) {
  if (year < 1 || year > 9999) return null;
  const january1 = daysFromCivil(year, 1, 1);
  // 1970-01-01 是星期四；0 = 星期一。
  const firstWeekday = (((january1 + 3) % 7) + 7) % 7;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  if ((week <= 0 || week >= 53) && !(week === 53 && (firstWeekday === 3 || (firstWeekday === 2 && leap)))) return null;
  if (weekday <= 0 || weekday >= 8) return null;
  const monday = january1 - firstWeekday + (firstWeekday > 3 ? 7 : 0);
  return civilFromDays(monday + (week - 1) * 7 + weekday - 1);
}

/** parse_hh_mm_ss_ff：返回 { code, values }，code < 0 失败，1 表示后面还有字符。 */
function isoClock(bytes, start, end) {
  const values = [0, 0, 0, 0];
  let position = start;
  let separated = true;
  for (let index = 0; index < 3; index += 1) {
    const value = isoDigits(bytes, position, 2);
    if (value === null) return { code: -3, values };
    values[index] = value;
    position += 2;
    const char = byteAt(bytes, position);
    position += 1;
    if (index === 0) separated = char === 0x3a;
    if (position >= end) return { code: char !== 0 ? 1 : 0, values };
    if (separated && char === 0x3a) continue;
    if (char === 0x2e || char === 0x2c) break;
    if (!separated) position -= 1;
    else return { code: -4, values };
  }
  const count = Math.min(end - position, 6);
  const fraction = isoDigits(bytes, position, count);
  if (fraction === null) return { code: -3, values };
  values[3] = fraction * 10 ** (6 - count);
  position += count;
  while (byteAt(bytes, position) >= 0x30 && byteAt(bytes, position) <= 0x39) position += 1;
  return { code: byteAt(bytes, position) !== 0 ? 1 : 0, values };
}

/** parse_isoformat_time：HH[:MM[:SS[.ffffff]]][Z|±HH[:MM[:SS[.ffffff]]]]。 */
function isoTime(bytes, start, end) {
  let zone = start;
  do {
    const char = byteAt(bytes, zone);
    if (char === 0x5a || char === 0x2b || char === 0x2d) break;
  } while (++zone < end);
  const clock = isoClock(bytes, start, zone);
  if (clock.code < 0) return null;
  if (zone === end) return clock.code === 1 ? null : { values: clock.values, offsetSeconds: null, offsetMicros: null };
  if (bytes[zone] === 0x5a) {
    return byteAt(bytes, zone + 1) !== 0 ? null : { values: clock.values, offsetSeconds: 0, offsetMicros: 0 };
  }
  const sign = bytes[zone] === 0x2d ? -1 : 1;
  const offset = isoClock(bytes, zone + 1, end);
  if (offset.code !== 0) return null;
  const [hours, minutes, seconds, micros] = offset.values;
  return { values: clock.values, offsetSeconds: sign * (hours * 3600 + minutes * 60 + seconds), offsetMicros: sign * micros };
}

function formatChinaTime(utcSeconds, micros) {
  const local = utcSeconds + CHINA_OFFSET_SECONDS;
  const days = Math.floor(local / 86400);
  const secondOfDay = local - days * 86400;
  const [year, month, day] = civilFromDays(days);
  const pad = (number, width = 2) => String(number).padStart(width, "0");
  const time = `${pad(Math.floor(secondOfDay / 3600))}:${pad(Math.floor((secondOfDay % 3600) / 60))}:${pad(secondOfDay % 60)}`;
  const fraction = micros ? `.${pad(micros, 6)}` : "";
  return {
    text: `${pad(year, 4)}-${pad(month)}-${pad(day)}T${time}${fraction}+08:00`,
    // 原仓库排序用 int(datetime.timestamp() * 1_000_000)：先除成浮点秒再乘回来，会差一微秒的地方跟着差。
    sort: Math.trunc(((utcSeconds * 1e6 + micros) / 1e6) * 1e6),
  };
}

function roundHalfEven(value) {
  const rounded = Math.round(value);
  return Math.abs(value - rounded) === 0.5 ? 2 * Math.round(value / 2) : rounded;
}

function daysInMonth(year, month) {
  if (month === 2) return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28;
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

function daysFromCivil(year, month, day) {
  const y = year - (month <= 2 ? 1 : 0);
  const era = Math.floor(y / 400);
  const yearOfEra = y - era * 400;
  const dayOfYear = Math.floor((153 * (month + (month > 2 ? -3 : 9)) + 2) / 5) + day - 1;
  const dayOfEra = yearOfEra * 365 + Math.floor(yearOfEra / 4) - Math.floor(yearOfEra / 100) + dayOfYear;
  return era * 146097 + dayOfEra - 719468;
}

function civilFromDays(days) {
  const z = days + 719468;
  const era = Math.floor(z / 146097);
  const dayOfEra = z - era * 146097;
  const yearOfEra = Math.floor((dayOfEra - Math.floor(dayOfEra / 1460) + Math.floor(dayOfEra / 36524) - Math.floor(dayOfEra / 146096)) / 365);
  const dayOfYear = dayOfEra - (365 * yearOfEra + Math.floor(yearOfEra / 4) - Math.floor(yearOfEra / 100));
  const monthPart = Math.floor((5 * dayOfYear + 2) / 153);
  const day = dayOfYear - Math.floor((153 * monthPart + 2) / 5) + 1;
  const month = monthPart + (monthPart < 10 ? 3 : -9);
  return [yearOfEra + era * 400 + (month <= 2 ? 1 : 0), month, day];
}

function nonNegativeInt(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

function isNonNegativeInteger(value) {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

// ---------------------------------------------------------------- 所有权

/**
 * 原仓库 _is_owned_draft_directory：是普通目录，且（要求时）有本服务的所有权标记、标记里的草稿 id 对得上。
 * 原仓库还要求目录名等于草稿 id；现在文件夹按草稿名命名，身份只看所有权标记。
 */
export function isOwnedDraftDirectory(directory, draftId, projectId, { ownerRequired = false } = {}) {
  if (isSymlink(directory) || !isDirectory(directory)) return false;
  try {
    const marker = path.join(directory, OWNER_MARKER);
    if (isSymlink(marker) || !isFile(marker)) return !ownerRequired;
    const owner = JSON.parse(readFileSync(marker, "utf8"));
    if (!isPlainObject(owner)) return false;
    const ownerId = owner.draft_id;
    return (
      typeof ownerId === "string" &&
      casefold(ownerId.trim().replace(/-/g, "")) === casefold(draftId.trim().replace(/-/g, "")) &&
      owner.generator === OWNER_GENERATOR &&
      (projectId == null || owner.project_id === projectId)
    );
  } catch {
    return false;
  }
}

function readOwner(directory) {
  try {
    const marker = path.join(directory, OWNER_MARKER);
    if (isSymlink(marker) || !isFile(marker)) return null;
    const value = JSON.parse(readFileSync(marker, "utf8"));
    return isPlainObject(value) ? value : null;
  } catch {
    return null;
  }
}

function removeOwnedDirectory(directory, draftId, { projectId = null } = {}) {
  if (!isOwnedDraftDirectory(directory, draftId, projectId, { ownerRequired: true })) return;
  try {
    rmSync(directory, { recursive: true, force: true });
  } catch {
    // 删不掉就留着，不能连带别的东西。
  }
}

// ---------------------------------------------------------------- 草稿文件夹名

/**
 * 草稿名 → 草稿文件夹名：换掉 Windows / macOS 不允许的字符和控制字符，去掉首尾空白和句点，
 * 避开 Windows 的保留设备名，按 UTF-8 字节限长（按字形截断，不切坏汉字和表情），空了用「AI 剪辑」。
 * suffix（如「 (2)」）接在限长后的名字后面。
 */
export function draftFolderName(name, { suffix = "" } = {}) {
  const trim = (text) => text.replace(FOLDER_EDGES, "");
  let text = trim((typeof name === "string" ? name : "").toWellFormed().normalize("NFC").replace(UNSAFE_FOLDER_CHARACTERS, "_"));
  if (WINDOWS_RESERVED_NAMES.test(text.split(".")[0].trim())) text = `_${text}`;
  text = trim(fitUtf8(text, MAX_FOLDER_BYTES - Buffer.byteLength(suffix)));
  return `${text || DEFAULT_FOLDER_NAME}${suffix}`;
}

function fitUtf8(text, limit) {
  if (Buffer.byteLength(text) <= limit) return text;
  let fitted = "";
  let used = 0;
  for (const { segment } of GRAPHEMES.segment(text)) {
    const size = Buffer.byteLength(segment);
    if (used + size > limit) break;
    fitted += segment;
    used += size;
  }
  return fitted;
}

/** 不和草稿根目录下已有的条目、也不和索引里登记过的文件夹重名（不区分大小写）的文件夹名。 */
function availableFolderName(root, name, document) {
  const key = (value) => casefold(value.normalize("NFC"));
  const taken = new Set();
  for (const item of document?.all_draft_store ?? []) {
    if (!isPlainObject(item) || typeof item.draft_fold_path !== "string") continue;
    const folder = item.draft_fold_path.split(/[\\/]+/).filter(Boolean).at(-1);
    if (folder) taken.add(key(folder));
  }
  for (let number = 1; number <= MAX_FOLDER_SUFFIX; number += 1) {
    const candidate = draftFolderName(name, { suffix: number === 1 ? "" : ` (${number})` });
    if (!taken.has(key(candidate)) && !lexists(path.join(root, candidate))) return candidate;
  }
  throw new EditError("DRAFT_BOX_COLLISION", "No free draft folder name is left", { status: 409 });
}

/**
 * 索引登记的草稿目录：父目录必须就是草稿根目录、名字是普通的一层目录名（不是 .. 也不是隐藏目录）。
 * 返回 <草稿根目录>/<名字>，不合格返回 null。
 */
function indexedFolderPath(raw, root) {
  if (typeof raw !== "string" || !raw.trim() || raw.includes("\0")) return null;
  const declared = purePath(raw);
  const name = path.basename(declared);
  if (!name || name.startsWith(".") || !samePath(path.dirname(declared), root)) return null;
  return path.join(root, name);
}

// ---------------------------------------------------------------- 主时间线修复

/**
 * 剪映 11.6 第一次保存后改读 Timelines/<主时间线>/<主文件>，只换草稿根目录的主文件会让它继续读旧时间线。
 * 这里在暂存目录里拷一份 Timelines、把旧文件备份进 .jianying_ai_repair_backups/<token>/，再写入新内容；
 * 旧草稿在发布前不动，失败时原样可恢复。
 */
function stageMainTimelineRepair(source, staged, content, token, { mainFile = BOX_PROFILES.windows.mainFile, contentText = null } = {}) {
  const timelines = path.join(source, "Timelines");
  if (!lexists(timelines)) return null;
  try {
    if (isSymlink(timelines) || !isDirectory(timelines)) throw new Error("Unsafe timeline directory");
    assertNoLinks(timelines);
    const manifestPath = path.join(timelines, "project.json");
    if (statSync(manifestPath).size > MAX_TIMELINE_MANIFEST_BYTES) throw new Error("Oversized timeline manifest");
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    if (!isPlainObject(manifest)) throw new Error("Invalid timeline manifest");
    const mainId = manifest.main_timeline_id;
    const entries = manifest.timelines;
    if (
      typeof mainId !== "string" ||
      !DRAFT_UUID.test(mainId) ||
      !Array.isArray(entries) ||
      entries.filter((entry) => isPlainObject(entry) && entry.id === mainId).length !== 1 ||
      !isFile(path.join(timelines, mainId, mainFile))
    ) {
      throw new Error("Invalid main timeline");
    }
    const destination = path.join(staged, "Timelines");
    cpSync(timelines, destination, { recursive: true, errorOnExist: true, force: false, preserveTimestamps: true });
    const snapshot = path.join(destination, REPAIR_BACKUPS, token);
    mkdirSync(snapshot, { recursive: true });
    copyFileSync(manifestPath, path.join(snapshot, "project.json"));
    for (const name of [mainFile, `${mainFile}.bak`]) {
      const previous = path.join(timelines, mainId, name);
      if (isFile(previous)) copyFileSync(previous, path.join(snapshot, name));
    }
    const encoded = Buffer.from(documentWithId(contentText, content, mainId), "utf8");
    const mainPath = path.join(destination, mainId, mainFile);
    atomicWrite(mainPath, encoded, { replace: true });
    const fallback = `${mainPath}.bak`;
    if (pathExists(fallback)) atomicWrite(fallback, encoded, { replace: true });
    if (!readFileSync(mainPath).equals(encoded)) throw new Error("Main timeline write could not be verified");
    return path.join("Timelines", mainId, mainFile);
  } catch (error) {
    const failure = new EditError("DRAFT_BOX_TIMELINE_INVALID", "Could not safely rebuild the owned draft's main timeline", { status: 409 });
    failure.cause = error;
    throw failure;
  }
}

function assertNoLinks(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (entry.isSymbolicLink()) throw new Error("Linked timeline entry");
    if (entry.isDirectory()) assertNoLinks(path.join(directory, entry.name));
  }
}

/** 主时间线文件 = 草稿内容换成主时间线的 id。尽量沿用草稿模块输出的原文（保留数字写法），只替换 id。 */
function documentWithId(contentText, content, newId) {
  const replacement = { ...content, id: newId };
  if (typeof contentText === "string" && typeof content?.id === "string") {
    const literal = JSON.stringify(content.id);
    if (contentText.split(literal).length === 2) {
      const candidate = contentText.replace(literal, JSON.stringify(newId));
      try {
        if (isDeepStrictEqual(JSON.parse(candidate), replacement)) return candidate;
      } catch {
        // 退回整体重写。
      }
    }
  }
  return JSON.stringify(replacement, null, 4);
}

// ---------------------------------------------------------------- Mac：素材拷进草稿

/** 先定好每个素材在草稿里的最终路径（修复时草稿还在暂存目录，路径要写成发布后的位置）。 */
async function planMaterials(media, finalDirectory) {
  const sources = new Map();
  const replacements = new Map();
  for (const info of media ?? []) {
    const raw = info?.path == null ? "" : String(info.path);
    if (!raw) continue;
    let real;
    try {
      real = await realpath(raw);
    } catch {
      throw new EditError("MEDIA_NOT_FOUND", "Clip material is unavailable", { status: 422 });
    }
    const name = materialFileName(real);
    sources.set(real, name);
    const destination = path.join(finalDirectory, MATERIALS_DIRECTORY, name);
    replacements.set(raw, destination);
    replacements.set(path.resolve(raw), destination);
    replacements.set(real, destination);
  }
  return { sources, replacements };
}

async function copyPlannedMaterials({ plan, directory, contentPath, contentText, copyMaterial }) {
  const folder = path.join(directory, MATERIALS_DIRECTORY);
  await mkdir(folder);
  for (const [source, name] of plan.sources) await copyMaterial(source, path.join(folder, name));
  // 草稿模块按 materialPath 写好了新路径时这里什么都不改；兜底处理没走 materialPath 的旧路径。
  const rewritten = rewriteJsonPaths(contentText, plan.replacements, 4);
  if (rewritten !== contentText) atomicWrite(contentPath, rewritten, { replace: true });
  const metadataPath = path.join(directory, "draft_meta_info.json");
  if (isFile(metadataPath)) {
    const metadataText = readFileSync(metadataPath, "utf8");
    const metadata = rewriteJsonPaths(metadataText, plan.replacements, 2);
    if (metadata !== metadataText) atomicWrite(metadataPath, metadata, { replace: true });
  }
  return rewritten;
}

/** 同一个源文件（按真实路径）只拷一份；文件名用路径哈希，后缀保留，剪映靠后缀认类型。 */
export function materialFileName(sourcePath) {
  return `${createHash("sha256").update(sourcePath).digest("hex").slice(0, 16)}${path.extname(sourcePath).toLowerCase()}`;
}

/** APFS 上 COPYFILE_FICLONE 只建写时复制的克隆，不占额外空间；不支持时 Node 自己退回普通复制。 */
async function cloneOrCopyFile(source, destination) {
  try {
    await copyFile(source, destination, fsConstants.COPYFILE_EXCL | fsConstants.COPYFILE_FICLONE);
  } catch (error) {
    if (error?.code === "EEXIST" || error?.code === "ENOENT") throw error;
    await rm(destination, { force: true });
    await copyFile(source, destination, fsConstants.COPYFILE_EXCL);
  }
}

/** 把 JSON 文本里等于旧路径的字符串换成新路径；按字面量替换以保留原排版，替换不干净再整体重写。 */
function rewriteJsonPaths(text, replacements, indent) {
  let rewritten = text;
  for (const [from, to] of replacements) rewritten = rewritten.split(JSON.stringify(from)).join(JSON.stringify(to));
  const document = JSON.parse(rewritten);
  if (!containsString(document, replacements)) return rewritten;
  return JSON.stringify(mapStrings(document, (value) => (replacements.has(value) ? replacements.get(value) : value)), null, indent);
}

function containsString(value, replacements) {
  if (typeof value === "string") return replacements.has(value);
  if (Array.isArray(value)) return value.some((item) => containsString(item, replacements));
  if (isPlainObject(value)) return Object.values(value).some((item) => containsString(item, replacements));
  return false;
}

function mapStrings(value, transform) {
  if (typeof value === "string") return transform(value);
  if (Array.isArray(value)) return value.map((item) => mapStrings(item, transform));
  if (isPlainObject(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, mapStrings(item, transform)]));
  return value;
}

/** 暂存目录里生成的 meta 若带着暂存路径，换成最终发布路径。 */
function relocateStrings(document, from, to) {
  return mapStrings(document, (value) => {
    if (samePath(value, from)) return to;
    const prefix = from.endsWith(path.sep) ? from : from + path.sep;
    return value.startsWith(prefix) ? path.join(to, value.slice(prefix.length)) : value;
  });
}

// ---------------------------------------------------------------- 文件系统小工具

function atomicWrite(target, data, { replace }) {
  mkdirSync(path.dirname(target), { recursive: true });
  const temporary = path.join(path.dirname(target), `.${path.basename(target)}.${randomBytes(8).toString("hex")}.tmp`);
  try {
    writeFileDurably(temporary, Buffer.isBuffer(data) ? data : Buffer.from(String(data), "utf8"), 0o600);
    if (replace) {
      renameSync(temporary, target);
    } else {
      try {
        linkExclusive(temporary, target);
      } catch (error) {
        if (error?.code === "EEXIST") throw new EditError("PROJECT_CONFLICT", "Target already exists", { status: 409 });
        throw error;
      }
    }
  } finally {
    try {
      unlinkSync(temporary);
    } catch {
      // 已经换上去了。
    }
  }
}

/** 不覆盖地发布临时文件；exFAT 这类不支持硬链接的盘上退回独占复制（同草稿模块的做法）。 */
function linkExclusive(source, target) {
  try {
    linkSync(source, target);
  } catch (error) {
    if (!["EPERM", "ENOTSUP", "EXDEV", "ENOSYS", "EOPNOTSUPP"].includes(error?.code)) throw error;
    copyFileSync(source, target, fsConstants.COPYFILE_EXCL);
  }
}

function writeFileDurably(target, data, mode) {
  const descriptor = openSync(target, "wx", mode);
  try {
    let offset = 0;
    while (offset < data.length) offset += writeSync(descriptor, data, offset, data.length - offset);
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
}

function readLimited(target, limit) {
  const descriptor = openSync(target, "r");
  try {
    const chunks = [];
    let total = 0;
    while (total < limit) {
      const buffer = Buffer.alloc(Math.min(1024 * 1024, limit - total));
      const count = readSync(descriptor, buffer, 0, buffer.length, null);
      if (count === 0) break;
      chunks.push(count === buffer.length ? buffer : buffer.subarray(0, count));
      total += count;
    }
    return Buffer.concat(chunks, total);
  } finally {
    closeSync(descriptor);
  }
}

/**
 * 一律用系统的 realpath（同草稿模块、素材库）：Windows 上它会把 8.3 短名（RUNNER~1）、映射盘展开成真实路径，
 * Mac 上取磁盘上的大小写；JS 版 realpathSync 不会。两边拼法不同，「是不是同一个目录」的字符串比较就对不上。
 * 个别虚拟盘上系统接口会报错，这时退回 JS 版。
 */
function realpathOf(target) {
  try {
    return realpathSync.native(target);
  } catch (error) {
    if (error?.code === "ENOENT" || error?.code === "ENOTDIR") throw error;
    return realpathSync(target);
  }
}

function regularFile(target) {
  try {
    if (isSymlink(target) || !isFile(target)) return null;
    return realpathOf(target);
  } catch {
    return null;
  }
}

function indexCreatable(paths) {
  if (!paths.indexPath || !paths.draftsRoot || lexists(paths.indexPath)) return false;
  const directory = path.dirname(path.resolve(paths.indexPath));
  return isDirectory(directory) && !isSymlink(directory) && isDirectory(paths.draftsRoot);
}

function lstatOrNull(target) {
  try {
    return lstatSync(target);
  } catch {
    return null;
  }
}

function statOrNull(target) {
  try {
    return statSync(target);
  } catch {
    return null;
  }
}

function lexists(target) {
  return lstatOrNull(target) !== null;
}

function pathExists(target) {
  return statOrNull(target) !== null;
}

function isSymlink(target) {
  return lstatOrNull(target)?.isSymbolicLink() ?? false;
}

function isDirectory(target) {
  return statOrNull(target)?.isDirectory() ?? false;
}

function isFile(target) {
  return statOrNull(target)?.isFile() ?? false;
}

/** 像 Python 的 Path.resolve()（非 strict）：存在的部分解开符号链接，不存在的部分原样接上。 */
function resolveLoose(target) {
  if (String(target).includes("\0")) throw new Error("path contains NUL");
  const absolute = path.resolve(target);
  try {
    return realpathOf(absolute);
  } catch {
    const parent = path.dirname(absolute);
    if (parent === absolute) return absolute;
    return path.join(resolveLoose(parent), path.basename(absolute));
  }
}

function normalizeRoot(raw, env) {
  try {
    return resolveLoose(path.resolve(expandVars(String(raw), env ?? {})));
  } catch {
    return null;
  }
}

/** Python os.path.expandvars：$NAME、${NAME}（Windows 上还有 %NAME%），没定义的原样保留。 */
function expandVars(value, env) {
  let expanded = value.replace(/\$(\w+|\{[^}]*\})/g, (whole, name) => {
    const key = name.startsWith("{") ? name.slice(1, -1) : name;
    return Object.hasOwn(env, key) ? String(env[key]) : whole;
  });
  if (CASE_INSENSITIVE_PATHS) {
    expanded = expanded.replace(/%([^%]+)%/g, (whole, name) => {
      const key = Object.keys(env).find((candidate) => candidate.toLowerCase() === name.toLowerCase());
      return key === undefined ? whole : String(env[key]);
    });
  }
  return expanded;
}

/** 像 str(Path(raw))：合并重复分隔符、去掉 "." 段，不解析 ".."。 */
function purePath(raw) {
  const separator = CASE_INSENSITIVE_PATHS ? /[\\/]+/ : /\/+/;
  const parts = String(raw).split(separator);
  const kept = parts.filter((part, index) => part !== "." && (part !== "" || index === 0));
  const joined = kept.join(path.sep);
  return joined || (String(raw).startsWith(path.sep) ? path.sep : ".");
}

function samePath(first, second) {
  const normalize = (value) => (CASE_INSENSITIVE_PATHS ? String(value).replace(/\//g, "\\").toLowerCase() : String(value));
  return normalize(first) === normalize(second);
}

function isRelativeTo(child, parent) {
  const relative = path.relative(parent, child);
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
}

function isOsError(error) {
  return !(error instanceof EditError) && typeof error?.code === "string" && typeof error?.syscall === "string";
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

// Python str.casefold 与逐字小写结果不同的字符（Unicode 15）。它们的全量折叠恰好等于「先大写再小写」：
// ß→ss、ς→σ、ﬁ→fi、ᾳ→αι、µ→μ……切罗基字母另算（折叠成大写）。
const FOLD_VIA_UPPER =
  /[\u00b5\u00df\u0149\u017f\u01f0\u0345\u0390\u03b0\u03c2\u03d0\u03d1\u03d5\u03d6\u03f0\u03f1\u03f5\u0587\u1c80-\u1c88\u1e96-\u1e9b\u1f50\u1f52\u1f54\u1f56\u1f80-\u1f87\u1f90-\u1f97\u1fa0-\u1fa7\u1fb2-\u1fb4\u1fb6\u1fb7\u1fbe\u1fc2-\u1fc4\u1fc6\u1fc7\u1fd2\u1fd3\u1fd6\u1fd7\u1fe2-\u1fe4\u1fe6\u1fe7\u1ff2-\u1ff4\u1ff6\u1ff7\ufb00-\ufb06\ufb13-\ufb17]/;

/** 同 Python str.casefold。逐字处理：整串 toLowerCase 会按词尾规则把 Σ 变成 ς，casefold 不会。 */
function casefold(value) {
  const text = String(value);
  if (/^[\x00-\x7f]*$/.test(text)) return text.toLowerCase();
  let folded = "";
  for (const char of text) {
    for (const lower of char.toLowerCase()) {
      const code = lower.codePointAt(0);
      if (code >= 0xab70 && code <= 0xabbf) folded += String.fromCodePoint(code - 0xab70 + 0x13a0);
      else if (code >= 0x13f8 && code <= 0x13fd) folded += String.fromCodePoint(code - 8);
      else if (FOLD_VIA_UPPER.test(lower)) folded += lower.toUpperCase().toLowerCase();
      else folded += lower;
    }
  }
  return folded;
}

function toText(content) {
  if (typeof content === "string") return content;
  if (content instanceof Uint8Array) return Buffer.from(content).toString("utf8");
  throw new EditError("DRAFT_BOX_INVALID_DRAFT", "Generated draft content is missing", { status: 500 });
}

function noop() {}

/** 测试替换用（相当于原仓库测试里 monkeypatch 的模块函数）；模块内部都经由这里调用。 */
export const internals = {
  casefold,
  dumpIndexTree,
  parseIndexTree,
  atomicReplaceIndex,
  stageMainTimelineRepair,
  removeOwnedDirectory,
};

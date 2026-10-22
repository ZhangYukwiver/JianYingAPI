import { readFileSync } from "node:fs";

import { invalid } from "./errors.mjs";

// 剪映特效/滤镜/转场/动画/字体/蒙版/混合模式目录，由 node/scripts/ai-edit/export-jianying-catalog.py
// 从 pyJianYingDraft（Apache-2.0，见 node/licenses）导出。约 1.2MB，第一次用到才读。
let catalog = null;

function load() {
  catalog ??= JSON.parse(readFileSync(new URL("./catalog.json", import.meta.url), "utf8"));
  return catalog;
}

export function catalogSource() {
  return load().source;
}

/** 某个枚举的全部成员，顺序与 pyJianYingDraft 一致：[{ name, meta }]。 */
export function members(enumName) {
  const list = load().enums[enumName];
  if (!list) throw new Error(`Unknown catalog enum: ${enumName}`);
  return list;
}

/**
 * 对应原仓库 draft.py 的 _enum_member：先按成员名找（如「叠化」「_1980」）。
 * ponytail: 另外接受目录里的显示名（meta.name / meta.title），方便模型直接用看到的中文名；
 * 原仓库的第二轮比较实际上只认成员名，这里是它的超集。
 */
export function enumMember(enumName, raw) {
  const list = members(enumName);
  const found = list.find((member) => member.name === raw)
    ?? list.find((member) => (member.meta.name ?? member.meta.title) === raw);
  if (!found) throw invalid("DRAFT_FEATURE_INVALID", `Unknown ${enumName}: ${raw}`);
  return found;
}

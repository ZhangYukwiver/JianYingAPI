# Node 版：工程编辑、草稿生成、写草稿箱

这里是本仓库 Python 版「工程编辑 → 草稿序列化 → 写进剪映草稿箱」三段的 Node 移植，取自 [DouyinDataReview](https://github.com/ZhangYukwiver/DouyinDataReview) 的 AI 剪辑功能（`collector/aiEdit/`），在那边打包进桌面应用使用。

和 Python 版相比：

- 纯 Node（20 及以上），没有运行时依赖，不需要 Python、pyJianYingDraft 和 ffprobe。素材时长、宽高、旋转角由 `media.mjs` 直接解析文件得到。
- Windows 和 Mac 版剪映都能写。两边草稿箱的差异（索引位置、主文件名、索引字段、素材是否拷进草稿）集中在 `draftBox.mjs` 的 `BOX_PROFILES`，Mac 部分按剪映 Mac 11.6 的真实草稿结构对过。
- 只有库，没有 HTTP/MCP 服务，也不搬素材商店、贴纸库这些 Windows 界面自动化。同样不导出视频。

## 模块

| 文件 | 对应 Python 版 | 内容 |
| --- | --- | --- |
| `aiEdit/projectStore.mjs` | `service.py` 的 ProjectService、`models.py`、`storage.py` | 工程、轨道、片段、关键帧、特效滤镜转场动画、蒙版抠像、标记、SRT 字幕；带 revision 的乐观锁 |
| `aiEdit/draft.mjs`、`aiEdit/draft/` | `draft.py` 和用到的 pyJianYingDraft 0.3.1 | 工程序列化成剪映草稿 JSON，按 Python 的 JSON 输出习惯逐字节对齐 |
| `aiEdit/draftBox.mjs` | `draft_box.py` | 找草稿箱、读索引、新建草稿、原位修复本服务拥有的草稿；写入前检查剪映是否在运行，失败回滚 |
| `aiEdit/media.mjs` | ffprobe 探测 | MP4/MOV/MKV/WebM/AVI、图片、常见音频格式的时长和尺寸，含显示矩阵旋转、EXIF 方向 |
| `aiEdit/catalog.json` | pyJianYingDraft 的枚举 | 特效、滤镜、转场、动画、蒙版、混合模式目录，由 `scripts/ai-edit/export-jianying-catalog.py` 导出 |

## 和 Python 版的有意差异

对照测试里两边输出逐字段一致，下面这些是故意改的：

- `fit: cover/stretch` 在素材比画布更宽时铺不满（`draft.py` 只算了一个方向），已修；fit 按转正后的宽高算，手机竖拍不会算反。
- 同一个素材在 `materials.videos` 里出现两份，已修。
- 草稿里的 platform 不再写死 windows，按目标剪映写 windows 或 mac。
- 新草稿的文件夹按草稿名命名（剪映自己就是这样），原位修复按索引里登记的路径找文件夹。
- Python 版会变成 500 的内部异常（片段重叠、动画冲突、参数越界等）改成 422 结构化错误。

## 用法

```js
import { mkdir } from "node:fs/promises";
import { ProjectStore } from "./aiEdit/projectStore.mjs";
import { createDraftBox } from "./aiEdit/draftBox.mjs";

const store = new ProjectStore({ directory: "./projects", mediaRoots: ["/path/to/media"] });
let project = await store.createProject({ name: "Node 示例", canvas: { width: 1080, height: 1920, fps: 30 } });

project = await store.createTrack(project.id, { track_type: "video", name: "画面" });
await store.addMediaSegment(project.id, project.tracks.at(-1).id, {
  path: "/path/to/media/clip.mp4",
  timeline: { start_us: 0, duration_us: 2_000_000 },
  fit: "cover",
});
project = await store.createTrack(project.id, { track_type: "text", name: "字幕" });
project = await store.addTextSegment(project.id, project.tracks.at(-1).id, {
  text: "你好，剪映",
  timeline: { start_us: 0, duration_us: 2_000_000 },
});

// 不传路径时自动找本机剪映的草稿箱（Windows 读注册表/配置，Mac 用 ~/Movies/JianyingPro）。
// 想先试写到别处，就给一个已存在的文件夹：
await mkdir("./drafts", { recursive: true });
const box = createDraftBox({ draftsRoot: "./drafts", indexPath: "./drafts/root_meta_info.json" });
const draft = await box.createDraft({ project, media: await store.mediaInfos(project), draftName: project.name });
// 之后改了工程，写回同一份草稿：
// await box.repairDraft({ project, media: await store.mediaInfos(project), draftId: draft.draftId });
```

时间一律是整数微秒。写真实草稿箱前要先退出剪映，否则会报 `DRAFT_BOX_APP_RUNNING`。

## 测试

```bash
cd node
npm install
npm test
```

`fixtures/ai-edit/` 里的对照结果是用本仓库 Python 版跑出来的，媒体样本全是合成的。Python 版改了行为后，可以在它的虚拟环境里用 `scripts/ai-edit/generate-*.py` 重新生成（用法见各脚本文件头），再跑 `npm test` 看 Node 版哪里没跟上。

## 许可证

本目录代码按 MIT 发布（见 `LICENSE`）。`aiEdit/catalog.json` 和 `aiEdit/draft/` 的部分逻辑来自 pyJianYingDraft，许可证见 `licenses/pyJianYingDraft-Apache-2.0.txt`。

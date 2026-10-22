# JianYingAPI · 剪映 AI 本地接口

通过 **HTTP/JSON API** 或 **MCP stdio**，让 AI 客户端创建剪映工程、编排时间线、生成草稿，并操作部分剪映桌面界面。

这是一个面向 Windows 电脑版剪映的**非官方本地桥接服务**。草稿生成基于 `pyJianYingDraft`，界面操作基于 Windows UI Automation 和可选 OCR。适合由 AI 负责规划，再调用明确的编辑工具执行。

DeepSeek 等模型可通过外部客户端调用这些接口。本仓库不内置 DeepSeek 推理后端、不需要模型 API Key，也不自动配置任何 AI 客户端。

不想装 Python，或者要写 Mac 版剪映的草稿箱：[`node/`](node/README.md) 里有工程编辑、草稿生成和写草稿箱的 Node 移植版，纯 Node、不需要 ffprobe，Windows 和 Mac 都能用（不含 HTTP/MCP 服务和界面操作）。

> 当前属于实验性实现，未覆盖剪映全部功能。草稿文件生成和索引写入已有自动测试；剪映 11.6 中完整打开、回放和编辑的兼容性仍需人工验证。写入返回的 `jianying_ui_verified: false` 表示尚未完成界面验收，不能视作视频制作成功。

## 功能与边界

| 模块 | 已实现 | 使用边界 |
| --- | --- | --- |
| 工程与画布 | 新建、列表、读取、改名、分辨率、帧率 | 数据保存在本地服务目录 |
| 轨道 | 视频、音频、文字、贴纸、特效、滤镜；增删改、排序、静音 | 不允许同轨片段重叠 |
| 片段编辑 | 媒体/文字/贴纸片段，修改、删除、移动、分割，素材区间裁剪、固定倍速、音量 | 媒体须位于配置的白名单目录 |
| 画面与声音 | 位置、缩放、旋转、透明度、翻转、裁剪、淡入淡出 | 受草稿库支持范围限制 |
| 效果 | 特效、滤镜、全局效果轨、转场、蒙版、色度抠像、背景填充、混合模式 | 资源和具体效果的兼容性取决于剪映及账号权限 |
| 文字与字幕 | 字体样式、描边、背景、阴影、气泡、花字、SRT 导入与分组删除 | 不包含语音识别生成字幕 |
| 关键帧 | 支持的属性关键帧批量写入与删除 | 具体属性见 `/api/editing/options` |
| 时间线标记 | 创建、读取、删除标记 | 仅服务工程元数据，不保证显示为剪映原生标记 |
| 动画 | 目录检索、动画引用、时长、已安装本地资源路径关联 | 服务不独立下载资源；缺失资源由剪映打开草稿时尝试处理 |
| 草稿输出 | 将服务工程生成为本地草稿文件 | 不直接修改剪映当前打开的工程 |
| 草稿箱 | 状态、搜索列表、元数据详情、新建、原位修复本服务拥有的草稿 | 写入前必须退出剪映；不解密任意现有草稿 |
| 独立素材商店 | 状态、诊断、打开、读取、可用分类导航 | 已检查的剪映 11.6 独立商店没有搜索框，搜索可能返回 `unsupported` |
| 编辑器贴纸库 | 打开、读取、关键词搜索、分类浏览 | 依赖剪映窗口、布局和 OCR；不提取卡片资源 ID、不添加到时间线 |

不包含视频导出。为兼容旧客户端保留的 `/export` 路由只返回 `unsupported`，不会生成 MP4。智能抠像、自动字幕、补帧、防抖、速度曲线等未实现。

素材商店接口操作本机已登录的剪映窗口，不调用剪映私有网络接口，不提供素材下载、购买或账号登录功能。剪映自身的联网行为及会员权限仍由客户端处理。

## 安装与启动

需要 Windows、PowerShell、Python **3.12**、[uv](https://docs.astral.sh/uv/getting-started/installation/)。添加图片、视频、音频前，还需要将 FFmpeg 中的 **ffprobe** 加入 `PATH`，或配置 `JY_FFPROBE`。

建议克隆到较短的本地目录，以减少 Windows 深层草稿路径的长度问题。

```powershell
git clone https://github.com/ZhangYukwiver/JianYingAPI.git
Set-Location JianYingAPI
.\setup.ps1
.\start.ps1 http
```

`setup.ps1` 使用 `uv.lock` 安装固定版本依赖、生成合成示例图片和音频，并执行测试。依赖安装需要网络；仓库不包含用户媒体或剪映商店素材。缺少 ffprobe 时，工程、文字和 SRT 操作仍可使用，添加媒体会返回 `FFPROBE_UNAVAILABLE`。

需要素材商店 OCR 时安装可选依赖：

```powershell
.\setup.ps1 -StoreOcr
```

启动后打开：

- 接口文档：[http://127.0.0.1:8765/docs](http://127.0.0.1:8765/docs)
- OpenAPI：[http://127.0.0.1:8765/openapi.json](http://127.0.0.1:8765/openapi.json)
- 健康检查：[http://127.0.0.1:8765/api/health](http://127.0.0.1:8765/api/health)
- 当前能力：[http://127.0.0.1:8765/api/capabilities](http://127.0.0.1:8765/api/capabilities)

修改端口：

```powershell
$env:JY_PORT = "8766"
.\start.ps1 http
```

也可以直接运行入口：

```powershell
.\.venv\Scripts\jianying-ai.exe http --host 127.0.0.1 --port 8765
.\.venv\Scripts\jianying-ai.exe mcp
```

界面操作需要本机安装并打开剪映，素材商店需要相应登录状态。草稿生成与 HTTP/MCP 启动本身不要求剪映窗口正在运行。

## HTTP 使用示例

时间统一使用整数微秒，`1_000_000` 表示 1 秒；时间线区间不超过 24 小时。下面创建一个带文字的工程，并保存为服务目录内的草稿，无需私有素材。

```powershell
$ApiBase = "http://127.0.0.1:8765"

$Project = (Invoke-RestMethod -Method Post "$ApiBase/api/projects" `
    -ContentType "application/json" `
    -Body '{"name":"API demo","canvas":{"width":1280,"height":720,"fps":30}}').project
$ProjectId = $Project.id

$Project = (Invoke-RestMethod -Method Post "$ApiBase/api/projects/$ProjectId/tracks" `
    -ContentType "application/json" `
    -Body '{"track_type":"text","name":"Caption"}').project
$TextTrackId = $Project.tracks[-1].id

$TextBody = @{
    text = "Hello Jianying"
    timeline = @{ start_us = 0; duration_us = 3000000 }
    font_size = 12
    color = "#FFFFFF"
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post `
    "$ApiBase/api/projects/$ProjectId/tracks/$TextTrackId/text" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($TextBody))

$Draft = Invoke-RestMethod -Method Post "$ApiBase/api/projects/$ProjectId/draft"
$Draft.draft
```

添加安装时生成的示例图片：

```powershell
$Project = (Invoke-RestMethod -Method Post "$ApiBase/api/projects/$ProjectId/tracks" `
    -ContentType "application/json" `
    -Body '{"track_type":"video","name":"Picture"}').project
$VideoTrackId = $Project.tracks[-1].id
$MediaBody = @{
    path = (Resolve-Path ".\samples\generated\sample.png").Path
    timeline = @{ start_us = 0; duration_us = 3000000 }
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post `
    "$ApiBase/api/projects/$ProjectId/tracks/$VideoTrackId/media" `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($MediaBody))
```

修改工程会使之前保存的草稿过期。再次调用 `/draft` 会生成对应版本的草稿；如果需要更新草稿箱中**同一份草稿**，使用下一节的修复接口。

写入请求可带 `?expect_revision=当前revision` 进行版本检查。冲突返回 HTTP 409 `PROJECT_CONFLICT`，应重新读取工程后决定如何处理。

## 草稿箱写入与同草稿修复

先退出剪映，再检查草稿箱状态。服务通过当前用户的剪映索引和注册表配置发现草稿位置。

```powershell
Invoke-RestMethod "$ApiBase/api/draft-box/status"

# 新建并登记到真实草稿箱
$BoxDraft = Invoke-RestMethod -Method Post "$ApiBase/api/projects/$ProjectId/draft-box"
$DraftId = $BoxDraft.id

# 以后修改服务工程后，仍写回原 UUID 和原目录
Invoke-RestMethod -Method Put "$ApiBase/api/projects/$ProjectId/draft-box/$DraftId"
```

修复从保存的服务工程重新构建内容，校验所有权标记、工程 ID 和草稿 ID，并保留原草稿 UUID、目录、索引创建时间。已存在且结构有效的 `Timelines` 主时间线会同步重建；原主时间线文件在草稿内保留备份。未生成的附属文件保留。

这不是对任意剪映草稿的编辑或解密接口。剪映中人工修改的时间线不会反向同步到服务工程；原位修复会用服务工程重建生成内容。请先自行备份重要草稿。服务在暂存构建、校验后替换目录和索引，并在失败时尝试回滚；若返回 `DRAFT_BOX_REPAIR_ROLLBACK_FAILED`，应按响应中的本地恢复路径处理，先不要再次写入。

## 主要接口

完整参数、返回模型和所有路由以 `/docs`、`/openapi.json` 为准。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/health`、`GET /api/diagnose` | 健康检查与本机诊断 |
| `GET /api/capabilities`、`GET /api/editing/options` | 能力与编辑参数目录 |
| `POST /api/projects`、`GET /api/projects` | 新建、列出工程 |
| `GET /api/projects/{project_id}`、`PATCH /api/projects/{project_id}` | 读取、修改工程 |
| `PUT /api/projects/{project_id}/canvas` | 画布设置 |
| `/api/projects/{project_id}/tracks/...` | 轨道、媒体、文字、贴纸及轨道效果编辑 |
| `/api/projects/{project_id}/segments/...` | 片段修改、分割、移动、动画、效果、关键帧等 |
| `POST /api/projects/{project_id}/subtitles` | 以 `text/plain; charset=utf-8` 正文导入 SRT |
| `/api/projects/{project_id}/markers/...` | 服务工程标记 |
| `GET /api/animations/catalog` | 动画目录搜索，支持 `query`、`kind`、`target`、分页等 |
| `POST /api/projects/{project_id}/draft` | 保存到服务草稿目录 |
| `GET /api/draft-box/status`、`GET /api/draft-box`、`GET /api/draft-box/{draft_id}` | 草稿箱状态、列表、元数据 |
| `POST /api/projects/{project_id}/draft-box` | 向真实草稿箱新增草稿 |
| `PUT /api/projects/{project_id}/draft-box/{draft_id}` | 原位修复本服务拥有的同一草稿 |
| `/api/material-store/...` | 独立商店的状态、诊断、打开、读取、搜索和分类请求 |
| `/api/sticker-library/...` | 编辑器贴纸库的打开、读取、搜索和分类请求 |

有路由并不代表当前剪映窗口能完成该操作。界面接口返回的 `available`、`needs_gui`、`unsupported` 和验证信息共同描述实际能力；调用前应检查状态和诊断。

错误使用统一结构：

```json
{
  "ok": false,
  "error": {
    "code": "PROJECT_CONFLICT",
    "message": "Project changed; read it again before saving",
    "details": {"current_revision": 3}
  }
}
```

## MCP 与 AI 客户端

MCP 使用 **stdio**，不需要先启动 HTTP 服务。HTTP 和 MCP 可以通过同一个 `JY_DATA_ROOT` 访问同一套工程；应串行执行写入，避免多个进程同时修改同一工程。

通用 MCP 客户端配置示例，需将示例绝对路径替换为自己的克隆目录：

```json
{
  "mcpServers": {
    "jianying": {
      "command": "C:\\Projects\\JianYingAPI\\.venv\\Scripts\\python.exe",
      "args": ["-m", "jianying_service.cli", "mcp"],
      "env": {
        "JY_DATA_ROOT": "C:\\Projects\\JianYingAPI\\runtime",
        "JY_MEDIA_ROOT": "C:\\Projects\\JianYingAPI\\samples"
      }
    }
  }
}
```

客户端支持的配置格式可能不同。工具包括 `create_project`、`create_track`、`add_media_clip`、`add_text_clip`、`save_draft`、`create_draft_box_entry`、`repair_draft_box_entry` 等，以 MCP `tools/list` 返回结果为准。stdout 只承载协议消息，日志和错误使用 stderr。

推荐调用顺序：查询能力 → 创建工程 → 建轨道和片段 → 写入效果/字幕 → 保存草稿或写入草稿箱 → 人工在剪映验收。AI 客户端应将 `unsupported`、`needs_gui`、`jianying_ui_verified: false` 原样反馈，不能把接口调用成功描述为剪映回放已验证。

## 配置

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `JY_DATA_ROOT` | `runtime` | 服务工程、版本记录和草稿的写入根目录 |
| `JY_MEDIA_ROOT` | `samples` | 素材读取白名单，Windows 多目录以分号分隔 |
| `JY_HOST` | `127.0.0.1` | 仅接受 `127.0.0.1`、`localhost`、`::1` |
| `JY_PORT` | `8765` | HTTP 端口 |
| `JY_FFPROBE` | `ffprobe` | 素材探测程序或其完整路径 |
| `JY_JIANYING_EXE` | 未设置 | 可选剪映启动程序路径，用于诊断 |
| `JY_JIANYING_DRAFTS_ROOT` | 自动发现 | 可选覆盖真实草稿根目录，不改变剪映索引文件位置 |

直接使用 CLI 时，相对路径以进程工作目录解析。`start.ps1` 会进入仓库目录，并通过 `-DataRoot`、`-MediaRoot` 显式设置根目录；使用脚本时通过参数改变这些位置：

```powershell
.\start.ps1 http -DataRoot "C:\Projects\JianYingData" -MediaRoot "C:\Media"
```

动画本地缓存仅从素材白名单内查找：`animation_resources/<effect_id>/<md5>/config.json` 及配套资源。目录和配置校验通过后写入路径；不完整包会返回 `ANIMATION_RESOURCE_INVALID`。检索目录中的动画名称和资源 ID 来自依赖库，不保证与当前剪映商店实时一致。

## 安全与隐私

- 默认且通过 CLI 强制仅监听回环地址；HTTP 校验本地 `Host`、同源 `Origin` 和浏览器 `Sec-Fetch-Site`，拒绝跨来源网页访问。不启用跨域 CORS。
- 这不是带用户认证的远程服务，同机进程仍可调用。不要通过反向代理、端口映射或隧道将接口开放到公网，不要给不可信 MCP 客户端或网页应用访问权限。
- 素材要求绝对路径，解析后必须处于白名单内；常规写入局限于服务数据根。真实草稿箱写入使用专用受限接口，拒绝修改不属于服务的草稿。
- 不提供任意 shell 执行、账号凭据读取、素材下载或远程文件上传接口。请只处理自己信任且拥有使用权的媒体及动画资源；文件解析器和剪映客户端并非沙箱。
- 诊断、工程与草稿列表可能包含本机路径和项目名称。发布日志或问题报告前先脱敏。项目不需要在仓库中保存 DeepSeek、GitHub 或剪映凭据。
- `.gitignore` 排除环境文件、常见密钥、媒体、草稿、运行数据、日志和缓存；发布仍应人工检查暂存文件，不应只依赖忽略规则。

漏洞反馈和更多部署边界见 [SECURITY.md](SECURITY.md)。

## 开发与验证

```powershell
uv sync --extra test --locked
New-Item -ItemType Directory -Force .\tmp | Out-Null
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider --basetemp .\tmp\tests
```

测试覆盖工程版本、输入与路径校验、草稿生成、草稿箱所有权/冲突/回滚/同草稿修复、HTTP/MCP 一致性、商店适配逻辑，以及本地 HTTP 的 Host 和来源限制。测试使用临时工程和模拟界面，不把真实剪映窗口操作当作自动测试前提。

草稿序列化使用 `pyJianYingDraft==0.3.1` 的 10.8 兼容布局，部分元数据标记 11.6。剪映原生加密草稿时间线不在本项目读取或解密范围内；版本升级、会员资源、字体与动画缺失都可能影响实际效果。

主要代码位于 `src/jianying_service/`：`app.py` 为 HTTP，`mcp_server.py` 为 MCP，`service.py` 为编辑逻辑，`draft.py` 为草稿序列化，`draft_box.py` 为草稿箱操作，`material_store*.py` 和 `sticker_library.py` 为界面桥接。

本项目依赖 FastAPI、MCP Python SDK、Pydantic、pyJianYingDraft、UIAutomation，以及可选 RapidOCR/Pillow。剪映名称、客户端及商店素材归各权利人所有；本项目与剪映官方无隶属或背书关系。

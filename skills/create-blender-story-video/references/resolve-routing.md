# DaVinci Resolve 路由与剪辑

在镜头 MP4 已完成校验、准备连接 Resolve 时读取本文件。

## 1. 选择控制入口

1. 检查当前会话是否有可实际调用的 Resolve MCP 工具，不凭配置名称推断成功。
2. 检查项目内已验证的 Lua/Python 入口和官方脚本 API 是否能连接当前 Resolve。
3. MCP 复合操作覆盖任务且读回完整时使用 MCP；需要精确 API 方法或已有可靠脚本时使用官方 API。
4. 两者都可用时，选择未验证步骤更少、状态读回更强且最容易避免重复写入的入口，并把理由写入 `resolve-run.json.route`。
5. 两条路径都不可用时停止剪辑，保留已验证镜头 MP4。

不要未经用户允许安装、升级或替换第三方 Resolve MCP，不要静默改用其他剪辑器。选定入口后不要在同一次写操作中途混用；失败时先读回项目和时间线，再决定如何避免重复写入。

## 2. 修改前保护现场

先只读检查 Resolve Studio、外部脚本设置、当前项目、项目数据库、时间线、媒体池、轨道、分辨率、帧率和渲染预设。保存项目，用 `ProjectManager:ExportProject()` 导出带时间戳的 `.drp`，为本次剪辑创建递增版本时间线。

`edit/resolve-run.json` 使用 `assets/schemas/resolve-run.schema.json`，记录入口、能力、项目名、备份、时间线、源计划、渲染任务和读回结果。

## 3. 帧号与素材映射

storyboard 的三种帧号不得混用：

| 字段 | 含义 |
| --- | --- |
| `timeline_frame_start/end` | 全片一基、包含结束帧的时间线范围，也用于 PNG 文件名 |
| `source_frame_start/end` | 编码后单镜头 MP4 的零基素材入出点 |
| `record_frame` | Resolve 零基时间线写入位置 |

`validate_project.py` 会检查连续范围、持续帧数、`record_frame = timeline_frame_start - 1` 和 `resolve-run.source_plan` 是否与 storyboard 完全一致。

例如第二个镜头的时间线范围为 181–360、时长 180 帧，则其生成 MP4 的素材范围为 0–179，`record_frame` 为 180。不要把 181 当作这个 MP4 的源入点。

## 4. 建立时间线计划

从 storyboard 生成 `source_plan`，每项包含：

- `shot_id` 和镜头 MP4 绝对 `source_path`。
- `timeline_frame_start/end`、`source_frame_start/end`、`record_frame`。
- `duration_frames` 和 `track_index`。

在导入媒体前设置时间线分辨率、fps 和输出匹配设置。常用键包括 `timelineFrameRate`、`timelineResolutionWidth`、`timelineResolutionHeight`、`timelineOutputResMatchTimelineRes` 和 `timelinePixelAspectRatio`；必须按当前 Resolve 版本读回，不因返回真值就假定生效。

转场、字幕、标记、音乐和音效也使用明确的时间线帧号。期望总帧数必须与 storyboard 相同。

## 5. 执行与复测

典型官方入口：

```lua
local resolve = Resolve()
local projectManager = resolve:GetProjectManager()
local project = projectManager:GetCurrentProject()
```

使用 `MediaPool:ImportMedia()` 导入已验证镜头；使用 `AppendToTimeline()` 时明确区分 source 入出点与 `recordFrame`。每次写操作后读回：

- 时间线名、分辨率、fps、起止帧和总帧数。
- TimelineItem 名称、起点、终点、持续帧数和源文件。
- 视频、音频和字幕轨数量、名称与启用状态。
- 转场、字幕、标记、音乐和音效位置。
- 间隙、重叠、离线媒体和重复导入。

恢复中断任务前先读取现状，跳过已经与计划一致的素材。

## 6. 声音与节奏

用户未批准声音方案时不要添加音乐。已有音乐时保持分镜节拍帧，对白、音乐和音效使用独立轨道，避免把唯一音频烘焙进不可逆中间文件。检查削波、突兀切断、静音空洞、声画错位和结尾余音；需要用户后续配乐时保留无 BGM 版本或可独立静音音乐轨。

## 7. 渲染与交付

先探测当前 Resolve 支持的格式、编码器和设置。记录 job ID、目标路径、设置和开始时间；轮询到明确完成或失败终态，不把“已加入队列”当作完成，不静默降级为 H.264。

若 Resolve 无法直接产生合格 HEVC，先导出已验证的高质量母版，再按渲染与交付细则使用 `encode_media.py final`。按母版实际色彩属性传入输入参数。

导出后确认文件停止增长，执行 `verify_media.py` 全片探测与解码，保存项目并导出 `.drt` 或最终 `.drp`。把渲染终态、输出和备份写回 `resolve-run.json`，最后运行 `validate_project.py`。校验失败时保留失败产物和最后已验证版本；只有显式 `--replace` 才允许编码器替换现有成片。

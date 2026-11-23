---
name: create-blender-story-video
description: 将一句视频创意或既有视觉参考推进为风格锁定、分镜、AI 参考图、Blender MCP 建模与摄像机、可断点渲染、只读监控、DaVinci Resolve MCP 或官方脚本 API 剪辑，以及经过真实媒体校验的 4K 30 fps H.265 Rec.709 MP4。支持自主创作、共同创作、参考复刻和中断项目恢复；用于 3D 短片、竖屏氛围片、产品或概念视频，以及长时间渲染与剪辑的进度页面。
---

# Create Blender Story Video

把昂贵的 3D 制作拆成逐步变贵的阶段。创作方向由所选模式决定，生产状态由版本化 JSON 和随包脚本决定；不要依赖聊天上下文、临时编写的渲染器或文字声明续跑。

## 核心约束

- 默认交付竖屏 9:16、2160×3840、30 fps、H.265/HEVC Main、`yuv420p`、有限范围 Rec.709 和 `hvc1`；横屏默认 3840×2160。用户明确要求的画幅、帧率和时长由 `storyboard.json` 统一派生到渲染、监控、编码和验收。
- 正式渲染输出逐帧 PNG。只有完整解码、尺寸、颜色布局和编号集合均通过后才编码镜头 MP4。
- Blender MCP 用于 GUI 交互建模、观察和调整；项目内 Blender Python 保存可复现的构建与动画；随包 `blender_batch_render.py` 和 `render_supervisor.py` 负责正式渲染。
- 监督器单实例运行，区分周期心跳与帧进度，原子提交帧和状态。它不会终止无法确认归属的 PID，也不会覆盖已有成片，除非显式使用 `--replace`。
- 同时使用参考图对照和可测量的场景事实验收。MCP 成功返回、单张截图或视觉模型判断都不能单独证明场景正确。
- 监控页只读。最终“完成”只接受与当前 storyboard、成片身份、媒体探测和 SHA-256 绑定的 `delivery/delivery-report.json`；`render-state.json` 的 `complete` 只表示渲染完成。
- DaVinci Resolve MCP 和官方脚本 API 均可使用。不要自行安装第三方 MCP，不要在连接失败时静默换剪辑器。
- 保存源文件、项目脚本、日志、帧序列、状态、QA 报告和工程备份。

## 支持环境与预检

正式监督器目前支持 Windows 和 PowerShell。校验、编码和监控脚本可在其他平台运行，但未验证的平台不得宣称支持自动恢复正式渲染。

准备 Python 3.11+、Blender、FFmpeg/FFprobe，以及依赖：

```bash
python -m pip install -r /path/to/skill/scripts/requirements.txt
ffmpeg -hide_banner -h filter=zscale
ffmpeg -hide_banner -encoders
```

`sRGB → Rec.709` 的实际转换需要 `zscale`，最终编码需要 `libx265`。启动时检查 Blender MCP、GPU、磁盘、图像生成能力、Resolve 进程、Resolve MCP 与官方 API；只阻塞当前阶段所需的缺失能力。已有项目先运行 `validate_project.py` 并恢复真实进度。

只询问会改变创作方向或交付规格的问题。未指定时采用 30 秒、5 个镜头、竖屏 9:16、30 fps、风格化 Eevee、无对白，并明确告知。只有基准测试证明原生 4K 有不可接受的稳定性或耗时风险且用户明确同意，才使用 2× 放大并标记 `upscaled_4k`。

## 选择工作模式

把模式写入 `creative-brief.md` 和 `storyboard.json.production_mode`：

1. `autonomous`：用户明确授权创意和工程决策。连续推进，把取舍写入 `creative-decisions.md`；新增费用、缺少凭据或无法安全推断时再停下。
2. `co_create`：默认模式。在风格与分镜、参考图、Blender 预览三个确认点暂停；确认后不重复询问。
3. `reference_rebuild`：拆解用户参考的构图、材质、灯光、节奏和摄像机语法，不复制无授权资产。除非用户明确授权自主推进，否则沿用共同创作确认行为。

模式只控制谁做决定，不降低技术验收标准。切换模式时记录原因、已确认内容和下一阶段，不重复已通过的工作。

## 项目状态与工具

沿用已有目录，不创建平行的状态体系：

```text
creative-brief.md
creative-decisions.md
style-lock.md
storyboard.md
storyboard.json
references/generation-manifest.json
shot-status.json
blender/*.blend
blender/scripts/*.py
production-renders/shot_01/frame_000001.png
render/render-state.json
render/validation/
render/logs/
render/quarantine/
monitor/index.html
monitor/server.py
monitor/monitor-config.json
edit/resolve-run.json
delivery/final.mp4
delivery/delivery-report.json
workflow-retrospective.md
```

随包机器可读契约位于 `assets/schemas/`，工具位于 `scripts/`：

| 工具 | 用途 |
| --- | --- |
| `validate_project.py` | 校验全部 JSON schema、连续帧号、source/record 映射和跨文件一致性 |
| `generate_monitor_config.py` | 生成只引用 storyboard 的最小监控配置 |
| `render_supervisor.py` / `resume-render.ps1` | 单实例、可恢复、带重试预算的正式渲染 |
| `blender_batch_render.py` | Blender 内执行临时帧渲染、完整校验和原子提交 |
| `verify_frames.py` | 完整解码 PNG，更新 `frames_validated` 闸门 |
| `encode_media.py` | 参数化编码镜头 H.264 或最终 H.265，并处理输入色彩转换 |
| `verify_media.py` | 全片解码、媒体规格检查和绑定文件身份的 QA 报告 |

修改任何状态后运行：

```bash
python /path/to/skill/scripts/validate_project.py --project-root /path/to/project
```

### 帧号契约

- `timeline_frame_start/end`：全片连续、从 1 开始、包含结束帧，也用于 PNG 文件名。
- `source_frame_start/end`：单个镜头 MP4 的素材帧号；本流水线生成的素材从 0 开始。
- `record_frame`：Resolve 的零基时间线写入位置，等于 `timeline_frame_start - 1`。
- `duration_frames = timeline_end - timeline_start + 1 = source_end - source_start + 1`。

不要在 storyboard 中另存运行状态。创作和技术闸门进入 `shot-status.json`，渲染运行状态进入 `render-state.json`，剪辑执行进入 `resolve-run.json`，最终证据进入 `delivery-report.json`。

## 工作流

### 1. 形成创意并锁定风格

先提出一个推荐方向。把前提、核心情绪、观众、平台、时长、画幅、叙事弧线、必须保留内容和禁区写入 `creative-brief.md`。在 `style-lock.md` 固定色彩、材质、光向、镜头焦段族、机位、比例、纹理、后期、字体和负面约束，并生成可逐字复用的 `STYLE_CAPSULE`。

### 2. 写成可生产的分镜

若已确定音乐，先把 BPM、拍点相位、段落和重音映射到准确帧号。每个镜头只承担一个主要视觉动作，并记录主体动作、摄像机、焦段、机位、构图、连续性、转场、字幕、音频和可验证验收条件。

使用 `assets/schemas/storyboard.schema.json` 规定的字段。参考图画幅与 storyboard 一致，不在参考图里生成字幕、UI 或片名。确认点 1 按当前模式完成用户确认或内部放行。

### 3. 生成一致的参考图

完整读取 [reference-gates.md](references/reference-gates.md)。先生成风格母图，再把母图和同一 `STYLE_CAPSULE` 用作各镜头共同参考；保存原图、联系表和 `generation-manifest.json`，记录真实模型、尺寸、提示词、输入参考、输出路径、任务 ID 和服务实际返回的 seed。风格漂移时回到母图，不靠堆形容词补救。确认点 2 按模式放行。

### 4. 用 Blender MCP 建立场景

在 GUI 确认 MCP 连接、活动文件、场景、版本和摄像机。按共享世界与材质 → 独立镜头 Collection → 几何块面与主光 → 低分辨率定帧 → 材质灯光细节与动画 → 可复现脚本的顺序制作，保存 blockout、approved 和 animation 检查点。

按“对照 → 定位差异 → 单次定向修正 → 同条件复测”执行，并更新 `shot-status.json`。确认点 3 提交逐镜头预览定帧或 playblast。正式渲染前，所有创作闸门必须为 `pass`，或有用户明确批准的 `accepted_deviation`。

### 5. 正式渲染并断点续跑

完整读取 [render-and-delivery.md](references/render-and-delivery.md) 和 [stability-and-recovery.md](references/stability-and-recovery.md)。固定 approved `.blend` 并记录 SHA-256，先用目标设置做 5–10 帧基准测试。

```bash
python /path/to/skill/scripts/render_supervisor.py \
  --project-root /path/to/project \
  --blender /path/to/blender
```

Windows 也可使用 `scripts/resume-render.ps1`。监督器每批启动新 Blender 进程，保存独立日志、活动进程身份、最近 20 个帧耗时和逐镜头已验证帧；失败后缩小批次并按预算重试。渲染完成后再运行：

```bash
python /path/to/skill/scripts/verify_frames.py \
  --project-root /path/to/project --update-status
```

### 6. 可选只读监控页

完整读取 [monitor-dashboard.md](references/monitor-dashboard.md)。复制 `assets/render-monitor-dashboard/` 的页面、服务和配置样例到项目 `monitor/`，再生成真实配置：

```bash
python /path/to/skill/scripts/generate_monitor_config.py --project-root /path/to/project
python /path/to/project/monitor/server.py --project-root /path/to/project
```

配置只指向 `storyboard.json`，不另写镜头范围或交付规格。页面展示真实、派生、模拟和过期状态，不提供控制接口。

### 7. 使用 Resolve 剪辑

完整读取 [resolve-routing.md](references/resolve-routing.md)。探测 MCP 与官方脚本 API，选择已验证、覆盖完整且读回更强的入口；备份工程，用递增版本时间线，按 storyboard 的 timeline/source/record 映射生成 `resolve-run.json`。每次写操作后读回，恢复时跳过已一致的素材，避免重复导入或追加。

### 8. 编码、验证并交付

镜头先通过 `verify_frames.py`，再编码：

```bash
python /path/to/skill/scripts/encode_media.py shot \
  --project-root /path/to/project --shot-id shot_01
```

Resolve 导出高质量母版后，按母版真实色彩属性执行最终编码；下列参数必须按实际母版修改：

```bash
python /path/to/skill/scripts/encode_media.py final \
  --project-root /path/to/project --master /path/to/edit-master.mov \
  --input-primaries bt709 --input-transfer bt709 \
  --input-matrix bt709 --input-range tv
python /path/to/skill/scripts/verify_media.py --project-root /path/to/project
python /path/to/skill/scripts/validate_project.py --project-root /path/to/project
```

只有最终报告为 `complete`、当前文件 SHA-256 与报告一致、必要闸门通过且源文件和工程备份已保存，才宣布完成。交付最终 MP4、Resolve 备份或时间线导出、关键源文件路径、QA 报告及任何尚未完成的声音或字幕项。

## 持续改进

每次交付后，把实测设置、帧耗时、崩溃原因、恢复方法、Resolve 接口差异和最终验证结果写入 `workflow-retrospective.md`。区分项目特例与可复用规律；只有用户要求更新技能时才修改技能包。

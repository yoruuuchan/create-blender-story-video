# 渲染与交付细则

在 Blender 预览已经批准、准备正式渲染、编码或验证最终媒体时读取本文件。进入 Resolve 时另读 Resolve 路由细则。

## 1. 选择渲染档位

先用目标场景、目标摄像机和正式设置渲染 5–10 帧，记录帧耗时、峰值内存、显存、温度和驱动错误，不按理论速度承诺时间。

| 档位 | 用途 | 默认起点 |
| --- | --- | --- |
| Draft | 构图、运动、穿模检查 | Eevee，交付短边 540 像素，16–32 samples |
| Preview | 材质、灯光和动画批准 | Eevee，交付短边 1080 像素，32–64 samples |
| Delivery | 正式帧 | Eevee，storyboard 源分辨率，64–128 samples |
| Photoreal | 风格确有需要时 | Cycles GPU，按基准决定 samples |

正式源尺寸和交付尺寸只由 `storyboard.json.delivery` 决定。竖屏默认 2160×3840，横屏默认 3840×2160；批准放大时分别使用 1080×1920 或 1920×1080 源帧，并设置 `upscaled_4k=true`。其他尺寸由用户明确批准，校验器会检查源与交付画幅一致。

## 2. 色彩输入与输出

`delivery.input_color` 描述 PNG 输入的真实色彩属性。Blender 常见 sRGB PNG 使用：

```json
{
  "primaries": "bt709",
  "transfer": "iec61966-2-1",
  "matrix": "gbr",
  "range": "pc"
}
```

`encode_media.py` 在输入 transfer 与 Rec.709 不同时使用 `zscale` 做实际转换；只有输入已经是兼容的 RGB Rec.709 时才使用普通 `scale` 做矩阵与范围转换。不要用 `-color_trc bt709` 代替颜色转换。

Resolve 母版可能已经是 Rec.709 YUV。最终编码必须按母版实际属性传入 `--input-primaries`、`--input-transfer`、`--input-matrix` 和 `--input-range`，不要沿用 PNG 假设。固定测试图和切点抽帧用于验证标签之外的视觉一致性。

## 3. 正式渲染

先校验创作闸门、storyboard 和 approved `.blend`：

```bash
python /path/to/skill/scripts/validate_project.py --project-root /path/to/project
python /path/to/skill/scripts/render_supervisor.py \
  --project-root /path/to/project --blender /path/to/blender
```

监督器默认：

- 使用 storyboard 中的线程、批次、冷却和重试预算；Windows 子进程使用 `BelowNormal`。
- 默认 `--factory-startup`；依赖用户插件时先验证，再使用 `--no-factory-startup`。
- 每批启动新的后台 Blender，stdout/stderr 分批保存。
- 用单实例锁避免两个监督器写同一项目。
- 用独立线程写周期心跳，用帧事件更新 `last_progress_at` 和最近 20 个耗时样本。
- 先把帧写入 `render/.pending/`，完整校验后原子移动到正式文件名。
- 隔离不可解码帧，保留完整帧；发现重复、额外编号或混合颜色布局时停止，而不是猜测。

## 4. 帧序列验收

```bash
python /path/to/skill/scripts/verify_frames.py \
  --project-root /path/to/project --update-status
```

每个镜头必须满足：

- `timeline_frame_start/end` 对应的规范文件名集合完整，无重复或额外编号。
- 每张 PNG 的所有 chunk CRC 正确，IDAT 可完整解压，尺寸符合源分辨率。
- 位深、color type 和 interlace 在镜头内一致。
- 第一帧、最后一帧、中段帧和跨镜头切点经过视觉复核。

验证报告和 `frames_validated` 证据均落盘。不要用“文件存在”或“大小大于某值”代替解码。

## 5. 编码镜头中间素材

```bash
python /path/to/skill/scripts/encode_media.py shot \
  --project-root /path/to/project --shot-id shot_01
```

脚本从 storyboard 派生输入起始编号、帧数、fps、分辨率、色彩和音轨数，输出 H.264 CRF 18 中间素材。输出前再次完整探测和解码；已有镜头文件只有显式 `--replace` 才允许替换。

storyboard 的 `source_frame_start/end` 描述编码后镜头 MP4 的零基素材范围，`record_frame` 描述 Resolve 时间线位置。不要把全片 PNG 帧号直接当作素材入点。

## 6. 最终 H.265

优先让 Resolve 直接导出合格 HEVC；否则从 Resolve 导出高质量母版，再运行：

```bash
python /path/to/skill/scripts/encode_media.py final \
  --project-root /path/to/project --master /path/to/edit-master.mov \
  --input-primaries bt709 --input-transfer bt709 \
  --input-matrix bt709 --input-range tv
```

输出为 HEVC Main、`yuv420p`、storyboard fps、`hvc1`、有限范围 Rec.709；声音方案包含音轨时使用 AAC 320 kbps，无音轨时不虚构空轨。默认拒绝覆盖已有成片。

## 7. 最终媒体 QA

```bash
python /path/to/skill/scripts/verify_media.py --project-root /path/to/project
python /path/to/skill/scripts/validate_project.py --project-root /path/to/project
```

`verify_media.py` 使用 FFprobe `-count_frames` 检查全部规格、平均帧率、容器/视频流/音轨时长，FFmpeg `-xerror` 全片解码，并生成 `delivery/delivery-report.json`。时长容差为一帧或 50 毫秒中的较大值，用于覆盖正常封装和音频 priming 差异。报告绑定：

- storyboard 文件 SHA-256。
- 成片相对路径、大小、mtime/ctime、inode 和 SHA-256。
- 当前媒体探测结果、规格结论、全片解码结论，以及检查前后文件完全一致的 `stable_file`。
- FFprobe/FFmpeg 版本与检查时间。

补充抽取每个镜头中段、切点前后帧，生成联系表并目视检查。最终报告为 `complete` 才能作为交付证据；监控页会重算当前 SHA-256，替换成片后旧报告立即失效。

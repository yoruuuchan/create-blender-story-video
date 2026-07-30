# 渲染与交付细则

在 Blender 预览已经批准、准备正式渲染或验证最终媒体时读取本文件。进入 Resolve 时另读主 Skill 指定的 Resolve 路由细则。

## 目录

1. 选择渲染档位
2. 低负载与可靠性
3. 可断点渲染
4. 校验帧序列
5. 编码剪辑用镜头 MP4
6. 编码最终 H.265
7. 最终媒体 QA

## 1. 选择渲染档位

先用 5–10 帧基准测试实际场景，不按理论速度承诺时间。

| 档位 | 用途 | 建议设置 |
| --- | --- | --- |
| Draft | 构图、运动、穿模检查 | Eevee，540×960，16–32 samples |
| Preview | 材质、灯光和动画批准 | Eevee，1080×1920，32–64 samples |
| Delivery | 默认正式竖屏帧 | Eevee，2160×3840，64–128 samples |
| Photoreal | 只有风格确实需要时 | Cycles GPU，2160×3840，去噪，按基准测试决定 samples |

横屏项目把正式分辨率改为 3840×2160。优先保持清晰轮廓、稳定运动和正确光照。不要为不可见的采样差异显著增加渲染时间。渲染文字或需要逐像素锐利的合成元素时，直接使用交付分辨率。

默认正式交付为原生 4K。若 5–10 帧基准测试证明原生 4K 会造成不可接受的崩溃或时长风险，先报告实测数据；只有用户明确批准后才改为 1080×1920 渲染和 Lanczos 2× 放大，并在 `storyboard.json`、`resolve-run.json` 和最终报告中标记 `upscaled_4k`。

## 2. 低负载与可靠性

- 默认限制 Blender 渲染线程为 4，并把后台进程设为 `BelowNormal`。
- 默认每帧留 1 秒空档；用户允许满载时再减少。
- 原生 4K 首批使用 5–15 帧；机器已有卡死或重启记录时从 5 帧开始。Draft 或 Preview 可从 60 帧开始。先测量后调整，确保单批明显短于超时上限。
- 在原生 4K 批次之间默认冷却 15 秒；温度、功耗或驱动稳定性仍异常时延长，不靠持续满载赌完成。
- 不默认设置 CPU processor affinity；它可能造成“线程未能启动”。线程数限制已经足够。
- 将 stdout 和 stderr 分开保存，每批使用独立日志。
- 对自包含 `.blend`，后台渲染可使用 `--factory-startup`，避免 GUI 插件和 Blender MCP 干扰。依赖用户插件时先验证，不要盲目使用。
- 不在 `blender -b` 中启动 Blender MCP；MCP 只服务 GUI 交互，正式渲染脚本应独立运行。

让后台 runner 与 Codex 前台工具调用解耦。Windows 上由独立 PowerShell 监督进程管理 Blender 子进程、重试、冷却、心跳和日志；Codex 只读取状态和做有依据的干预。

## 3. 可断点渲染

使用项目内 Blender Python 脚本完成：

1. 从 `storyboard.json` 读取镜头、帧范围和摄像机。
2. 为每帧设置场景帧、摄像机和输出路径。
3. 只有现有 PNG 能被完整打开、尺寸和颜色模式正确时才跳过。
4. 渲染并写入 `POOL_PROGRESS` 日志标记。
5. 批次完成后写入 `POOL_COMPLETE` 标记。

不要只用“文件存在”或“大小大于 4 KB”判断完整性。进程崩溃可能留下可疑的部分文件。

崩溃时依次检查：

- Windows 与 WSL 中是否仍有 Blender/FFmpeg 进程。
- 最后一个成功的 `Saved:` 和进度标记。
- stderr、Blender crash 文件和驱动模块。
- 新帧是否仍在增长。

若看到 `nvoglv64.dll`、`EXCEPTION_ACCESS_VIOLATION` 或长进程随机崩溃，优先缩短批次、每批重启 Blender、使用 factory startup，并继续复用已验证帧；不要先重做场景。

## 4. 校验帧序列

对每个镜头核对：

- 预期编号集合与实际文件集合完全一致。
- 每张图片都能完整解码。
- 所有图片尺寸和颜色模式一致。
- 第一帧、最后一帧和中段帧画面正确。
- 跨镜头帧范围连续且没有重复。

完成校验前不要覆盖剪辑项目中的占位视频。

## 5. 编码剪辑用镜头 MP4

优先使用项目已有 FFmpeg。剪辑用镜头可以继续使用解码负担较低的 H.264；它们是 Resolve 中间素材，不是最终交付。把临时结果编码到独立目录，验证后再替换 Resolve 素材。

参考命令：

```bash
ffmpeg -framerate 30 \
  -start_number START \
  -i "frame_%06d.png" \
  -frames:v COUNT \
  -vf "scale=2160:3840:flags=lanczos:out_color_matrix=bt709:out_range=tv" \
  -an -c:v libx264 -preset medium -crf 18 \
  -pix_fmt yuv420p -fps_mode cfr -r 30 \
  -color_range tv -colorspace bt709 \
  -color_primaries bt709 -color_trc bt709 \
  -x264-params "colorprim=bt709:transfer=bt709:colormatrix=bt709" \
  -movflags +faststart shot.mp4
```

使用 `ffprobe -count_frames` 核对 codec、分辨率、像素格式、色彩标记、帧率、时长、实际帧数和音轨数量。

若视频已经正确编码但 `color_transfer` 或 `color_primaries` 仍为 unknown，可无损补写 H.264 VUI：

```bash
ffmpeg -i source.mp4 -map 0:v:0 -c:v copy \
  -bsf:v "h264_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1" \
  -an -movflags +faststart tagged.mp4
```

先验证 `tagged.mp4`，再原子替换正式文件。

## 6. 编码最终 H.265

优先让 Resolve 直接导出符合规格的 H.265。若当前 Resolve 不提供合格的 HEVC 编码器，可从已验证的高质量剪辑母版编码最终 MP4；这只是交付编码路径，不替换 Resolve 剪辑。先用 `ffmpeg -encoders` 确认 `libx265` 可用，不要静默降级为 H.264。

默认竖屏参考命令：

```bash
ffmpeg -i "edit-master.mov" \
  -map 0:v:0 -map "0:a?" \
  -vf "scale=2160:3840:flags=lanczos:out_color_matrix=bt709:out_range=tv" \
  -c:v libx265 -preset slow -crf 18 -profile:v main \
  -pix_fmt yuv420p -tag:v hvc1 -fps_mode cfr -r 30 \
  -color_range tv -colorspace bt709 \
  -color_primaries bt709 -color_trc bt709 \
  -x265-params "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited" \
  -c:a aac -b:a 320k -movflags +faststart final.mp4
```

横屏项目把缩放改为 `3840:2160`。若成片无音轨，保留可选音频映射，不虚构空音轨。

若 H.265 视频内容正确但 `color_transfer` 或 `color_primaries` 仍为 unknown，可无损补写 HEVC VUI：

```bash
ffmpeg -i source.mp4 -map 0 -c copy \
  -bsf:v "hevc_metadata=video_full_range_flag=0:colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1" \
  -movflags +faststart tagged.mp4
```

先验证 `tagged.mp4` 的视频、音频、帧数和色彩标记，再替换正式文件。

## 7. 最终媒体 QA

对最终 MP4 执行：

1. `ffprobe -count_frames` 检查 HEVC Main、2160×3840、30 fps、`yuv420p`、有限范围、Rec.709、`hvc1` 和实际帧数；横屏项目检查 3840×2160。
2. FFmpeg 全片解码到 null 输出，启用 `-xerror`。
3. 抽取每个镜头中段以及每个切点前后帧。
4. 生成联系表并进行目视检查。
5. 计算 SHA-256。

最终报告写明绝对路径、文件大小、时长、帧数、音轨数量、原生或放大 4K 状态，以及任何尚未交付的声音或字幕。

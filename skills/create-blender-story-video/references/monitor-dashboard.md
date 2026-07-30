# 只读渲染与剪辑监控页

用户需要在浏览器里观察长时间渲染、恢复、Resolve 剪辑或最终编码状态时读取本文件。

## 目录

1. 边界
2. 使用模板
3. 归一化状态
4. 本地运行
5. Cloudflare 远程查看
6. 验收

## 边界

- 监督脚本和 Resolve 自动化负责执行，页面只负责观察。
- 页面、HTTP 服务或远程连接失败时，生产任务必须继续运行。
- 第一版只允许读取，不提供启动、停止、重试、重启、上传或任意命令接口。
- 不直接轮询 Blender MCP 或 Resolve MCP；让执行进程把状态持久化后再由页面读取。
- 页面显示“模拟数据”或“真实数据”来源，不能把示例状态误当成生产证据。

## 使用模板

把 `assets/render-monitor-dashboard/index.html` 复制到项目的 `monitor/index.html`。模板是无框架单文件页面，包含渲染中、冷却恢复、剪辑中和已完成四种演示状态。

接入真实项目时：

1. 保留演示状态作为离线预览，但默认加载 `monitor/summary.json`。
2. 把最新有效帧复制或链接为 `monitor/latest-frame.png`；先完整解码，再原子替换。
3. 每 2–5 秒轮询一次摘要；页面不可见时降低频率。
4. 请求失败时保留最后一次有效数据，并明确显示“数据已过期”及最后更新时间。
5. 不因页面请求触发 Blender、FFmpeg、Resolve 或 PowerShell 操作。

## 归一化状态

由项目内服务读取 `render/render-state.json`、`shot-status.json` 和 `edit/resolve-run.json`，再原子写入：

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-30T12:00:00+09:00",
  "source": "live",
  "project_name": "project-name",
  "stage": "render",
  "status": "running",
  "progress": {
    "current": 517,
    "total": 900,
    "percent": 57.4,
    "eta_seconds": 8076
  },
  "current": {
    "shot_id": "shot_03",
    "batch": "481-540",
    "last_valid_frame": 517,
    "median_frame_seconds": 41.2
  },
  "health": {
    "heartbeat_at": "2026-07-30T11:59:56+09:00",
    "stale_after_seconds": 90,
    "gpu_percent": 88,
    "vram_used_gb": 7.1,
    "gpu_temperature_c": 72,
    "memory_used_gb": 21.6
  },
  "recovery": {
    "batch_retry_count": 0,
    "total_crash_count": 1,
    "reboot_resume_count": 0,
    "auto_resume_enabled": false,
    "last_error_signature": ""
  },
  "resolve": {
    "timeline": "",
    "render_job_status": ""
  },
  "delivery": {
    "probe_passed": false,
    "decode_passed": false,
    "sha256": ""
  }
}
```

只使用 `idle`、`running`、`cooldown`、`failed`、`complete` 作为渲染状态。页面根据 `stage` 区分 `render`、`edit` 和 `delivery`，不要根据百分比猜测阶段。

ETA 使用最近 20 个有效帧耗时的中位数计算，排除失败、重试和冷却时间；样本不足时显示“估算中”。心跳超过 `stale_after_seconds` 后显示过期警告，但不要自动判定进程已经崩溃。

## 本地运行

- 默认只绑定 `127.0.0.1`，从项目根目录提供静态文件和只读状态。
- 使用独立于渲染监督器的低资源进程；不要让监督器依赖 Web 服务 PID。
- 对状态和预览图禁用浏览器缓存，静态 HTML/CSS 可以正常缓存。
- 记录服务端口和 PID，项目结束后正常关闭；端口冲突时选择一个明确的新端口，不扫描整机端口。

## Cloudflare 远程查看

浏览器中的 Cloudflare 页面不能直接读取本机文件。只有用户明确要求远程访问时，才选择以下一种方式：

1. 使用 Cloudflare Tunnel 暴露本机只读服务，并用 Cloudflare Access 限制身份。
2. 把脱敏后的 `summary.json` 和最新预览图主动推送到受保护的远程存储。

远程状态不得包含绝对本机路径、用户名、环境变量、令牌、完整命令行、原始日志或任意控制路由。只开放必要的 `GET` 请求，设置短缓存和过期标记；任务完成后停用 Tunnel 或状态推送。若用户要求完全公开，只展示经用户确认的高层进度，不展示设备、错误或文件细节。

## 验收

- 在 375、768、1024 和 1440 像素宽度下无横向滚动。
- 键盘可操作状态切换，焦点可见，颜色不是唯一状态提示，并支持减少动态效果。
- 模拟、实时、过期、失败和完成状态都有明确文字。
- 页面停止、刷新或断网不会改变任何生产状态。
- “完成”只来自已经通过帧序列或最终媒体校验的磁盘状态。

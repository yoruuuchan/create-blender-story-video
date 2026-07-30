# 只读渲染与剪辑监控页

用户需要在浏览器里观察长时间渲染、恢复、Resolve 剪辑或最终编码状态时读取本文件。

## 目录

1. 边界
2. 模板与配置
3. 数据和状态
4. 本地运行
5. Cloudflare 远程查看
6. 验收

## 边界

- 监督脚本和 Resolve 自动化负责执行，页面只负责观察。
- 页面、HTTP 服务或远程连接失败时，生产任务必须继续运行。
- 只允许读取，不提供启动、停止、重试、重启、上传或任意命令接口。
- 不直接轮询 Blender MCP 或 Resolve MCP；优先读取执行进程持久化的状态。
- 页面显示模拟、持久化或磁盘派生来源，不能把示例状态误当成生产证据。
- 默认只绑定 `127.0.0.1`；服务会拒绝非回环地址。

## 模板与配置

把 `assets/render-monitor-dashboard/` 中的三个文件复制到项目的 `monitor/`：

```text
monitor/
├── index.html
├── server.py
└── monitor-config.json
```

内置模板默认使用 KUNLUN Design System：深色工业操作台、青蓝主信号、琥珀与红色只表达警告或故障、等宽字体、切角面板和阶梯式动效；不要加入紫色、圆角卡片或弹性动画。可见文案使用中文主标签，仅保留 Blender、GPU、HEVC、Rec.709 等必要技术缩写，避免让中文用户依赖英文状态码理解进度。若本机存在 `D:\DESIGN\KUNLUN Design System`（WSL：`/mnt/d/DESIGN/KUNLUN Design System`），修改页面前先读取其中的 `SKILL.md`、指南、tokens、组件和 UI kit，以它们为视觉事实来源。发布模板应内嵌所需 tokens 与字体回退，不在运行时依赖该磁盘目录。

其中 `monitor-config.json` 从 `monitor-config.example.json` 复制后修改。路径都相对项目根目录，且必须留在项目根目录内：

```json
{
  "project_name": "project-name",
  "project_file": "scene.blend",
  "render_root": "render/frames",
  "final_media": "delivery/final.mp4",
  "ffprobe": "",
  "shots": [
    {
      "id": "shot_01",
      "label": "SHOT 01",
      "start": 1,
      "end": 180,
      "directory": "shot_01"
    }
  ],
  "expected": {
    "width": 2160,
    "height": 3840,
    "fps": 30,
    "frames": 180,
    "codec": "hevc",
    "pix_fmt": "yuv420p",
    "color": "bt709",
    "color_range": "tv",
    "codec_tag": "hvc1",
    "audio_streams": 0
  }
}
```

`ffprobe` 留空时从 `PATH` 查找；也可填写项目根目录内的相对路径。横屏项目把期望尺寸改为 `3840×2160`。声音方案包含音轨时同步修改 `audio_streams`。

## 数据和状态

服务优先读取：

```text
render/render-state.json
shot-status.json
edit/resolve-run.json
```

同时只读盘点配置中的 PNG 序列和最终媒体。没有统一状态 JSON 时，`source` 标记为 `derived`，只显示能从磁盘确认的事实；重试、崩溃和自动续跑等无法确认的字段显示“未记录”。

服务只开放以下 GET 路由：

- `/api/status`：内存中生成的归一化摘要，禁用缓存。
- `/api/latest-frame`：最近一张已用 Pillow 完整解码的 PNG；无法解码时不暴露文件。
- `/healthz`：页面服务健康状态。
- `/`、`/index.html`：静态页面。

其他路径返回 404，POST 返回 405。页面每 5 秒轮询一次，标签页不可见时降为 15 秒；请求失败时保留最后一次有效数据，并显示过期时间。媒体探测按文件大小和修改时间缓存，页面刷新不会重复扫描整片。

只使用 `idle`、`running`、`cooldown`、`failed`、`complete` 作为状态。使用 `stage` 区分 `render`、`edit` 和 `delivery`，不要根据百分比猜测阶段。ETA 只使用 `render-state.json` 中最近 20 个有效帧耗时的中位数；样本不足时显示“估算中”。

“完成”必须同时来自最终媒体规格通过、全片解码通过和 SHA-256 已记录。只有帧文件齐全但没有 `shot-status.json` 校验记录时，镜头显示“帧齐全”，不能显示“已验证”。

Pillow 不可用时状态页仍可运行，但最近帧保持隐藏。不要退化为暴露未经完整解码的图片。

## 本地运行

从任意目录运行：

```bash
python3 /path/to/project/monitor/server.py \
  --project-root /path/to/project \
  --config /path/to/project/monitor/monitor-config.json \
  --host 127.0.0.1 \
  --port 4876
```

- 服务启动后打开 `http://127.0.0.1:4876/`；若端口冲突而改用了其他端口，以启动日志打印的 URL 为准。
- 使用独立于渲染监督器的低资源进程；不要让监督器依赖 Web 服务 PID。
- 对 API 和预览图禁用浏览器缓存，静态 HTML 可以短时缓存。
- 记录端口和 PID；端口冲突时选择一个明确的新端口，不扫描整机端口。
- 若需要长期开启，把服务独立放到后台；其退出只能影响观察页。
- `?demo=1` 仅用于离线预览界面状态，必须保留醒目的模拟数据标记。

## Cloudflare 远程查看

浏览器中的 Cloudflare 页面不能直接读取本机文件。只有用户明确要求远程访问时，才选择以下一种方式：

1. 使用 Cloudflare Tunnel 暴露 `127.0.0.1` 服务，并用 Cloudflare Access 限制身份。
2. 把脱敏后的摘要和最近预览图主动推送到受保护的远程存储。

远程状态不得包含绝对本机路径、用户名、环境变量、令牌、完整命令行、原始日志或任意控制路由。只开放必要的 GET 请求，设置短缓存和过期标记；任务完成后停用 Tunnel 或状态推送。若用户要求完全公开，只展示经用户确认的高层进度，不展示设备、错误或文件细节。

## 验收

- 在 375、768、1024 和 1440 像素宽度下无横向滚动。
- 键盘焦点可见，颜色不是唯一状态提示，并支持减少动态效果。
- 加载、模拟、真实派生、实时、过期、失败和完成状态都有明确文字。
- 页面停止、刷新、断网或收到非 GET 请求不会改变任何生产状态。
- 最近帧已经完整解码；“完成”只来自最终媒体的完整校验记录。

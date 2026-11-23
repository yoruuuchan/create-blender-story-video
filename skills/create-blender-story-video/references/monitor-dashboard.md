# 只读渲染与剪辑监控页

用户需要在浏览器观察长时间渲染、恢复、Resolve 剪辑或最终编码状态时读取本文件。

## 1. 边界

- 监督脚本和 Resolve 自动化负责执行，页面只负责观察。
- 页面、HTTP 服务或远程连接失败不得影响生产任务。
- 只开放 GET，不提供启动、停止、重试、上传或任意命令接口。
- 不直接轮询 Blender MCP 或 Resolve MCP；只读磁盘状态、帧和媒体。
- 默认只绑定 `127.0.0.1`，并校验 `Host`、`Origin` 和 `Sec-Fetch-Site`，防止 DNS rebinding 读取本机状态。
- 模拟、持久化、派生和过期状态必须明确标记。

## 2. 模板与单一配置来源

复制三个文件：

```text
monitor/
├── index.html
├── server.py
└── monitor-config.json
```

`monitor-config.json` 只包含：

```json
{
  "schema_version": 1,
  "storyboard": "storyboard.json",
  "ffprobe": ""
}
```

用工具生成，不要手工维护第二份镜头表：

```bash
python /path/to/skill/scripts/generate_monitor_config.py --project-root /path/to/project
```

服务从 storyboard 派生项目名、approved blend、逐镜头 `frame_directory`、timeline 范围、交付分辨率、fps、总帧数、编码、色彩、标签和音轨数。`ffprobe` 留空时从 PATH 查找；填写路径时必须位于项目根目录内。

内置页面采用 KUNLUN Design System 的深色工业操作台语言。视觉调整不得改变只读边界、真实数据标记和完成判定，也不依赖任何本机设计目录才能运行。

## 3. 数据和状态

服务读取：

```text
storyboard.json
render/render-state.json
shot-status.json
edit/resolve-run.json
delivery/delivery-report.json
```

同时只读盘点 PNG 序列和最终媒体。没有统一状态时 `source=derived`，无法确认的重试、崩溃和自动续跑字段显示“未记录”。

- 页面状态只用 `idle`、`running`、`cooldown`、`failed`、`complete`，阶段只用 `render`、`edit`、`delivery`。
- `running/cooldown` 必须有有效且未过期的 `health.heartbeat_at`；缺失或非法时标记 stale 并回退到磁盘与进程证据。
- ETA 使用 `current.recent_frame_durations_seconds` 的中位数，没有样本时显示“估算中”。
- 当前镜头优先读取 `current.shot_id`，批次显示 `batch_start/end`。
- 镜头只有 `frames_validated.status=pass` 才显示“已验证”；帧文件齐全但未通过时只显示“帧齐全”。
- Pillow 不可用时隐藏最近帧，不暴露未经完整解码的图片。

## 4. 最终完成判定

`render-state.json.status=complete` 只表示帧渲染完成，不能证明成片存在或通过 QA。

最终 `complete` 必须同时满足：

1. 当前成片存在，FFprobe 规格检查通过。
2. `delivery-report.json.status=complete`，`spec_passed`、`decode_passed` 和 `stable_file` 均为 true。
3. 报告中的 storyboard SHA-256、期望规格和实际媒体探测与当前值一致。
4. 报告中的相对路径、大小、mtime/ctime、inode 与当前成片一致。
5. 服务重算的当前成片 SHA-256 与报告完全一致。

任何一项变化都会使旧 QA 失效。缓存返回深拷贝，连续刷新不会重复追加问题或增大响应。

## 5. 本地运行

```bash
python3 /path/to/project/monitor/server.py \
  --project-root /path/to/project \
  --config /path/to/project/monitor/monitor-config.json \
  --host 127.0.0.1 --port 4876
```

服务只开放 `/`、`/index.html`、`/api/status`、`/api/latest-frame` 和 `/healthz`。其他路径返回 404，写请求返回 405。API 与预览禁用缓存，页面每 5 秒轮询、后台标签页每 15 秒轮询；前端同一时间只允许一个请求和一个后续定时器。

`?demo=1` 只用于离线界面演示，必须保留醒目的模拟标记。

## 6. Cloudflare 远程查看

只有用户明确要求时才通过 Cloudflare Tunnel + Access 或受保护的脱敏状态推送开放远程访问。Tunnel 保留的域名必须通过重复参数显式加入允许列表。允许项按 `hostname` 或 `hostname:port` 精确匹配，配置带端口的允许项不会放行同域名的其他端口：

```bash
python3 server.py --project-root /path/to/project \
  --allowed-host status.example.com
```

远程状态不得包含绝对本机路径、用户名、环境变量、令牌、完整命令行、原始日志或控制路由。任务完成后停用 Tunnel 或状态推送；仅绑定回环地址不等于完成身份保护。

## 7. 验收

- 在 375、768、1024 和 1440 像素宽度下无横向滚动。
- 键盘焦点可见，颜色不是唯一状态提示，支持减少动态效果。
- 加载、模拟、派生、实时、过期、失败和完成状态都有明确文字。
- 页面停止、刷新、断网或收到非 GET 请求不改变生产状态。
- 非法 Host、跨站 Origin 和 DNS rebinding 请求返回 403。
- 声明 complete 但无成片、无报告、旧报告或替换媒体均不能显示 QA 通过。

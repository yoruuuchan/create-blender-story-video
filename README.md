# Create Blender Story Video

面向 Codex 与 Claude Code 的端到端 3D 视频制作 Skill：把一句创意或视觉参考推进为风格锁定、分镜、AI 参考图、Blender 场景、可恢复渲染、只读进度监控、DaVinci Resolve 剪辑和经过媒体校验的成片。

An Agent Skill for Codex and Claude Code that turns an idea or visual reference into a style-locked storyboard, reference images, a Blender production, resilient rendering, a read-only progress dashboard, DaVinci Resolve editing, and a verified final video.

## 默认交付

- 竖屏优先：2160×3840；横屏项目：3840×2160
- 30 fps
- H.265/HEVC Main，`yuv420p`
- 有限范围 Rec.709，MP4 `hvc1`
- 原生 4K 正式渲染；只有用户明确批准时才允许标记为 `upscaled_4k` 的放大路线

## 工作流

1. 选择自主创作、共同创作或参考复刻模式。
2. 固定创意、风格和可生产分镜。
3. 生成风格母图和逐镜头参考图。
4. 使用 Blender MCP 与可复现脚本建模、布光、放置摄像机并完成阶段验收。
5. 使用短批次、持久化状态和独立监督进程完成可断点、可重启恢复的逐帧渲染。
6. 按需启用本地只读页面，查看帧进度、最近画面、硬件负载、恢复次数和剪辑阶段。
7. 选择当前更方便且经过验证的 Resolve MCP 或官方脚本 API 完成剪辑。
8. 验证帧数、编码、色彩标签、全片解码和 SHA-256 后交付。

## 可选监控页

Skill 内置无框架页面、只读 Python 状态服务与配置样例：

```text
skills/create-blender-story-video/assets/render-monitor-dashboard/
├── index.html
├── server.py
└── monitor-config.example.json
```

复制配置样例为 `monitor-config.json` 并填写项目路径后，运行 `python3 server.py --project-root <项目目录>`。服务默认只监听 `127.0.0.1:4876`，只开放状态、健康检查和最近已完整解码帧的 GET 路由；关闭页面或服务不会影响 Blender、Resolve 或渲染监督进程。若要通过 Cloudflare 域名远程查看，应额外配置只读数据传输与身份保护，不直接公开本机路径、日志或命令入口。

## 安装

### Codex

使用 Codex 自带的 Skill 安装脚本：

```bash
python ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo yoruuuchan/create-blender-story-video \
  --path skills/create-blender-story-video
```

安装后在下一轮对话中使用：

```text
$create-blender-story-video
```

### Claude Code

Claude Code 会从个人 Skill 目录自动发现：

```bash
git clone https://github.com/yoruuuchan/create-blender-story-video.git
mkdir -p ~/.claude/skills
cp -R create-blender-story-video/skills/create-blender-story-video \
  ~/.claude/skills/create-blender-story-video
```

然后使用：

```text
/create-blender-story-video
```

Claude Code 的个人 Skill 路径规则见其[官方文档](https://code.claude.com/docs/en/slash-commands).

## 文件结构

```text
skills/create-blender-story-video/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── assets/
│   └── render-monitor-dashboard/
│       ├── index.html
│       ├── monitor-config.example.json
│       └── server.py
└── references/
    ├── monitor-dashboard.md
    ├── reference-gates.md
    ├── render-and-delivery.md
    ├── resolve-routing.md
    └── stability-and-recovery.md
```

## 运行边界

- 本仓库不自动安装 Blender MCP、DaVinci Resolve MCP、Blender、Resolve 或 FFmpeg。
- 不会未经允许修改驱动、BIOS、超频、电压或系统开机任务。
- 自动重启续跑必须由用户明确批准，并设置重试预算，避免循环重启。
- 没有通过逐帧校验和最终媒体解码时，不把中间产物当作交付成果。

## Licensing

This repository is licensed under the [MIT License](LICENSE).

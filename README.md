# Create Blender Story Video

给 Codex 和 Claude Code 用的 3D 视频制作 Skill，从头做到尾。给它一句创意或一张视觉参考，它会一路往下推：锁定风格、出分镜、生成 AI 参考图、搭 Blender 场景、做能断点恢复的渲染、开只读的进度监控、在 DaVinci Resolve 里剪辑，最后交出经过媒体校验的成片。

An Agent Skill for Codex and Claude Code. Give it an idea or a visual reference and it carries the work through to a verified final video: a style-locked storyboard, reference images, a Blender production, rendering that can recover from interruptions, a read-only progress dashboard, and DaVinci Resolve editing.

## 默认交付

- 竖屏优先：2160×3840；横屏项目：3840×2160
- 30 fps
- H.265/HEVC Main，`yuv420p`
- 有限范围 Rec.709，MP4 `hvc1`
- 正式渲染用原生 4K；放大路线要标记为 `upscaled_4k`，而且只有用户明确批准才允许

## 制作流程

1. 选模式：自主创作、共同创作，或参考复刻。
2. 定下创意、风格，和能直接拿去生产的分镜。
3. 生成风格母图，再逐个镜头出参考图。
4. 用 Blender MCP 和可复现的脚本建模、布光、摆摄像机，每个阶段做完验收。
5. 逐帧渲染：短批次跑，状态持久化，另有一个独立的监督进程。中断了能从断点接着渲，重启后也能恢复。
6. 需要的话，开一个本地只读页面，看帧进度、最近画面、硬件负载、恢复次数和剪辑阶段。
7. 剪辑用 Resolve MCP 或官方脚本 API，挑当下更方便、而且验证过的那个。
8. 核对帧数、编码、色彩标签，全片解码一遍，校验 SHA-256，然后交付。

## 进度监控页（可选）

Skill 里自带一个用 KUNLUN Design System 做的中文界面、一个只读的 Python 状态服务，还有配置样例：

```text
skills/create-blender-story-video/assets/render-monitor-dashboard/
├── index.html
├── server.py
└── monitor-config.example.json
```

用法：

1. 把配置样例复制成 `monitor-config.json`，填上项目路径。
2. 运行 `python3 server.py --project-root <项目目录>`。
3. 打开 `http://127.0.0.1:4876/`。如果换了端口，以启动日志里打印的 URL 为准。

服务默认只监听回环地址，只开放这几个 GET 路由：状态、健康检查、最近已完整解码的帧。关掉页面或服务，不会影响 Blender、Resolve 或渲染监督进程。

想通过 Cloudflare 域名远程查看的话，要另外配置只读的数据传输和身份保护，不直接公开本机路径、日志或命令入口。

## 怎么装

### Codex

用 Codex 自带的 Skill 安装脚本：

```bash
python ~/.codex/skills/.system/skill-installer/scripts/install-skill-from-github.py \
  --repo yoruuuchan/create-blender-story-video \
  --path skills/create-blender-story-video
```

装好后，在下一轮对话里这样调用：

```text
$create-blender-story-video
```

### Claude Code

Claude Code 会自动发现个人 Skill 目录里的 Skill，复制进去就行：

```bash
git clone https://github.com/yoruuuchan/create-blender-story-video.git
mkdir -p ~/.claude/skills
cp -R create-blender-story-video/skills/create-blender-story-video \
  ~/.claude/skills/create-blender-story-video
```

然后这样调用：

```text
/create-blender-story-video
```

个人 Skill 目录的路径规则，见 Claude Code 的[官方文档](https://code.claude.com/docs/en/slash-commands)。

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

## 边界

- 本仓库不会自动安装 Blender MCP、DaVinci Resolve MCP、Blender、Resolve 或 FFmpeg。
- 未经允许，不会改驱动、BIOS、超频、电压或系统开机任务。
- 自动重启续跑必须由用户明确批准，还要设好重试预算，免得陷入循环重启。
- 逐帧校验和最终媒体解码都通过之前，中间产物不算交付成果。

## 这个仓库和运行副本的关系

`create-blender-story-video` 技能以这个仓库为唯一事实源。改内容先在这里提交、推送，再同步到运行副本，也就是本机 agent 技能库里的 `3d-game\create-blender-story-video`（2026-09-15 核对过，和这里的源文件一致）。技能库那份只是运行时镜像，不是第二个上游，也别把两者当成两个项目。

## Licensing

This repository is licensed under the [MIT License](LICENSE).

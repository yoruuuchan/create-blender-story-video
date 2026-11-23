# 渲染稳定性与重启恢复

在机器容易卡死、Blender 无响应、显卡驱动崩溃或整机可能重启时读取本文件。

## 1. 区分故障层级

- **Blender 崩溃：** 进程退出，系统仍正常。
- **Blender 无响应：** 进程仍在，但输出和帧进度停止。
- **驱动复位：** 画面闪黑、进程消失或日志出现 `nvoglv64.dll`、`nvlddmkm`、`EXCEPTION_ACCESS_VIOLATION`。
- **系统冻结或重启：** 整机失去响应、蓝屏或重启。

只根据日志、事件、转储和资源数据判断原因。Kernel-Power 41 只表示上次关机不正常，不等于已经找到根因。

## 2. 渲染前基线

- 固定 approved `.blend`，由监督器记录路径和 SHA-256；渲染期间不要让 GUI 改写同一文件。
- 估算 PNG、临时文件和日志空间，至少预留 1.3 倍。
- 检查内存、显存、GPU 温度和其他占用程序；不要自动关闭用户应用。
- 使用原厂稳定设置，不自动修改超频、电压、BIOS、驱动或电源策略。
- 用正式设置渲染 5 帧并记录资源与稳定性。基准出现 WHEA、蓝屏或整机重启时停止自动重试。
- 修改睡眠策略或自动登录续跑必须获得用户批准，并在结束后恢复或禁用。

## 3. 独立监督器

Windows 使用：

```powershell
& /path/to/skill/scripts/resume-render.ps1 -ProjectRoot /path/to/project -Blender /path/to/blender.exe
```

或直接运行 `render_supervisor.py`。监督器与交互式 Agent、Blender GUI 和监控服务解耦。

`render/render-state.json` 使用 `assets/schemas/render-state.schema.json`，核心字段包括：

- `supervisor_id`、storyboard/approved blend SHA-256。
- `current.shot_id`、批次范围、下一缺失帧和最后有效帧。
- `current.recent_frame_durations_seconds`：最多 20 个真实渲染耗时样本，用于 ETA 和动态超时。
- `current.active_process`：PID、可执行文件、参数数组和启动时间。
- `frames.verified_by_shot`：逐镜头已完整验证的帧集合。
- `health.heartbeat_at`：监督器周期心跳。
- `health.last_progress_at`：最近有效帧进度，不与心跳混用。
- `recovery`：批次重试、崩溃、自动续跑次数和错误签名。

状态写同目录临时文件，flush/fsync 后原子替换。单实例锁位于 `render/.render-supervisor.lock`；锁文件存在不代表任务运行，只有持有锁的进程才拥有项目。

## 4. 安全提交逐帧结果

- Blender 写入 `render/.pending/<shot>/`，验证 PNG 后原子替换正式帧。
- 已存在帧必须完整解码才跳过；不可解码帧移入 `render/quarantine/`。
- 每批结束后重新盘点批次，进程崩溃但已提交的完整帧仍可计入。
- 每个镜头完成后再次全量验证，并写 `frames_validated` 闸门。
- 重复文件名、额外编号或混合颜色布局需要明确处理，监督器不会擅自删除。

## 5. 崩溃、超时与重启恢复

Blender 崩溃或无响应时：

1. 读取最后心跳、最后进度、日志和文件增长时间，区分正常长帧与挂起。
2. 动态超时以最近帧耗时中位数为依据；监督器只终止本次自己创建的子进程。
3. 验证并保留完整帧，隔离部分帧。
4. 同一批按 storyboard 预算重试，每次缩小批次并延长冷却。
5. 固定帧反复失败时单独检查该帧的对象、粒子、几何节点、合成和摄像机状态。
6. 超过预算后状态设为 `failed`，停止自动循环并报告错误签名。

启动时如果旧 `active_pid` 仍存活，监督器会停止并请求人工核实可执行文件和命令；它不会用 `os.kill(pid, 0)` 探测 Windows 进程，也不会杀死无法确认归属的进程。

整机重启后：

1. 运行 `validate_project.py`，核对 storyboard 和 approved blend 哈希。
2. 检查 Windows 事件、转储、Blender crash 文件和失败批次日志。
3. 重新完整清点已有帧，从第一个缺失或无效帧恢复。
4. 默认由用户或 Agent 重新启动监督器。
5. 只有明确批准时才使用 `--enable-auto-resume`，登录任务使用 `--reboot-resume`；任务至少延迟 5 分钟，并受 `max_reboot_resumes` 限制。完成、失败或预算耗尽后禁用任务。

## 6. 按证据降载

| 证据 | 优先处理 |
| --- | --- |
| 驱动复位或随机 Blender 崩溃 | 每批重启 Blender、缩短批次、factory startup、减少非必要显存占用 |
| 显存接近上限 | 减少纹理、额外 pass、重型几何和驻留镜头；仍不稳定再请求放大路线 |
| 内存耗尽或大量换页 | 减少同批镜头、缓存、粒子和几何节点峰值 |
| 温度逼近限制或明显降频 | 延长冷却、降低并发和样本，不修改固件 |
| 固定帧稳定复现失败 | 隔离单帧与场景状态，不重做全部有效帧 |
| WHEA、BugCheck、蓝屏或整机重启 | 停止自动重试，保存证据并排查硬件、供电、驱动和温度 |

## 7. 重启后诊断

收集故障前后窗口的 `Display`、`nvlddmkm`、`WHEA-Logger`、`BugCheck` 和 `Kernel-Power` 事件，以及 `C:\Windows\Minidump`、Blender crash 文件、最后心跳、最后有效帧和批次 stderr。把时间、事件 ID、错误模块和批次写入日志或状态，不只记录“又卡了”。

## 8. 渲染完成条件

- `render-state.json.status=complete`，且没有本项目活动 Blender 子进程。
- 每个镜头的预期编号集合与有效帧集合完全一致，颜色布局一致。
- approved `.blend` 与 storyboard 哈希未变化。
- 所有 `frames_validated` 闸门为 `pass`，验证报告和批次日志已保存。
- 自动续跑任务若曾启用，已经禁用并向用户报告。

这只是渲染完成，不等于最终成片 QA 通过；最终完成另需有效的 `delivery-report.json`。

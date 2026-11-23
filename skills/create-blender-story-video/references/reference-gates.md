# 参考图与 Blender 验收闸门

在生成参考图、制作 Blender blockout、调整材质灯光或批准动画预览时读取本文件。

## 共同原则

- 参考图是视觉目标，Blender 可测量状态是事实来源；不要求像素级复制 AI 图片。
- 每轮只修正一个明确问题，保存修正前后的预览和测量结果。
- `shot-status.json` 使用 `assets/schemas/shot-status.schema.json`，并由 `validate_project.py` 校验。
- 创作闸门只允许 `pending`、`pass`、`fail`、`accepted_deviation`。偏差必须记录 `accepted_reason` 和 `approved_by`。
- `frames_validated` 不允许 `accepted_deviation`，只能由完整 PNG 校验写为 `pass`。
- 工具不可用时记录替代检查；不要把“未检查”写成通过。

最小结构：

```json
{
  "schema_version": 1,
  "shots": [
    {
      "shot_id": "shot_01",
      "gates": {
        "reference_ready": {"status": "pending", "evidence": [], "method": "", "checked_at": "2026-01-01T00:00:00+00:00"},
        "blockout": {"status": "pending", "evidence": [], "method": "", "checked_at": "2026-01-01T00:00:00+00:00"},
        "lookdev": {"status": "pending", "evidence": [], "method": "", "checked_at": "2026-01-01T00:00:00+00:00"},
        "camera_animation": {"status": "pending", "evidence": [], "method": "", "checked_at": "2026-01-01T00:00:00+00:00"},
        "frames_validated": {"status": "pending", "evidence": [], "method": "", "checked_at": "2026-01-01T00:00:00+00:00"}
      },
      "issues": [],
      "next_action": "",
      "updated_at": "2026-01-01T00:00:00+00:00"
    }
  ]
}
```

## A. 参考图就绪

逐镜头检查：

- 文件可完整解码，画幅和方向符合 storyboard。
- 使用已批准母图和同一 `STYLE_CAPSULE`，没有未解释的风格漂移。
- 画面只表达一个镜头，不包含拼贴、字幕、片名或生成式 UI。
- 主体、环境、时间、光向、景别和摄像机方向与分镜一致。
- 主体身份、关键道具、材质语言和空间规则跨镜头连续。
- 图像明确表达需要复现的轮廓、比例、遮挡和构图；无法判断的背面或结构列为假设。
- `generation-manifest.json` 记录真实生成信息。

任一镜头不通过时只重做该镜头；全片风格漂移时回到母图。

## B. Blockout 对照

从目标摄像机渲染定帧，必要时补正交、侧面或斜侧视角。检查主体画面占比、视觉中心、裁切、出框、遮挡、穿插、近裁剪面、主轮廓、地平线、前中后景，以及尺寸、间距、接触、对齐和比例。记录焦段、位置、旋转和传感器设置；不要用对象变形掩盖错误透视。

## C. 材质与灯光对照

在 blockout 通过后检查主色、明度层级、粗糙度、光向、色温、阴影、轮廓光、雾、主体与背景分离、材质尺度、高频细节，以及跨镜头一致的色彩管理、曝光和查看变换。镜头级偏移必须有叙事理由。

## D. 摄像机与动画对照

使用低分辨率 playblast 检查首帧、中段、落定帧和末帧：

- `subject_action` 与 `camera_action` 各自只有一个主要意图。
- 加速度、`hold_frames` 和切点符合分镜。
- 没有意外抖动、速度突变、穿模、裁切或焦点漂移。
- 跨镜头主体位置、运动方向、光线和空间关系连续，或有明确断裂意图。
- 音乐节拍误差写入验收条件。

## E. 帧序列验证

```bash
python /path/to/skill/scripts/verify_frames.py \
  --project-root /path/to/project --shot-id shot_01 --update-status
```

验证器检查精确编号集合、重复和额外文件、PNG chunk CRC、完整 zlib 解压、尺寸、位深、color type 和 interlace 一致性。报告写入 `render/validation/<shot>.frames.json`，证据路径写入 `frames_validated`。

## 迭代与放行

1. 从当前闸门列出最多三个可观察差异。
2. 选择影响最大的一个并定位到几何、变换、材质、灯光、摄像机或动画。
3. 保存检查点，只修改对应参数。
4. 使用同一摄像机、帧号和测量方法重新生成证据。
5. 更新状态，通过后再进入下一闸门。

同一问题连续两轮没有改善时停止微调，检查父子层级、未应用变换、约束、驱动器、摄像机绑定和场景帧。所有创作闸门为 `pass` 或经用户明确接受后，镜头才可进入正式渲染。

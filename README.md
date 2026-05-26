# Batch Video Ad Trimmer

本工具用于批量辅助去除视频片头/片尾广告。推荐使用桌面界面：选择视频或目录，点击开始分析，选择正片开始/结束片段，然后生成无损裁剪后的视频。

## Requirements

- Python 3.11+
- FFmpeg / ffprobe

如果使用 `winget install Gyan.FFmpeg` 后当前 PowerShell 仍然找不到 `ffmpeg`，工具会自动尝试查找 WinGet 安装目录。仍找不到时，可以在桌面界面里点击“选择 FFmpeg”手动指定 `ffmpeg.exe` 和 `ffprobe.exe`。

## Desktop Workflow

可以直接双击：

```text
启动视频裁剪工具.bat
```

也可以用命令启动：

启动桌面界面：

```powershell
py video_ad_trimmer.py gui
```

使用流程：

1. 直接拖拽视频文件/文件夹到窗口，或点击“选择视频”“选择目录”。
2. 按需调整“片头范围(分钟)”和“片尾范围(分钟)”，默认都是 5。
3. 如需统一输出到新目录，填写或选择“输出目录”；留空则保存到原视频目录。
4. 点击“开始分析”。
5. 分析完成后，在片头区域选择“真正正片开始的片段”。
6. 在片尾区域选择“真正正片结束的片段”。
7. 如果没有片头或片尾广告，点击“无片头广告”或“无片尾广告”。
8. 需要微调时，填写片头/片尾 offset 秒数。
9. 如果同一目录下的视频片头/片尾一致，可以先选好一个视频，再点击“应用当前选择到全部视频”。
10. 点击“生成视频”。

输出视频默认保存在原视频同目录，文件名为：

```text
原文件名_yyyyMMdd_HHmmss.扩展名
```

示例：

```text
海贼王1153_20260521_094322.mp4
```

默认生成模式会尽量只重编码切点附近并复制中间片段；检测到输出时长明显偏差时会自动兜底为精确生成，不会覆盖原视频。

## Logs

- 桌面界面每次点击“开始分析”都会创建一个会话目录，例如 `ad_trim_output/gui_session_yyyyMMdd_HHmmss`。
- 日志文件在该会话目录下：`cliptailor.log`。
- 如果裁剪不准确，请保留这几个文件：`cliptailor.log`、`manifest.json`、`selections.json`。
- 反馈时说明原视频文件名、你选择的开始/结束时间、实际输出时长，以及是否看到“自动兜底”相关结果。

## CLI Workflow

命令行仍然保留：

```powershell
py video_ad_trimmer.py analyze .\videos -o .\ad_trim_output --recursive
py video_ad_trimmer.py serve -d .\ad_trim_output
py video_ad_trimmer.py cut -m .\ad_trim_output\manifest.json -s .\ad_trim_output\selections.json
```

单个视频不需要 `--recursive`：

```powershell
py video_ad_trimmer.py analyze .\海贼王1153.mp4 -o .\ad_trim_output
```

可选导出 LosslessCut CSV：

```powershell
py video_ad_trimmer.py csv -m .\ad_trim_output\manifest.json -s .\ad_trim_output\selections.json
```

## Useful Options

```powershell
py video_ad_trimmer.py analyze .\videos -o .\ad_trim_output --scan-seconds 300 --scene-threshold 0.32 --min-segment-seconds 0.5
```

- `--scan-seconds`：分析片头/片尾多少秒，默认 300。
- `--scene-threshold`：场景变化阈值，越低候选片段越多，默认 0.32。
- `--min-segment-seconds`：短片段会合并到相邻片段，默认 0.5。
- `--max-segments-per-side`：每侧最多候选片段数，默认 45。

## Notes

- 桌面版首版使用 Python 内置 `tkinter`，不需要额外安装前端框架。
- 首版提供“选择视频/目录”，暂不强制实现拖拽。
- 无损裁剪受关键帧影响，工具会用 ffprobe 查找选择时间点附近的关键帧。
- 分析生成的候选缩略图保存在 `ad_trim_output/gui_session_*` 或指定输出目录中。

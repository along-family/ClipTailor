# ClipTailor v2 Portable

This package is designed for users who do not want to install Python or FFmpeg.

## Start

Double-click one of these files:

```text
ClipTailor.exe
启动视频裁剪工具.bat
```

For the default portable folder package, keep the whole folder together. Do not move only `ClipTailor.exe`, because the bundled `tools\ffmpeg\bin` folder is required for video analysis and cutting.

If the package was built with `build_portable.ps1 -OneFile`, `ClipTailor.exe` already contains FFmpeg and can be copied by itself.

## Folder Layout

```text
ClipTailor-v2-portable\
  ClipTailor.exe
  启动视频裁剪工具.bat
  tools\
    ffmpeg\
      bin\
        ffmpeg.exe
        ffprobe.exe
```

## Notes

- No Python installation is required on the user's computer.
- No system FFmpeg installation is required on the user's computer.
- Output videos are not written over the original files.
- If antivirus software blocks first launch, allow `ClipTailor.exe` and try again.

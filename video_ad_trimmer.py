#!/usr/bin/env python3
"""
Batch helper for trimming uncertain intro/outro ad blocks from videos.

Workflow:
  1. analyze: detect candidate segments at the head/tail and export first frames.
  2. serve: open a tiny local review API for saving selections from review.html.
 3. cut: trim videos from saved selections with ffmpeg stream copy or exact re-encode.

The script intentionally keeps the final ad decision human-in-the-loop. Scene
cuts are used only to produce useful candidate chunks and thumbnails.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".flv",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
    ".wmv",
}

DEFAULT_SCAN_SECONDS = 5 * 60
DEFAULT_SCENE_THRESHOLD = 0.32
MIN_SEGMENT_SECONDS = 0.5
DEFAULT_MIN_SEGMENT_SECONDS = 0.5
DEFAULT_MERGE_GAP_SECONDS = 0.5
DEFAULT_MAX_SEGMENTS_PER_SIDE = 45
DEFAULT_GUI_OUTPUT_ROOT = "ad_trim_output"
LOG_FILE_NAME = "cliptailor.log"
DEFAULT_AUTO_REENCODE_THRESHOLD = 0.5
KEYFRAME_SEARCH_WINDOW_SECONDS = 120.0
SMART_RENDER_MIN_COPY_SECONDS = 1.0
FAST_PRECISE_SEEK_MARGIN_SECONDS = 8.0
OUTPUT_DURATION_TOLERANCE_SECONDS = 0.75
PREFERRED_VIDEO_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")
VIDEO_ENCODER_CHOICES = ("auto", *PREFERRED_VIDEO_ENCODERS)
PORTABLE_TOOL_DIRS = (
    Path("ffmpeg") / "bin",
    Path("tools") / "ffmpeg" / "bin",
    Path("_internal") / "ffmpeg" / "bin",
)
LOGGER = logging.getLogger("cliptailor")


@dataclass(frozen=True)
class ToolPaths:
    ffmpeg: str
    ffprobe: str


@dataclass(frozen=True)
class Segment:
    index: int
    start: float
    end: float
    thumbnail: str


@dataclass(frozen=True)
class RenderSegment:
    label: str
    mode: str
    start: float
    end: float


@dataclass(frozen=True)
class SourceProfile:
    video_codec: str | None
    audio_codec: str | None
    audio_streams: int
    subtitle_streams: int


@dataclass(frozen=True)
class CutPlan:
    mode: str
    decision: str
    requested_start: float
    requested_end: float
    actual_start: float
    actual_end: float
    keyframe_start: float
    keyframe_end: float
    start_delta: float
    end_delta: float
    alignment_available: bool
    video_encoder: str | None = None
    segments: tuple[RenderSegment, ...] = ()
    fallback_reason: str | None = None


class ToolError(RuntimeError):
    pass


class OutputDurationMismatch(ToolError):
    pass


def main() -> int:
    argv = sys.argv[1:]
    if not argv and getattr(sys, "frozen", False):
        argv = ["gui"]

    parser = argparse.ArgumentParser(
        description="Analyze video heads/tails, export candidate ad thumbnails, and batch trim selections."
    )
    parser.add_argument("--ffmpeg", default=os.environ.get("FFMPEG", "ffmpeg"), help="Path to ffmpeg.")
    parser.add_argument("--ffprobe", default=os.environ.get("FFPROBE", "ffprobe"), help="Path to ffprobe.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Analyze videos and create thumbnails plus review.html.")
    analyze.add_argument("input", help="Video file or directory containing videos.")
    analyze.add_argument("-o", "--output", default="ad_trim_output", help="Output directory.")
    analyze.add_argument("--scan-seconds", type=float, default=DEFAULT_SCAN_SECONDS, help="Seconds to scan at each end.")
    analyze.add_argument("--scene-threshold", type=float, default=DEFAULT_SCENE_THRESHOLD, help="FFmpeg scene threshold.")
    analyze.add_argument(
        "--min-segment-seconds",
        type=float,
        default=DEFAULT_MIN_SEGMENT_SECONDS,
        help="Merge/filter candidate segments shorter than this.",
    )
    analyze.add_argument(
        "--merge-gap-seconds",
        type=float,
        default=DEFAULT_MERGE_GAP_SECONDS,
        help="Merge adjacent boundaries closer than this.",
    )
    analyze.add_argument(
        "--max-segments-per-side",
        type=int,
        default=DEFAULT_MAX_SEGMENTS_PER_SIDE,
        help="Limit candidate segments per side by downsampling boundaries.",
    )
    analyze.add_argument("--recursive", action="store_true", help="Search input directories recursively.")
    analyze.add_argument("--overwrite", action="store_true", help="Replace existing analysis output.")

    cut = subparsers.add_parser("cut", help="Trim videos from selections.json.")
    cut.add_argument("-m", "--manifest", default="ad_trim_output/manifest.json", help="Path to manifest.json.")
    cut.add_argument("-s", "--selections", default="ad_trim_output/selections.json", help="Path to selections.json.")
    cut.add_argument("-o", "--output", default=None, help="Directory for trimmed videos. Defaults to each source directory.")
    cut.add_argument("--overwrite", action="store_true", help="Overwrite existing trimmed videos.")
    cut.add_argument("--copy-unchanged", action="store_true", help="Copy videos where both sides are marked as no ads.")
    cut.add_argument("--smart-render-edges", action="store_true", help="Re-encode only the cut edges and copy the middle section.")
    cut.add_argument("--reencode", action="store_true", help="Always re-encode output for exact cuts.")
    cut.add_argument(
        "--video-encoder",
        choices=VIDEO_ENCODER_CHOICES,
        default="auto",
        help="Video encoder for smart/exact render. auto prefers GPU encoders when available.",
    )
    cut.add_argument(
        "--auto-reencode-threshold",
        type=float,
        default=None,
        help="Auto-switch to smart render when keyframe cuts drift more than this many seconds. Disabled by default.",
    )
    cut.add_argument("--dry-run", action="store_true", help="Print ffmpeg commands without running them.")

    csv_cmd = subparsers.add_parser("csv", help="Create LosslessCut-compatible CSV from selections.")
    csv_cmd.add_argument("-m", "--manifest", default="ad_trim_output/manifest.json", help="Path to manifest.json.")
    csv_cmd.add_argument("-s", "--selections", default="ad_trim_output/selections.json", help="Path to selections.json.")
    csv_cmd.add_argument("-o", "--output", default=None, help="CSV output path. Defaults to <analysis>/losslesscut.csv.")

    serve = subparsers.add_parser("serve", help="Serve review UI and save selections.json from the browser.")
    serve.add_argument("-d", "--directory", default="ad_trim_output", help="Analysis output directory.")
    serve.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    serve.add_argument("--port", type=int, default=8765, help="Port to bind.")

    gui = subparsers.add_parser("gui", help="Open the desktop review and trimming app.")
    gui.add_argument("--output-root", default=DEFAULT_GUI_OUTPUT_ROOT, help="Directory for GUI analysis sessions.")

    args = parser.parse_args(argv)
    tools = discover_tool_paths(ToolPaths(args.ffmpeg, args.ffprobe))

    try:
        if args.command == "analyze":
            require_tools(tools)
            analyze_videos(args, tools)
        elif args.command == "cut":
            if not args.dry_run:
                require_tools(tools)
            cut_videos(args, tools)
        elif args.command == "csv":
            export_losslesscut_csv(args)
        elif args.command == "serve":
            serve_review(args)
        elif args.command == "gui":
            launch_gui(args, tools)
        else:
            parser.error(f"Unknown command: {args.command}")
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    return 0


def require_tools(tools: ToolPaths, need_probe: bool = True) -> None:
    missing = []
    if not resolve_executable(tools.ffmpeg):
        missing.append(f"ffmpeg ({tools.ffmpeg})")
    if need_probe and not resolve_executable(tools.ffprobe):
        missing.append(f"ffprobe ({tools.ffprobe})")
    if missing:
        raise ToolError(
            "Missing required tool(s): "
            + ", ".join(missing)
            + ". Install FFmpeg or pass --ffmpeg/--ffprobe with full paths."
        )


def discover_tool_paths(tools: ToolPaths) -> ToolPaths:
    bundled_ffmpeg = find_portable_tool("ffmpeg.exe") if is_default_tool_name(tools.ffmpeg, "ffmpeg") else None
    bundled_ffprobe = find_portable_tool("ffprobe.exe") if is_default_tool_name(tools.ffprobe, "ffprobe") else None
    ffmpeg = bundled_ffmpeg or resolve_executable(tools.ffmpeg) or find_winget_tool("ffmpeg.exe") or tools.ffmpeg
    ffprobe = bundled_ffprobe or resolve_executable(tools.ffprobe) or find_winget_tool("ffprobe.exe") or tools.ffprobe
    return ToolPaths(ffmpeg=ffmpeg, ffprobe=ffprobe)


def is_default_tool_name(value: str, tool_name: str) -> bool:
    return value.lower() in {tool_name, f"{tool_name}.exe"}


def portable_roots() -> list[Path]:
    roots = [Path(__file__).resolve().parent]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
        bundle_dir = getattr(sys, "_MEIPASS", None)
        if bundle_dir:
            roots.insert(0, Path(bundle_dir).resolve())
    unique_roots = []
    for root in roots:
        if root not in unique_roots:
            unique_roots.append(root)
    return unique_roots


def find_portable_tool(filename: str) -> str | None:
    for root in portable_roots():
        for relative_dir in PORTABLE_TOOL_DIRS:
            candidate = root / relative_dir / filename
            if candidate.exists():
                return str(candidate)
        candidate = root / filename
        if candidate.exists():
            return str(candidate)
    return None


def find_winget_tool(filename: str) -> str | None:
    roots = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "Microsoft" / "WinGet" / "Packages")
    for root in roots:
        if not root.exists():
            continue
        matches = sorted(
            root.rglob(filename),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return str(matches[0])
    return None


def resolve_executable(value: str) -> str | None:
    path = Path(value)
    if path.exists():
        return str(path)
    return shutil.which(value)


def configure_logging(log_path: Path | None) -> Path | None:
    if log_path is None:
        return None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    resolved = log_path.resolve()
    for handler in LOGGER.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == resolved:
            return log_path
    for handler in list(LOGGER.handlers):
        if isinstance(handler, logging.FileHandler):
            LOGGER.removeHandler(handler)
            handler.close()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.info("logging started: %s", log_path)
    return log_path


def log_info(message: str, *args: Any) -> None:
    if LOGGER.handlers:
        LOGGER.info(message, *args)


def log_error(message: str, *args: Any) -> None:
    if LOGGER.handlers:
        LOGGER.error(message, *args)


def analyze_videos(args: argparse.Namespace, tools: ToolPaths) -> None:
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    log_path = configure_logging(output_dir / LOG_FILE_NAME)
    if log_path:
        print(f"log: {log_path}")
    log_info(
        "analyze start input=%s output=%s scan=%s scene_threshold=%s min_segment=%s merge_gap=%s max_segments=%s recursive=%s",
        input_path,
        output_dir,
        args.scan_seconds,
        args.scene_threshold,
        args.min_segment_seconds,
        args.merge_gap_seconds,
        args.max_segments_per_side,
        args.recursive,
    )
    if args.min_segment_seconds < MIN_SEGMENT_SECONDS:
        raise ToolError(f"--min-segment-seconds must be at least {format_seconds(MIN_SEGMENT_SECONDS)}.")
    if output_dir.exists() and args.overwrite:
        remove_analysis_output(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    videos = find_videos(input_path, args.recursive)
    if not videos:
        raise ToolError(f"No supported video files found in {input_path}.")

    manifest = analyze_video_list(
        videos=videos,
        output_dir=output_dir,
        tools=tools,
        scan_seconds=args.scan_seconds,
        scene_threshold=args.scene_threshold,
        min_segment_seconds=args.min_segment_seconds,
        merge_gap_seconds=args.merge_gap_seconds,
        max_segments_per_side=args.max_segments_per_side,
        progress=lambda ordinal, total, video: print(f"[{ordinal}/{total}] analyzing {video}"),
    )
    write_analysis_files(output_dir, manifest)
    print(f"done: {output_dir}")
    print(f"review: {output_dir / 'review.html'}")
    print(f"save selections with: py video_ad_trimmer.py serve -d {quote_path(output_dir)}")


def analyze_video_list(
    *,
    videos: list[Path],
    output_dir: Path,
    tools: ToolPaths,
    scan_seconds: float,
    back_scan_seconds: float | None = None,
    scene_threshold: float,
    min_segment_seconds: float,
    merge_gap_seconds: float,
    max_segments_per_side: int,
    progress: Callable[[int, int, Path], None] | None = None,
) -> dict[str, Any]:
    front_scan_seconds = scan_seconds
    tail_scan_seconds = back_scan_seconds if back_scan_seconds is not None else scan_seconds
    items: list[dict[str, Any]] = []
    for ordinal, video in enumerate(videos, start=1):
        if progress:
            progress(ordinal, len(videos), video)
        duration = probe_duration(video, tools)
        video_id = make_video_id(ordinal, video)
        item_dir = output_dir / "items" / video_id
        item_dir.mkdir(parents=True, exist_ok=True)

        front = analyze_side(
            video=video,
            side="front",
            window_start=0.0,
            window_end=min(duration, front_scan_seconds),
            duration=duration,
            item_dir=item_dir,
            tools=tools,
            scene_threshold=scene_threshold,
            min_segment_seconds=min_segment_seconds,
            merge_gap_seconds=merge_gap_seconds,
            max_segments=max_segments_per_side,
        )
        back = analyze_side(
            video=video,
            side="back",
            window_start=max(0.0, duration - tail_scan_seconds),
            window_end=duration,
            duration=duration,
            item_dir=item_dir,
            tools=tools,
            scene_threshold=scene_threshold,
            min_segment_seconds=min_segment_seconds,
            merge_gap_seconds=merge_gap_seconds,
            max_segments=max_segments_per_side,
        )

        items.append(
            {
                "id": video_id,
                "source": str(video),
                "name": video.name,
                "duration": duration,
                "front": [segment_to_dict(seg) for seg in front],
                "back": [segment_to_dict(seg) for seg in back],
            }
        )

    manifest = {
        "version": 1,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "settings": {
            "scanSeconds": scan_seconds,
            "frontScanSeconds": front_scan_seconds,
            "backScanSeconds": tail_scan_seconds,
            "sceneThreshold": scene_threshold,
            "minSegmentSeconds": min_segment_seconds,
            "mergeGapSeconds": merge_gap_seconds,
            "maxSegmentsPerSide": max_segments_per_side,
        },
        "items": items,
    }
    return manifest


def write_analysis_files(output_dir: Path, manifest: dict[str, Any]) -> None:
    write_json(output_dir / "manifest.json", manifest)
    write_json(output_dir / "selections.example.json", build_default_selections(manifest))
    write_review_html(output_dir / "review.html", manifest)


def remove_analysis_output(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    cwd = Path.cwd().resolve()
    try:
        resolved.relative_to(cwd)
    except ValueError as exc:
        raise ToolError(f"Refusing to remove output outside current workspace: {resolved}") from exc
    shutil.rmtree(resolved)


def find_videos(input_path: Path, recursive: bool) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() in VIDEO_EXTENSIONS else []
    if not input_path.is_dir():
        raise ToolError(f"Input path does not exist: {input_path}")
    iterator = input_path.rglob("*") if recursive else input_path.iterdir()
    return sorted(path.resolve() for path in iterator if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS)


def probe_duration(video: Path, tools: ToolPaths) -> float:
    cmd = [
        tools.ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    result = run_command(cmd, capture=True)
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ToolError(f"Could not read duration for {video}: {result.stdout!r}") from exc
    if duration <= 0:
        raise ToolError(f"Invalid duration for {video}: {duration}")
    return duration


def analyze_side(
    *,
    video: Path,
    side: str,
    window_start: float,
    window_end: float,
    duration: float,
    item_dir: Path,
    tools: ToolPaths,
    scene_threshold: float,
    min_segment_seconds: float,
    merge_gap_seconds: float,
    max_segments: int,
) -> list[Segment]:
    side_dir = item_dir / side
    side_dir.mkdir(parents=True, exist_ok=True)
    for old_file in side_dir.glob("*.jpg"):
        old_file.unlink()
    for old_file in side_dir.glob("*.png"):
        old_file.unlink()

    if window_end <= window_start:
        return []

    boundaries = detect_scene_boundaries(video, window_start, window_end, scene_threshold, tools)
    segment_ranges = build_segments(
        window_start=window_start,
        window_end=window_end,
        boundaries=boundaries,
        min_segment_seconds=min_segment_seconds,
        merge_gap_seconds=merge_gap_seconds,
        max_segments=max_segments,
    )

    segments: list[Segment] = []
    for index, (start, end) in enumerate(segment_ranges, start=1):
        thumb_name = f"{index:03d}_{format_time_for_name(start)}.png"
        thumb_path = side_dir / thumb_name
        capture_time = clamp(start + 0.25, 0.0, max(0.0, duration - 0.01))
        extract_frame(video, capture_time, thumb_path, tools)
        segments.append(
            Segment(
                index=index,
                start=round(start, 3),
                end=round(end, 3),
                thumbnail=to_posix_relative(thumb_path, item_dir.parent.parent),
            )
        )
    return segments


def detect_scene_boundaries(video: Path, start: float, end: float, threshold: float, tools: ToolPaths) -> list[float]:
    duration = max(0.0, end - start)
    if duration <= 0:
        return []
    filter_expr = f"select='gt(scene,{threshold})',showinfo"
    cmd = [
        tools.ffmpeg,
        "-hide_banner",
        "-ss",
        format_seconds(start),
        "-t",
        format_seconds(duration),
        "-i",
        str(video),
        "-vf",
        filter_expr,
        "-an",
        "-f",
        "null",
        "-",
    ]
    result = run_command(cmd, capture=True)
    output = result.stderr
    times: list[float] = []
    for match in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", output):
        times.append(start + float(match.group(1)))
    return sorted(set(round(value, 3) for value in times if start < value < end))


def build_segments(
    *,
    window_start: float,
    window_end: float,
    boundaries: list[float],
    min_segment_seconds: float,
    merge_gap_seconds: float,
    max_segments: int,
) -> list[tuple[float, float]]:
    cleaned = merge_close_boundaries(boundaries, merge_gap_seconds)
    points = [window_start, *cleaned, window_end]
    segments = [(points[index], points[index + 1]) for index in range(len(points) - 1)]
    segments = merge_short_segments(segments, min_segment_seconds)
    segments = limit_segments(segments, max_segments)
    return [(round(start, 3), round(end, 3)) for start, end in segments if end > start]


def merge_close_boundaries(boundaries: list[float], min_gap: float) -> list[float]:
    merged: list[float] = []
    for boundary in boundaries:
        if not merged or boundary - merged[-1] >= min_gap:
            merged.append(boundary)
        else:
            merged[-1] = boundary
    return merged


def merge_short_segments(segments: list[tuple[float, float]], min_length: float) -> list[tuple[float, float]]:
    if not segments:
        return []
    result = list(segments)
    changed = True
    while changed and len(result) > 1:
        changed = False
        for index, (start, end) in enumerate(result):
            if end - start >= min_length:
                continue
            if index == 0:
                result[1] = (start, result[1][1])
                del result[0]
            elif index == len(result) - 1:
                result[index - 1] = (result[index - 1][0], end)
                del result[index]
            else:
                previous_length = result[index - 1][1] - result[index - 1][0]
                next_length = result[index + 1][1] - result[index + 1][0]
                if previous_length <= next_length:
                    result[index - 1] = (result[index - 1][0], end)
                    del result[index]
                else:
                    result[index + 1] = (start, result[index + 1][1])
                    del result[index]
            changed = True
            break
    return result


def limit_segments(segments: list[tuple[float, float]], max_segments: int) -> list[tuple[float, float]]:
    if max_segments <= 0 or len(segments) <= max_segments:
        return segments
    ratio = len(segments) / max_segments
    kept: list[tuple[float, float]] = []
    for index in range(max_segments):
        start_index = int(index * ratio)
        end_index = int((index + 1) * ratio) - 1
        end_index = max(start_index, min(end_index, len(segments) - 1))
        kept.append((segments[start_index][0], segments[end_index][1]))
    return kept


def extract_frame(video: Path, seconds: float, output: Path, tools: ToolPaths) -> None:
    cmd = [
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        format_seconds(seconds),
        "-i",
        str(video),
        "-vf",
        "scale=480:-2",
        "-frames:v",
        "1",
        "-y",
        str(output),
    ]
    run_command(cmd, capture=True)


def cut_videos(args: argparse.Namespace, tools: ToolPaths) -> None:
    manifest_path = Path(args.manifest).expanduser().resolve()
    selections_path = Path(args.selections).expanduser().resolve()
    log_path = configure_logging(manifest_path.parent / LOG_FILE_NAME)
    if log_path:
        print(f"log: {log_path}")
    log_info(
        "cut start manifest=%s selections=%s output=%s overwrite=%s reencode=%s smart_render_edges=%s auto_threshold=%s dry_run=%s",
        manifest_path,
        selections_path,
        args.output,
        args.overwrite,
        args.reencode,
        args.smart_render_edges,
        args.auto_reencode_threshold,
        args.dry_run,
    )
    manifest = read_json(manifest_path)
    selections = read_json(selections_path)
    output_dir = Path(args.output).expanduser().resolve() if args.output else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    selection_map = {item["id"]: item for item in selections.get("items", [])}
    for item in manifest.get("items", []):
        selected = selection_map.get(item["id"])
        if not selected:
            print(f"skip {item['name']}: no selection")
            continue
        source = Path(item["source"])
        requested_start, requested_end, unchanged = compute_requested_range(item, selected)
        log_info(
            "selection source=%s start=%s end=%s duration=%s unchanged=%s selection=%s",
            source,
            format_timestamp(requested_start),
            format_timestamp(requested_end),
            format_timestamp(max(0.0, requested_end - requested_start)),
            unchanged,
            json.dumps(selected, ensure_ascii=False, sort_keys=True),
        )
        if unchanged and not args.copy_unchanged:
            print(f"skip {item['name']}: no ads selected, source will stay unchanged")
            log_info("skip source=%s reason=no ads selected", source)
            continue
        auto_reencode_threshold = (
            float("inf") if args.auto_reencode_threshold is None else max(0.0, float(args.auto_reencode_threshold))
        )
        plan = choose_cut_plan(
            source=source,
            requested_start=requested_start,
            requested_end=requested_end,
            tools=tools,
            force_precise=args.reencode,
            prefer_smart_edges=args.smart_render_edges,
            auto_reencode_threshold=auto_reencode_threshold,
            video_encoder=args.video_encoder,
            allow_missing_alignment=args.dry_run,
        )
        if plan.actual_end <= plan.actual_start:
            print(f"skip {item['name']}: invalid range {plan.actual_start}..{plan.actual_end}")
            continue
        output = output_path_for_source(output_dir, source, args.overwrite)
        if args.dry_run:
            print(f"# {source.name}: {format_cut_plan_summary(plan)}")
            commands = execute_cut_plan(plan, tools, source, output, args.overwrite, dry_run=True)
            for command in commands:
                print(" ".join(quote_arg(part) for part in command))
            continue
        print(f"cut {source.name}: {format_cut_plan_summary(plan)}")
        execute_cut_plan(plan, tools, source, output, args.overwrite, dry_run=False)
        log_info("cut wrote source=%s output=%s size=%s", source, output, output.stat().st_size)
        print(f"  wrote {output} ({format_file_size(output.stat().st_size)})")


def export_losslesscut_csv(args: argparse.Namespace) -> None:
    manifest_path = Path(args.manifest).expanduser().resolve()
    selections_path = Path(args.selections).expanduser().resolve()
    manifest = read_json(manifest_path)
    selections = read_json(selections_path)
    output_path = Path(args.output).expanduser().resolve() if args.output else manifest_path.parent / "losslesscut.csv"
    selection_map = {item["id"]: item for item in selections.get("items", [])}

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["segment start", "segment end", "label"])
        for item in manifest.get("items", []):
            selected = selection_map.get(item["id"])
            if not selected:
                continue
            start, end, unchanged = compute_requested_range(item, selected)
            if unchanged:
                continue
            if end > start:
                writer.writerow([format_seconds(start), format_seconds(end), Path(item["source"]).stem])
    print(f"wrote {output_path}")


def compute_requested_range(item: dict[str, Any], selection: dict[str, Any]) -> tuple[float, float, bool]:
    front_index = selection.get("frontIndex")
    back_index = selection.get("backIndex")
    front_time = selection.get("frontTimeSeconds")
    back_time = selection.get("backTimeSeconds")
    front_offset = float(selection.get("frontOffsetSeconds", 0) or 0)
    back_offset = float(selection.get("backOffsetSeconds", 0) or 0)

    start = 0.0
    end = float(item["duration"])
    if front_time is not None:
        start = float(front_time)
    elif front_index is not None:
        front_segment = get_segment_by_index(item.get("front", []), int(front_index))
        if front_segment:
            start = float(front_segment["start"]) + front_offset
    if back_time is not None:
        end = float(back_time)
    elif back_index is not None:
        back_segment = get_segment_by_index(item.get("back", []), int(back_index))
        if back_segment:
            end = float(back_segment["end"]) + back_offset
    duration = float(item["duration"])
    unchanged = front_index is None and back_index is None and front_time is None and back_time is None
    return clamp(start, 0.0, end), clamp(end, 0.0, duration), unchanged


def get_segment_by_index(segments: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
    for segment in segments:
        if int(segment.get("index", -1)) == index:
            return segment
    return None


def find_nearest_keyframe(
    video: Path,
    seconds: float,
    tools: ToolPaths,
    prefer: str,
    *,
    search_window_seconds: float = KEYFRAME_SEARCH_WINDOW_SECONDS,
) -> tuple[float, bool]:
    search_start = max(0.0, seconds - search_window_seconds)
    search_end = seconds + search_window_seconds
    cmd = [
        tools.ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-skip_frame",
        "nokey",
        "-show_entries",
        "frame=best_effort_timestamp_time,pkt_pts_time",
        "-of",
        "csv=p=0",
        "-read_intervals",
        f"{format_seconds(search_start)}%{format_seconds(search_end)}",
        str(video),
    ]
    result = run_command(cmd, capture=True)
    keyframes: list[float] = []
    for line in result.stdout.splitlines():
        for value in line.split(","):
            value = value.strip()
            if not value:
                continue
            try:
                keyframes.append(float(value))
                break
            except ValueError:
                continue
    if not keyframes:
        return seconds, False
    if prefer == "before":
        candidates = [value for value in keyframes if value <= seconds]
        if candidates:
            return max(candidates), True
        return min(keyframes, key=lambda value: abs(value - seconds)), False
    if prefer == "after":
        candidates = [value for value in keyframes if value >= seconds]
        if candidates:
            return min(candidates), True
        return min(keyframes, key=lambda value: abs(value - seconds)), False
    return min(keyframes, key=lambda value: abs(value - seconds)), True


def choose_cut_plan(
    *,
    source: Path,
    requested_start: float,
    requested_end: float,
    tools: ToolPaths,
    force_precise: bool,
    prefer_smart_edges: bool,
    auto_reencode_threshold: float,
    video_encoder: str = "auto",
    allow_missing_alignment: bool = False,
) -> CutPlan:
    keyframe_start, keyframe_end, alignment_available = resolve_keyframe_range(
        source,
        requested_start,
        requested_end,
        tools,
        allow_missing_alignment=allow_missing_alignment,
    )
    start_delta = abs(keyframe_start - requested_start)
    end_delta = abs(keyframe_end - requested_end)
    if force_precise:
        selected_video_encoder = resolve_video_encoder(tools, video_encoder)
        return CutPlan(
            mode="precise",
            decision="forced",
            requested_start=requested_start,
            requested_end=requested_end,
            actual_start=requested_start,
            actual_end=requested_end,
            keyframe_start=keyframe_start,
            keyframe_end=keyframe_end,
            start_delta=start_delta,
            end_delta=end_delta,
            alignment_available=alignment_available,
            video_encoder=selected_video_encoder,
            segments=(RenderSegment("full", "precise", requested_start, requested_end),),
        )
    if prefer_smart_edges:
        selected_video_encoder = resolve_video_encoder(tools, video_encoder)
        return choose_smart_cut_plan(
            source=source,
            requested_start=requested_start,
            requested_end=requested_end,
            keyframe_start=keyframe_start,
            keyframe_end=keyframe_end,
            alignment_available=alignment_available,
            start_delta=start_delta,
            end_delta=end_delta,
            tools=tools,
            video_encoder=selected_video_encoder,
        )
    if alignment_available and max(start_delta, end_delta) > auto_reencode_threshold:
        selected_video_encoder = resolve_video_encoder(tools, video_encoder)
        return choose_smart_cut_plan(
            source=source,
            requested_start=requested_start,
            requested_end=requested_end,
            keyframe_start=keyframe_start,
            keyframe_end=keyframe_end,
            alignment_available=alignment_available,
            start_delta=start_delta,
            end_delta=end_delta,
            tools=tools,
            video_encoder=selected_video_encoder,
        )
    return CutPlan(
        mode="copy",
        decision="copy",
        requested_start=requested_start,
        requested_end=requested_end,
        actual_start=keyframe_start,
        actual_end=keyframe_end,
        keyframe_start=keyframe_start,
        keyframe_end=keyframe_end,
        start_delta=start_delta,
        end_delta=end_delta,
        alignment_available=alignment_available,
        fallback_reason=(
            f"drift {format_seconds(max(start_delta, end_delta))}s"
            if alignment_available and max(start_delta, end_delta) > auto_reencode_threshold
            else None
        ),
        segments=(RenderSegment("full", "copy", keyframe_start, keyframe_end),),
    )


def choose_smart_cut_plan(
    *,
    source: Path,
    requested_start: float,
    requested_end: float,
    keyframe_start: float,
    keyframe_end: float,
    alignment_available: bool,
    start_delta: float,
    end_delta: float,
    tools: ToolPaths,
    video_encoder: str,
) -> CutPlan:
    if not alignment_available:
        return build_precise_fallback_plan(
            requested_start=requested_start,
            requested_end=requested_end,
            keyframe_start=keyframe_start,
            keyframe_end=keyframe_end,
            start_delta=start_delta,
            end_delta=end_delta,
            alignment_available=alignment_available,
            tools=tools,
            video_encoder=video_encoder,
            reason="keyframe unavailable",
        )
    try:
        profile = probe_source_profile(source, tools)
    except ToolError as exc:
        return build_precise_fallback_plan(
            requested_start=requested_start,
            requested_end=requested_end,
            keyframe_start=keyframe_start,
            keyframe_end=keyframe_end,
            start_delta=start_delta,
            end_delta=end_delta,
            alignment_available=alignment_available,
            tools=tools,
            video_encoder=video_encoder,
            reason=str(exc),
        )
    compatible, reason = check_smart_render_compatibility(profile)
    if not compatible:
        return build_precise_fallback_plan(
            requested_start=requested_start,
            requested_end=requested_end,
            keyframe_start=keyframe_start,
            keyframe_end=keyframe_end,
            start_delta=start_delta,
            end_delta=end_delta,
            alignment_available=alignment_available,
            tools=tools,
            video_encoder=video_encoder,
            reason=reason,
        )
    middle_start, middle_start_found = find_nearest_keyframe(source, requested_start, tools, prefer="after")
    middle_end, middle_end_found = find_nearest_keyframe(source, requested_end, tools, prefer="before")
    if not middle_start_found or not middle_end_found:
        return build_precise_fallback_plan(
            requested_start=requested_start,
            requested_end=requested_end,
            keyframe_start=keyframe_start,
            keyframe_end=keyframe_end,
            start_delta=start_delta,
            end_delta=end_delta,
            alignment_available=alignment_available,
            tools=tools,
            video_encoder=video_encoder,
            reason="smart edges could not find inner keyframes",
        )
    segments: list[RenderSegment] = []
    if requested_start < middle_start:
        segments.append(RenderSegment("head", "precise", requested_start, middle_start))
    if middle_end - middle_start >= SMART_RENDER_MIN_COPY_SECONDS:
        segments.append(RenderSegment("middle", "copy", middle_start, middle_end))
    elif requested_start < middle_start or middle_end < requested_end:
        return build_precise_fallback_plan(
            requested_start=requested_start,
            requested_end=requested_end,
            keyframe_start=keyframe_start,
            keyframe_end=keyframe_end,
            start_delta=start_delta,
            end_delta=end_delta,
            alignment_available=alignment_available,
            tools=tools,
            video_encoder=video_encoder,
            reason="middle copy section is too short",
        )
    if middle_end < requested_end:
        segments.append(RenderSegment("tail", "precise", middle_end, requested_end))
    if not any(segment.mode == "copy" for segment in segments):
        return build_precise_fallback_plan(
            requested_start=requested_start,
            requested_end=requested_end,
            keyframe_start=keyframe_start,
            keyframe_end=keyframe_end,
            start_delta=start_delta,
            end_delta=end_delta,
            alignment_available=alignment_available,
            tools=tools,
            video_encoder=video_encoder,
            reason="selection stays inside one GOP",
        )
    return CutPlan(
        mode="smart",
        decision="smart",
        requested_start=requested_start,
        requested_end=requested_end,
        actual_start=requested_start,
        actual_end=requested_end,
        keyframe_start=keyframe_start,
        keyframe_end=keyframe_end,
        start_delta=start_delta,
        end_delta=end_delta,
        alignment_available=True,
        video_encoder=video_encoder,
        segments=tuple(segments),
    )


def build_precise_fallback_plan(
    *,
    requested_start: float,
    requested_end: float,
    keyframe_start: float,
    keyframe_end: float,
    start_delta: float,
    end_delta: float,
    alignment_available: bool,
    tools: ToolPaths,
    video_encoder: str,
    reason: str,
) -> CutPlan:
    return CutPlan(
        mode="precise",
        decision="fallback",
        requested_start=requested_start,
        requested_end=requested_end,
        actual_start=requested_start,
        actual_end=requested_end,
        keyframe_start=keyframe_start,
        keyframe_end=keyframe_end,
        start_delta=start_delta,
        end_delta=end_delta,
        alignment_available=alignment_available,
        video_encoder=video_encoder,
        segments=(RenderSegment("full", "precise", requested_start, requested_end),),
        fallback_reason=reason,
    )


def probe_source_profile(source: Path, tools: ToolPaths) -> SourceProfile:
    cmd = [
        tools.ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name",
        "-of",
        "json",
        str(source),
    ]
    result = run_command(cmd, capture=True)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Could not parse stream profile for {source.name}") from exc
    video_codec: str | None = None
    audio_codec: str | None = None
    audio_streams = 0
    subtitle_streams = 0
    for stream in payload.get("streams", []):
        codec_type = stream.get("codec_type")
        codec_name = stream.get("codec_name")
        if codec_type == "video" and video_codec is None:
            video_codec = codec_name
        elif codec_type == "audio":
            audio_streams += 1
            if audio_codec is None:
                audio_codec = codec_name
        elif codec_type == "subtitle":
            subtitle_streams += 1
    return SourceProfile(
        video_codec=video_codec,
        audio_codec=audio_codec,
        audio_streams=audio_streams,
        subtitle_streams=subtitle_streams,
    )


def check_smart_render_compatibility(profile: SourceProfile) -> tuple[bool, str]:
    if profile.video_codec != "h264":
        return False, f"smart render currently supports h264 video, got {profile.video_codec or 'unknown'}"
    if profile.audio_streams > 1:
        return False, "smart render currently supports only one audio stream"
    if profile.audio_codec not in (None, "aac"):
        return False, f"smart render currently supports AAC audio, got {profile.audio_codec}"
    if profile.subtitle_streams > 0:
        return False, "smart render currently does not support subtitle streams"
    return True, ""


def resolve_keyframe_range(
    source: Path,
    requested_start: float,
    requested_end: float,
    tools: ToolPaths,
    *,
    allow_missing_alignment: bool,
) -> tuple[float, float, bool]:
    if allow_missing_alignment and (not resolve_executable(tools.ffprobe) or not source.exists()):
        return requested_start, requested_end, False
    start_value, start_found = find_nearest_keyframe(source, requested_start, tools, prefer="before")
    end_value, end_found = find_nearest_keyframe(source, requested_end, tools, prefer="after")
    return (
        start_value,
        end_value,
        start_found and end_found,
    )


_VIDEO_ENCODER_CACHE: dict[str, str] = {}


def get_preferred_video_encoder(ffmpeg: str) -> str:
    resolved_ffmpeg = resolve_executable(ffmpeg) or ffmpeg
    cached = _VIDEO_ENCODER_CACHE.get(resolved_ffmpeg)
    if cached:
        return cached
    try:
        result = run_command([ffmpeg, "-hide_banner", "-encoders"], capture=True)
        available = parse_ffmpeg_encoder_names(result.stdout)
    except ToolError:
        available = set()
    encoder = pick_preferred_video_encoder(available)
    _VIDEO_ENCODER_CACHE[resolved_ffmpeg] = encoder
    return encoder


def resolve_video_encoder(tools: ToolPaths, video_encoder: str) -> str:
    if video_encoder == "auto":
        return get_preferred_video_encoder(tools.ffmpeg)
    if video_encoder in VIDEO_ENCODER_CHOICES:
        return video_encoder
    return get_preferred_video_encoder(tools.ffmpeg)


def parse_ffmpeg_encoder_names(output: str) -> set[str]:
    encoders: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and all(char.isupper() or char == "." for char in parts[0]):
            encoders.add(parts[1])
    return encoders


def pick_preferred_video_encoder(available: set[str]) -> str:
    for encoder in PREFERRED_VIDEO_ENCODERS:
        if encoder in available:
            return encoder
    return "libx264"


def format_cut_plan_summary(plan: CutPlan) -> str:
    requested = f"requested {format_timestamp(plan.requested_start)} -> {format_timestamp(plan.requested_end)}"
    if plan.alignment_available:
        aligned = f"keyframe {format_timestamp(plan.keyframe_start)} -> {format_timestamp(plan.keyframe_end)}"
    else:
        aligned = "keyframe unavailable"
    if plan.mode == "smart":
        segment_text = " | ".join(
            f"{segment.label}:{segment.mode} {format_timestamp(segment.start)}->{format_timestamp(segment.end)}"
            for segment in plan.segments
        )
        mode = f"smart ({plan.video_encoder})"
        reason = plan.fallback_reason or "edges reencoded, middle copied"
        actual = f"actual {format_timestamp(plan.actual_start)} -> {format_timestamp(plan.actual_end)}"
        return f"{requested}, {aligned}, {actual}, mode {mode}, {segment_text}, {reason}"
    mode = "copy" if plan.mode == "copy" else f"precise ({plan.video_encoder})"
    if plan.mode == "precise" and plan.decision == "forced":
        reason = "forced exact"
    elif plan.mode == "precise" and plan.decision == "fallback":
        reason = f"exact fallback, {plan.fallback_reason or 'smart render unavailable'}"
    elif plan.mode == "precise" and plan.decision == "auto":
        reason = f"auto exact, drift {format_seconds(max(plan.start_delta, plan.end_delta))}s"
    elif plan.mode == "precise":
        reason = f"auto exact, drift {format_seconds(max(plan.start_delta, plan.end_delta))}s"
    else:
        reason = f"drift {format_seconds(max(plan.start_delta, plan.end_delta))}s"
    actual = f"actual {format_timestamp(plan.actual_start)} -> {format_timestamp(plan.actual_end)}"
    return f"{requested}, {aligned}, {actual}, mode {mode}, {reason}"


def format_gui_cut_plan_mode(plan: CutPlan) -> str:
    if plan.mode == "smart":
        return f"头尾重编码 {plan.video_encoder}"
    if plan.mode == "copy":
        return "关键帧无损"
    return f"精确 {plan.video_encoder}"


def build_cut_command(
    ffmpeg: str,
    source: Path,
    output: Path,
    start: float,
    end: float,
    overwrite: bool,
    reencode: bool = False,
    video_encoder: str | None = None,
) -> list[str]:
    base = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    base.append("-y" if overwrite else "-n")
    if reencode:
        input_seek = max(0.0, start - FAST_PRECISE_SEEK_MARGIN_SECONDS)
        output_seek = max(0.0, start - input_seek)
        video_args = build_video_encoder_args(video_encoder or "libx264")
        cmd = [
            *base,
            "-ss",
            format_seconds(input_seek),
            "-i",
            str(source),
            "-ss",
            format_seconds(output_seek),
            "-t",
            format_seconds(max(0.0, end - start)),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            *video_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-c:s",
            "copy",
            str(output),
        ]
        if should_use_faststart(output):
            cmd[-1:-1] = ["-movflags", "+faststart"]
        return cmd
    return [
        *base,
        "-ss",
        format_seconds(start),
        "-i",
        str(source),
        "-t",
        format_seconds(max(0.0, end - start)),
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(output),
    ]


def build_video_encoder_args(video_encoder: str) -> list[str]:
    if video_encoder == "libx264":
        return [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
        ]
    return [
        "-c:v",
        video_encoder,
        "-pix_fmt",
        "yuv420p",
    ]


def should_use_faststart(output: Path) -> bool:
    return output.suffix.lower() in {".mp4", ".m4v", ".mov"}


def build_smart_copy_command(ffmpeg: str, source: Path, output: Path, start: float, end: float, overwrite: bool) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-ss",
        format_seconds(start),
        "-i",
        str(source),
        "-t",
        format_seconds(max(0.0, end - start)),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(output),
    ]


def build_concat_command(ffmpeg: str, list_file: Path, output: Path, overwrite: bool) -> list[str]:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(output),
    ]
    if should_use_faststart(output):
        cmd[-1:-1] = ["-movflags", "+faststart"]
    return cmd


def build_smart_render_commands(
    plan: CutPlan,
    ffmpeg: str,
    source: Path,
    output: Path,
    overwrite: bool,
) -> tuple[list[list[str]], list[Path]]:
    temp_root = Path(tempfile.mkdtemp(prefix=f"{source.stem}_smart_", dir=str(output.parent)))
    commands: list[list[str]] = []
    segment_paths: list[Path] = []
    for index, segment in enumerate(plan.segments, start=1):
        segment_path = temp_root / f"{index:02d}_{segment.label}.mkv"
        if segment.mode == "copy":
            command = build_smart_copy_command(ffmpeg, source, segment_path, segment.start, segment.end, overwrite=True)
        else:
            command = build_cut_command(
                ffmpeg,
                source,
                segment_path,
                segment.start,
                segment.end,
                overwrite=True,
                reencode=True,
                video_encoder=plan.video_encoder,
            )
        commands.append(command)
        segment_paths.append(segment_path)
    list_file = temp_root / "concat.txt"
    list_file.write_text("".join(f"file {format_concat_path(path)}\n" for path in segment_paths), encoding="utf-8", newline="\n")
    commands.append(build_concat_command(ffmpeg, list_file, output, overwrite))
    return commands, [temp_root]


def execute_cut_plan(
    plan: CutPlan,
    tools: ToolPaths,
    source: Path,
    output: Path,
    overwrite: bool,
    dry_run: bool,
) -> list[list[str]]:
    active_plan = plan
    active_overwrite = overwrite
    attempted_encoder_fallback = False
    attempted_duration_fallback = False
    log_info("execute start source=%s output=%s overwrite=%s dry_run=%s plan=%s", source, output, overwrite, dry_run, format_cut_plan_summary(plan))
    while True:
        try:
            commands = _execute_cut_plan_once(active_plan, tools, source, output, active_overwrite, dry_run)
            if not dry_run:
                verify_output_duration(active_plan, tools, output)
            log_info("execute done source=%s output=%s plan_mode=%s", source, output, active_plan.mode)
            return commands
        except OutputDurationMismatch as exc:
            log_error("duration mismatch source=%s output=%s error=%s", source, output, exc)
            if dry_run or active_plan.mode == "precise" or attempted_duration_fallback:
                raise
            active_plan = build_precise_fallback_plan(
                requested_start=plan.requested_start,
                requested_end=plan.requested_end,
                keyframe_start=plan.keyframe_start,
                keyframe_end=plan.keyframe_end,
                start_delta=plan.start_delta,
                end_delta=plan.end_delta,
                alignment_available=plan.alignment_available,
                tools=tools,
                video_encoder=active_plan.video_encoder or get_preferred_video_encoder(tools.ffmpeg),
                reason="output duration mismatch",
            )
            log_info("retry precise fallback source=%s reason=output duration mismatch plan=%s", source, format_cut_plan_summary(active_plan))
            active_overwrite = True
            attempted_duration_fallback = True
        except ToolError as exc:
            log_error("execute failed source=%s output=%s plan_mode=%s error=%s", source, output, active_plan.mode, exc)
            fallback_enabled = active_plan.video_encoder not in (None, "libx264") and active_plan.mode in {"precise", "smart"}
            if dry_run or not fallback_enabled or attempted_encoder_fallback:
                raise
            active_plan = replace_cut_plan_video_encoder(active_plan, "libx264")
            remove_zero_byte_file(output)
            log_info("retry encoder fallback source=%s encoder=libx264 plan=%s", source, format_cut_plan_summary(active_plan))
            active_overwrite = True
            attempted_encoder_fallback = True


def _execute_cut_plan_once(
    plan: CutPlan,
    tools: ToolPaths,
    source: Path,
    output: Path,
    overwrite: bool,
    dry_run: bool,
) -> list[list[str]]:
    cleanup_paths: list[Path] = []
    if plan.mode == "smart":
        commands, cleanup_paths = build_smart_render_commands(plan, tools.ffmpeg, source, output, overwrite)
    else:
        commands = [
            build_cut_command(
                tools.ffmpeg,
                source,
                output,
                plan.actual_start,
                plan.actual_end,
                overwrite,
                reencode=plan.mode == "precise",
                video_encoder=plan.video_encoder,
            )
        ]
    try:
        if not dry_run:
            for command in commands:
                log_info("run ffmpeg command=%s", format_command_for_log(command))
                try:
                    run_command(command, capture=False)
                except ToolError:
                    remove_zero_byte_file(output)
                    raise
        return commands
    finally:
        for cleanup_path in cleanup_paths:
            shutil.rmtree(cleanup_path, ignore_errors=True)


def verify_output_duration(plan: CutPlan, tools: ToolPaths, output: Path) -> None:
    if plan.mode == "copy":
        expected_duration = max(0.0, plan.actual_end - plan.actual_start)
    else:
        expected_duration = max(0.0, plan.requested_end - plan.requested_start)
    actual_duration = probe_duration(output, tools)
    drift = abs(actual_duration - expected_duration)
    log_info(
        "verify duration output=%s expected=%s actual=%s drift=%s tolerance=%s",
        output,
        format_timestamp(expected_duration),
        format_timestamp(actual_duration),
        format_seconds(drift),
        format_seconds(OUTPUT_DURATION_TOLERANCE_SECONDS),
    )
    if drift > OUTPUT_DURATION_TOLERANCE_SECONDS:
        remove_zero_byte_file(output)
        raise OutputDurationMismatch(
            f"Output duration drift {format_seconds(drift)}s exceeds {format_seconds(OUTPUT_DURATION_TOLERANCE_SECONDS)}s: "
            f"expected {format_timestamp(expected_duration)}, got {format_timestamp(actual_duration)}"
        )


def remove_zero_byte_file(path: Path) -> None:
    try:
        if path.is_file() and path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass


def replace_cut_plan_video_encoder(plan: CutPlan, video_encoder: str) -> CutPlan:
    return CutPlan(
        mode=plan.mode,
        decision=plan.decision,
        requested_start=plan.requested_start,
        requested_end=plan.requested_end,
        actual_start=plan.actual_start,
        actual_end=plan.actual_end,
        keyframe_start=plan.keyframe_start,
        keyframe_end=plan.keyframe_end,
        start_delta=plan.start_delta,
        end_delta=plan.end_delta,
        alignment_available=plan.alignment_available,
        video_encoder=video_encoder,
        segments=plan.segments,
        fallback_reason=plan.fallback_reason,
    )


def format_concat_path(path: Path) -> str:
    return "'" + str(path).replace("'", r"'\''") + "'"


def output_path_for_source(output_dir: Path | None, source: Path, overwrite: bool) -> Path:
    return timestamped_output_path_for_source(output_dir, source, overwrite, time.strftime("%Y%m%d_%H%M%S"))


def timestamped_output_path_for_source(
    output_dir: Path | None,
    source: Path,
    overwrite: bool,
    timestamp: str,
) -> Path:
    target_dir = output_dir if output_dir else source.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = target_dir / f"{source.stem}_{timestamp}{source.suffix}"
    if overwrite or not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = target_dir / f"{source.stem}_{timestamp}_{index}{source.suffix}"
        if not candidate.exists():
            return candidate
    raise ToolError(f"Could not create unique output path for {source.name}.")


def serve_review(args: argparse.Namespace) -> None:
    root = Path(args.directory).expanduser().resolve()
    if not root.exists():
        raise ToolError(f"Directory does not exist: {root}")

    class ReviewHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            request_path = urllib.parse.unquote(parsed.path.lstrip("/")) or "review.html"
            target = (root / request_path).resolve()
            if not is_within(target, root) or not target.exists() or target.is_dir():
                self.send_error(404)
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802
            if urllib.parse.urlparse(self.path).path != "/api/selections":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(length)
            try:
                payload = json.loads(data.decode("utf-8"))
                validate_selections_payload(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self.send_error(400, str(exc))
                return
            write_json(root / "selections.json", payload)
            response = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, fmt: str, *values: Any) -> None:
            print(fmt % values)

    server = ThreadingHTTPServer((args.host, args.port), ReviewHandler)
    url = f"http://{args.host}:{args.port}/review.html"
    print(f"serving {root}")
    print(f"open {url}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def validate_selections_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if not isinstance(payload.get("items"), list):
        raise ValueError("payload.items must be a list")


def write_review_html(path: Path, manifest: dict[str, Any]) -> None:
    manifest_json = json.dumps(manifest, ensure_ascii=False).replace("<", "\\u003c")
    content = REVIEW_HTML_TEMPLATE.replace("__MANIFEST_JSON__", manifest_json)
    path.write_text(content, encoding="utf-8", newline="\n")


def launch_gui(args: argparse.Namespace, tools: ToolPaths) -> None:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        raise ToolError("tkinter is not available in this Python installation.") from exc
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
    except ImportError:
        DND_FILES = None
        TkinterDnD = None

    class TrimmerGui:
        def __init__(self) -> None:
            self.root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
            self.root.title("视频广告裁剪工具")
            self.root.geometry("1180x760")
            self.dnd_enabled = TkinterDnD is not None and DND_FILES is not None
            self.tools = tools
            self.videos: list[Path] = []
            self.output_root = Path(args.output_root).expanduser().resolve()
            self.session_dir: Path | None = None
            self.manifest: dict[str, Any] | None = None
            self.items_by_id: dict[str, dict[str, Any]] = {}
            self.selections: dict[str, dict[str, Any]] = {}
            self.current_item_id: str | None = None
            self.image_refs: list[Any] = []
            self.busy = False
            self.front_scan_seconds = DEFAULT_SCAN_SECONDS
            self.back_scan_seconds = DEFAULT_SCAN_SECONDS
            self.scene_threshold = DEFAULT_SCENE_THRESHOLD
            self.min_segment_seconds = DEFAULT_MIN_SEGMENT_SECONDS
            self.max_segments_per_side = DEFAULT_MAX_SEGMENTS_PER_SIDE
            self.cut_output_dir: Path | None = None
            self.auto_reencode_threshold = DEFAULT_AUTO_REENCODE_THRESHOLD
            self.cut_mode_var = tk.StringVar(value="copy")
            self.video_encoder_var = tk.StringVar(value="auto")
            self._build()
            if self.dnd_enabled:
                self._set_status("请选择视频/目录，或直接拖拽视频/目录到窗口。")
            else:
                self._set_status("请选择视频或目录。")

        def run(self) -> None:
            self.root.mainloop()

        def _build(self) -> None:
            self.root.columnconfigure(0, weight=0)
            self.root.columnconfigure(1, weight=1)
            self.root.rowconfigure(1, weight=1)

            top = ttk.Frame(self.root, padding=10)
            top.grid(row=0, column=0, columnspan=2, sticky="ew")
            top.columnconfigure(10, weight=1)
            top.columnconfigure(1, weight=1)

            ttk.Button(top, text="选择视频", command=self._choose_files).grid(row=0, column=0, padx=(0, 6))
            ttk.Button(top, text="选择目录", command=self._choose_dir).grid(row=0, column=1, padx=(0, 6))
            ttk.Button(top, text="清空", command=self._clear_videos).grid(row=0, column=2, padx=(0, 16))
            ttk.Button(top, text="开始分析", command=self._start_analyze).grid(row=0, column=3, padx=(0, 6))
            ttk.Button(top, text="生成视频", command=self._start_cut).grid(row=0, column=4, padx=(0, 16))
            ttk.Button(top, text="选择 FFmpeg", command=self._choose_ffmpeg).grid(row=0, column=5, padx=(0, 6))
            ttk.Label(top, text="片头范围(分钟)").grid(row=0, column=6, padx=(10, 4))
            self.front_scan_minutes_var = tk.StringVar(value="5")
            ttk.Entry(top, textvariable=self.front_scan_minutes_var, width=6).grid(row=0, column=7, padx=(0, 8))
            ttk.Label(top, text="片尾范围(分钟)").grid(row=0, column=8, padx=(0, 4))
            self.back_scan_minutes_var = tk.StringVar(value="5")
            ttk.Entry(top, textvariable=self.back_scan_minutes_var, width=6).grid(row=0, column=9, padx=(0, 12))
            self.status_var = tk.StringVar()
            ttk.Label(top, textvariable=self.status_var).grid(row=0, column=10, sticky="ew")
            ttk.Label(top, text="输出目录").grid(row=1, column=0, sticky="w", pady=(8, 0))
            self.output_dir_var = tk.StringVar(value="")
            ttk.Entry(top, textvariable=self.output_dir_var).grid(row=1, column=1, columnspan=8, sticky="ew", pady=(8, 0), padx=(6, 6))
            ttk.Button(top, text="选择输出目录", command=self._choose_output_dir).grid(row=1, column=9, sticky="w", pady=(8, 0))
            ttk.Label(top, text="留空则保存到原视频目录").grid(row=1, column=10, sticky="w", pady=(8, 0))
            ttk.Label(top, text="最短片段(秒)").grid(row=2, column=0, sticky="w", pady=(8, 0))
            self.min_segment_seconds_var = tk.StringVar(value=format_seconds(DEFAULT_MIN_SEGMENT_SECONDS))
            ttk.Entry(top, textvariable=self.min_segment_seconds_var, width=6).grid(row=2, column=1, sticky="w", pady=(8, 0))
            ttk.Label(top, text="每侧最大候选").grid(row=2, column=2, sticky="w", padx=(10, 4), pady=(8, 0))
            self.max_segments_per_side_var = tk.StringVar(value=str(DEFAULT_MAX_SEGMENTS_PER_SIDE))
            ttk.Entry(top, textvariable=self.max_segments_per_side_var, width=6).grid(row=2, column=3, sticky="w", pady=(8, 0))
            ttk.Label(top, text="切点灵敏度").grid(row=2, column=4, sticky="w", padx=(10, 4), pady=(8, 0))
            self.scene_threshold_var = tk.StringVar(value=format_seconds(DEFAULT_SCENE_THRESHOLD))
            ttk.Entry(top, textvariable=self.scene_threshold_var, width=6).grid(row=2, column=5, sticky="w", pady=(8, 0))
            ttk.Label(
                top,
                text="灵敏度越低切得越细；最短片段越小切得越细；候选数越大保留越多",
            ).grid(row=2, column=6, columnspan=5, sticky="w", padx=(10, 0), pady=(8, 0))
            if self.dnd_enabled:
                self.drop_label = ttk.Label(
                    self.root,
                    text="拖拽视频文件或文件夹到这里",
                    anchor="center",
                    relief="groove",
                    padding=10,
                )
                self.drop_label.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))

            left = ttk.Frame(self.root, padding=(10, 0, 6, 10))
            left.grid(row=1, column=0, sticky="ns")
            ttk.Label(left, text="视频列表").pack(anchor="w")
            self.video_list = tk.Listbox(left, width=34, height=28)
            self.video_list.pack(fill="y", expand=True)
            self.video_list.bind("<<ListboxSelect>>", self._on_video_select)

            right = ttk.Frame(self.root, padding=(6, 0, 10, 10))
            right.grid(row=1, column=1, sticky="nsew")
            right.columnconfigure(0, weight=1)
            right.rowconfigure(1, weight=1)

            self.detail_var = tk.StringVar(value="分析完成后在这里选择片段。")
            ttk.Label(right, textvariable=self.detail_var).grid(row=0, column=0, sticky="ew", pady=(0, 6))

            self.canvas = tk.Canvas(right, highlightthickness=0)
            self.scrollbar = ttk.Scrollbar(right, orient="vertical", command=self.canvas.yview)
            self.content = ttk.Frame(self.canvas)
            self.content.bind(
                "<Configure>",
                lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
            )
            self.canvas_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
            self.canvas.configure(yscrollcommand=self.scrollbar.set)
            self.canvas.grid(row=1, column=0, sticky="nsew")
            self.scrollbar.grid(row=1, column=1, sticky="ns")
            self.canvas.bind("<Configure>", self._resize_canvas_window)
            self.root.bind_all("<MouseWheel>", self._on_canvas_mousewheel, add="+")
            self.root.bind_all("<Button-4>", self._on_canvas_mousewheel, add="+")
            self.root.bind_all("<Button-5>", self._on_canvas_mousewheel, add="+")
            self._setup_drag_drop()

            bottom = ttk.Frame(right)
            bottom.grid(row=2, column=0, sticky="ew", pady=(8, 0))
            ttk.Label(bottom, text="片头 offset 秒").pack(side="left")
            self.front_offset_var = tk.StringVar(value="0")
            ttk.Entry(bottom, textvariable=self.front_offset_var, width=8).pack(side="left", padx=(4, 12))
            ttk.Label(bottom, text="片尾 offset 秒").pack(side="left")
            self.back_offset_var = tk.StringVar(value="0")
            ttk.Entry(bottom, textvariable=self.back_offset_var, width=8).pack(side="left", padx=(4, 12))
            ttk.Button(bottom, text="保存当前选择", command=self._save_current_selection).pack(side="left")
            ttk.Button(bottom, text="应用当前选择到全部视频", command=self._apply_current_selection_to_all).pack(side="left", padx=(8, 0))
            ttk.Label(bottom, text="生成模式").pack(side="left", padx=(12, 4))
            ttk.Radiobutton(
                bottom,
                text="推荐: 切点精确/中间复制",
                variable=self.cut_mode_var,
                value="smart",
            ).pack(side="left")
            ttk.Radiobutton(
                bottom,
                text="快速: 关键帧复制(最快)",
                variable=self.cut_mode_var,
                value="copy",
            ).pack(side="left", padx=(8, 0))
            ttk.Radiobutton(
                bottom,
                text="始终精确生成(慢)",
                variable=self.cut_mode_var,
                value="precise",
            ).pack(side="left", padx=(8, 0))
            ttk.Label(bottom, text="编码器").pack(side="left", padx=(12, 4))
            ttk.Combobox(
                bottom,
                textvariable=self.video_encoder_var,
                values=VIDEO_ENCODER_CHOICES,
                width=12,
                state="readonly",
            ).pack(side="left")
            ttk.Label(
                bottom,
                text="默认快速；需要更准切点时再选推荐或始终精确，auto 会优先使用显卡编码",
            ).pack(side="left", padx=(12, 0))

        def _resize_canvas_window(self, event: Any) -> None:
            self.canvas.itemconfigure(self.canvas_window, width=event.width)

        def _on_canvas_mousewheel(self, event: Any) -> str | None:
            left = self.canvas.winfo_rootx()
            top = self.canvas.winfo_rooty()
            right = left + self.canvas.winfo_width()
            bottom = top + self.canvas.winfo_height()
            if not (left <= event.x_root < right and top <= event.y_root < bottom):
                return None
            if getattr(event, "num", None) == 4:
                units = -3
            elif getattr(event, "num", None) == 5:
                units = 3
            else:
                delta = getattr(event, "delta", 0)
                if delta == 0:
                    return "break"
                units = -max(1, abs(delta) // 120) if delta > 0 else max(1, abs(delta) // 120)
            self.canvas.yview_scroll(units, "units")
            return "break"

        def _setup_drag_drop(self) -> None:
            if not self.dnd_enabled:
                return
            for widget in (self.root, self.video_list, self.canvas, self.content, self.drop_label):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)

        def _on_drop(self, event: Any) -> None:
            paths = [Path(path) for path in self.root.tk.splitlist(event.data)]
            videos: list[Path] = []
            for path in paths:
                if path.is_dir():
                    videos.extend(find_videos(path, recursive=True))
                else:
                    videos.append(path)
            self._add_videos(videos)

        def _choose_files(self) -> None:
            paths = filedialog.askopenfilenames(
                title="选择视频",
                filetypes=[("Video files", " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS))), ("All files", "*.*")],
            )
            self._add_videos([Path(path) for path in paths])

        def _choose_dir(self) -> None:
            directory = filedialog.askdirectory(title="选择视频目录")
            if not directory:
                return
            self._add_videos(find_videos(Path(directory), recursive=True))

        def _choose_output_dir(self) -> None:
            directory = filedialog.askdirectory(title="选择输出目录")
            if directory:
                self.output_dir_var.set(directory)

        def _choose_ffmpeg(self) -> None:
            ffmpeg_path = filedialog.askopenfilename(title="选择 ffmpeg.exe", filetypes=[("ffmpeg", "ffmpeg.exe"), ("exe", "*.exe")])
            if not ffmpeg_path:
                return
            ffprobe_path = filedialog.askopenfilename(title="选择 ffprobe.exe", filetypes=[("ffprobe", "ffprobe.exe"), ("exe", "*.exe")])
            if not ffprobe_path:
                return
            self.tools = ToolPaths(ffmpeg_path, ffprobe_path)
            self._set_status("已设置 FFmpeg 路径。")

        def _add_videos(self, paths: list[Path]) -> None:
            existing = {path.resolve() for path in self.videos}
            added = []
            for path in paths:
                resolved = path.expanduser().resolve()
                if resolved.suffix.lower() not in VIDEO_EXTENSIONS or resolved in existing:
                    continue
                self.videos.append(resolved)
                existing.add(resolved)
                added.append(resolved)
            self._refresh_video_list()
            if added:
                self._set_status(f"已添加 {len(added)} 个视频。")

        def _clear_videos(self) -> None:
            self.videos.clear()
            self.manifest = None
            self.items_by_id.clear()
            self.selections.clear()
            self.current_item_id = None
            self._refresh_video_list()
            self._clear_content()
            self.detail_var.set("分析完成后在这里选择片段。")
            self._set_status("已清空。")

        def _refresh_video_list(self) -> None:
            self.video_list.delete(0, tk.END)
            for video in self.videos:
                self.video_list.insert(tk.END, video.name)

        def _start_analyze(self) -> None:
            if self.busy:
                return
            if not self.videos:
                messagebox.showwarning("提示", "请先选择视频或目录。")
                return
            missing = self._missing_tools()
            if missing:
                messagebox.showerror("缺少工具", "找不到：" + "、".join(missing) + "\n请点击“选择 FFmpeg”手动指定。")
                return
            front_minutes = parse_float(self.front_scan_minutes_var.get(), 5.0)
            back_minutes = parse_float(self.back_scan_minutes_var.get(), 5.0)
            scene_threshold = parse_float(self.scene_threshold_var.get(), DEFAULT_SCENE_THRESHOLD)
            min_segment_seconds = parse_float(self.min_segment_seconds_var.get(), DEFAULT_MIN_SEGMENT_SECONDS)
            max_segments_per_side = parse_int(self.max_segments_per_side_var.get(), DEFAULT_MAX_SEGMENTS_PER_SIDE)
            if front_minutes <= 0 or back_minutes <= 0:
                messagebox.showwarning("提示", "片头/片尾范围必须大于 0 分钟。")
                return
            if scene_threshold <= 0:
                messagebox.showwarning("提示", "切点灵敏度必须大于 0。")
                return
            if min_segment_seconds < MIN_SEGMENT_SECONDS:
                messagebox.showwarning("提示", f"最短片段秒数必须至少为 {format_seconds(MIN_SEGMENT_SECONDS)}。")
                return
            if max_segments_per_side <= 0:
                messagebox.showwarning("提示", "每侧最大候选数必须大于 0。")
                return
            self.front_scan_seconds = front_minutes * 60
            self.back_scan_seconds = back_minutes * 60
            self.scene_threshold = scene_threshold
            self.min_segment_seconds = min_segment_seconds
            self.max_segments_per_side = max_segments_per_side
            self.session_dir = self.output_root / f"gui_session_{time.strftime('%Y%m%d_%H%M%S')}"
            self.session_dir.mkdir(parents=True, exist_ok=True)
            log_path = configure_logging(self.session_dir / LOG_FILE_NAME)
            log_info(
                "gui analyze start session=%s videos=%s front_scan=%s back_scan=%s scene_threshold=%s min_segment=%s max_segments=%s",
                self.session_dir,
                [str(video) for video in self.videos],
                self.front_scan_seconds,
                self.back_scan_seconds,
                self.scene_threshold,
                self.min_segment_seconds,
                self.max_segments_per_side,
            )
            self.busy = True
            self._set_status(f"正在分析，请稍候... 日志: {log_path}")
            self._run_background(self._analyze_worker)

        def _analyze_worker(self) -> None:
            assert self.session_dir is not None
            manifest = analyze_video_list(
                videos=self.videos,
                output_dir=self.session_dir,
                tools=self.tools,
                scan_seconds=self.front_scan_seconds,
                back_scan_seconds=self.back_scan_seconds,
                scene_threshold=self.scene_threshold,
                min_segment_seconds=self.min_segment_seconds,
                merge_gap_seconds=DEFAULT_MERGE_GAP_SECONDS,
                max_segments_per_side=self.max_segments_per_side,
                progress=lambda ordinal, total, video: self._ui(lambda: self._set_status(f"正在分析 {ordinal}/{total}: {video.name}")),
            )
            write_analysis_files(self.session_dir, manifest)
            log_info("gui analyze done session=%s items=%s", self.session_dir, len(manifest.get("items", [])))
            self._ui(lambda: self._load_manifest(manifest))

        def _load_manifest(self, manifest: dict[str, Any]) -> None:
            self.manifest = manifest
            self.items_by_id = {item["id"]: item for item in manifest.get("items", [])}
            self.selections = {
                item["id"]: {
                    "id": item["id"],
                    "frontIndex": None,
                    "backIndex": None,
                    "frontTimeSeconds": None,
                    "backTimeSeconds": None,
                    "frontOffsetSeconds": 0,
                    "backOffsetSeconds": 0,
                }
                for item in manifest.get("items", [])
            }
            self.current_item_id = manifest["items"][0]["id"] if manifest.get("items") else None
            if self.current_item_id:
                self.video_list.selection_clear(0, tk.END)
                self.video_list.selection_set(0)
                self._render_current_item()
            self.busy = False
            self._set_status("分析完成，请选择片段。")

        def _on_video_select(self, _event: Any = None) -> None:
            if not self.manifest:
                return
            selection = self.video_list.curselection()
            if not selection:
                return
            index = int(selection[0])
            items = self.manifest.get("items", [])
            if index >= len(items):
                return
            self._save_current_selection(silent=True)
            self.current_item_id = items[index]["id"]
            self._render_current_item()

        def _render_current_item(self) -> None:
            self._clear_content()
            if not self.current_item_id or not self.session_dir:
                return
            item = self.items_by_id[self.current_item_id]
            selection = self.selections[self.current_item_id]
            self.front_offset_var.set(str(selection.get("frontOffsetSeconds", 0)))
            self.back_offset_var.set(str(selection.get("backOffsetSeconds", 0)))
            start, end, unchanged = compute_requested_range(item, selection)
            if unchanged:
                range_text = "未设置裁剪"
            elif selection.get("frontTimeSeconds") is not None or selection.get("backTimeSeconds") is not None:
                range_text = f"批量时间范围 {format_timestamp(start)} - {format_timestamp(end)} | 时长 {format_timestamp(end - start)}"
            else:
                range_text = f"选择范围 {format_timestamp(start)} - {format_timestamp(end)} | 时长 {format_timestamp(end - start)}"
            self.detail_var.set(f"{item['name']} | {format_timestamp(float(item['duration']))} | {range_text}")

            self._render_side(item, selection, "front", "片头：选择真正正片开始的片段", 0)
            self._render_side(item, selection, "back", "片尾：选择真正正片结束的片段", 1)

        def _render_side(self, item: dict[str, Any], selection: dict[str, Any], side: str, title: str, column: int) -> None:
            frame = ttk.LabelFrame(self.content, text=title, padding=8)
            frame.grid(row=0, column=column, sticky="nsew", padx=6, pady=6)
            self.content.columnconfigure(column, weight=1)
            no_text = "无片头广告" if side == "front" else "无片尾广告"
            ttk.Button(frame, text=no_text, command=lambda: self._select_segment(side, None)).grid(row=0, column=0, sticky="ew", pady=(0, 8))
            for row, segment in enumerate(item.get(side, []), start=1):
                selected = selection.get(f"{side}Index") == segment["index"]
                text = f"#{segment['index']} {format_timestamp(float(segment['start']))} - {format_timestamp(float(segment['end']))}"
                select_command = lambda s=side, idx=segment["index"]: self._select_segment(s, idx)
                button = ttk.Button(frame, text=text, command=select_command)
                button.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=4)
                if selected:
                    ttk.Label(frame, text="已选").grid(row=row, column=2, padx=(6, 0))
                image = self._load_thumbnail(segment["thumbnail"])
                if image:
                    label = ttk.Label(frame, image=image)
                    label.image = image
                    label.bind("<Button-1>", lambda _event, command=select_command: command())
                    label.configure(cursor="hand2")
                    label.grid(row=row, column=0, sticky="w", pady=4)
                    self.image_refs.append(image)
            frame.columnconfigure(1, weight=1)

        def _load_thumbnail(self, relative_path: str) -> Any:
            if not self.session_dir:
                return None
            path = self.session_dir / relative_path
            if not path.exists():
                return None
            try:
                image = tk.PhotoImage(file=str(path))
            except tk.TclError:
                return None
            max_width = 220
            if image.width() > max_width:
                factor = max(1, image.width() // max_width)
                image = image.subsample(factor, factor)
            return image

        def _select_segment(self, side: str, index: int | None) -> None:
            if not self.current_item_id:
                return
            self.selections[self.current_item_id][f"{side}Index"] = index
            self.selections[self.current_item_id][f"{side}TimeSeconds"] = None
            self._render_current_item()

        def _save_current_selection(self, silent: bool = False) -> None:
            if not self.current_item_id:
                return
            selection = self.selections[self.current_item_id]
            selection["frontOffsetSeconds"] = parse_float(self.front_offset_var.get(), 0.0)
            selection["backOffsetSeconds"] = parse_float(self.back_offset_var.get(), 0.0)
            if self.session_dir:
                write_json(self.session_dir / "selections.json", {"version": 1, "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "items": list(self.selections.values())})
            if not silent:
                self._set_status("已保存当前选择。")

        def _apply_current_selection_to_all(self) -> None:
            if not self.manifest or not self.current_item_id:
                return
            self._save_current_selection(silent=True)
            current_item = self.items_by_id[self.current_item_id]
            current_selection = self.selections[self.current_item_id]
            start, end, unchanged = compute_requested_range(current_item, current_selection)
            has_front = current_selection.get("frontIndex") is not None or current_selection.get("frontTimeSeconds") is not None
            has_back = current_selection.get("backIndex") is not None or current_selection.get("backTimeSeconds") is not None
            if unchanged:
                for selection in self.selections.values():
                    selection["frontIndex"] = None
                    selection["backIndex"] = None
                    selection["frontTimeSeconds"] = None
                    selection["backTimeSeconds"] = None
                    selection["frontOffsetSeconds"] = 0
                    selection["backOffsetSeconds"] = 0
            else:
                for item in self.manifest.get("items", []):
                    selection = self.selections[item["id"]]
                    if has_front:
                        selection["frontIndex"] = None
                        selection["frontTimeSeconds"] = start
                        selection["frontOffsetSeconds"] = 0
                    if has_back:
                        selection["backIndex"] = None
                        selection["backTimeSeconds"] = min(end, float(item["duration"]))
                        selection["backOffsetSeconds"] = 0
            self._save_all_selections()
            self._render_current_item()
            self._set_status("已将当前选择应用到全部视频。")

        def _save_all_selections(self) -> None:
            if self.session_dir:
                write_json(
                    self.session_dir / "selections.json",
                    {"version": 1, "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "items": list(self.selections.values())},
                )

        def _start_cut(self) -> None:
            if self.busy:
                return
            if not self.manifest or not self.session_dir:
                messagebox.showwarning("提示", "请先分析视频。")
                return
            missing = self._missing_tools()
            if missing:
                messagebox.showerror("缺少工具", "找不到：" + "、".join(missing))
                return
            self._save_current_selection(silent=True)
            output_dir_text = self.output_dir_var.get().strip()
            self.cut_output_dir = Path(output_dir_text).expanduser().resolve() if output_dir_text else None
            if self.cut_output_dir:
                self.cut_output_dir.mkdir(parents=True, exist_ok=True)
            self.busy = True
            self._set_status("正在生成视频...")
            self._run_background(self._cut_worker)

        def _cut_worker(self) -> None:
            assert self.manifest is not None
            results: list[str] = []
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            log_info("gui cut start session=%s mode=%s output_dir=%s", self.session_dir, self.cut_mode_var.get(), self.cut_output_dir)
            for item in self.manifest.get("items", []):
                selection = self.selections[item["id"]]
                source = Path(item["source"])
                requested_start, requested_end, unchanged = compute_requested_range(item, selection)
                log_info(
                    "gui selection source=%s start=%s end=%s duration=%s unchanged=%s selection=%s",
                    source,
                    format_timestamp(requested_start),
                    format_timestamp(requested_end),
                    format_timestamp(max(0.0, requested_end - requested_start)),
                    unchanged,
                    json.dumps(selection, ensure_ascii=False, sort_keys=True),
                )
                if unchanged:
                    results.append(f"跳过 {source.name}: 无片头/片尾广告")
                    log_info("gui skip source=%s reason=no ads selected", source)
                    continue
                cut_mode = self.cut_mode_var.get()
                auto_reencode_threshold = float("inf") if cut_mode == "copy" else self.auto_reencode_threshold
                plan = choose_cut_plan(
                    source=source,
                    requested_start=requested_start,
                    requested_end=requested_end,
                    tools=self.tools,
                    force_precise=cut_mode == "precise",
                    prefer_smart_edges=cut_mode == "smart",
                    auto_reencode_threshold=auto_reencode_threshold,
                    video_encoder=self.video_encoder_var.get(),
                )
                if plan.actual_end <= plan.actual_start:
                    results.append(f"跳过 {source.name}: 裁剪范围无效")
                    continue
                output = timestamped_output_path_for_source(self.cut_output_dir, source, overwrite=False, timestamp=timestamp)
                self._ui(
                    lambda name=source.name, mode=format_gui_cut_plan_mode(plan): self._set_status(f"正在生成: {name} ({mode})")
                )
                execute_cut_plan(plan, self.tools, source, output, overwrite=False, dry_run=False)
                log_info("gui cut wrote source=%s output=%s size=%s", source, output, output.stat().st_size)
                results.append(
                    f"{output} ({format_file_size(output.stat().st_size)}) | 模式:{format_gui_cut_plan_mode(plan)} | "
                    f"最终时长:{format_timestamp(plan.actual_end - plan.actual_start)} | "
                    f"选择:{format_timestamp(plan.requested_start)}-{format_timestamp(plan.requested_end)} | "
                    f"输出:{format_timestamp(plan.actual_start)}-{format_timestamp(plan.actual_end)}"
                )
            self._ui(lambda: self._show_cut_results(results))

        def _show_cut_results(self, results: list[str]) -> None:
            text = "\n".join(results) if results else "没有生成新视频。"
            if self.session_dir:
                text += f"\n\n日志文件: {self.session_dir / LOG_FILE_NAME}"
            self.busy = False
            self._set_status("处理完成。")
            messagebox.showinfo("处理完成", text)

        def _clear_content(self) -> None:
            for child in self.content.winfo_children():
                child.destroy()
            self.image_refs.clear()

        def _missing_tools(self) -> list[str]:
            self.tools = discover_tool_paths(self.tools)
            missing = []
            if not resolve_executable(self.tools.ffmpeg):
                missing.append("ffmpeg")
            if not resolve_executable(self.tools.ffprobe):
                missing.append("ffprobe")
            return missing

        def _run_background(self, target: Callable[[], None]) -> None:
            import threading

            def runner() -> None:
                try:
                    target()
                except Exception as exc:  # GUI boundary: surface all worker failures.
                    if LOGGER.handlers:
                        LOGGER.exception("gui worker failed")
                    log_path_text = f"\n\n日志文件: {self.session_dir / LOG_FILE_NAME}" if self.session_dir else ""
                    self._ui(lambda: messagebox.showerror("错误", str(exc) + log_path_text))
                    self._ui(self._mark_error)

            threading.Thread(target=runner, daemon=True).start()

        def _ui(self, callback: Callable[[], None]) -> None:
            self.root.after(0, callback)

        def _set_status(self, text: str) -> None:
            self.status_var.set(text)

        def _mark_error(self) -> None:
            self.busy = False
            self._set_status("发生错误。")

    TrimmerGui().run()


def parse_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value: str, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


REVIEW_HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ad Trim Review</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --text: #232323;
      --muted: #666b70;
      --line: #d9d9d2;
      --accent: #0b6bcb;
      --danger: #a33b2f;
      --selected: #0b6bcb;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Arial, "Microsoft YaHei", sans-serif;
      font-size: 14px;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      padding: 12px 18px;
      background: #ecece6;
      border-bottom: 1px solid var(--line);
    }
    h1 {
      margin: 0;
      font-size: 18px;
      line-height: 1.2;
    }
    button, select, input {
      font: inherit;
    }
    button {
      border: 1px solid #9da5aa;
      background: #fff;
      color: var(--text);
      border-radius: 6px;
      padding: 7px 10px;
      cursor: pointer;
    }
    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
    }
    button.danger {
      border-color: var(--danger);
      color: var(--danger);
    }
    main {
      width: min(1400px, 100%);
      margin: 0 auto;
      padding: 16px;
    }
    .video {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      margin-bottom: 16px;
      overflow: hidden;
    }
    .video-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
    }
    .name {
      min-width: 240px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .meta {
      color: var(--muted);
      font-size: 13px;
    }
    .side {
      padding: 12px;
    }
    .side h2 {
      margin: 0 0 8px;
      font-size: 15px;
    }
    .strip {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
      gap: 10px;
    }
    .segment {
      border: 2px solid transparent;
      border-radius: 8px;
      padding: 6px;
      background: #f4f4ef;
      text-align: left;
    }
    .segment.selected {
      border-color: var(--selected);
      background: #e8f1fb;
    }
    .segment img {
      display: block;
      width: 100%;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      border-radius: 5px;
      background: #ddd;
    }
    .segment .label {
      display: flex;
      justify-content: space-between;
      gap: 6px;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .controls label {
      display: inline-flex;
      gap: 6px;
      align-items: center;
      color: var(--muted);
    }
    .controls input {
      width: 74px;
      padding: 6px;
      border: 1px solid var(--line);
      border-radius: 6px;
    }
    .status {
      min-height: 20px;
      color: var(--muted);
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>视频广告裁剪确认</h1>
      <div class="status" id="status"></div>
    </div>
    <div class="controls">
      <button id="download">下载 selections.json</button>
      <button class="primary" id="save">保存 selections.json</button>
    </div>
  </header>
  <main id="app"></main>
  <script id="manifest" type="application/json">__MANIFEST_JSON__</script>
  <script>
    const manifest = JSON.parse(document.getElementById('manifest').textContent);
    const selections = new Map();
    const status = document.getElementById('status');
    const app = document.getElementById('app');
    const cards = new Map();

    function fmt(seconds) {
      seconds = Math.max(0, Number(seconds) || 0);
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      const s = Math.floor(seconds % 60);
      return [h, m, s].map(v => String(v).padStart(2, '0')).join(':');
    }

    function ensureSelection(item) {
      if (!selections.has(item.id)) {
        selections.set(item.id, {
          id: item.id,
          frontIndex: null,
          backIndex: null,
          frontOffsetSeconds: 0,
          backOffsetSeconds: 0
        });
      }
      return selections.get(item.id);
    }

    function renderSegment(item, side, segment) {
      const selection = ensureSelection(item);
      const button = document.createElement('button');
      button.className = 'segment';
      button.type = 'button';
      if (selection[side + 'Index'] === segment.index) {
        button.classList.add('selected');
      }
      button.innerHTML = `
        <img src="${segment.thumbnail}" alt="">
        <span class="label">
          <span>#${segment.index}</span>
          <span>${fmt(segment.start)} - ${fmt(segment.end)}</span>
        </span>
      `;
      button.addEventListener('click', () => {
        selection[side + 'Index'] = selection[side + 'Index'] === segment.index ? null : segment.index;
        rerenderItem(item);
      });
      return button;
    }

    function renderSide(item, side, title) {
      const section = document.createElement('section');
      section.className = 'side';
      const selection = ensureSelection(item);
      const offsetName = side + 'OffsetSeconds';
      section.innerHTML = `
        <h2>${title}</h2>
        <div class="controls">
          <button type="button" data-clear="${side}">${side === 'front' ? '无片头广告' : '无片尾广告'}</button>
          <button type="button" data-nudge="${side}" data-value="-30">-30s</button>
          <button type="button" data-nudge="${side}" data-value="-5">-5s</button>
          <button type="button" data-nudge="${side}" data-value="5">+5s</button>
          <button type="button" data-nudge="${side}" data-value="30">+30s</button>
          <label>offset <input data-offset="${side}" type="number" step="1" value="${selection[offsetName]}"> s</label>
        </div>
      `;
      const strip = document.createElement('div');
      strip.className = 'strip';
      for (const segment of item[side]) {
        strip.appendChild(renderSegment(item, side, segment));
      }
      section.appendChild(strip);
      section.querySelector('[data-clear]').addEventListener('click', () => {
        selection[side + 'Index'] = null;
        rerenderItem(item);
      });
      for (const button of section.querySelectorAll('[data-nudge]')) {
        button.addEventListener('click', () => {
          selection[offsetName] += Number(button.dataset.value);
          rerenderItem(item);
        });
      }
      section.querySelector('[data-offset]').addEventListener('change', event => {
        selection[offsetName] = Number(event.target.value) || 0;
        rerenderItem(item);
      });
      return section;
    }

    function renderVideo(item) {
      const selection = ensureSelection(item);
      const article = document.createElement('article');
      article.className = 'video';
      article.dataset.itemId = item.id;
      article.innerHTML = `
        <div class="video-head">
          <div>
            <div class="name">${escapeHtml(item.name)}</div>
            <div class="meta">${fmt(item.duration)} | ${escapeHtml(item.source)}</div>
          </div>
          <div class="meta">
            ${rangeText(item, selection)}
          </div>
        </div>
      `;
      article.appendChild(renderSide(item, 'front', '片头：选择真正正片开始的片段'));
      article.appendChild(renderSide(item, 'back', '片尾：选择真正正片结束的片段'));
      cards.set(item.id, article);
      return article;
    }

    function rerenderItem(item) {
      const currentArticle = cards.get(item.id);
      const nextArticle = renderVideo(item);
      if (currentArticle && currentArticle.isConnected) {
        currentArticle.replaceWith(nextArticle);
      } else {
        app.appendChild(nextArticle);
      }
      cards.set(item.id, nextArticle);
      status.textContent = summaryText();
    }

    function render() {
      app.innerHTML = '';
      cards.clear();
      for (const item of manifest.items) {
        app.appendChild(renderVideo(item));
      }
      status.textContent = summaryText();
    }

    function summaryText() {
      let changed = 0;
      let unchanged = 0;
      for (const item of manifest.items) {
        const selection = ensureSelection(item);
        if (selection.frontIndex !== null || selection.backIndex !== null) changed += 1;
        else unchanged += 1;
      }
      return `${manifest.items.length} 个视频，${changed} 个将裁剪，${unchanged} 个确认无广告`;
    }

    function rangeText(item, selection) {
      const start = selectedStart(item, selection);
      const end = selectedEnd(item, selection);
      if (selection.frontIndex === null && selection.backIndex === null) {
        return `无片头/片尾广告，视频保持原样 ${fmt(0)} - ${fmt(item.duration)}`;
      }
      return `预计正片范围 ${fmt(start)} - ${fmt(end)}；片头 ${selection.frontIndex ?? '无广告'} / 片尾 ${selection.backIndex ?? '无广告'}`;
    }

    function selectedStart(item, selection) {
      const segment = item.front.find(seg => seg.index === selection.frontIndex);
      return Math.max(0, (segment ? segment.start : 0) + Number(selection.frontOffsetSeconds || 0));
    }

    function selectedEnd(item, selection) {
      const segment = item.back.find(seg => seg.index === selection.backIndex);
      return Math.min(item.duration, (segment ? segment.end : item.duration) + Number(selection.backOffsetSeconds || 0));
    }

    function payload() {
      return {
        version: 1,
        savedAt: new Date().toISOString(),
        items: Array.from(selections.values())
      };
    }

    function downloadSelections() {
      const blob = new Blob([JSON.stringify(payload(), null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'selections.json';
      link.click();
      URL.revokeObjectURL(url);
    }

    async function saveSelections() {
      const response = await fetch('/api/selections', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload(), null, 2)
      });
      if (!response.ok) {
        throw new Error(await response.text());
      }
      status.textContent = '已保存 selections.json';
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, char => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[char]));
    }

    document.getElementById('download').addEventListener('click', downloadSelections);
    document.getElementById('save').addEventListener('click', () => {
      saveSelections().catch(error => {
        status.textContent = '保存失败。请先启动 serve，或使用下载按钮保存 selections.json';
        console.error(error);
      });
    });
    render();
  </script>
</body>
</html>
"""


def run_command(cmd: list[str], capture: bool) -> subprocess.CompletedProcess[str]:
    run_kwargs: dict[str, Any] = {"check": False}
    if os.name == "nt":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    log_info("command start capture=%s command=%s", capture, format_command_for_log(cmd))
    try:
        if capture:
            raw_result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **run_kwargs,
            )
            result = subprocess.CompletedProcess(
                raw_result.args,
                raw_result.returncode,
                stdout=decode_process_output(raw_result.stdout),
                stderr=decode_process_output(raw_result.stderr),
            )
        else:
            result = subprocess.run(cmd, **run_kwargs)
    except FileNotFoundError as exc:
        log_error("command missing command=%s", format_command_for_log(cmd))
        raise ToolError(f"Command not found: {cmd[0]}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        log_error("command failed code=%s command=%s detail=%s", result.returncode, format_command_for_log(cmd), detail[:4000])
        raise ToolError(f"Command failed: {' '.join(quote_arg(part) for part in cmd)}\n{detail}")
    if capture and ((result.stdout or "").strip() or (result.stderr or "").strip()):
        log_info(
            "command output command=%s stdout=%s stderr=%s",
            format_command_for_log(cmd),
            (result.stdout or "").strip()[:4000],
            (result.stderr or "").strip()[:4000],
        )
    log_info("command done code=%s command=%s", result.returncode, format_command_for_log(cmd))
    return result


def decode_process_output(data: bytes | None) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "gbk", sys.getdefaultencoding()):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def format_command_for_log(cmd: list[str]) -> str:
    return " ".join(quote_arg(part) for part in cmd)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def read_json(path: Path) -> Any:
    if not path.exists():
        raise ToolError(f"Missing file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def segment_to_dict(segment: Segment) -> dict[str, Any]:
    return {
        "index": segment.index,
        "start": segment.start,
        "end": segment.end,
        "thumbnail": segment.thumbnail,
    }


def build_default_selections(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "items": [
            {
                "id": item["id"],
                "frontIndex": None,
                "backIndex": None,
                "frontTimeSeconds": None,
                "backTimeSeconds": None,
                "frontOffsetSeconds": 0,
                "backOffsetSeconds": 0,
            }
            for item in manifest.get("items", [])
        ],
    }


def make_video_id(ordinal: int, video: Path) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", video.stem).strip("._-") or "video"
    return f"{ordinal:04d}_{safe_name[:80]}"


def format_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def format_timestamp(value: float) -> str:
    value = max(0.0, value)
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = value % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def format_time_for_name(value: float) -> str:
    return format_timestamp(value).replace(":", "-").replace(".", "_")


def format_file_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def to_posix_relative(path: Path, base: Path) -> str:
    return path.resolve().relative_to(base.resolve()).as_posix()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def quote_arg(value: str) -> str:
    if re.search(r"\s", value):
        return f'"{value}"'
    return value


def quote_path(path: Path) -> str:
    return quote_arg(str(path))


if __name__ == "__main__":
    raise SystemExit(main())

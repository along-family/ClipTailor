import unittest
from pathlib import Path
from unittest.mock import patch

import video_ad_trimmer as vat


class CutPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = vat.ToolPaths(ffmpeg="ffmpeg", ffprobe="ffprobe")
        self.source = Path("sample.mp4")

    @patch("video_ad_trimmer.resolve_keyframe_range", return_value=(13.2, 95.3, True))
    def test_choose_cut_plan_keeps_copy_when_drift_within_threshold(self, _resolve_keyframe_range: object) -> None:
        plan = vat.choose_cut_plan(
            source=self.source,
            requested_start=13.0,
            requested_end=95.0,
            tools=self.tools,
            force_precise=False,
            prefer_smart_edges=False,
            auto_reencode_threshold=0.5,
        )

        self.assertEqual(plan.mode, "copy")
        self.assertEqual(plan.decision, "copy")
        self.assertAlmostEqual(plan.actual_start, 13.2)
        self.assertAlmostEqual(plan.actual_end, 95.3)
        self.assertIsNone(plan.video_encoder)

    @patch("video_ad_trimmer.get_preferred_video_encoder", return_value="h264_nvenc")
    @patch("video_ad_trimmer.probe_source_profile", return_value=vat.SourceProfile("h264", "aac", 1, 0))
    @patch(
        "video_ad_trimmer.find_nearest_keyframe",
        side_effect=[(15.0, True), (90.0, True)],
    )
    @patch("video_ad_trimmer.resolve_keyframe_range", return_value=(3.0, 95.3, True))
    def test_choose_cut_plan_auto_smart_when_drift_exceeds_threshold(
        self,
        _resolve_keyframe_range: object,
        _find_nearest_keyframe: object,
        _probe_source_profile: object,
        _get_preferred_video_encoder: object,
    ) -> None:
        plan = vat.choose_cut_plan(
            source=self.source,
            requested_start=13.0,
            requested_end=95.0,
            tools=self.tools,
            force_precise=False,
            prefer_smart_edges=False,
            auto_reencode_threshold=0.5,
        )

        self.assertEqual(plan.mode, "smart")
        self.assertEqual(plan.decision, "smart")
        self.assertAlmostEqual(plan.actual_start, 13.0)
        self.assertAlmostEqual(plan.actual_end, 95.0)
        self.assertEqual([segment.label for segment in plan.segments], ["head", "middle", "tail"])
        self.assertEqual(plan.video_encoder, "h264_nvenc")

    @patch("video_ad_trimmer.get_preferred_video_encoder", return_value="libx264")
    @patch("video_ad_trimmer.resolve_keyframe_range", return_value=(13.0, 95.0, True))
    def test_choose_cut_plan_respects_forced_precise_mode(
        self,
        _resolve_keyframe_range: object,
        _get_preferred_video_encoder: object,
    ) -> None:
        plan = vat.choose_cut_plan(
            source=self.source,
            requested_start=13.0,
            requested_end=95.0,
            tools=self.tools,
            force_precise=True,
            prefer_smart_edges=False,
            auto_reencode_threshold=0.5,
        )

        self.assertEqual(plan.mode, "precise")
        self.assertEqual(plan.decision, "forced")
        self.assertAlmostEqual(plan.actual_start, 13.0)
        self.assertAlmostEqual(plan.actual_end, 95.0)
        self.assertEqual(plan.video_encoder, "libx264")

    @patch("video_ad_trimmer.get_preferred_video_encoder", return_value="h264_nvenc")
    @patch("video_ad_trimmer.probe_source_profile", return_value=vat.SourceProfile("h264", "aac", 1, 0))
    @patch(
        "video_ad_trimmer.find_nearest_keyframe",
        side_effect=[(3.0, True), (95.3, True), (15.0, True), (90.0, True)],
    )
    def test_choose_cut_plan_builds_smart_segments_when_requested(
        self,
        _find_nearest_keyframe: object,
        _probe_source_profile: object,
        _get_preferred_video_encoder: object,
    ) -> None:
        plan = vat.choose_cut_plan(
            source=self.source,
            requested_start=13.0,
            requested_end=95.0,
            tools=self.tools,
            force_precise=False,
            prefer_smart_edges=True,
            auto_reencode_threshold=0.5,
        )

        self.assertEqual(plan.mode, "smart")
        self.assertEqual(plan.decision, "smart")
        self.assertAlmostEqual(plan.actual_start, 13.0)
        self.assertAlmostEqual(plan.actual_end, 95.0)
        self.assertEqual([segment.label for segment in plan.segments], ["head", "middle", "tail"])
        self.assertEqual(plan.video_encoder, "h264_nvenc")

    @patch("video_ad_trimmer.get_preferred_video_encoder", return_value="libx264")
    @patch("video_ad_trimmer.probe_source_profile", return_value=vat.SourceProfile("hevc", "aac", 1, 0))
    @patch("video_ad_trimmer.resolve_keyframe_range", return_value=(13.0, 95.0, True))
    def test_choose_cut_plan_falls_back_to_precise_when_smart_render_is_unsupported(
        self,
        _resolve_keyframe_range: object,
        _probe_source_profile: object,
        _get_preferred_video_encoder: object,
    ) -> None:
        plan = vat.choose_cut_plan(
            source=self.source,
            requested_start=13.0,
            requested_end=95.0,
            tools=self.tools,
            force_precise=False,
            prefer_smart_edges=True,
            auto_reencode_threshold=0.5,
        )

        self.assertEqual(plan.mode, "precise")
        self.assertEqual(plan.decision, "fallback")
        self.assertIn("h264", plan.fallback_reason or "")


class CommandBuilderTests(unittest.TestCase):
    def test_build_precise_cut_command_uses_exact_seek_order_and_encoder(self) -> None:
        cmd = vat.build_cut_command(
            "ffmpeg",
            Path("input.mp4"),
            Path("output.mp4"),
            13.0,
            95.0,
            overwrite=False,
            reencode=True,
            video_encoder="h264_nvenc",
        )

        self.assertLess(cmd.index("-i"), cmd.index("-ss"))
        self.assertIn("h264_nvenc", cmd)
        self.assertIn("+faststart", cmd)
        self.assertIn("192k", cmd)

    def test_execute_cut_plan_builds_smart_render_command_chain(self) -> None:
        plan = vat.CutPlan(
            mode="smart",
            decision="smart",
            requested_start=13.0,
            requested_end=95.0,
            actual_start=13.0,
            actual_end=95.0,
            keyframe_start=3.0,
            keyframe_end=105.0,
            start_delta=10.0,
            end_delta=10.0,
            alignment_available=True,
            video_encoder="h264_nvenc",
            segments=(
                vat.RenderSegment("head", "precise", 13.0, 15.0),
                vat.RenderSegment("middle", "copy", 15.0, 90.0),
                vat.RenderSegment("tail", "precise", 90.0, 95.0),
            ),
        )

        commands = vat.execute_cut_plan(
            plan,
            vat.ToolPaths(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            Path("input.mp4"),
            Path("output.mp4"),
            overwrite=False,
            dry_run=True,
        )

        self.assertEqual(len(commands), 4)
        self.assertIn("concat", commands[-1])
        self.assertIn("copy", commands[-1])

    @patch("video_ad_trimmer._execute_cut_plan_once")
    def test_execute_cut_plan_falls_back_to_libx264_when_hardware_encoder_fails(self, mock_execute_once: object) -> None:
        plan = vat.CutPlan(
            mode="precise",
            decision="forced",
            requested_start=13.0,
            requested_end=95.0,
            actual_start=13.0,
            actual_end=95.0,
            keyframe_start=13.0,
            keyframe_end=95.0,
            start_delta=0.0,
            end_delta=0.0,
            alignment_available=True,
            video_encoder="h264_nvenc",
            segments=(vat.RenderSegment("full", "precise", 13.0, 95.0),),
        )
        mock_execute_once.side_effect = [
            vat.ToolError("encoder failed"),
            [["ffmpeg", "-c:v", "libx264"]],
        ]

        commands = vat.execute_cut_plan(
            plan,
            vat.ToolPaths(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            Path("input.mp4"),
            Path("output.mp4"),
            overwrite=False,
            dry_run=False,
        )

        self.assertEqual(commands, [["ffmpeg", "-c:v", "libx264"]])
        first_plan = mock_execute_once.call_args_list[0].args[0]
        second_plan = mock_execute_once.call_args_list[1].args[0]
        self.assertEqual(first_plan.video_encoder, "h264_nvenc")
        self.assertEqual(second_plan.video_encoder, "libx264")
        self.assertFalse(mock_execute_once.call_args_list[0].args[4])
        self.assertTrue(mock_execute_once.call_args_list[1].args[4])

    def test_pick_preferred_video_encoder_uses_priority_order(self) -> None:
        available = {"h264_amf", "libx264", "h264_qsv"}
        self.assertEqual(vat.pick_preferred_video_encoder(available), "h264_qsv")


if __name__ == "__main__":
    unittest.main()

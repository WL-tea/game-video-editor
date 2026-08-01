import tempfile
import unittest
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.premiere_xml import (
    CandidateSpec,
    SourceSpec,
    TimelineError,
    build_timeline,
    generate_job,
    write_fcp7_xml,
)


class PremiereXmlTimelineTests(unittest.TestCase):
    def source(self, duration=400):
        return SourceSpec(
            source_id="CEL02",
            path=r"C:\素材\celeste-recording.mp4",
            duration_frames=duration,
            timebase=60,
            width=1920,
            height=1080,
        )

    def test_selected_clip_keeps_source_position_and_linked_audio(self):
        timeline = build_timeline(
            self.source(),
            [CandidateSpec("S01", 100, 150, "保留", label="房间3_关键操作")],
        )

        selected = timeline.clips_for("V2")
        self.assertEqual([(clip.start, clip.end) for clip in selected], [(100, 150)])
        self.assertEqual(selected[0].source_start, 100)
        self.assertEqual(selected[0].source_end, 150)
        self.assertEqual(timeline.audio_track_for("V2"), "A2")

    def test_overlapping_backups_use_minimum_extra_tracks_and_keep_full_ranges(self):
        candidates = [
            CandidateSpec("S01", 100, 150, "保留"),
            CandidateSpec("S02", 120, 220, "备选", label="备选A"),
            CandidateSpec("S03", 200, 280, "备选", label="备选B"),
            CandidateSpec("S04", 260, 300, "备选", label="备选C"),
        ]

        timeline = build_timeline(self.source(), candidates)

        backup_clips = timeline.clips_for_status("备选")
        self.assertEqual(
            [(clip.candidate_id, clip.track) for clip in backup_clips],
            [("S02", "V3"), ("S03", "V4"), ("S04", "V3")],
        )
        self.assertEqual(
            [(clip.candidate_id, clip.source_start, clip.source_end) for clip in backup_clips],
            [("S02", 120, 220), ("S03", 200, 280), ("S04", 260, 300)],
        )

    def test_non_overlapping_backup_is_cut_into_v1_and_marked(self):
        timeline = build_timeline(
            self.source(),
            [
                CandidateSpec("S01", 100, 150, "保留"),
                CandidateSpec("S02", 250, 300, "备选", label="房间4_备选"),
            ],
        )

        v1 = timeline.clips_for("V1")
        self.assertEqual(
            [(clip.start, clip.end, clip.candidate_id) for clip in v1],
            [(0, 100, None), (150, 250, None), (250, 300, "S02"), (300, 400, None)],
        )
        self.assertEqual(v1[2].label, "房间4_备选")

    def test_overlapping_selected_candidates_are_rejected(self):
        with self.assertRaisesRegex(TimelineError, "selected candidates overlap"):
            build_timeline(
                self.source(),
                [
                    CandidateSpec("S01", 100, 180, "保留"),
                    CandidateSpec("S02", 150, 220, "保留"),
                ],
            )

    def test_vfr_source_is_rejected_before_xml_generation(self):
        with self.assertRaisesRegex(TimelineError, "constant frame rate"):
            build_timeline(
                SourceSpec(
                    source_id="CEL02",
                    path=r"C:\素材\vfr.mp4",
                    duration_frames=400,
                    timebase=60,
                    width=1920,
                    height=1080,
                    constant_frame_rate=False,
                ),
                [],
            )

    def test_multiple_sources_write_independent_packages_with_traceable_manifest(self):
        job = {
            "sources": [
                {
                    "source_id": "CEL01",
                    "path": r"C:\素材\one.mp4",
                    "duration_frames": 120,
                    "timebase": 60,
                    "width": 1920,
                    "height": 1080,
                    "candidates": [],
                },
                {
                    "source_id": "CEL02",
                    "path": r"C:\素材\two.mp4",
                    "duration_frames": 180,
                    "timebase": 60,
                    "width": 1920,
                    "height": 1080,
                    "candidates": [],
                },
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "job.json"
            output_dir = Path(temp_dir) / "drafts"
            input_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            outputs = generate_job(input_path, output_dir)
            manifest = output_dir / "CEL02__时间线清单-v1.csv"

            self.assertEqual(len(outputs), 6)
            self.assertTrue((output_dir / "CEL01__粗剪-v1.xml").exists())
            self.assertTrue((output_dir / "CEL02__PR导入说明-v1.md").exists())
            self.assertTrue(manifest.read_bytes().startswith(b"\xef\xbb\xbf"))
            header = manifest.read_text(encoding="utf-8-sig").splitlines()[0]
            self.assertIn("源文件路径", header)
            self.assertIn("生成状态", header)
            self.assertIn("生成版本", header)

    def test_generate_job_uses_requested_version_in_all_outputs(self):
        job = {
            "source_id": "CEL03",
            "path": r"C:\素材\three.mp4",
            "duration_frames": 60,
            "timebase": 60,
            "width": 1920,
            "height": 1080,
            "candidates": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "job.json"
            output_dir = Path(temp_dir) / "drafts"
            input_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            outputs = generate_job(input_path, output_dir, version="v3")

            self.assertEqual(
                {output.name for output in outputs},
                {
                    "CEL03__粗剪-v3.xml",
                    "CEL03__时间线清单-v3.csv",
                    "CEL03__PR导入说明-v3.md",
                },
            )

    def test_xml_contains_fcp7_sequence_source_timecodes_and_audio_link(self):
        source = self.source()
        timeline = build_timeline(
            source,
            [CandidateSpec("S01", 100, 150, "保留", label="房间3_关键操作")],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "CEL02__粗剪-v1.xml"
            write_fcp7_xml(source, timeline, output)
            xml = output.read_text(encoding="utf-8")

        self.assertIn('<xmeml version="5">', xml)
        self.assertIn("<name>CEL02__粗剪</name>", xml)
        self.assertIn("<name>[已选] S01｜房间3_关键操作</name>", xml)
        self.assertIn("<in>100</in>", xml)
        self.assertIn("<out>150</out>", xml)
        self.assertIn("<start>100</start>", xml)
        self.assertIn("<end>150</end>", xml)
        self.assertIn("<trackname>已选_粗剪</trackname>", xml)
        self.assertIn("<trackname>已选_粗剪_音频</trackname>", xml)
        self.assertIn("file:///C:/%E7%B4%A0%E6%9D%90/celeste-recording.mp4", xml)

    def test_xml_links_video_and_audio_clipitems_in_both_directions(self):
        source = self.source()
        timeline = build_timeline(
            source,
            [CandidateSpec("S01", 100, 150, "保留", label="房间3_关键操作")],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "CEL02__粗剪-v1.xml"
            write_fcp7_xml(source, timeline, output)
            root = ET.parse(output).getroot()

        video_track = next(
            track
            for track in root.findall("./project/children/sequence/media/video/track")
            if track.findtext("trackname") == "已选_粗剪"
        )
        audio_track = next(
            track
            for track in root.findall("./project/children/sequence/media/audio/track")
            if track.findtext("trackname") == "已选_粗剪_音频"
        )
        video_clip = video_track.find("clipitem")
        audio_clip = audio_track.find("clipitem")
        self.assertIsNotNone(video_clip)
        self.assertIsNotNone(audio_clip)
        video_id = video_clip.attrib["id"]
        audio_id = audio_clip.attrib["id"]
        self.assertIn(audio_id, video_clip.findall("link/linkclipref")[0].text)
        self.assertIn(video_id, audio_clip.findall("link/linkclipref")[0].text)

    def test_xml_marker_keeps_full_flow_id_and_generation_version(self):
        source = self.source()
        timeline = build_timeline(
            source,
            [
                CandidateSpec(
                    "S01",
                    100,
                    150,
                    "保留",
                    label="房间3_关键操作",
                    candidate_type="完整流程",
                    full_flow_id="FLOW-01",
                )
            ],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "CEL02__粗剪-v2.xml"
            write_fcp7_xml(source, timeline, output, version="v2")
            root = ET.parse(output).getroot()

        marker_comment = root.findtext(
            "./project/children/sequence/media/video/track/clipitem/marker/comment"
        )
        self.assertIn("FLOW-01", marker_comment)
        self.assertIn("版本 v2", marker_comment)

    def test_xml_clip_links_keep_matching_clip_indices_on_each_track(self):
        source = self.source()
        timeline = build_timeline(
            source,
            [
                CandidateSpec("S01", 100, 150, "保留"),
                CandidateSpec("S02", 200, 250, "保留"),
            ],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "CEL02__粗剪-v1.xml"
            write_fcp7_xml(source, timeline, output)
            root = ET.parse(output).getroot()

        video_track = next(
            track
            for track in root.findall("./project/children/sequence/media/video/track")
            if track.findtext("trackname") == "已选_粗剪"
        )
        audio_track = next(
            track
            for track in root.findall("./project/children/sequence/media/audio/track")
            if track.findtext("trackname") == "已选_粗剪_音频"
        )
        for index, (video_clip, audio_clip) in enumerate(
            zip(video_track.findall("clipitem"), audio_track.findall("clipitem")),
            start=1,
        ):
            self.assertEqual(video_clip.findtext("link/clipindex"), str(index))
            self.assertEqual(audio_clip.findtext("link/clipindex"), str(index))


if __name__ == "__main__":
    unittest.main()

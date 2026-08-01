"""Build source-referenced FCP7 XML timelines for Premiere Pro."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import quote


SELECTED_STATUS = "保留"
BACKUP_STATUS = "备选"


class TimelineError(ValueError):
    """Raised when candidate ranges cannot form the requested timeline."""


def _validate_version(version: str) -> None:
    if not version or any(character in version for character in "\\/:*?\"<>|"):
        raise ValueError("version must be a non-empty filename-safe value")


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    path: str
    duration_frames: int
    timebase: int
    width: int
    height: int
    constant_frame_rate: bool = True
    audio_channels: int = 2
    audio_sample_rate: int = 48000

    def validate(self) -> None:
        if not self.source_id.strip():
            raise TimelineError("source_id must not be empty")
        if self.duration_frames <= 0:
            raise TimelineError("duration_frames must be positive")
        if self.timebase <= 0:
            raise TimelineError("timebase must be positive")
        if self.width <= 0 or self.height <= 0:
            raise TimelineError("source dimensions must be positive")
        if not self.constant_frame_rate:
            raise TimelineError("constant frame rate is required for exact XML")
        if self.audio_channels < 0 or self.audio_sample_rate <= 0:
            raise TimelineError("audio metadata is invalid")


@dataclass(frozen=True)
class CandidateSpec:
    candidate_id: str
    source_start: int
    source_end: int
    status: str
    label: str = ""
    candidate_type: str = "片段候选"
    full_flow_id: str = ""

    def validate(self, source: SourceSpec) -> None:
        if not self.candidate_id.strip():
            raise TimelineError("candidate_id must not be empty")
        if self.source_start < 0 or self.source_end <= self.source_start:
            raise TimelineError(f"invalid range for candidate {self.candidate_id}")
        if self.source_end > source.duration_frames:
            raise TimelineError(
                f"candidate {self.candidate_id} ends after source duration"
            )
        if self.status not in {SELECTED_STATUS, BACKUP_STATUS, "暂不采用"}:
            raise TimelineError(
                f"unsupported status for {self.candidate_id}: {self.status}"
            )


@dataclass(frozen=True)
class TimelineClip:
    track: str
    start: int
    end: int
    source_start: int
    source_end: int
    status: str
    candidate_ids: tuple[str, ...] = field(default_factory=tuple)
    label: str = ""
    candidate_type: str = ""
    full_flow_id: str = ""

    @property
    def candidate_id(self) -> str | None:
        if not self.candidate_ids:
            return None
        return "+".join(self.candidate_ids)

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def source_duration(self) -> int:
        return self.source_end - self.source_start

    @property
    def is_linked(self) -> bool:
        return True


@dataclass
class Timeline:
    source: SourceSpec
    clips: list[TimelineClip]

    def clips_for(self, track: str) -> list[TimelineClip]:
        return sorted(
            (clip for clip in self.clips if clip.track == track),
            key=lambda clip: (clip.start, clip.end, clip.candidate_id or ""),
        )

    def clips_for_status(self, status: str) -> list[TimelineClip]:
        return sorted(
            (clip for clip in self.clips if clip.status == status),
            key=lambda clip: (clip.start, clip.end, clip.candidate_id or ""),
        )

    @staticmethod
    def audio_track_for(video_track: str) -> str:
        if not video_track.startswith("V"):
            raise TimelineError(f"not a video track: {video_track}")
        return f"A{video_track[1:]}"

    @property
    def video_tracks(self) -> list[str]:
        return sorted(
            {clip.track for clip in self.clips},
            key=lambda track: int(track[1:]),
        )


def _overlaps(left: CandidateSpec, right: CandidateSpec) -> bool:
    return left.source_start < right.source_end and right.source_start < left.source_end


def _display_label(candidate: CandidateSpec) -> str:
    return candidate.label or candidate.candidate_id


def _backup_track_indices(candidates: Iterable[CandidateSpec]) -> dict[str, int]:
    """Color interval overlaps with the fewest available backup lanes."""

    lane_ends: list[int] = []
    assignments: dict[str, int] = {}
    ordered = sorted(candidates, key=lambda item: (item.source_start, item.source_end, item.candidate_id))
    for candidate in ordered:
        for index, lane_end in enumerate(lane_ends):
            if lane_end <= candidate.source_start:
                lane_ends[index] = candidate.source_end
                assignments[candidate.candidate_id] = index
                break
        else:
            assignments[candidate.candidate_id] = len(lane_ends)
            lane_ends.append(candidate.source_end)
    return assignments


def _make_clip(
    track: str,
    start: int,
    end: int,
    candidate: CandidateSpec,
    *,
    candidate_ids: tuple[str, ...] | None = None,
) -> TimelineClip:
    ids = candidate_ids or (candidate.candidate_id,)
    return TimelineClip(
        track=track,
        start=start,
        end=end,
        source_start=candidate.source_start if candidate_ids is None else start,
        source_end=candidate.source_end if candidate_ids is None else end,
        status=candidate.status,
        candidate_ids=ids,
        label=_display_label(candidate),
        candidate_type=candidate.candidate_type,
        full_flow_id=candidate.full_flow_id,
    )


def build_timeline(source: SourceSpec, candidates: list[CandidateSpec]) -> Timeline:
    """Build a source-aligned timeline using the project's V1/V2/V3+ rules."""

    source.validate()
    seen_ids: set[str] = set()
    for candidate in candidates:
        candidate.validate(source)
        if candidate.candidate_id in seen_ids:
            raise TimelineError(f"duplicate candidate_id: {candidate.candidate_id}")
        seen_ids.add(candidate.candidate_id)

    selected = [item for item in candidates if item.status == SELECTED_STATUS]
    backups = [item for item in candidates if item.status == BACKUP_STATUS]
    for index, left in enumerate(selected):
        for right in selected[index + 1 :]:
            if _overlaps(left, right):
                raise TimelineError(
                    f"selected candidates overlap: {left.candidate_id}, {right.candidate_id}"
                )

    separate_backup_ids = {
        backup.candidate_id
        for backup in backups
        if any(_overlaps(backup, chosen) for chosen in selected)
    }
    # A backup that overlaps an already separated backup must also leave V1;
    # otherwise the backup group would be split across tracks.
    changed = True
    while changed:
        changed = False
        separated = [item for item in backups if item.candidate_id in separate_backup_ids]
        for backup in backups:
            if backup.candidate_id in separate_backup_ids:
                continue
            if any(_overlaps(backup, separated_item) for separated_item in separated):
                separate_backup_ids.add(backup.candidate_id)
                changed = True
    overlapping_backups = [
        backup for backup in backups if backup.candidate_id in separate_backup_ids
    ]
    backup_lanes = _backup_track_indices(overlapping_backups)

    clips: list[TimelineClip] = []
    clips.extend(
        _make_clip("V2", item.source_start, item.source_end, item)
        for item in sorted(selected, key=lambda candidate: candidate.source_start)
    )
    clips.extend(
        _make_clip(f"V{3 + backup_lanes[item.candidate_id]}", item.source_start, item.source_end, item)
        for item in sorted(overlapping_backups, key=lambda candidate: candidate.source_start)
    )

    boundaries = {0, source.duration_frames}
    for candidate in candidates:
        boundaries.add(candidate.source_start)
        boundaries.add(candidate.source_end)
    ordered_boundaries = sorted(boundaries)
    for start, end in zip(ordered_boundaries, ordered_boundaries[1:]):
        covering_selected = any(
            item.source_start <= start and end <= item.source_end for item in selected
        )
        covering_overlap_backup = any(
            item.source_start <= start and end <= item.source_end
            for item in overlapping_backups
        )
        if covering_selected or covering_overlap_backup:
            continue

        covering_backups = [
            item
            for item in backups
            if item.source_start <= start and end <= item.source_end
        ]
        if covering_backups:
            ids = tuple(item.candidate_id for item in covering_backups)
            labels = "／".join(_display_label(item) for item in covering_backups)
            first = covering_backups[0]
            clips.append(
                TimelineClip(
                    track="V1",
                    start=start,
                    end=end,
                    source_start=start,
                    source_end=end,
                    status=BACKUP_STATUS,
                    candidate_ids=ids,
                    label=labels,
                    candidate_type="／".join(item.candidate_type for item in covering_backups),
                    full_flow_id="／".join(
                        item.full_flow_id for item in covering_backups if item.full_flow_id
                    ),
                )
            )
        else:
            clips.append(
                TimelineClip(
                    track="V1",
                    start=start,
                    end=end,
                    source_start=start,
                    source_end=end,
                    status="未选",
                )
            )

    return Timeline(source=source, clips=clips)


def _file_url(path: str) -> str:
    resolved = Path(path).absolute().as_posix()
    if len(resolved) >= 2 and resolved[1] == ":":
        return "file:///" + quote(resolved, safe="/:~")
    return "file://" + quote(resolved, safe="/:~")


def _add(parent: ET.Element, tag: str, text: str | int | bool | None = None, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrs)
    if text is not None:
        element.text = str(text)
    return element


def _add_rate(parent: ET.Element, source: SourceSpec) -> None:
    rate = _add(parent, "rate")
    _add(rate, "timebase", source.timebase)
    _add(rate, "ntsc", "FALSE")


def _append_source_file(parent: ET.Element, source: SourceSpec, file_id: str) -> None:
    file_element = _add(parent, "file", id=file_id)
    _add(file_element, "name", Path(source.path).name)
    _add(file_element, "pathurl", _file_url(source.path))
    _add(file_element, "duration", source.duration_frames)
    _add_rate(file_element, source)
    media = _add(file_element, "media")
    video = _add(media, "video")
    characteristics = _add(video, "samplecharacteristics")
    _add_rate(characteristics, source)
    _add(characteristics, "width", source.width)
    _add(characteristics, "height", source.height)
    _add(characteristics, "anamorphic", "FALSE")
    _add(characteristics, "pixelaspectratio", "Square Pixels")
    _add(characteristics, "fielddominance", "none")
    audio = _add(media, "audio")
    _add(audio, "samplecharacteristics")
    _add(audio, "channelcount", source.audio_channels)
    _add(audio, "samplerate", source.audio_sample_rate)


def _clip_name(clip: TimelineClip, source: SourceSpec) -> str:
    if clip.status == SELECTED_STATUS:
        prefix = "已选"
    elif clip.status == BACKUP_STATUS:
        prefix = "备选"
    else:
        prefix = "未选"
    candidate_id = clip.candidate_id or source.source_id
    label = f"｜{clip.label}" if clip.label else ""
    return f"[{prefix}] {candidate_id}{label}"


def _append_clip_item(
    track_element: ET.Element,
    clip: TimelineClip,
    source: SourceSpec,
    file_id: str,
    clip_id: str,
    audio_clip_id: str,
    include_file: bool,
    clip_index: int,
    version: str,
) -> None:
    item = _add(track_element, "clipitem", id=clip_id)
    _add(item, "name", _clip_name(clip, source))
    _add(item, "duration", clip.source_duration)
    _add_rate(item, source)
    _add(item, "enabled", "TRUE")
    _add(item, "in", clip.source_start)
    _add(item, "out", clip.source_end)
    _add(item, "start", clip.start)
    _add(item, "end", clip.end)
    if include_file:
        _append_source_file(item, source, file_id)
    else:
        _add(item, "file", id=file_id)
    link = _add(item, "link")
    _add(link, "linkclipref", audio_clip_id)
    _add(link, "mediatype", "audio")
    _add(link, "trackindex", clip.track[1:])
    _add(link, "clipindex", clip_index)
    if clip.candidate_id:
        marker = _add(item, "marker")
        _add(marker, "name", clip.candidate_id)
        _add(
            marker,
            "comment",
            f"候选类型 {clip.candidate_type or '未标注'}；"
            f"完整流程关联 ID {clip.full_flow_id or '无'}；"
            f"源时间码 {clip.source_start}-{clip.source_end}；"
            f"状态 {clip.status}；版本 {version}",
        )


def _track_name(track: str) -> str:
    if track == "V1":
        return "原视频_未选"
    if track == "V2":
        return "已选_粗剪"
    if track.startswith("V"):
        return f"备选_重叠层{int(track[1:]) - 2}"
    raise TimelineError(f"unsupported video track: {track}")


def _append_track(
    media_element: ET.Element,
    track: str,
    timeline: Timeline,
    source: SourceSpec,
    file_id: str,
    *,
    audio: bool,
    version: str,
) -> None:
    container = _add(media_element, "audio" if audio else "video")
    track_element = _add(container, "track")
    track_label = f"{_track_name(track)}_音频" if audio else _track_name(track)
    _add(track_element, "trackname", track_label)
    _add(track_element, "enabled", "TRUE")
    _add(track_element, "locked", "FALSE")
    for index, clip in enumerate(timeline.clips_for(track), start=1):
        if audio:
            clip_id = f"A{track[1:]}-{clip.start}-{index}"
            item = _add(track_element, "clipitem", id=clip_id)
            _add(item, "name", _clip_name(clip, source))
            _add(item, "duration", clip.source_duration)
            _add_rate(item, source)
            _add(item, "enabled", "TRUE")
            _add(item, "in", clip.source_start)
            _add(item, "out", clip.source_end)
            _add(item, "start", clip.start)
            _add(item, "end", clip.end)
            _add(item, "file", id=file_id)
            link = _add(item, "link")
            _add(link, "linkclipref", f"V{track[1:]}-{clip.start}-{index}")
            _add(link, "mediatype", "video")
            _add(link, "trackindex", track[1:])
            _add(link, "clipindex", index)
        else:
            audio_clip_id = f"A{track[1:]}-{clip.start}-{index}"
            _append_clip_item(
                track_element,
                clip,
                source,
                file_id,
                f"{track}-{clip.start}-{index}",
                audio_clip_id,
                include_file=index == 1,
                clip_index=index,
                version=version,
            )


def write_fcp7_xml(
    source: SourceSpec,
    timeline: Timeline,
    output: Path,
    version: str = "v1",
) -> None:
    """Write one source-aligned sequence as a standard FCP7 XML file."""

    source.validate()
    _validate_version(version)
    output.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element("xmeml", {"version": "5"})
    project = _add(root, "project")
    _add(project, "name", "game-video-editor")
    children = _add(project, "children")
    sequence = _add(children, "sequence", id=f"sequence-{source.source_id}")
    _add(sequence, "name", f"{source.source_id}__粗剪")
    _add(sequence, "duration", source.duration_frames)
    _add_rate(sequence, source)
    timecode = _add(sequence, "timecode")
    _add_rate(timecode, source)
    _add(timecode, "string", "00:00:00:00")
    _add(timecode, "frame", "0")
    _add(timecode, "displayformat", "NDF")
    media = _add(sequence, "media")
    file_id = f"file-{source.source_id}"
    for track in timeline.video_tracks:
        _append_track(media, track, timeline, source, file_id, audio=False, version=version)
    for track in timeline.video_tracks:
        _append_track(media, track, timeline, source, file_id, audio=True, version=version)
    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    tree.write(output, encoding="utf-8", xml_declaration=True)


def write_manifest(
    source: SourceSpec,
    timeline: Timeline,
    output: Path,
    version: str = "v1",
) -> None:
    _validate_version(version)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "源素材 ID",
        "序列 ID",
        "源文件路径",
        "候选 ID",
        "候选类型",
        "状态",
        "源入点",
        "源出点",
        "序列入点",
        "序列出点",
        "视频轨",
        "音频轨",
        "中文标签",
        "完整流程关联 ID",
        "标记文本",
        "生成版本",
        "生成状态",
    ]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for clip in sorted(timeline.clips, key=lambda item: (item.start, item.track)):
            writer.writerow(
                {
                    "源素材 ID": source.source_id,
                    "序列 ID": f"sequence-{source.source_id}",
                    "源文件路径": source.path,
                    "候选 ID": clip.candidate_id or "",
                    "候选类型": clip.candidate_type,
                    "状态": clip.status,
                    "源入点": clip.source_start,
                    "源出点": clip.source_end,
                    "序列入点": clip.start,
                    "序列出点": clip.end,
                    "视频轨": clip.track,
                    "音频轨": timeline.audio_track_for(clip.track),
                    "中文标签": clip.label,
                    "完整流程关联 ID": clip.full_flow_id,
                    "标记文本": (
                        f"候选 {clip.candidate_id}；候选类型 {clip.candidate_type or '未标注'}；"
                        f"完整流程关联 ID {clip.full_flow_id or '无'}；"
                        f"源时间码 {clip.source_start}-{clip.source_end}；状态 {clip.status}；版本 {version}"
                        if clip.candidate_id
                        else ""
                    ),
                    "生成版本": version,
                    "生成状态": "已生成",
                }
            )


def load_job(path: Path) -> list[tuple[SourceSpec, list[CandidateSpec]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = data.get("sources", [data])
    jobs = []
    for source_data in sources:
        source = SourceSpec(
            source_id=source_data["source_id"],
            path=source_data["path"],
            duration_frames=int(source_data["duration_frames"]),
            timebase=int(source_data["timebase"]),
            width=int(source_data["width"]),
            height=int(source_data["height"]),
            constant_frame_rate=bool(source_data.get("constant_frame_rate", True)),
            audio_channels=int(source_data.get("audio_channels", 2)),
            audio_sample_rate=int(source_data.get("audio_sample_rate", 48000)),
        )
        candidates = [
            CandidateSpec(
                candidate_id=item["candidate_id"],
                source_start=int(item["source_start"]),
                source_end=int(item["source_end"]),
                status=item["status"],
                label=item.get("label", ""),
                candidate_type=item.get("candidate_type", "片段候选"),
                full_flow_id=item.get("full_flow_id", ""),
            )
            for item in source_data.get("candidates", [])
        ]
        jobs.append((source, candidates))
    return jobs


def generate_job(input_path: Path, output_dir: Path, version: str = "v1") -> list[Path]:
    _validate_version(version)

    outputs: list[Path] = []
    for source, candidates in load_job(input_path):
        timeline = build_timeline(source, candidates)
        xml_path = output_dir / f"{source.source_id}__粗剪-{version}.xml"
        manifest_path = output_dir / f"{source.source_id}__时间线清单-{version}.csv"
        notes_path = output_dir / f"{source.source_id}__PR导入说明-{version}.md"
        write_fcp7_xml(source, timeline, xml_path, version=version)
        write_manifest(source, timeline, manifest_path, version=version)
        notes_path.write_text(
            f"# {source.source_id} Premiere 导入说明\n\n"
            f"导入 `{xml_path.name}`，并确认源素材路径指向：`{source.path}`。\n\n"
            "导入后检查素材链接、轨道、音画同步、候选标记和时间线清单。\n",
            encoding="utf-8",
        )
        outputs.extend([xml_path, manifest_path, notes_path])
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="JSON timeline input")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory")
    parser.add_argument("--version", default="v1", help="Output version suffix, for example v2")
    args = parser.parse_args(argv)
    try:
        for output in generate_job(args.input, args.output_dir, version=args.version):
            print(output)
    except (OSError, KeyError, ValueError, json.JSONDecodeError, TimelineError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

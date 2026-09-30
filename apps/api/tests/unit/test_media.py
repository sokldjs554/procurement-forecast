"""Media stage without ffmpeg: caption parsing, window planning, transcript assembly, and that a
transcript reads back through the same speaker logic text minutes go through."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.domain.grounding import locate_quote, official_evidence_issue
from app.media.captions import (
    UNKNOWN,
    Caption,
    CaptionTrack,
    Speaker,
    band_changed,
    band_signature,
    merge_readings,
    parse_caption,
    read_captions,
)
from app.media.ffmpeg import Frame, Window, plan_windows
from app.media.job import DirCheckpoints, MediaCost
from app.media.stt import FixtureSTT, Segment
from app.media.transcript import assemble, merge_segments
from app.parsing.chunking import chunk_minutes, split_turns
from app.parsing.ocr import OCRResult


@pytest.mark.parametrize(
    ("band", "role", "name"),
    [
        ("도로과장 송태아", "도로과장", "송태아"),
        ("교통도로국 도로과장 송태아", "도로과장", "송태아"),
        ("송태아 도로과장", "도로과장", "송태아"),
        ("도로과장송태아", "도로과장", "송태아"),  # OCR lost the space
        ("조우현 의원", "의원", "조우현"),
        ("조우현의원", "의원", "조우현"),
        ("조 우 현 위원", "위원", "조우현"),  # one syllable at a time
        ("도시건설위원장 조우현", "위원장", "조우현"),
        ("조우현 위원장", "위원장", "조우현"),
        ("의장 박명숙", "의장", "박명숙"),
        ("전문위원 홍길동", "전문위원", "홍길동"),
        ("성남시장 신상진", "성남시장", "신상진"),
        ("| 도로과장 송태아 |", "도로과장", "송태아"),  # band borders read as bars
    ],
)
def test_parse_caption_reads_role_and_name(band: str, role: str, name: str) -> None:
    assert parse_caption(band) == Speaker(role, name)


@pytest.mark.parametrize(
    "band",
    ["", "제295회 성남시의회 본회의", "생중계 다시보기", "도시건설위원회 회의", "12:31:07", "LIVE"],
)
def test_parse_caption_ignores_bands_without_a_speaker(band: str) -> None:
    assert parse_caption(band) is None


def test_speaker_header_is_what_text_minutes_print() -> None:
    assert Speaker("의원", "조우현").header() == "○조우현 의원"
    assert Speaker("도로과장", "송태아").header() == "○도로과장 송태아"
    assert Speaker("위원장", "박재신").header() == "○위원장 박재신"
    assert UNKNOWN.header() == "○발언자 미상"


def test_merge_readings_joins_repeats_and_closes_on_a_cleared_band() -> None:
    a, b = Speaker("의원", "조우현"), Speaker("도로과장", "송태아")
    captions = merge_readings(
        [
            (4.0, a, "조우현 의원", 0.9),
            (0.0, a, "조우현 의원", 0.8),  # out of order on purpose
            (8.0, None, "", 0.0),
            (12.0, b, "도로과장 송태아", 0.9),
            (14.0, b, "도로과장 송태아", 0.95),
        ]
    )
    assert [(c.start, c.end, c.speaker, c.confidence) for c in captions] == [
        (0.0, 8.0, a, 0.9),
        (12.0, 14.0, b, 0.95),
    ]


def test_caption_track_carries_the_speaker_until_the_next_caption() -> None:
    a, b = Speaker("의원", "조우현"), Speaker("도로과장", "송태아")
    track = CaptionTrack([Caption(12.0, 14.0, b, "", 0.9), Caption(3.0, 8.0, a, "", 0.9)])
    assert track.speaker_at(0.0) is None  # before any caption (and outside the lead)
    assert track.speaker_at(1.6) == a  # the band goes up a moment after they start
    assert track.speaker_at(10.0) == a  # band cleared, still talking
    assert track.speaker_at(11.0) == b
    assert track.speaker_at(600.0) == b


def _band(text: str) -> Image.Image:
    img = Image.new("RGB", (640, 108), (20, 20, 20))
    ImageDraw.Draw(img).rectangle((40, 30, 40 + 18 * len(text), 70), fill=(240, 240, 240))
    return img


def test_band_signature_ignores_noise_and_sees_a_new_caption() -> None:
    a = band_signature(_band("조우현 의원"))
    noisy = _band("조우현 의원")
    noisy.putpixel((300, 50), (0, 0, 0))
    assert not band_changed(a, band_signature(noisy))
    assert band_changed(a, band_signature(_band("도시건설위원장 박재신")))
    assert band_changed(None, a)


class _CountingOCR:
    name = "counting"

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.calls = 0

    async def recognize(self, image: Image.Image) -> OCRResult:
        text = self._texts[self.calls]
        self.calls += 1
        return OCRResult(text, 0.9, self.name)


async def test_read_captions_only_ocrs_frames_whose_band_changed(tmp_path: Path) -> None:
    bands = ["조우현 의원"] * 3 + ["도로과장 송태아"] * 2
    frames = []
    for i, text in enumerate(bands):
        path = tmp_path / f"f_{i:06d}.png"
        _band(text).save(path)
        frames.append(Frame(i * 2.0, path))
    ocr = _CountingOCR(["조우현 의원", "도로과장 송태아"])
    captions, stats = await read_captions(frames, ocr)
    assert ocr.calls == 2
    assert (stats.frames, stats.ocr, stats.read) == (5, 2, 5)
    assert [(c.start, c.speaker.name) for c in captions] == [(0.0, "조우현"), (6.0, "송태아")]


async def test_read_captions_drops_low_confidence_readings(tmp_path: Path) -> None:
    path = tmp_path / "f_000001.png"
    _band("x").save(path)

    class Unsure:
        name = "unsure"

        async def recognize(self, image: Image.Image) -> OCRResult:
            return OCRResult("도로과장 송태아", 0.2, self.name)

    captions, stats = await read_captions([Frame(0.0, path)], Unsure())
    assert captions == [] and stats.read == 0


def test_plan_windows_cuts_in_the_longest_nearby_pause() -> None:
    silences = [(590.0, 590.5), (605.0, 607.0), (700.0, 701.0), (1195.0, 1197.0)]
    windows = plan_windows(1500.0, silences, target=600, search=30)
    assert [(w.start, w.end, w.cut) for w in windows] == [
        (0.0, 606.0, "silence"),
        (606.0, 1196.0, "silence"),
        (1196.0, 1500.0, "end"),
    ]
    assert all(w.keep_start == w.start and w.keep_end == w.end for w in windows)


def test_plan_windows_overlaps_hard_cuts_and_splits_ownership() -> None:
    windows = plan_windows(1300.0, [], target=600, search=30, overlap=2)
    assert [(w.start, w.end, w.keep_start, w.keep_end, w.cut) for w in windows] == [
        (0.0, 602.0, 0.0, 601.0, "hard"),
        (598.0, 1200.0, 601.0, 1199.0, "hard"),  # every window is target + overlap long
        (1196.0, 1300.0, 1199.0, 1300.0, "end"),
    ]
    # Ownership tiles the recording: no gap, no double.
    for left, right in pairwise(windows):
        assert left.keep_end == right.keep_start


def test_plan_windows_short_recording_is_one_window() -> None:
    assert plan_windows(90.0, [(40.0, 41.0)]) == [Window(0, 0.0, 90.0, 0.0, 90.0, "end")]


def test_merge_segments_keeps_overlap_words_once() -> None:
    w0 = Window(0, 0.0, 602.0, 0.0, 601.0, "hard")
    w1 = Window(1, 598.0, 900.0, 601.0, 900.0, "end")
    both = Segment(599.5, 602.0, "사업비는 삼억 원입니다.")  # midpoint 600.75 → w0
    late = Segment(600.5, 602.0, "네.")  # midpoint 601.25 → w1
    merged = merge_segments(
        [
            (w0, [Segment(10.0, 12.0, "질의하겠습니다."), both, late, Segment(0, 1, "  ")]),
            (w1, [both, late, Segment(899.0, 901.0, "마치겠습니다.")]),
        ]
    )
    assert [s.text for s in merged] == [
        "질의하겠습니다.",
        "사업비는 삼억 원입니다.",
        "네.",
        "마치겠습니다.",  # the last window owns everything after its keep_start
    ]


MEMBER = Speaker("의원", "조우현")
OFFICIAL = Speaker("도로과장", "송태아")
CHAIR = Speaker("위원장", "박재신")

SEGMENTS = [
    Segment(0.5, 5.0, "작년 여름에 우동 지하차도가 잠길 뻔했잖습니까."),
    Segment(5.5, 9.5, "수위 센서 같은 거라도 달 계획이 있는지요."),
    Segment(
        12.5,
        17.5,
        "지하차도 3곳에 수위계와 자동 차단시설을 설치하는 사업을 이번 제1회 추경에 "
        "2억 4천만원 편성했습니다.",
    ),
    Segment(18.0, 21.5, "8월 말까지 설치를 마칠 계획입니다."),
    Segment(24.5, 30.0, "더 질의하실 위원님 안 계십니까?"),
]
CAPTIONS = [
    Caption(0.0, 11.0, MEMBER, "조우현 의원", 0.9),
    Caption(12.0, 23.0, OFFICIAL, "도로과장 송태아", 0.9),
    Caption(24.0, 30.0, CHAIR, "도시건설위원장 박재신", 0.9),
]
ANSWER = "2억 4천만원 편성했습니다"


def test_assemble_prints_minutes_that_split_turns_reads_back() -> None:
    t = assemble(SEGMENTS, CaptionTrack(CAPTIONS))
    assert t.text.splitlines() == [
        "○조우현 의원 작년 여름에 우동 지하차도가 잠길 뻔했잖습니까. "
        "수위 센서 같은 거라도 달 계획이 있는지요.",
        "○도로과장 송태아 지하차도 3곳에 수위계와 자동 차단시설을 설치하는 사업을 이번 "
        "제1회 추경에 2억 4천만원 편성했습니다. 8월 말까지 설치를 마칠 계획입니다.",
        "○위원장 박재신 더 질의하실 위원님 안 계십니까?",
    ]
    turns = split_turns(t.text)
    assert [(x.role, x.name, x.is_member, x.is_chair) for x in turns] == [
        ("의원", "조우현", True, False),
        ("도로과장", "송태아", False, False),
        ("위원장", "박재신", False, True),
    ]
    # The assembler's own turn spans agree with the parser's.
    assert [(x["start"], x["end"]) for x in t.turns] == [(x.start, x.end) for x in turns]
    assert [(x["t0"], x["t1"]) for x in t.turns] == [(0.5, 9.5), (12.5, 21.5), (24.5, 30.0)]
    # Question and answer stay one exchange, the chair's line is procedure.
    assert [c.kind for c in chunk_minutes(t.text)] == ["exchange", "procedure"]


def test_timeline_maps_every_segment_to_its_characters_and_seconds() -> None:
    t = assemble(SEGMENTS, CaptionTrack(CAPTIONS))
    assert len(t.timeline) == len(SEGMENTS)
    for entry, seg in zip(t.timeline, SEGMENTS, strict=True):
        assert t.text[entry["start"] : entry["end"]] == seg.text
        assert (entry["t0"], entry["t1"]) == (seg.start, seg.end)
    start = t.text.index(ANSWER)
    assert t.time_range(start, start + len(ANSWER)) == (12.5, 17.5)
    assert t.time_range(0, len(t.text)) == (0.5, 30.0)
    assert t.time_range(len(t.text), len(t.text) + 5) is None


def _answer_issue(text: str) -> str | None:
    check = locate_quote(text, ANSWER + ". 8월 말까지 설치를 마칠 계획입니다")
    assert check.found
    return official_evidence_issue(text, [check], char_start=0)


def test_an_answer_under_an_official_caption_is_executive_evidence() -> None:
    assert _answer_issue(assemble(SEGMENTS, CaptionTrack(CAPTIONS)).text) is None


def test_an_answer_nobody_could_attribute_goes_to_review() -> None:
    # No caption read at all (audio-only, OCR off, or an unreadable band): the same words are
    # not evidence that the executive committed to anything.
    text = assemble(SEGMENTS, None).text
    assert text.startswith("○발언자 미상 ")
    assert _answer_issue(text) == "official_evidence_missing"
    # Nor when only the member's band was read: the answer must not inherit the member.
    member_only = assemble(SEGMENTS, CaptionTrack(CAPTIONS[:1])).text
    assert _answer_issue(member_only) == "official_evidence_missing"


async def test_fixture_stt_serves_the_segments_a_window_overlaps(tmp_path: Path) -> None:
    path = tmp_path / "segments.json"
    path.write_text(json.dumps({"segments": [s.to_json() for s in SEGMENTS]}), encoding="utf-8")
    stt = FixtureSTT.from_file(path)
    got = await stt.transcribe(path, Window(1, 11.0, 23.0, 11.0, 23.0, "silence"), language="ko")
    assert [s.start for s in got] == [12.5, 18.0]


async def test_dir_checkpoints_round_trip_and_leave_no_temp_files(tmp_path: Path) -> None:
    store = DirCheckpoints(tmp_path / "ck")
    assert await store.get("stt:fixture:recorded:0.00-11.00") is None
    await store.put("stt:fixture:recorded:0.00-11.00", {"segments": [], "seconds": 0.1})
    assert await store.get("stt:fixture:recorded:0.00-11.00") == {"segments": [], "seconds": 0.1}
    assert [p.suffix for p in (tmp_path / "ck").iterdir()] == [".json"]


def test_media_cost_rounds_floats_only() -> None:
    cost = MediaCost(audio_seconds_sent=12.345678, ocr_frames=3)
    assert cost.to_json()["audio_seconds_sent"] == 12.3457
    assert cost.to_json()["ocr_frames"] == 3

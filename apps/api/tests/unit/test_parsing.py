import struct
import zlib

import pytest

from app.demo.synth import render_hwpx
from app.parsing.chunking import MAX_CHUNK_CHARS, chunk_budget, chunk_minutes, split_turns
from app.parsing.dispatch import decode_text, structured_to_text
from app.parsing.hwp import (
    HWPTAG_PARA_TEXT,
    HwpError,
    decode_para_text,
    extract_hwpx,
    iter_records,
    section_text,
)
from app.parsing.ocr_correct import LexiconCorrector, correct_ocr_text, fix_numbers
from app.sources.clik import html_to_text

MINUTES = """제301회 강남구의회 임시회
(10시 02분 개의)
○위원장 윤서준  의석을 정돈하여 주시기 바랍니다.
○위원 박지훈  스마트쉘터를 더 늘릴 계획이 있습니까?
○스마트도시과장 이정민  네, 내년도 본예산에 3억 5천만원을 반영하겠습니다.
추가로 말씀드리면 하반기에 발주할 예정입니다.
○위원 김현윤  단속 실적이 줄었다는데 이유가 뭡니까?
○교통행정과장 최민석  인력 두 명이 휴직 중이라 그렇습니다.
(12시 10분 산회)"""


def _record(tag: int, payload: bytes, level: int = 0) -> bytes:
    size = len(payload)
    if size < 0xFFF:
        return struct.pack("<I", tag | (level << 10) | (size << 20)) + payload
    return (
        struct.pack("<I", tag | (level << 10) | (0xFFF << 20)) + struct.pack("<I", size) + payload
    )


def test_split_turns_keeps_continuation_lines() -> None:
    turns = split_turns(MINUTES)
    assert [t.role for t in turns] == ["위원장", "위원", "스마트도시과장", "위원", "교통행정과장"]
    answer = turns[2]
    assert "하반기에 발주할 예정" in MINUTES[answer.start : answer.end]


def test_chunk_minutes_groups_question_with_answers() -> None:
    chunks = chunk_minutes(MINUTES)
    kinds = [c.kind for c in chunks]
    assert kinds == ["procedure", "exchange", "exchange"]
    first = chunks[1]
    assert "스마트쉘터" in first.text and "반영하겠습니다" in first.text
    assert MINUTES[first.char_start : first.char_end] == first.text
    assert first.labels == ["위원 박지훈", "스마트도시과장 이정민"]


def test_chunk_budget_attaches_basis_lines_and_department() -> None:
    text = (
        "2026년도 강남구 세출예산 사업명세서\n(단위: 천원)\n부서: 스마트도시과\n"
        "세부사업: 스마트쉘터 설치  352,000  0  352,000\n  ㅇ 스마트쉘터 7개소 × 50,000천원 = 350,000\n"
        "부서: 총무과\n세부사업: 업무추진비  51,555  48,977  2,578\n"
    )
    chunks = chunk_budget(text)
    assert len(chunks) == 2
    assert chunks[0].labels == ["부서: 스마트도시과"]
    assert "7개소" in chunks[0].text
    assert chunks[1].labels == ["부서: 총무과"]


# 성남시 2026년 제2회 추경 세출예산사업명세서 p.2 as pypdf reads it (trimmed, 2026-09-27): no
# "세부사업" label, and 부서·정책·단위사업 subtotals have the same shape as the 세부사업 rows.
SEONGNAM_2026_S2_PAGE = """부서ㆍ정책ㆍ단위(회계)ㆍ세부사업ㆍ편성목 예산액 기 정 액 비교증감
세 출 예 산 사 업 명 세 서
2026년도 추경 2 회 일반회계 전체
부서: 지역경제상권과
정책: 지역경제활성화
단위: 지역경제활성화 추진 (단위:천원)
지역경제상권과 42,735,345 42,060,345 675,000
국 15,226 15,226 0
도 204,711 204,711 0
시 42,515,408 41,840,408 675,000
지역경제활성화 18,840,950 18,165,950 675,000
국 15,226 15,226 0
시 18,707,863 18,032,863 675,000
지역경제활성화 추진 4,603,272 4,103,272 500,000
도 113,293 113,293 0
시 4,489,979 3,989,979 500,000
지역경제활성화 3,204,200 2,704,200 500,000
306 출연금 2,900,000 2,400,000 500,000
01 출연금 2,900,000 2,400,000 500,000
 ○성남시 소상공인 특례보증 출연금
2,900,000 2,400,000 500,000
상권 활성화 추진 14,186,376 14,011,376 175,000
상권활성화재단 출연 4,039,360 3,864,360 175,000
306 출연금 4,039,360 3,864,360 175,000
01 출연금 4,039,360 3,864,360 175,000
 ○성남시 상권활성화재단 출연금
4,039,360 3,864,360 175,000
 경정 2,900,000,000원
"""


def test_chunk_budget_reads_the_real_table_rows() -> None:
    chunks = chunk_budget(SEONGNAM_2026_S2_PAGE)
    # the two 세부사업 rows; the column header and the 부서·정책·단위 subtotals start no chunk
    assert [c.text.splitlines()[0] for c in chunks] == [
        "지역경제활성화 3,204,200 2,704,200 500,000",
        "상권활성화재단 출연 4,039,360 3,864,360 175,000",
    ]
    assert all(c.labels == ["부서: 지역경제상권과"] for c in chunks)
    # 구청 부서 names have a space: "부서: 분당구 건설과"
    assert chunk_budget("부서: 분당구 건설과\n" + SEONGNAM_2026_S2_PAGE.split("\n", 4)[4])[
        0
    ].labels == ["부서: 분당구 건설과"]
    assert "306 출연금" in chunks[1].text and "○성남시 상권활성화재단 출연금" in chunks[1].text


def test_chunk_budget_skips_the_tail_of_a_wrapped_basis_line() -> None:
    # 성남시 2026 제2회 추경: the "○…" basis line wraps, and its tail looks like a row
    text = (
        "고유가 피해지원금 63,037,861 0 63,037,861\n"
        "101 인건비 673,639 0 673,639\n"
        " ○자원안보위기 경보 에너지 민생안정 지원\n"
        " 사업 전담인력 673,639 0 673,639\n"
        "201 일반운영비 43,000 0 43,000\n"
    )
    assert [c.text.splitlines()[0] for c in chunk_budget(text)] == [
        "고유가 피해지원금 63,037,861 0 63,037,861"
    ]


def test_hwp5_record_parser_handles_controls_and_extended_size() -> None:
    text = "스마트쉘터 설치\t352,000"
    units = [ord(c) for c in text.replace("\t", "")]
    # inline tab control occupies 8 WCHARs; an extended control (code 11, a table) too.
    payload = struct.pack("<8H", 11, 0, 0, 0, 0, 0, 0, 11)
    payload += struct.pack(f"<{len('스마트쉘터 설치')}H", *[ord(c) for c in "스마트쉘터 설치"])
    payload += struct.pack("<8H", 9, 0, 0, 0, 0, 0, 0, 9)
    payload += struct.pack(f"<{len('352,000')}H", *[ord(c) for c in "352,000"])
    payload += struct.pack("<H", 13)
    big = "가" * 3000  # forces the 0xFFF extended size form
    stream = (
        _record(66, b"\x00" * 20)  # PARA_HEADER, ignored
        + _record(HWPTAG_PARA_TEXT, payload, level=1)
        + _record(HWPTAG_PARA_TEXT, struct.pack(f"<{len(big)}H", *[ord(c) for c in big]), level=1)
    )
    records = iter_records(stream)
    assert [r[0] for r in records] == [66, HWPTAG_PARA_TEXT, HWPTAG_PARA_TEXT]
    assert decode_para_text(records[1][2]) == "스마트쉘터 설치\t352,000\n"
    assert section_text(stream).splitlines()[0] == "스마트쉘터 설치\t352,000"
    assert len(units) > 0
    # And compressed streams decode the same way (raw deflate, wbits=-15).
    compressor = zlib.compressobj(wbits=-15)
    compressed = compressor.compress(stream) + compressor.flush()
    assert section_text(zlib.decompress(compressed, -15)) == section_text(stream)


def test_hwp5_truncated_stream_raises() -> None:
    with pytest.raises(HwpError):
        iter_records(struct.pack("<I", HWPTAG_PARA_TEXT | (100 << 20)) + b"\x00" * 10)


def test_hwpx_round_trip() -> None:
    text = "2026년도 예산서\n(단위: 천원)\n세부사업: 스마트폴 구축  334,000"
    assert extract_hwpx(render_hwpx(text)) == text


def test_fix_numbers_in_table_context() -> None:
    assert fix_numbers("스마트폴 구축 33O,OOO 0 l,806.000", table_context=True) == (
        "스마트폴 구축 330,000 0 1,806,000"
    )
    assert fix_numbers("사업비 3억 5천만윈") == "사업비 3억 5천만원"
    assert fix_numbers("3월 중 착수") == "3월 중 착수"  # a month is not money


def test_correct_ocr_text_fixes_ai_and_unit_header() -> None:
    corrector = LexiconCorrector(["스마트쉘터", "세부사업", "산출기초"])
    out = correct_ocr_text("(단 위 : 천 원)\n세부샤업: 스마트쉘티 설치 Al 기반", corrector)
    assert out.startswith("(단위: 천원)")
    assert "세부사업" in out and "스마트쉘터" in out and "AI 기반" in out


def test_lexicon_corrector_leaves_compounds_alone() -> None:
    corrector = LexiconCorrector(["스마트폴", "구축"])
    assert corrector.correct_token("스마트폴구축") == "스마트폴구축"
    assert corrector.correct_token("스마트풀을") == "스마트폴을"


def test_decode_text_falls_back_to_cp949() -> None:
    assert decode_text("예산서".encode("cp949")) == "예산서"


def test_structured_to_text_renders_amount() -> None:
    text = structured_to_text(
        "bid_notice", "스마트쉘터 구축사업", "서울특별시 강남구", {"amount_krw": 350_000_000}
    )
    assert "[입찰공고] 스마트쉘터 구축사업" in text
    assert "3억 5,000만원" in text


def test_html_to_text_keeps_speaker_lines() -> None:
    html = "<p>○위원 박지훈&nbsp; 질문입니다.<br>○과장 이정민 답변입니다.</p>"
    assert html_to_text(html).splitlines() == [
        "○위원 박지훈 질문입니다.",
        "○과장 이정민 답변입니다.",
    ]


# 성남시의회 제312회 예산결산특별위원회 제1차(2026.09.04.), HWP: members are printed with the name
# glued to 위원 ("○조우현위원"), officials as role then name ("○분당구청장 정상철").
SNCOUNCIL_MINUTES = (
    "○위원장 서은경  다음은 조우현 위원님 질의해 주시기 바랍니다.\n"
    "○조우현위원  우리 분당구청은 어때요? 분당구청 전동보장구 충전시설 이게 예산에 잡혀 있는 게 "
    "아니고 기설치돼 있나요, 다?\n"
    "○분당구청장 정상철  설치되어 있는 걸 제외하고 이번에 다 하는 겁니다. \n"
    "○조우현위원  이번에? \n"
    "○분당구청장 정상철  예.\n"
    "○전문위원 홍길동 보고드리겠습니다.\n"
)


def test_split_turns_reads_names_glued_to_the_member_role() -> None:
    turns = split_turns(SNCOUNCIL_MINUTES)
    assert [(t.role, t.name) for t in turns] == [
        ("위원장", "서은경"),
        ("위원", "조우현"),
        ("분당구청장", "정상철"),
        ("위원", "조우현"),
        ("분당구청장", "정상철"),
        ("전문위원", "홍길동"),  # one space after the role: a role, not a member's name
    ]
    chunks = chunk_minutes(SNCOUNCIL_MINUTES)
    assert [c.kind for c in chunks] == ["procedure", "exchange", "exchange"]
    assert chunks[1].labels == ["위원 조우현", "분당구청장 정상철"]
    assert "전동보장구 충전시설" in chunks[1].text and "다 하는 겁니다" in chunks[1].text


def test_a_long_speech_keeps_its_lead_line_with_what_follows() -> None:
    # 제307회 본회의 제1차: the piece ended on "주요사업비 예산 반영 내역입니다." and the list
    # itself started the next piece, without the line that says what it is.
    head = "○행정기획조정실장 주광호  "
    lead = "  주요사업비 예산 반영 내역입니다. \n"
    filler = "세입 예산안을 설명드리겠습니다. "
    n = (MAX_CHUNK_CHARS - len(head) - len(lead) - 10) // len(filler)
    speech = (
        head + filler * n + "\n" + lead + "  수내교 전면 개축공사 105억 원, 박물관 건립 168억 원, "
        "성남시 보훈회관 이전 건립 15억 원 등입니다.\n"
    )
    chunks = chunk_minutes(speech)
    assert len(chunks) == 2
    assert chunks[0].text.rstrip().endswith("주요사업비 예산 반영 내역입니다.")
    assert chunks[1].text.lstrip().startswith("주요사업비 예산 반영 내역입니다.")
    assert speech[chunks[1].char_start : chunks[1].char_end] == chunks[1].text

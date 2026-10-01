"""Rebuild the audit outputs from the archived public evidence and existing replay.

No network, database, paid model call, or production mutation is performed.
Human adjudications below are explicit; evidence absence is never a negative label.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPLAY = ROOT.parent / "historical-full-source-replay-v10.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(name, data):
    (ROOT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


replay = json.loads(REPLAY.read_text())
cases = [
    {
        "case_id": "new-blind-029",
        "project": "동복합문화센터 리모델링 공사 (신사동·세곡동)",
        "proposed_budget_krw": 1967852000,
        "input_pdf_pages": [60],
        "match_scope": "Two-site programme, including supervision; individual building/trade tenders are components, not the whole programme.",
        "replay_title": "동복합문화센터 리모델링 공사",
        "outcome_state": "both_sites_have_official_work_announcements_completion_unverified",
        "evidence": [
            {
                "source": "segok-official.html",
                "displayed_post_date": "2025-08-12",
                "kind": "planned_work_notice",
                "fact": "Official library notice announces Segok complex remodeling 2025-09-01 to 2025-11-30, with restroom works 2025-09-22 to 2025-11-21.",
                "proves": "Work announcement for Segok component only.",
                "does_not_prove": "Tender publication, award, actual commencement, completion, or programme expenditure.",
            },
            {
                "source": "sinsa-official.html",
                "displayed_post_date": "2025-07-21",
                "kind": "amended_planned_work_notice",
                "fact": "Official library notice announces Sinsa complex floors 5 and 6 remodeling. Current amended content gives works 2025-09-29 to 2025-12-31 and closure through 2026-01-02.",
                "amendment_date": None,
                "proves": "Official announcement covers the second site and identifies its floors.",
                "does_not_prove": "Current amended schedule was already public on 2025-07-21; actual completion or tender identity.",
            },
            {
                "source": "sinsa-reopening.html",
                "poster": "sinsa-reopening-poster.jpg",
                "displayed_post_date": "2025-12-23",
                "kind": "reopening_announcement",
                "announced_event_at": "2026-01-05T10:00:00+09:00",
                "fact": "Sinsa resident center announces reopening of its fifth-floor library on 2026-01-05 at 10:00. The original poster was visually read.",
                "proves": "A separate official reopening announcement consistent with the earlier closure.",
                "does_not_prove": "Actual reopening, completion of all floors or trades, procurement identity, or completed implementation within 2025.",
            },
        ],
        "secondary_tender_leads": [
            {
                "id": "R25BK00985683-000",
                "title": "세곡동복합문화센터 리모델링 공사 (건축, 기계)",
                "reported_post_date": "2025-07-30",
                "reported_gross_estimate_krw": 520386000,
                "lead_url": "https://seenthis.kr/bid/297115",
                "official_candidate_url": "https://www.g2b.go.kr/link/PNPE027_01/single/?bidPbancNo=R25BK00985683&bidPbancOrd=000",
            },
            {
                "id": "R25BK00986354-000",
                "title": "세곡동복합문화센터 리모델링 공사 (전기)",
                "reported_post_date": "2025-07-30",
                "reported_gross_estimate_krw": 97320000,
                "lead_url": "https://seenthis.kr/bid/297180",
            },
            {
                "id": "R25BK01008684-000",
                "title": "세곡동복합문화센터 화장실 환경개선공사",
                "reported_post_date": "2025-08-13",
                "reported_gross_estimate_krw": 205493000,
                "lead_url": "https://seenthis.kr/bid/312331",
            },
            {
                "id": "R25BK01034655-000",
                "title": "신사동복합문화센터 리모델링 공사 (건축, 기계)",
                "lead_url": "https://www.bidpro.co.kr/n0_ipchal/konggo/sisul/konggo_view.asp?num=3653802",
            },
        ],
    },
    {
        "case_id": "new-blind-030",
        "project": "일원1동 스마트보안등 시스템 구축공사",
        "proposed_budget_krw": 300000000,
        "input_pdf_pages": [111, 112],
        "match_scope": "Ilwon1/Daecheong Park residential streets only; 713 lights is the three-area total, not the target quantity.",
        "replay_title": "일원1동 스마트보안등 시스템 구축공사",
        "outcome_state": "official_implementation_report_tender_unverified",
        "evidence": [
            {
                "source": "lights-official.html",
                "displayed_post_date": "2025-08-08",
                "kind": "official_implementation_report",
                "fact": "District reports 287 smart security lights installed in Ilwon1, among 713 newly installed across Ilwon1, Sinsa and Samsung2; 3,209 total operating district-wide.",
                "target_installed_units": 287,
                "three_area_new_units": 713,
                "district_total_units": 3209,
                "proves": "Official report of target-area implementation during 2025.",
                "does_not_prove": "Original tender ID/date, contracted spend, exact completion day, or causal forecast skill.",
            }
        ],
        "secondary_tender_leads": [
            {
                "id": None,
                "title": "일원1동 스마트보안등 시스템 구축공사",
                "lead_url": "https://www.sankun.com/bid-info/frQfmIHF4O3PP6r7QXNfFoBlQhKISKacdPNR8f-U_7k",
                "note": "Search discovery only; not primary procurement proof.",
            }
        ],
    },
    {
        "case_id": "new-blind-031",
        "project": "스마트 빗물펌프장 원격제어 고도화 사업",
        "proposed_budget_krw": 145000000,
        "input_pdf_pages": [128],
        "match_scope": "New capital upgrade of aging small-pump-station remote control, including manufacture/installation of one RTU panel.",
        "replay_title": "스마트 빗물펌프장 원격제어 고도화 사업",
        "outcome_state": "unresolved_not_negative",
        "evidence": [],
        "secondary_tender_leads": [],
        "rejected_near_matches": [
            {
                "source": "water-audit-minutes.html",
                "meeting_date": "2025-11-24",
                "fact": "Audit discusses a seven-month remote-monitoring/control maintenance service ending 2025-11-15, approximately KRW133m.",
                "reason": "Operating/maintenance service differs from the predeclared new RTU capital upgrade; no explicit link establishes equivalence.",
            },
            {
                "source": "water-outcome-report.pdf",
                "pages": 10,
                "reason": "Selective major-project report has no identified target upgrade entry; it is not an exhaustive procurement register, so absence is not evidence of non-occurrence.",
            },
            {
                "url": "https://subang.gangnam.go.kr/",
                "reason": "Smart flood-information website is a different budget project (KRW55m in the source), not proof of the KRW145m RTU upgrade.",
            },
        ],
    },
]

for case in cases:
    replay_title = case.pop("replay_title")
    matches = [r for r in replay["signals"] if r["signal"]["title"] == replay_title]
    case["replay"] = {
        "status": matches[0]["verdict"] if matches else "not_emitted",
        "matching_signals": matches,
        "auto_accepted": any(r["verdict"] == "accepted" for r in matches),
    }
    case.update(
        original_tender_verified=False,
        original_tender_id=None,
        original_tender_publication_date=None,
        confirmed_contract_amount_krw=None,
        forecast_score_eligible=False,
        verified_lead_days=None,
        non_occurrence_proven=False,
    )

findings = {
    "schema_version": 1,
    "review_date_utc": "2026-10-01",
    "study_type": "retrospective_public_evidence_adjudication_with_current_code_replay",
    "cohort": {
        "declaration": "../historical-followup.json",
        "case_ids": [c["case_id"] for c in cases],
        "count": 3,
        "inclusion": "All three cases declared before this primary-source validation are retained. They were already seen by developers; this is not an unseen frozen forecast cohort.",
    },
    "horizon": {
        "target_fiscal_year": 2025,
        "target_start_inclusive": "2025-01-01T00:00:00+09:00",
        "target_end_exclusive": "2026-01-01T00:00:00+09:00",
        "retrospective_retrieval_date_utc": "2026-10-01",
        "was_prospectively_registered": False,
        "rule": "Classify dated reports of 2025 activity. Later events remain separate; search is not an exhaustive horizon-wide outcome census.",
    },
    "input": {
        "url": "https://www.gncouncil.go.kr/attach/council_text/323/323_3_20.pdf",
        "archive": "budget-report-current.pdf",
        "sha256": sha(ROOT / "budget-report-current.pdf"),
        "parsed_text": "budget-report-current.txt",
        "parsed_text_sha256": sha(ROOT / "budget-report-current.txt"),
        "byte_identical_to_preexisting_pdf": True,
        "parsed_text_identical_to_replay_input": True,
        "report_date": "2024-12-17",
        "pdf_creation_and_modification_at": "2024-12-19T08:41:47+00:00",
        "http_last_modified": "2024-12-19T08:47:35+00:00",
        "historical_public_availability_date": None,
        "historical_public_availability_verified": False,
        "metadata_caution": "Report/meeting dates, PDF creation and current Last-Modified are provenance clues, not proof of public availability on that date.",
        "budget_semantics": "Budget proposal in a review report; not a procurement estimate, award amount, or verified final project appropriation.",
        "bill_provenance": {
            "archive": "council-budget-bill.html",
            "bill_number": 432,
            "introduced": "2024-11-04",
            "committee_decision": "2024-12-17",
            "plenary_decision": "2024-12-19",
            "decision": "수정가결",
            "upload_timestamp_found": False,
        },
        "alternative_publication_record": {
            "archive": "gangnam-budget-page2.html",
            "title": "2025년 강남구 예산서",
            "displayed_post_date": "2025-01-21",
            "attachment_url": "https://www.gangnam.go.kr/file/1/get/02a54cd5-102a-4082-b6dc-37aeb728b39c/download.do",
            "attachment_fetched": False,
            "reason": "District robots disallows /file/*, including download and preview. Attachment content equivalence was not checked; this date is not assigned to the council input or used for lead days.",
        },
    },
    "replay": {
        "artifact": "../historical-full-source-replay-v10.json",
        "sha256": sha(REPLAY),
        "executed_at": replay["executed_at"],
        "code_revision": replay["code_revision"],
        "extractor": replay["extractor"],
        "chunks": replay["chunks"],
        "triage_passed": replay["triage_passed"],
        "full_document_signals": len(replay["signals"]),
        "future_outcome_data_passed_to_extractor": replay["future_outcome_data_passed_to_extractor"],
        "interpretation": "Current code applied to old archived input; not an actual prediction made in 2024. Outcome searching occurred after source/model inspection.",
        "previous_replay": {
            "artifact": "../historical-full-source-replay-v9.json",
            "cohort_emitted": 2,
            "cohort_needs_review": 2,
            "cohort_auto_accepted": 0,
            "cohort_not_emitted": 1,
            "change": "v10 handles formfeed section boundaries; the previously missing two-site remodeling programme now appears. This is a code-development comparison on an already-seen source, not new independent validation.",
        },
    },
    "cases": cases,
    "descriptive_counts_not_accuracy": {
        "retained_cases": 3,
        "replay_emitted_total": 3,
        "replay_emitted_for_review": 2,
        "replay_auto_accepted": 1,
        "replay_not_emitted": 0,
        "official_target_implementation_reports": 1,
        "programme_with_both_site_work_announcements": 1,
        "target_outcome_unresolved": 1,
        "cases_with_primary_tender_record": 0,
        "proven_negative_outcomes": 0,
    },
    "forecast_metrics": {
        "scorable_forecasts": 0,
        "precision": None,
        "recall": None,
        "verified_lead_days": None,
        "reason": "No original primary tender records, unverified historical input availability, incomplete outcome census, and non-blind retrospective selection prevent forecast scoring.",
    },
    "limitations": [
        "No complete municipal/G2B tender or award register was retrieved.",
        "Contract Seoul's current robots excludes contract and order-plan paths; those paths were not automated after discovering the prohibition.",
        "G2B direct-link retrieval and robots retrieval were unavailable (gateway/timeouts); no evasion or alternate blocked path was used.",
        "District notice title searches are bounded queries, not exhaustive searches of attachments, alternate names or contract systems.",
        "Secondary tender indexes remain candidate discovery, not primary outcome evidence; their dates/amounts are unverified reports.",
        "Programmes can split into sites, construction trades, materials and supervision. No sum of discovered components is claimed to exhaust the programme.",
        "A planned work or reopening notice is not a completion certificate. A later reopening event is outside the 2025 target year.",
        "Source report does not prove final adopted line-item amounts; the overall budget bill was modified before approval.",
    ],
}
write("findings.json", findings)

searches = {
    "scope": "Material search routes; not an exhaustive search-engine transcript or outcome census.",
    "date_utc": "2026-10-01",
    "routes": [
        {"route": "Official council bill and plenary attachment navigation", "artifacts": ["council-budget-bill.html", "council-budget-plenary.html", "budget-report-public-viewer-lookup.json"], "result": "Provenance and meeting dates; no upload timestamp."},
        {"route": "Official Gangnam budget listing pages1-2", "artifacts": ["gangnam-budget-list.html", "gangnam-budget-page2.html"], "result": "Alternative budget book posted2025-01-21; attachment path excluded by robots."},
        {"route": "District notice title search", "queries": ["원격", "스마트보안등", "복합문화센터"], "artifacts": ["notice-pump.html", "notice-lights.html", "notice-remodel.html"], "result": "No target records in these responses; not a negative procurement finding."},
        {"route": "Current district contract menu", "artifact": "lights-official.html", "result": "Links lead to contract.seoul.go.kr/new1/views/contractInfo.do and orderPlan.do, and G2B single-link searches. Seoul robots prohibits relevant paths."},
        {"route": "Archived district opendoc order link", "artifact": "gangnam-opendoc-order.meta.json", "result": "HTTP404; not a target outcome test."},
        {"route": "G2B tender single-link and robots", "artifact": "g2b-robots.meta.json", "result": "Gateway/timeouts; no original tender obtained."},
        {"route": "Official 2025-11-24 water audit minutes and linked work report", "artifacts": ["water-audit-minutes.html", "water-outcome-report.pdf"], "result": "Maintenance contract is not the target upgrade. Selected-programme report cannot establish absence."},
        {"route": "Search engines1 and2, exact titles and aliases", "queries": ["강남구 스마트 빗물펌프장2025", "강남구 원격제어 고도화 계약", "일원1동 스마트보안등 시스템 구축공사 입찰", "세곡동복합문화센터 리모델링", "신사동 주민도서관 재개관"], "result": "Official notices plus secondary tender leads. Final conclusions depend on archived official bytes."},
    ],
}
write("search-log.json", searches)

# Verify all archived successful response hashes before building a manifest.
retrievals = []
for meta in sorted(ROOT.glob("*.meta.json")):
    record = json.loads(meta.read_text())
    if record.get("file"):
        target = ROOT / record["file"]
        assert target.is_file(), (meta, target)
        assert sha(target) == record["sha256"], meta
    retrievals.append({"metadata": meta.name, **record})

files = [
    {"file": f.name, "bytes": f.stat().st_size, "sha256": sha(f)}
    for f in sorted(ROOT.iterdir())
    if f.is_file() and f.name != "manifest.json"
]
write("manifest.json", {
    "review_date_utc": "2026-10-01",
    "hash_algorithm": "sha256",
    "request_policy": "Individual public requests; no login, paid calls or permission bypass; cookie response headers omitted from metadata.",
    "retrievals": retrievals,
    "initial_robots_snapshots": [
        {"file": "robots-www.gncouncil.go.kr.txt", "url": "https://www.gncouncil.go.kr/robots.txt", "access_date_utc": "2026-10-01", "exact_access_time_recorded": False},
        {"file": "robots-www.gangnam.go.kr.txt", "url": "https://www.gangnam.go.kr/robots.txt", "access_date_utc": "2026-10-01", "exact_access_time_recorded": False},
        {"file": "robots-library.gangnam.go.kr.txt", "url": "https://library.gangnam.go.kr/robots.txt", "access_date_utc": "2026-10-01", "exact_access_time_recorded": False},
    ],
    "parsing": {
        "pdf": "pdftotext -layout; page indices are 1-based; budget parsed text is byte-identical to archived replay input. Page128 was rendered with pdftoppm and visually checked for the separate KRW55m/145m projects and RTU-panel scope; qa-budget-pump-page128.png preserves that view.",
        "water_pdf_warning": "Poppler warned Invalid Font Weight / Can't get Fields array but extracted23984bytes of readable text across10pages. It is used only as a reviewed selective report, not as complete procurement data.",
        "html": "UTF-8 public HTML preserved; selected dates/headings/body read directly. water-audit-minutes.txt removes tags and unescapes entities, with no inference of absent rows.",
        "image": "sinsa-reopening-poster.jpg visually inspected; transcription in findings.json.",
        "failed_lookup_files": "council-*-report-lookup.json are error HTML despite the json filename/content-type; only *-public-viewer-lookup.json contain successful JSON mappings.",
    },
    "files": files,
})
assert len(cases) == 3
assert sum(bool(c["replay"]["matching_signals"]) for c in cases) == 3
assert sum(c["replay"]["auto_accepted"] for c in cases) == 1
assert sum(c["replay"]["status"] == "needs_review" for c in cases) == 2
for case in cases:
    for signal in case["replay"]["matching_signals"]:
        assert signal["stored_budget_krw"] == case["proposed_budget_krw"]
        assert signal["stored_expected_year"] == 2025
assert sha(ROOT / "budget-report-current.txt") == replay["source_sha256"]
print("Validated 3 retained cases, 3 v10 emissions, 2 needs_review, 1 accepted, correct amounts/year, 0 scorable; archived response hashes checked.")

import type { Schemas } from "@/lib/api/client";

export function relationSignal(id: number, stage: string): Schemas["SignalOut"] {
  const text = stage === "budget_line" ? "도서관 개선 예산을 편성합니다." : "도서관 냉난방기를 구매합니다.";
  return {
    id, stage, category: "facility", stage_label: stage === "budget_line" ? "예산 편성" : "입찰공고",
    observed_at: "2026-09-29", title: `근거 신호 ${id}`, summary: text, department: null,
    budget_krw: null, expected_year: null, expected_half: null, commitment: "committed",
    confidence: 0.99, verdict: "accepted", extractor: "test", context: text, context_offset: 0,
    evidence: [{ quote: text, found: true, score: 100, start: 0, end: text.length, method: "exact" }],
    document: { id, title: `원문 문서 ${id}`, doc_type: stage === "budget_line" ? "budget_book" : "bid_notice", url: `https://example.org/doc/${id}`, publisher_raw: "성남시", parse_method: "plain", published_at: "2026-09-29" },
    link: { method: "seed", score: 1, tentative: false, reasons: {} },
  };
}

export function relationOpportunity(id: number): Schemas["OpportunityDetail"] {
  const project = id === 1;
  return {
    id, title: project ? "도서관 개선 사업" : `냉난방기 구매 ${id}`,
    institution: { code: "LG-41130", name: "성남시" }, department: null,
    category: "facility", category_label: "시설", stage: project ? "budget_line" : "bid_notice",
    stage_label: project ? "예산 편성" : "입찰공고", status: "open", est_budget_krw: null,
    bid_window_start: null, bid_window_end: null, window_passed: false, bid_published_at: null,
    tender_out: !project, conversion_prob: 0.5, signal_count: 1, first_seen_at: "2026-09-29",
    last_signal_at: "2026-09-29", score: null, reasons: [], feedback: null, lead_days: null,
    keywords: [], best_commitment: "committed", signals: [relationSignal(project ? 10 : 20, project ? "budget_line" : "bid_notice")],
    budget_trajectory: [], breakdown: null, briefs: [],
  };
}

export const relationFixture: Schemas["RelationOut"] = {
  id: 7, kind: "project_contract", project_id: 1, contract_id: 2,
  status: "confirmed", effective_status: "stale", valid: false, validity_reasons: ["evidence_changed"],
  version: 3, evidence_signal_ids: [10, 20], note: "원문 대조 기록", updated_by: 9,
  created_at: "2026-09-29T01:00:00Z", updated_at: "2026-09-29T02:00:00Z",
  project: { id: 1, title: "도서관 개선 사업", institution_code: "LG-41130", stage: "budget_line" },
  contract: { id: 2, title: "냉난방기 구매 2", institution_code: "LG-41130", stage: "bid_notice" },
  evidence: [{ signal_id: 10, signal_title: "근거 신호 10", stage: "budget_line", opportunity_id: 1,
    document_id: 10, document_title: "원문 문서 10", document_url: "https://example.org/doc/10",
    document_content_hash: "original-content", source_text_sha256: "source-text", fingerprint: "fingerprint",
    evidence: [{ quote: "모델 인용", source_quote: "저장 당시 실제 원문", start: 0, end: 12 }],
  }],
  history: [{ version: 3, status: "confirmed", note: "원문 대조 기록", actor_user_id: 9,
    actor_name: "검토자", created_at: "2026-09-29T02:00:00Z", evidence_signal_ids: [10, 20], evidence: [],
  }],
};

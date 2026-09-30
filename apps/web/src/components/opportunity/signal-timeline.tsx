import { ExternalLink, FileText, Link2, ScanLine } from "lucide-react";

import { Badge } from "@/components/ui/primitives";
import type { Schemas } from "@/lib/api/client";
import { formatDate, formatKRW } from "@/lib/format";
import { DEMO_STATIC } from "@/lib/demo/fetch";
import { COMMITMENT_LABEL, stageIndex } from "@/lib/utils";

import { EvidenceContext } from "./evidence";
import { DemoSource } from "./demo-source";

type Signal = Schemas["SignalOut"];

const DOC_TYPE_LABEL: Record<string, string> = {
  council_minutes: "지방의회 회의록",
  budget_book: "세출예산서",
  order_plan: "나라장터 발주계획",
  prespec: "나라장터 사전규격",
  bid_notice: "나라장터 입찰공고",
  award: "낙찰 정보",
};

const PARSE_LABEL: Record<string, string> = {
  ocr: "스캔본 OCR",
  mixed: "일부 OCR",
  hwpx: "HWPX",
  hwp5: "HWP",
  text_layer: "PDF",
  structured: "Open API",
  plain: "텍스트",
  html: "HTML",
};

function linkLabel(link: Signal["link"]): string | null {
  if (!link) return null;
  if (link.method === "seed") return "처음 잡힌 신호";
  if (link.method === "ref") return "공고 번호로 이어짐";
  const pct = Math.round(link.score * 100);
  return `사업명 유사도 ${pct}점으로 이어짐${link.tentative ? " (확인 중)" : ""}`;
}

export function SignalTimeline({ signals }: { signals: Signal[] }) {
  return (
    <ol className="relative space-y-6 border-l border-line pl-6">
      {signals.map((s) => {
        const idx = stageIndex(s.stage);
        return (
          <li key={s.id} className="relative">
            <span
              className="absolute top-1 -left-[31px] size-3 rounded-full ring-4 ring-surface"
              style={{ background: `var(--stage-${Math.min(idx + 1, 6)})` }}
              aria-hidden
            />
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="tabular text-[13px] font-semibold text-ink">{formatDate(s.observed_at)}</span>
              <Badge tone="accent">{s.stage_label}</Badge>
              {s.commitment && s.stage === "council_mention" ? (
                <Badge tone={s.commitment === "committed" ? "good" : s.commitment === "declined" ? "critical" : "neutral"}>
                  {COMMITMENT_LABEL[s.commitment] ?? s.commitment}
                </Badge>
              ) : null}
              {s.budget_krw ? <span className="text-[13px] text-ink-2">{formatKRW(s.budget_krw)}</span> : null}
              {s.expected_year ? (
                <span className="text-[13px] text-muted">
                  · {s.expected_year}년{s.expected_half ? (s.expected_half === "H1" ? " 상반기" : " 하반기") : ""}
                </span>
              ) : null}
            </div>
            <p className="mt-1 text-[14px] font-medium text-ink">{s.title}</p>
            <div className="mt-2">
              <EvidenceContext context={s.context} contextOffset={s.context_offset} evidence={s.evidence} />
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-muted">
              <span className="inline-flex items-center gap-1">
                <FileText className="size-3.5" aria-hidden />
                {DOC_TYPE_LABEL[s.document.doc_type] ?? s.document.doc_type} · {s.document.title}
              </span>
              {s.document.parse_method ? (
                <span className="inline-flex items-center gap-1">
                  <ScanLine className="size-3.5" aria-hidden />
                  {PARSE_LABEL[s.document.parse_method] ?? s.document.parse_method}
                </span>
              ) : null}
              {linkLabel(s.link) ? (
                <span className="inline-flex items-center gap-1">
                  <Link2 className="size-3.5" aria-hidden />
                  {linkLabel(s.link)}
                </span>
              ) : null}
              <span>추출: {s.extractor.split(":").slice(0, 2).join(" ")}</span>
              {!DEMO_STATIC && s.document.url ? (
                <a
                  href={s.document.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-accent-text hover:underline"
                >
                  원문 <ExternalLink className="size-3" aria-hidden />
                </a>
              ) : null}
            </div>
            {DEMO_STATIC ? <DemoSource documentId={s.document.id} evidence={s.evidence} /> : null}
          </li>
        );
      })}
    </ol>
  );
}

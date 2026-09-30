import { Radar } from "lucide-react";

import type { Schemas } from "@/lib/api/client";
import { formatDate, formatMonth, headStartLabel } from "@/lib/format";
import { DEMO_STATIC } from "@/lib/demo/fetch";

const SOURCE: Record<string, string> = {
  council_minutes: "회의록",
  budget_book: "세출예산서",
  order_plan: "발주계획",
  prespec: "사전규격",
  bid_notice: "입찰공고",
};

/** The one thing this product does that a bid-alert service can't: say how early it knew. */
export function HeadStart({ detail }: { detail: Schemas["OpportunityDetail"] }) {
  const label = headStartLabel(detail.head_start_days);
  if (!label) return null;
  const first = [...detail.signals].sort((a, b) => a.observed_at.localeCompare(b.observed_at))[0];
  const published = Boolean(detail.bid_published_at);
  const source = first
    ? [first.document.publisher_raw, SOURCE[first.document.doc_type] ?? first.stage_label].filter(Boolean).join(" ")
    : null;

  return (
    <div className="flex items-center gap-4 rounded-xl border border-accent/25 bg-accent-soft px-5 py-4">
      <div className="shrink-0 rounded-lg bg-surface px-3.5 py-2 text-center shadow-card">
        <div className="text-[11px] text-muted">{published ? "공고보다" : "예상보다"}</div>
        <div className="text-[22px] leading-tight font-bold tracking-tight text-accent-text">{label}</div>
        <div className="text-[11px] text-muted">먼저</div>
      </div>
      <div className="min-w-0">
        <p className="flex items-center gap-1.5 text-[15px] font-semibold text-ink">
          <Radar className="size-4 shrink-0 text-accent-text" aria-hidden />
          {DEMO_STATIC ? "예시 일정: " : ""}
          {published
            ? `입찰공고가 나오기 ${label} 전에 먼저 찾아낸 사업이에요`
            : `입찰이 예상되는 때보다 ${label}쯤 앞서 찾아낸 사업이에요`}
        </p>
        {DEMO_STATIC ? <p className="mt-1 text-[12px] text-muted">가상 문서의 날짜로 계산한 예시예요. 실제 선행기간을 검증한 성과가 아니에요.</p> : null}
        <p className="mt-1 text-[13px] leading-relaxed text-ink-2">
          {first ? `처음 잡힌 건 ${formatDate(first.observed_at)}${source ? `, ${source}` : ""}에서였어요. ` : null}
          {published
            ? `입찰공고는 ${formatDate(detail.bid_published_at)}에 나왔어요.`
            : `입찰은 ${formatMonth(detail.bid_window_start)}쯤으로 보고 있어요.`}
        </p>
      </div>
    </div>
  );
}

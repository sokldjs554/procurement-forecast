import { Building2, CalendarClock, Coins, Layers } from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/primitives";
import type { Schemas } from "@/lib/api/client";
import { formatDate, formatKRW, formatPercent, formatWindow, headStartLabel, leadLabel } from "@/lib/format";
import { STATUS_LABEL } from "@/lib/utils";
import { DEMO_STATIC } from "@/lib/demo/fetch";

import { StageRail } from "./stage-rail";

type Card = Schemas["OpportunityCard"];

export function OpportunityCard({ item }: { item: Card }) {
  const lead = leadLabel(item.lead_days);
  // Only once the tender is out: then it is a fact, and it doesn't compete with the
  // "입찰 약 N개월 후" countdown that pre-tender cards already show.
  const headStart = item.tender_out ? headStartLabel(item.head_start_days) : null;
  return (
    <Link
      href={`/app/opportunities/${item.id}`}
      className="group block rounded-xl border border-line bg-surface p-5 shadow-card transition-colors hover:border-line-strong"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone={item.status === "bid_open" ? "warning" : "accent"}>{item.stage_label}</Badge>
            <Badge>{item.category_label}</Badge>
            {item.status === "bid_open" ? <Badge tone="warning">{STATUS_LABEL.bid_open}</Badge> : null}
            {lead ? <Badge tone="outline">입찰 {lead}</Badge> : null}
            {item.window_passed ? <Badge tone="outline">예상 시기 지남</Badge> : null}
            {headStart ? <Badge tone="good">{DEMO_STATIC ? "예시: " : ""}공고 {headStart} 전에 찾음</Badge> : null}
          </div>
          <h3 className="mt-2 text-[16px] leading-snug font-semibold text-ink group-hover:text-accent-text">
            {item.title}
          </h3>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-ink-2">
            <span className="inline-flex items-center gap-1">
              <Building2 className="size-3.5 text-muted" aria-hidden />
              {item.institution.name}
              {item.department ? <span className="text-muted">· {item.department}</span> : null}
            </span>
            <span className="inline-flex items-center gap-1">
              <Coins className="size-3.5 text-muted" aria-hidden />
              {formatKRW(item.est_budget_krw)}
            </span>
            <span className="inline-flex items-center gap-1">
              <CalendarClock className="size-3.5 text-muted" aria-hidden />
              {item.tender_out
                ? `입찰공고 ${formatDate(item.bid_published_at)}`
                : `입찰 예상 ${formatWindow(item.bid_window_start, item.bid_window_end)}`}
            </span>
            <span className="inline-flex items-center gap-1">
              <Layers className="size-3.5 text-muted" aria-hidden />
              신호 {item.signal_count}건
            </span>
          </div>
        </div>
        {item.score !== null ? (
          <div className="shrink-0 text-right">
            <div className="text-[11px] text-muted">적합도</div>
            <div className="text-[22px] font-semibold tracking-tight text-ink">
              {Math.round(item.score * 100)}
            </div>
          </div>
        ) : null}
      </div>
      <div className="mt-4">
        <StageRail stage={item.stage} compact />
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <ul className="flex flex-wrap gap-1.5">
          {item.reasons.slice(0, 4).map((r) => (
            <li key={r} className="rounded-md bg-surface-2 px-2 py-0.5 text-[12px] text-ink-2">
              {r}
            </li>
          ))}
        </ul>
        {/* once the tender is out there is no probability left to show */}
        {item.tender_out ? null : (
          <span className="text-[12px] text-muted">
            공고 전환 추정치{DEMO_STATIC ? " (데모)" : ""} <span className="font-semibold text-ink">{formatPercent(item.conversion_prob)}</span>
          </span>
        )}
      </div>
    </Link>
  );
}

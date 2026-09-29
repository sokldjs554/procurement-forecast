"use client";

import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Badge, Card, CardHeader, ErrorNote, Skeleton } from "@/components/ui/primitives";
import type { Schemas } from "@/lib/api/client";
import { useOpportunityRelations, type Relation } from "@/lib/api/relations";
import { DEMO_STATIC } from "@/lib/demo/fetch";
import { formatDateTime } from "@/lib/format";

export const RELATION_STATUS_LABEL = { proposed: "제안", confirmed: "확정", rejected: "기각", stale: "근거 변경 · 재검토 필요" };

const REASON_LABEL: Record<string, string> = {
  evidence_changed: "판단 당시와 현재의 원문 또는 신호가 달라졌어요.",
  endpoint_missing: "관계의 사업 또는 계약 기회를 찾을 수 없어요.",
  self_relation: "같은 기회를 사업과 계약으로 지정할 수 없어요.",
  institution_mismatch: "양쪽 수요 기관이 다르거나 확인되지 않았어요.",
  evidence_ids_invalid: "중복 없이 2~20개의 근거 신호를 선택해 주세요.",
  missing_evidence: "기존 근거 신호를 찾을 수 없어요.",
  evidence_not_eligible: "승인되고 연결이 확정된 신호가 필요해요.",
  evidence_institution_mismatch: "근거 신호의 수요 기관이 일치하지 않아요.",
  evidence_stage_mismatch: "근거 신호와 원문 문서의 단계가 일치하지 않아요.",
  evidence_wrong_endpoint_or_stage: "사업·계약의 역할에 맞는 근거 신호를 선택해 주세요.",
  both_endpoint_roles_required: "사업 쪽과 계약 쪽의 근거를 각각 선택해 주세요.",
  source_unavailable: "원문이 없거나 처리가 완료되지 않았어요.",
  ungrounded_evidence: "인용과 원문 위치를 확인할 수 없어요.",
  relation_cycle: "관계가 순환하게 되어 저장할 수 없어요.",
  relation_missing: "아직 저장되지 않은 관계는 기각할 수 없어요.",
};

export function relationReasonLabel(reason: string): string {
  const [code = "", signalId] = reason.split(":");
  const label = REASON_LABEL[code] ?? "관계 근거를 다시 확인해 주세요.";
  return signalId ? `${label} (신호 #${signalId})` : label;
}

export function RelationEvidence({ evidence }: { evidence: Schemas["RelationEvidenceOut"][] }) {
  return (
    <div className="space-y-3">
      {evidence.map((item) => (
        <div key={item.signal_id} className="rounded-lg border border-line bg-surface-2/40 p-3">
          <p className="text-[13px] font-medium">{item.signal_title} <span className="text-muted">· 신호 #{item.signal_id}</span></p>
          {item.evidence.map((quote, index) => (
            <blockquote key={index} className="mt-2 border-l-2 border-line-strong pl-3 text-[13px] leading-6 whitespace-pre-wrap text-ink-2">{quote.source_quote}</blockquote>
          ))}
          <p className="mt-2 text-[12px] text-muted">
            {item.document_title}
            {item.document_url && /^https?:\/\//i.test(item.document_url) ? (
              <> · <a href={item.document_url} target="_blank" rel="noreferrer" className="text-accent-text hover:underline">원문 열기</a></>
            ) : null}
          </p>
        </div>
      ))}
    </div>
  );
}

export function RelationSummary({ relation }: { relation: Relation }) {
  const stale = relation.effective_status === "stale";
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={stale ? "warning" : relation.effective_status === "confirmed" ? "good" : "neutral"}>
          {RELATION_STATUS_LABEL[relation.effective_status]}
        </Badge>
        <span className="text-[12px] text-muted">관계 #{relation.id} · 버전 {relation.version}</span>
      </div>
      <div className="grid gap-2 text-sm sm:grid-cols-2">
        <p><span className="mr-2 text-muted">사업</span><Link className="text-accent-text hover:underline" href={`/app/opportunities/${relation.project_id}`}>{relation.project?.title ?? `기회 #${relation.project_id}`}</Link></p>
        <p><span className="mr-2 text-muted">계약</span><Link className="text-accent-text hover:underline" href={`/app/opportunities/${relation.contract_id}`}>{relation.contract?.title ?? `기회 #${relation.contract_id}`}</Link></p>
      </div>
      {stale ? <p className="text-[13px] text-muted">저장된 판단: {RELATION_STATUS_LABEL[relation.status]} · 현재 유효한 확정 관계로 보지 않습니다.</p> : null}
      {relation.validity_reasons.length ? <ul className="list-inside list-disc text-[13px] text-muted">{relation.validity_reasons.map((reason) => <li key={reason}>{relationReasonLabel(reason)}</li>)}</ul> : null}
      <p className="text-[13px] whitespace-pre-wrap text-ink-2">{relation.note}</p>
      {relation.evidence.length ? (
        <details className="text-[13px]">
          <summary className="cursor-pointer text-accent-text">판단 당시 원문 근거 ({relation.evidence.length}개)</summary>
          <div className="mt-3"><RelationEvidence evidence={relation.evidence} /></div>
        </details>
      ) : null}
      <p className="text-[12px] text-muted">최종 검토 {formatDateTime(relation.updated_at)}</p>
    </div>
  );
}

export function RelationHistory({ relation }: { relation: Relation }) {
  return (
    <details className="mt-4 border-t border-line pt-4">
      <summary className="cursor-pointer text-sm font-medium">검토 이력 ({relation.history.length}건)</summary>
      <ol className="mt-3 space-y-4">
        {relation.history.map((event) => (
          <li key={event.version} className="rounded-lg bg-surface-2/50 p-3 text-[13px]">
            <p className="font-medium">버전 {event.version} · {RELATION_STATUS_LABEL[event.status]} · {event.actor_name ?? "검토자 정보 없음"}</p>
            <p className="mt-1 text-muted">{formatDateTime(event.created_at)}</p>
            <p className="my-2 whitespace-pre-wrap">{event.note}</p>
            <details>
              <summary className="cursor-pointer text-accent-text">이 판단의 근거 보기</summary>
              <div className="mt-2"><RelationEvidence evidence={event.evidence} /></div>
            </details>
          </li>
        ))}
      </ol>
    </details>
  );
}

export function OpportunityRelations({ opportunityId }: { opportunityId: number }) {
  const query = useOpportunityRelations(opportunityId);
  if (DEMO_STATIC) return null;
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <Card>
      <CardHeader title="사업·계약 관계" description="하나의 사업에서 나뉜 계약을 별도로 보여줍니다. 관계가 있어도 예산을 합산하거나 발주 예측 확률을 높이지 않습니다." />
      <div className="space-y-4 p-5">
        {query.isLoading ? <Skeleton className="h-24" /> : null}
        {query.error ? <ErrorNote error={query.error} /> : null}
        {!query.isLoading && !query.error && !items.length ? <p className="text-sm text-muted">등록된 사업·계약 관계가 없어요.</p> : null}
        {items.map((relation) => <div key={relation.id} className="rounded-lg border border-line p-4"><RelationSummary relation={relation} /></div>)}
        {query.hasNextPage ? <Button variant="secondary" loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>관계 더 보기</Button> : null}
        {query.error ? <Button variant="secondary" onClick={() => void query.refetch()}>관계 다시 불러오기</Button> : null}
      </div>
    </Card>
  );
}

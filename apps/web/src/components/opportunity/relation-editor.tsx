"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { EvidenceContext } from "@/components/opportunity/evidence";
import { Button } from "@/components/ui/button";
import { Card, ErrorNote, Field, Input, Select, Skeleton, Textarea } from "@/components/ui/primitives";
import { ApiError, newIdempotencyKey, type Schemas } from "@/lib/api/client";
import { useOpportunity } from "@/lib/api/hooks";
import { RelationError, useDecideRelation, type Relation, type RelationStatus } from "@/lib/api/relations";

import { relationReasonLabel } from "./relations";

const PROJECT_STAGES = ["council_mention", "budget_line"];
const CONTRACT_STAGES = ["order_plan", "prespec", "bid_notice", "award"];

function eligible(signal: Schemas["SignalOut"], project: boolean): boolean {
  return signal.verdict === "accepted" && signal.link !== null && !signal.link.tentative
    && (project ? PROJECT_STAGES : CONTRACT_STAGES).includes(signal.stage)
    && signal.evidence.length > 0 && signal.evidence.every((item) => item.found && item.start !== null && item.end !== null);
}

function EvidencePicker({ data, project, selected, onToggle, disabled }: {
  data: Schemas["OpportunityDetail"]; project: boolean; selected: number[]; onToggle: (id: number) => void; disabled: boolean;
}) {
  return (
    <section aria-label={project ? "사업 근거" : "계약 근거"} className="min-w-0 space-y-3 rounded-lg border border-line p-4">
      <h3 className="text-sm font-semibold"><Link className="text-accent-text hover:underline" href={`/app/opportunities/${data.id}`}>{data.title}</Link></h3>
      <p className="text-[13px] text-muted">{data.institution.name} · 기회 #{data.id} · {data.stage_label}</p>
      {!data.signals.some((signal) => eligible(signal, project)) ? <p className="text-[13px] text-critical">선택 가능한 {project ? "사업" : "계약"} 근거가 없어요.</p> : null}
      {data.signals.map((signal) => (
        <div key={signal.id} className="space-y-2 border-t border-line pt-3">
          <label className="flex items-start gap-2 text-[13px] font-medium">
            <input type="checkbox" className="mt-1 accent-[var(--accent)]" checked={selected.includes(signal.id)} disabled={disabled || !eligible(signal, project) || (selected.length >= 20 && !selected.includes(signal.id))} onChange={() => onToggle(signal.id)} />
            {signal.title} (#{signal.id}) · {signal.stage_label}
          </label>
          {!eligible(signal, project) ? <p className="text-[12px] text-muted">역할에 맞는 승인 신호와 확정 연결, 원문 위치가 필요해요.</p> : null}
          <EvidenceContext context={signal.context} contextOffset={signal.context_offset} evidence={signal.evidence} maxChars={600} />
          <p className="text-[12px] text-muted">{signal.document.title}
            {signal.document.url && /^https?:\/\//i.test(signal.document.url) ? <> · <a className="text-accent-text hover:underline" href={signal.document.url} target="_blank" rel="noreferrer">원문 열기</a></> : null}
          </p>
        </div>
      ))}
    </section>
  );
}

function decisionError(error: unknown): string {
  if (!(error instanceof Error)) return "저장 결과를 확인하지 못했어요. 최신 상태를 불러와 확인해 주세요.";
  if (error.message === "stale_version") return "다른 검토자가 관계를 변경했어요. 최신 상태를 불러온 뒤 다시 판단해 주세요.";
  if (error.message === "contract_already_has_confirmed_project") return "이 계약에는 이미 확정된 사업 관계가 있어요. 기존 관계를 확인해 주세요.";
  if (error.message === "idempotency_key_reused") return "같은 요청 번호로 다른 판단을 보낼 수 없어요. 최신 상태를 확인해 주세요.";
  if (error instanceof RelationError && error.issues.length) return error.issues.map(relationReasonLabel).join(" ");
  if (error instanceof ApiError && error.status === 403) return "관계를 검토할 관리자 권한이 필요해요.";
  if (error instanceof ApiError && error.status === 422) return "입력값과 양쪽 원문 근거를 확인해 주세요.";
  return "저장 결과를 확인하지 못했어요. 최신 상태를 불러와 확인해 주세요.";
}

export function RelationEditor({ relation, onSaved, onReload }: {
  relation?: Relation; onSaved: (saved: Relation) => void; onReload: (pair: { project_id: number; contract_id: number }) => void;
}) {
  const [projectId, setProjectId] = useState(relation ? String(relation.project_id) : "");
  const [contractId, setContractId] = useState(relation ? String(relation.contract_id) : "");
  const [loaded, setLoaded] = useState<{ project: number; contract: number } | null>(relation ? { project: relation.project_id, contract: relation.contract_id } : null);
  const [selected, setSelected] = useState<number[]>([]);
  const [status, setStatus] = useState<RelationStatus>("proposed");
  const [note, setNote] = useState("");
  const [inputError, setInputError] = useState("");
  const decide = useDecideRelation();
  const project = useOpportunity(loaded?.project ?? Number.NaN);
  const contract = useOpportunity(loaded?.contract ?? Number.NaN);
  const sameInputs = loaded?.project === Number(projectId) && loaded?.contract === Number(contractId);
  const hasBothRoles = project.data?.signals.some((signal) => eligible(signal, true) && selected.includes(signal.id))
    && contract.data?.signals.some((signal) => eligible(signal, false) && selected.includes(signal.id));
  const institutionsMatch = !!project.data?.institution.code && project.data.institution.code === contract.data?.institution.code;
  const staleVersion = decide.error instanceof Error && decide.error.message === "stale_version";
  const canSave = note.trim().length > 0 && !staleVersion && (status === "rejected" ? !!relation : sameInputs && hasBothRoles && institutionsMatch);

  const load = () => {
    const p = Number(projectId), c = Number(contractId);
    if (![p, c].every((id) => Number.isSafeInteger(id) && id > 0) || p === c) {
      setInputError("서로 다른 양의 정수 기회 ID를 입력해 주세요.");
      return;
    }
    setInputError("");
    setSelected([]);
    setLoaded({ project: p, contract: c });
    if (loaded?.project === p && loaded?.contract === c) {
      void project.refetch();
      void contract.refetch();
    }
  };
  const toggle = (id: number) => setSelected((ids) => ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id]);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canSave || decide.isPending) return;
    decide.mutate({
      body: { project_id: Number(projectId), contract_id: Number(contractId), status, expected_version: relation?.version ?? 0, evidence_signal_ids: status === "rejected" ? [] : selected, note: note.trim() },
      idempotencyKey: newIdempotencyKey("relation"),
    }, { onSuccess: onSaved });
  };

  return (
    <Card className="p-5">
      <form onSubmit={submit} className="space-y-4">
        <h2 className="text-[16px] font-semibold">{relation ? `관계 #${relation.id} 다시 판단 · 버전 ${relation.version}` : "새 사업·계약 관계"}</h2>
        <p className="text-[13px] text-muted">사업은 의회·예산 신호, 계약은 발주계획·사전규격·입찰 신호로 확인합니다. 양쪽 원문을 읽고 근거를 각각 선택해 주세요.</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="사업 기회 ID" htmlFor="relation-project"><Input id="relation-project" inputMode="numeric" value={projectId} disabled={!!relation || decide.isPending} onChange={(event) => setProjectId(event.target.value)} /></Field>
          <Field label="계약 기회 ID" htmlFor="relation-contract"><Input id="relation-contract" inputMode="numeric" value={contractId} disabled={!!relation || decide.isPending} onChange={(event) => setContractId(event.target.value)} /></Field>
        </div>
        <Button type="button" variant="secondary" disabled={decide.isPending} onClick={load}>양쪽 기회 불러오기</Button>
        {inputError ? <ErrorNote error={new Error(inputError)} /> : null}
        {loaded && sameInputs ? (
          <>
            {project.isLoading || contract.isLoading ? <Skeleton className="h-24" /> : null}
            {project.error || contract.error ? <ErrorNote error={project.error ?? contract.error} /> : null}
            {project.data && contract.data ? (
              <>
                {!institutionsMatch ? <ErrorNote error={new Error("양쪽 수요 기관이 다르거나 확인되지 않았어요.")} /> : null}
                <div className="grid items-start gap-4 xl:grid-cols-2">
                  <EvidencePicker data={project.data} project selected={selected} onToggle={toggle} disabled={decide.isPending} />
                  <EvidencePicker data={contract.data} project={false} selected={selected} onToggle={toggle} disabled={decide.isPending} />
                </div>
                <p className="text-[12px] text-muted">근거 {selected.length}/20개 선택 · 저장 시 현재 원문과 위치를 다시 검증합니다.</p>
              </>
            ) : null}
          </>
        ) : null}
        <Field label="관계 판단" htmlFor="relation-status">
          <Select id="relation-status" value={status} disabled={decide.isPending} onChange={(event) => setStatus(event.target.value as RelationStatus)}>
            <option value="proposed">제안 — 추가 검토 필요</option>
            <option value="confirmed">확정 — 양쪽 근거를 대조함</option>
            {relation ? <option value="rejected">기각 — 관계 철회</option> : null}
          </Select>
        </Field>
        {status === "rejected" ? <p className="text-[13px] text-muted">기각은 원문이 없어져도 가능합니다. 기존 판단과 근거는 이력에 남습니다.</p> : null}
        <Field label="판단 근거" htmlFor="relation-note" hint="같은 사업과 계약이라고 판단하거나 기각한 이유를 적어 주세요.">
          <Textarea id="relation-note" maxLength={2000} value={note} disabled={decide.isPending} onChange={(event) => setNote(event.target.value)} />
        </Field>
        {decide.error ? <ErrorNote error={new Error(decisionError(decide.error))} /> : null}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={!canSave} loading={decide.isPending}>관계 저장</Button>
          {decide.error ? <Button type="button" variant="secondary" disabled={decide.isPending} onClick={() => onReload({ project_id: Number(projectId), contract_id: Number(contractId) })}>최신 상태 불러오기</Button> : null}
        </div>
      </form>
    </Card>
  );
}

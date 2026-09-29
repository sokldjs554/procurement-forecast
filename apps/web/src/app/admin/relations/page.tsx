"use client";

import Link from "next/link";
import { useState } from "react";

import { RelationEditor } from "@/components/opportunity/relation-editor";
import { RelationHistory, RelationSummary } from "@/components/opportunity/relations";
import { Button } from "@/components/ui/button";
import { Card, EmptyState, ErrorNote, Field, PageHeader, Select, Skeleton } from "@/components/ui/primitives";
import { api, unwrap } from "@/lib/api/client";
import { useAdminRelations, useMixedRelations, useRelation, type Relation, type RelationStatus } from "@/lib/api/relations";
import { DEMO_STATIC } from "@/lib/demo/fetch";

function MixedAudit() {
  const query = useMixedRelations();
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">한 기회에 사업 신호와 계약 신호가 함께 들어 있는 기존 그룹입니다. 혼합 여부만 찾은 목록이며, 올바른 관계나 분리 방법을 확정한 결과가 아닙니다. 원문과 구성 신호를 먼저 확인해 주세요.</p>
      {query.isLoading ? <Skeleton className="h-40" /> : null}
      {query.error ? <ErrorNote error={query.error} /> : null}
      {!query.isLoading && !query.error && !items.length ? <Card><EmptyState title="현재 조회 범위에 혼합 그룹이 없어요" /></Card> : null}
      {items.map((item) => (
        <Card key={item.opportunity.id} className="space-y-2 p-5">
          <Link className="font-semibold text-accent-text hover:underline" href={`/app/opportunities/${item.opportunity.id}`}>{item.opportunity.title}</Link>
          <p className="text-[13px] text-muted">기회 #{item.opportunity.id} · 기관 {item.opportunity.institution_code ?? "미확인"}</p>
          <p className="text-sm">사업 신호: {item.project_signal_ids.map((id) => `#${id}`).join(", ")}</p>
          <p className="text-sm">계약 신호: {item.contract_signal_ids.map((id) => `#${id}`).join(", ")}</p>
          <p className="text-[13px] break-all text-muted">공고 번호: {item.bid_notice_numbers.length ? item.bid_notice_numbers.join(", ") : "확인된 번호 없음"}</p>
        </Card>
      ))}
      {query.hasNextPage ? <Button variant="secondary" loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>혼합 그룹 더 보기</Button> : null}
      {query.error ? <Button variant="secondary" onClick={() => void query.refetch()}>혼합 그룹 다시 불러오기</Button> : null}
    </div>
  );
}

function RelationReview({ id, onSaved }: { id: number; onSaved: (relation: Relation) => void }) {
  const query = useRelation(id);
  const [reload, setReload] = useState(0);
  return (
    <div className="space-y-4">
      {query.isLoading ? <Skeleton className="h-48" /> : null}
      {query.error ? <><ErrorNote error={query.error} /><Button variant="secondary" onClick={() => void query.refetch()}>검토 정보 다시 불러오기</Button></> : null}
      {query.data ? (
        <>
          <Card className="p-5"><RelationSummary relation={query.data} /><RelationHistory relation={query.data} /></Card>
          <RelationEditor key={`${id}:${query.data.version}:${reload}`} relation={query.data} onSaved={onSaved} onReload={() => {
            void query.refetch().then((result) => { if (!result.error) setReload((value) => value + 1); });
          }} />
        </>
      ) : null}
    </div>
  );
}

function RelationsConsole() {
  const [tab, setTab] = useState<"relations" | "mixed">("relations");
  const [status, setStatus] = useState<RelationStatus | "all">("all");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [newEditor, setNewEditor] = useState(0);
  const [notice, setNotice] = useState("");
  const [reloadError, setReloadError] = useState<Error | null>(null);
  const [reloading, setReloading] = useState(false);
  const list = useAdminRelations(status === "all" ? undefined : status);
  const items = list.data?.pages.flatMap((page) => page.items) ?? [];
  const saved = (relation: Relation) => {
    setSelectedId(relation.id);
    setNotice(`관계 #${relation.id}의 판단을 저장했어요.`);
    setReloadError(null);
  };

  // A response may be lost during creation. Find the exact pair before allowing another
  // version-0 decision, including pairs beyond the first list page.
  const reloadNew = async (pair: { project_id: number; contract_id: number }) => {
    if (reloading) return;
    setReloading(true);
    setReloadError(null);
    try {
      let after_id = 0;
      while (true) {
        const page = unwrap(await api.GET("/api/opportunities/{opportunity_id}/relations", {
          params: { path: { opportunity_id: pair.project_id }, query: { after_id, limit: 100 } },
        }));
        const found = page.items.find((item) => item.project_id === pair.project_id && item.contract_id === pair.contract_id);
        if (found) {
          setSelectedId(found.id);
          setNotice("현재 저장된 관계를 불러왔어요. 최신 근거를 다시 선택해 주세요.");
          break;
        }
        if (page.next_after_id === null) {
          setNotice("현재 저장된 관계가 없어요. 양쪽 기회를 다시 불러와 판단해 주세요.");
          setNewEditor((value) => value + 1);
          break;
        }
        after_id = page.next_after_id;
      }
      await list.refetch();
    } catch (error) {
      setReloadError(error instanceof Error ? error : new Error("최신 상태를 불러오지 못했어요."));
    } finally {
      setReloading(false);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader title="사업·계약 관계" description="하나의 사업과 여러 계약의 관계를 근거와 함께 검토합니다. 기존 기회를 합치거나 금액·예측을 변경하지 않습니다." />
      <div className="flex flex-wrap gap-2" role="group" aria-label="관계 조회">
        <Button variant={tab === "relations" ? "primary" : "secondary"} aria-pressed={tab === "relations"} onClick={() => setTab("relations")}>관계 목록·검토</Button>
        <Button variant={tab === "mixed" ? "primary" : "secondary"} aria-pressed={tab === "mixed"} onClick={() => setTab("mixed")}>혼합 그룹 감사</Button>
      </div>
      {tab === "mixed" ? <MixedAudit /> : (
        <>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <Field label="저장된 판단으로 필터" htmlFor="relations-filter" hint="확정 목록에도 원문 변경으로 재검토가 필요한 관계가 포함될 수 있어요.">
              <Select id="relations-filter" value={status} onChange={(event) => setStatus(event.target.value as RelationStatus | "all")}>
                <option value="all">전체</option><option value="proposed">제안</option><option value="confirmed">확정으로 저장됨</option><option value="rejected">기각</option>
              </Select>
            </Field>
            <Button variant="secondary" onClick={() => { setSelectedId(null); setNewEditor((value) => value + 1); setNotice(""); setReloadError(null); }}>새 관계 작성</Button>
          </div>
          {list.isLoading ? <Skeleton className="h-32" /> : null}
          {list.error ? <><ErrorNote error={list.error} /><Button variant="secondary" onClick={() => void list.refetch()}>목록 다시 불러오기</Button></> : null}
          {!list.isLoading && !list.error && !items.length ? <Card><EmptyState title="이 조건에 맞는 관계가 없어요" /></Card> : null}
          <div className="space-y-3">
            {items.map((relation) => (
              <Card key={relation.id} className="p-5">
                <RelationSummary relation={relation} />
                <Button className="mt-3" size="sm" variant={selectedId === relation.id ? "primary" : "secondary"} onClick={() => { setSelectedId(relation.id); setNotice(""); setReloadError(null); }}>관계 #{relation.id} 검토·이력</Button>
              </Card>
            ))}
          </div>
          {list.hasNextPage ? <Button variant="secondary" loading={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>관계 목록 더 보기</Button> : null}
          {notice ? <p role="status" className="text-sm text-good-text">{notice}</p> : null}
          {reloadError ? <ErrorNote error={reloadError} /> : null}
          {reloading ? <p role="status" className="text-sm text-muted">현재 저장된 관계를 확인하고 있어요…</p> : null}
          {selectedId === null ? <RelationEditor key={newEditor} onSaved={saved} onReload={(pair) => void reloadNew(pair)} /> : <RelationReview key={selectedId} id={selectedId} onSaved={saved} />}
        </>
      )}
    </div>
  );
}

export default function RelationsPage() {
  if (DEMO_STATIC) return <p className="text-sm text-muted">사업·계약 관계 검토는 운영 환경에서 사용할 수 있어요.</p>;
  return <RelationsConsole />;
}

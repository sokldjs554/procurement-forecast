"use client";

import { Radar, Search } from "lucide-react";
import Link from "next/link";
import { useDeferredValue, useMemo, useState } from "react";

import { OpportunityCard } from "@/components/opportunity/opportunity-card";
import { DemoShowcase } from "@/components/landing/demo-showcase";
import { DEMO_STATIC } from "@/lib/demo/fetch";
import { Button } from "@/components/ui/button";
import { ChipGroup, Segmented } from "@/components/ui/controls";
import { EmptyState, ErrorNote, Input, PageHeader, Select, Skeleton } from "@/components/ui/primitives";
import { useCategories, useFeed, type FeedFilters, type FeedSort } from "@/lib/api/hooks";
import { STAGES } from "@/lib/utils";

type StatusKey = "open" | "bid_open" | "all";

const SORTS: { key: FeedSort; label: string }[] = [
  { key: "score", label: "잘 맞는 순" },
  { key: "soon", label: "입찰이 가까운 순" },
  { key: "recent", label: "새 소식 순" },
];

export default function FeedPage() {
  const [query, setQuery] = useState("");
  const [stages, setStages] = useState<string[]>([]);
  const [category, setCategory] = useState("");
  // Pre-tender demand is the point of the product, so that is what the feed opens on.
  const [status, setStatus] = useState<StatusKey>("open");
  const [sort, setSort] = useState<FeedSort>("score");
  const deferredQuery = useDeferredValue(query);
  const categories = useCategories();

  const filters: FeedFilters = useMemo(
    () => ({
      q: deferredQuery.trim() || undefined,
      stage: stages,
      category: category ? [category] : [],
      status: status === "all" ? ["open", "bid_open"] : [status],
      sort,
    }),
    [deferredQuery, stages, category, status, sort],
  );
  const feed = useFeed(filters);
  const items = feed.data?.pages.flatMap((p) => p.items) ?? [];
  // Per-stage counts come with the first page and ignore the stage chips, so the header and
  // the chips describe the whole feed, not just what has been loaded so far.
  const counts = feed.data?.pages[0]?.stage_counts ?? {};
  const all = Object.values(counts).reduce((a, b) => a + b, 0);
  const early = (counts.council_mention ?? 0) + (counts.budget_line ?? 0);

  return (
    <div className="space-y-6">
      <PageHeader
        title="기회 피드"
        description={
          feed.data
            ? `우리 회사와 맞는 사업을 ${all.toLocaleString("ko-KR")}건 찾았어요.` +
              (early ? ` 그중 ${early.toLocaleString("ko-KR")}건은 아직 발주계획도 안 나온 초기 단계예요.` : "")
            : "우리 회사와 맞는 사업을 찾고 있어요…"
        }
      />

      {DEMO_STATIC ? <section className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-accent/25 bg-accent-soft p-5" aria-label="추천 체험 순서">
        <div>
          <h2 className="font-semibold text-ink">처음이라면 이 사례부터 보세요</h2>
          <p className="mt-1 text-sm text-ink-2">연결된 문서 → 예시 원문과 근거 → 영업 브리핑 순서로 확인해 보세요.</p>
        </div>
        <DemoShowcase />
      </section> : null}

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative w-full sm:w-64">
          <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted" aria-hidden />
          <Input
            aria-label="사업명 검색"
            placeholder="사업명으로 찾기 (예: 스마트쉘터)"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="pl-9"
          />
        </div>
        <Select
          aria-label="분야"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="w-full sm:w-44"
        >
          <option value="">모든 분야</option>
          {categories.data?.map((c) => (
            <option key={c.key} value={c.key}>
              {c.label}
            </option>
          ))}
        </Select>
        <Segmented<StatusKey>
          ariaLabel="상태"
          value={status}
          onChange={setStatus}
          options={[
            { key: "open", label: "공고 전" },
            { key: "bid_open", label: "입찰 진행" },
            { key: "all", label: "전체" },
          ]}
        />
        <Select
          aria-label="정렬"
          value={sort}
          onChange={(e) => setSort(e.target.value as FeedSort)}
          className="w-full sm:ml-auto sm:w-44"
        >
          {SORTS.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
            </option>
          ))}
        </Select>
      </div>
      <ChipGroup
        ariaLabel="단계"
        options={STAGES.slice(0, 5).map((s) => ({ key: s.key, label: s.label, count: feed.data ? (counts[s.key] ?? 0) : undefined }))}
        value={stages}
        onChange={setStages}
      />

      {feed.error ? <ErrorNote error={feed.error} /> : null}

      <div className={feed.isPlaceholderData ? "space-y-3 opacity-60 transition-opacity" : "space-y-3"}>
        {feed.isLoading
          ? Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-44 w-full rounded-xl" />)
          : items.map((item) => <OpportunityCard key={item.id} item={item} />)}
      </div>

      {!feed.isLoading && items.length === 0 ? (
        <EmptyState
          icon={<Radar className="size-8" aria-hidden />}
          title="이 조건에 맞는 사업은 아직 없어요"
          description="키워드나 분야를 조금 넓혀 보세요. 회의록과 예산서는 매일 새로 들어오니까 내일 다시 봐도 좋아요."
          action={
            <Link href="/app/profile">
              <Button variant="secondary">관심 분야 바꾸기</Button>
            </Link>
          }
        />
      ) : null}

      {feed.hasNextPage ? (
        <div className="flex justify-center">
          <Button variant="secondary" onClick={() => feed.fetchNextPage()} loading={feed.isFetchingNextPage}>
            더 보기
          </Button>
        </div>
      ) : null}
    </div>
  );
}

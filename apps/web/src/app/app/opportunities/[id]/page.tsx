"use client";

import { ArrowLeft, Building2, EyeOff, ThumbsDown, ThumbsUp, Trophy } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { BarList } from "@/components/charts/bar-list";
import { BudgetLine, StatTile } from "@/components/charts/budget-line";
import { OpportunityRelations } from "@/components/opportunity/relations";
import { Briefs } from "@/components/opportunity/briefs";
import { DEMO_STATIC } from "@/lib/demo/fetch";
import { HeadStart } from "@/components/opportunity/head-start";
import { SignalTimeline } from "@/components/opportunity/signal-timeline";
import { StageRail } from "@/components/opportunity/stage-rail";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardHeader, ErrorNote, Skeleton } from "@/components/ui/primitives";
import { useFeedback, useOpportunity } from "@/lib/api/hooks";
import { formatDate, formatKRW, formatPercent, formatWindow, leadLabel } from "@/lib/format";
import { STATUS_LABEL } from "@/lib/utils";

const FEATURE_LABEL: Record<string, string> = {
  semantic: "회사 소개와 얼마나 비슷한지",
  keyword: "관심 키워드",
  category: "관심 분야",
  region: "관심 지역",
  budget: "원하는 사업 규모",
  conversion: "공고로 이어질 가능성",
  lead_time: "영업할 수 있는 시간",
};

function Why({ breakdown, tenderOut }: { breakdown: Record<string, unknown> | null | undefined; tenderOut: boolean }) {
  const features = (breakdown?.features ?? {}) as Record<string, number>;
  const weights = (breakdown?.weights ?? {}) as Record<string, number>;
  const data = Object.keys(FEATURE_LABEL)
    .filter((k) => k in features)
    .map((k) => ({
      key: k,
      // once the tender is out, the conversion feature is a fact (1.0), not a likelihood
      label: k === "conversion" && tenderOut ? "입찰공고가 나옴" : (FEATURE_LABEL[k] ?? k),
      value: (features[k] ?? 0) * (weights[k] ?? 0) * 100,
      note: `${Math.round((features[k] ?? 0) * 100)}점 × 비중 ${Math.round((weights[k] ?? 0) * 100)}%`,
    }))
    .sort((a, b) => b.value - a.value);
  if (!data.length) return <p className="text-sm text-muted">아직 우리 회사 기준으로 점수를 매기지 않은 사업이에요.</p>;
  return (
    <BarList
      data={data}
      format={(v) => `${v.toFixed(1)}점`}
      caption="항목별로 적합도에 보탠 점수"
      max={Math.max(...Object.values(weights).map((w) => w * 100))}
    />
  );
}


function FeedbackBar({ id, current }: { id: number; current: string | null }) {
  const feedback = useFeedback(id);
  const options = [
    { key: "relevant", label: "관련 있어요", icon: ThumbsUp },
    { key: "irrelevant", label: "관련 없어요", icon: ThumbsDown },
    { key: "won", label: "수주했어요", icon: Trophy },
    { key: "dismissed", label: "숨기기", icon: EyeOff },
  ] as const;
  return (
    <div className="flex flex-wrap gap-1.5" role="group" aria-label="추천 피드백">
      {options.map(({ key, label, icon: Icon }) => (
        <Button
          key={key}
          size="sm"
          variant={current === key ? "primary" : "secondary"}
          aria-pressed={current === key}
          onClick={() => feedback.mutate(current === key ? null : key)}
        >
          <Icon className="size-3.5" aria-hidden />
          {label}
        </Button>
      ))}
    </div>
  );
}

export default function OpportunityPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  const { data, error, isLoading } = useOpportunity(id);

  if (isLoading) return <Skeleton className="h-96 w-full" />;
  if (error || !data) return <ErrorNote error={error ?? new Error("이 사업을 찾을 수 없어요")} />;

  const lead = leadLabel(data.lead_days);
  const reached = data.signals.map((s) => s.stage);
  // by date, not by stage: council minutes about a tender already out do not count as early
  const earlySignals = data.bid_published_at
    ? data.signals.filter((s) => s.observed_at < (data.bid_published_at as string)).length
    : 0;
  return (
    <div className="space-y-6">
      <Link href="/app" className="inline-flex items-center gap-1 text-[13px] text-ink-2 hover:text-ink">
        <ArrowLeft className="size-4" aria-hidden /> 기회 피드
      </Link>

      {DEMO_STATIC ? <nav aria-label="사례 체험 순서" className="flex flex-wrap gap-x-5 gap-y-2 rounded-lg border border-line bg-surface p-3 text-[13px] text-accent-text">
        <a href="#evidence" className="hover:underline">① 연결된 문서·예시 원문</a>
        <a href="#recommendation" className="hover:underline">② 추천 이유</a>
        <a href="#briefing" className="hover:underline">③ 영업 브리핑</a>
      </nav> : null}

      <header className="space-y-3">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone="accent">{data.stage_label}</Badge>
          <Badge>{data.category_label}</Badge>
          <Badge tone={data.status === "bid_open" ? "warning" : "outline"}>{STATUS_LABEL[data.status] ?? data.status}</Badge>
          {lead ? <Badge tone="outline">입찰 {lead}</Badge> : null}
        </div>
        <h1 className="text-[24px] leading-tight font-bold tracking-tight text-ink">{data.title}</h1>
        <p className="inline-flex items-center gap-1.5 text-sm text-ink-2">
          <Building2 className="size-4 text-muted" aria-hidden />
          {data.institution.name}
          {data.department ? <span className="text-muted">· {data.department}</span> : null}
        </p>
      </header>

      <HeadStart detail={data} />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile label="추정 예산" value={formatKRW(data.est_budget_krw)} sub="가장 진행된 단계의 문서 기준" />
        {data.tender_out ? (
          <StatTile label="입찰공고" value={formatDate(data.bid_published_at)} sub="나라장터에 올라온 날" />
        ) : (
          <StatTile
            label="입찰 예상 시기"
            value={formatWindow(data.bid_window_start, data.bid_window_end)}
            sub={DEMO_STATIC ? "데모용 추정 일정" : lead ? `입찰 ${lead}` : data.window_passed ? "예상 시기가 지났는데 아직 공고 전" : undefined}
          />
        )}
        {data.tender_out ? (
          <StatTile label="공고 전에 잡힌 신호" value={`${earlySignals}건`} sub="입찰공고일보다 먼저 나온 문서" />
        ) : (
          <StatTile label={DEMO_STATIC ? "공고 전환 추정치 (데모)" : "공고 전환 추정치"} value={formatPercent(data.conversion_prob)} sub="합성 데이터 기반 · 실제 정확도 미검증" />
        )}
        <StatTile label="우리 회사 적합도" value={data.score !== null ? `${Math.round(data.score * 100)}점` : "–"} sub={`신호 ${data.signal_count}건`} />
      </div>

      <Card className="px-5 py-4">
        <StageRail stage={data.stage} reached={reached} />
      </Card>

      <OpportunityRelations opportunityId={data.id} />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
        <Card id="evidence" className="scroll-mt-20">
          <CardHeader
            title="신호 타임라인"
            description="문서마다 근거가 된 문장을 원문 그대로 형광펜으로 칠해 뒀어요. 원문에서 근거를 못 찾은 내용은 아예 보여주지 않아요."
          />
          <div className="px-5 pt-5 pb-6">
            <SignalTimeline signals={data.signals} />
          </div>
        </Card>

        <div className="space-y-6">
          <Card id="recommendation" className="scroll-mt-20">
            <CardHeader title="추천한 이유" />
            <div className="px-5 pt-3 pb-5">
              <Why breakdown={data.breakdown} tenderOut={data.tender_out ?? false} />
            </div>
          </Card>
          {data.budget_trajectory.length >= 2 ? (
            <Card>
              <CardHeader title="금액 추이" />
              <div className="px-5 pt-3 pb-5">
                <BudgetLine points={data.budget_trajectory} />
              </div>
            </Card>
          ) : null}
          <Briefs detail={data} />
          <Card className="p-5">
            <p className="mb-3 text-[13px] font-medium text-ink">이 추천, 도움이 됐나요?</p>
            <FeedbackBar id={data.id} current={data.feedback} />
            <p className="mt-2 text-[12px] text-muted">‘관련 없어요’나 ‘숨기기’를 누르면 피드와 알림에서 빠져요. 남겨 주신 의견은 추천 모델을 학습시킬 때도 써요.</p>
          </Card>
        </div>
      </div>
    </div>
  );
}

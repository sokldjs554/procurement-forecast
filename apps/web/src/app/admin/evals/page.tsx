"use client";

import { Info } from "lucide-react";

import { BarList, type BarDatum } from "@/components/charts/bar-list";
import { StatTile } from "@/components/charts/budget-line";
import { Card, CardHeader, EmptyState, ErrorNote, PageHeader, Skeleton } from "@/components/ui/primitives";
import type { Schemas } from "@/lib/api/client";
import { useEvals } from "@/lib/api/hooks";
import { formatDateTime, formatPercent } from "@/lib/format";

type Run = Schemas["EvalRunOut"];
type Metrics = Record<string, unknown>;

const COHORT_LABEL: Record<string, string> = {
  "council_mention:committed": "의회 답변 · '반영·편성' 확약",
  "council_mention:planned": "의회 답변 · 추진 계획",
  "council_mention:reviewing": "의회 답변 · '검토하겠다'",
  "council_mention:declined": "의회 답변 · '어렵다'",
  "budget_line:committed": "예산서 편성",
};

const num = (v: unknown): number | null => (typeof v === "number" ? v : null);
const obj = (v: unknown): Metrics => (v && typeof v === "object" ? (v as Metrics) : {});

function Backtest({ run }: { run: Run }) {
  const m = run.metrics;
  const coverage = obj(m.tender_early_coverage);
  const lead = obj(m.lead_time_days);
  const conv = obj(m.conversion_by_first_signal);
  const data: BarDatum[] = Object.entries(COHORT_LABEL)
    .filter(([k]) => k in conv)
    .map(([k, label]) => {
      const c = obj(conv[k]);
      return { key: k, label, value: num(c.rate) ?? 0, note: `n=${num(c.n) ?? 0}` };
    });
  return (
    <Card>
      <CardHeader
        title="백테스트: 선행 신호는 실제 입찰로 이어졌나"
        description={`기준일 ${String(m.as_of ?? "")} · 판단 유예 ${String(m.horizon_days ?? "")}일 · ${formatDateTime(run.created_at)}`}
      />
      <div className="space-y-6 p-5">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
          <StatTile
            label="공고 이전 신호가 있었던 입찰"
            value={formatPercent(num(coverage.rate))}
            sub={`입찰 ${String(coverage.tenders ?? 0)}건 중 ${String(coverage.with_early_signal ?? 0)}건`}
          />
          <StatTile label="선행 기간 중앙값" value={num(lead.median) !== null ? `${Math.round(num(lead.median) as number)}일` : "–"} sub="첫 공개 신호 → 입찰공고" />
          <StatTile
            label="선행 기간 p25 ~ p75"
            value={num(lead.p25) !== null ? `${Math.round(num(lead.p25) as number)} ~ ${Math.round(num(lead.p75) as number)}일` : "–"}
          />
        </div>
        <BarList
          data={data}
          format={(v) => formatPercent(v)}
          max={1}
          caption="첫 신호 종류별로 실제 입찰까지 간 비율이에요. 랭킹의 '공고 전환 추정치'를 이 값으로 맞춰요"
        />
      </div>
    </Card>
  );
}

function Extraction({ run }: { run: Run }) {
  const m = run.metrics;
  const fields = obj(m.field_accuracy);
  const triage = obj(m.triage);
  const data: BarDatum[] = [
    { key: "precision", label: "정밀도", value: num(m.precision) ?? 0 },
    { key: "recall", label: "재현율", value: num(m.recall) ?? 0 },
    ...Object.entries(fields).map(([k, v]) => ({ key: k, label: `필드 정확도 · ${k}`, value: num(v) ?? 0 })),
  ];
  const synthetic = run.label === "extraction";
  return (
    <Card>
      <CardHeader
        title={synthetic ? "추출 — 합성 정답 대비" : "추출 — 수기 작성 세트 (생성기 밖의 문장)"}
        description={`${String(run.params.extractor_mode ?? "")} · ${formatDateTime(run.created_at)}`}
      />
      <div className="space-y-4 p-5">
        <BarList data={data} format={(v) => formatPercent(v, 1)} max={1} caption="비율" />
        {synthetic && triage.chunks ? (
          <p className="text-[13px] text-ink-2">
            트리아지로 청크 {String(triage.chunks)}개 중 {formatPercent(num(triage.skipped_rate))}는 LLM을 부르지 않고 건너뛰었어요. 그렇게
            건너뛰고도 정답 신호는 {formatPercent(num(triage.gold_recall_after_triage), 1)}를 찾았어요.
          </p>
        ) : null}
      </div>
    </Card>
  );
}

function Linking({ run }: { run: Run }) {
  const p = obj(run.metrics.pairwise);
  return (
    <Card>
      <CardHeader title="기회 연결 (서로 다른 문서 속 같은 사업 묶기)" description={formatDateTime(run.created_at)} />
      <div className="p-5">
        <BarList
          data={[
            { key: "p", label: "쌍 정밀도", value: num(p.precision) ?? 0 },
            { key: "r", label: "쌍 재현율", value: num(p.recall) ?? 0 },
            { key: "single", label: "한 기회로 온전히 묶인 사업", value: num(run.metrics.truths_in_single_opportunity) ?? 0 },
          ]}
          format={(v) => formatPercent(v, 1)}
          max={1}
          caption="비율"
        />
      </div>
    </Card>
  );
}

function Ocr({ run }: { run: Run }) {
  const m = run.metrics;
  return (
    <Card>
      <CardHeader title="OCR — 스캔 예산서" description={`${String(m.scanned_documents ?? 0)}건 · ${formatDateTime(run.created_at)}`} />
      <div className="grid grid-cols-2 gap-3 p-5">
        <StatTile label="문자 오류율(CER) 원본" value={formatPercent(num(m.cer_raw), 2)} />
        <StatTile label="문자 오류율(CER) 보정 후" value={formatPercent(num(m.cer_corrected), 2)} />
        <StatTile label="금액 토큰 정확도" value={formatPercent(num(m.amount_token_accuracy_corrected), 1)} sub="예산서의 핵심 필드" />
      </div>
    </Card>
  );
}

export default function EvalsPage() {
  const evals = useEvals();
  const runs = evals.data ?? [];
  const latest = (kind: string, label?: string) => runs.find((r) => r.kind === kind && (!label || r.label === label));
  const backtest = latest("backtest");
  const extraction = latest("extraction", "extraction");
  const realistic = latest("extraction", "realistic");
  const linking = latest("ranking", "linking");
  const ocr = latest("ocr");
  return (
    <div className="space-y-6">
      <PageHeader title="평가·백테스트" description="프롬프트, 파서, 랭커를 바꾸기 전과 후를 같은 정답으로 비교해요 (manage eval all --record)." />
      <div className="flex items-start gap-2 rounded-lg border border-line bg-surface px-4 py-3 text-[13px] text-ink-2">
        <Info className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden />
        합성 데이터로 잰 수치는 파이프라인이 설계대로 도는지 보여줄 뿐, 실제 데이터에서의 정확도는 아니에요. 그래서 생성기와 다른 문장으로
        직접 쓴 평가 세트를 따로 뒀어요.
      </div>
      {evals.error ? <ErrorNote error={evals.error} /> : null}
      {evals.isLoading ? <Skeleton className="h-96" /> : null}
      {!evals.isLoading && runs.length === 0 ? (
        <Card>
          <EmptyState title="아직 평가 기록이 없어요" description="manage eval all --record를 실행하면 여기에 쌓여요." />
        </Card>
      ) : null}
      {backtest ? <Backtest run={backtest} /> : null}
      <div className="grid gap-6 lg:grid-cols-2">
        {extraction ? <Extraction run={extraction} /> : null}
        {realistic ? <Extraction run={realistic} /> : null}
        {linking ? <Linking run={linking} /> : null}
        {ocr ? <Ocr run={ocr} /> : null}
      </div>
    </div>
  );
}

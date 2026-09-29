"use client";

import { ClipboardCheck } from "lucide-react";
import { useState } from "react";

import { EvidenceContext } from "@/components/opportunity/evidence";
import { Button } from "@/components/ui/button";
import { Segmented } from "@/components/ui/controls";
import { Badge, Card, EmptyState, ErrorNote, Field, Input, PageHeader, Select, Skeleton } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/toast";
import type { Schemas } from "@/lib/api/client";
import { useCategories, useDecideReview, useInstitutions, useReviewQueue } from "@/lib/api/hooks";
import { formatDate, formatKRW } from "@/lib/format";
import { COMMITMENT_LABEL } from "@/lib/utils";

const ISSUE_LABEL: Record<string, string> = {
  evidence_not_found: "근거 문장을 원문에서 찾지 못함",
  partial_evidence: "근거 일부만 확인",
  budget_mismatch: "금액이 원문과 불일치",
  budget_unsupported: "금액의 근거 없음",
  year_unverified: "연도를 원문으로 확인 불가",
  low_confidence: "낮은 확신도",
  institution_unresolved: "기관을 특정할 수 없음 (동명 기관)",
};

function issueLabel(code: string): string {
  if (code.startsWith("degraded:")) return `LLM 폴백으로 추출 (${code.slice(9)})`;
  return ISSUE_LABEL[code] ?? code;
}

type Item = Schemas["ReviewItemOut"];
type Commitment = NonNullable<Schemas["ReviewDecisionIn"]["commitment"]>;

function ReviewCard({ item }: { item: Item }) {
  const decide = useDecideReview();
  const institutions = useInstitutions();
  const categories = useCategories();
  const toast = useToast();
  const s = item.signal;
  const [title, setTitle] = useState(s.title);
  const [budget, setBudget] = useState(s.budget_krw ? String(s.budget_krw) : "");
  const [year, setYear] = useState(s.expected_year ? String(s.expected_year) : "");
  const [commitment, setCommitment] = useState<string>(s.commitment ?? "");
  const [institution, setInstitution] = useState("");
  const [category, setCategory] = useState(s.category);
  const needsInstitution = item.reasons.includes("institution_unresolved");

  const submit = (action: "approve" | "edit" | "reject") => {
    const body: Schemas["ReviewDecisionIn"] = { action };
    if (action === "edit") {
      if (title !== s.title) body.title = title;
      if (budget && Number(budget) !== s.budget_krw) body.budget_krw = Number(budget);
      if (year && Number(year) !== s.expected_year) body.expected_year = Number(year);
      if (commitment && commitment !== s.commitment) body.commitment = commitment as Commitment;
      if (institution) body.institution_code = institution;
      if (category !== s.category) body.category = category as Schemas["Category"];
    }
    decide.mutate(
      { id: item.id, body },
      { onSuccess: () => toast("good", action === "reject" ? "기각했어요" : "승인했어요. 사업 연결을 다시 돌릴게요.") },
    );
  };

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="accent">{s.stage_label}</Badge>
        {item.reasons.map((r) => (
          <Badge key={r} tone="warning">
            {issueLabel(r)}
          </Badge>
        ))}
      </div>
      <h3 className="mt-2 text-[15px] font-semibold text-ink">{s.title}</h3>
      <p className="mt-0.5 text-[13px] text-muted">
        {item.institution_name ?? "기관 미확인"} · {formatDate(s.observed_at)} · {s.document.title} · 추출 {s.extractor}
      </p>
      <div className="mt-3">
        <EvidenceContext context={s.context} contextOffset={s.context_offset} evidence={s.evidence} maxChars={500} />
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="사업명" htmlFor={`t-${item.id}`}>
          <Input id={`t-${item.id}`} value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <Field label="분야" htmlFor={`category-${item.id}`}>
          <Select id={`category-${item.id}`} value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="other">기타·분야 미확인</option>
            {categories.data?.map((cat) => <option key={cat.key} value={cat.key}>{cat.label}</option>)}
          </Select>
        </Field>
        <Field label="금액 (원)" htmlFor={`b-${item.id}`} hint={s.budget_krw ? `추출값 ${formatKRW(s.budget_krw)}` : undefined}>
          <Input id={`b-${item.id}`} inputMode="numeric" value={budget} onChange={(e) => setBudget(e.target.value.replace(/\D/g, ""))} />
        </Field>
        <Field label="예상 연도" htmlFor={`y-${item.id}`}>
          <Input id={`y-${item.id}`} inputMode="numeric" value={year} onChange={(e) => setYear(e.target.value.replace(/\D/g, "").slice(0, 4))} />
        </Field>
        <Field label="의지 수준" htmlFor={`c-${item.id}`}>
          <Select id={`c-${item.id}`} value={commitment} onChange={(e) => setCommitment(e.target.value)}>
            <option value="">–</option>
            {Object.entries(COMMITMENT_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </Select>
        </Field>
        {needsInstitution ? (
          <Field label="수요 기관 지정" htmlFor={`i-${item.id}`} hint="이름이 같은 기관(예: 중구)이 여럿이에요. 실제 기관을 골라 주세요.">
            <Select id={`i-${item.id}`} value={institution} onChange={(e) => setInstitution(e.target.value)}>
              <option value="">선택</option>
              {institutions.data?.map((inst) => (
                <option key={inst.code} value={inst.code}>
                  {inst.name}
                </option>
              ))}
            </Select>
          </Field>
        ) : null}
      </div>
      <div className="mt-4 flex flex-wrap justify-end gap-2">
        <Button variant="danger" size="sm" onClick={() => submit("reject")} disabled={decide.isPending}>
          기각
        </Button>
        <Button variant="secondary" size="sm" onClick={() => submit("edit")} disabled={decide.isPending || (needsInstitution && !institution)}>
          수정 후 승인
        </Button>
        <Button size="sm" onClick={() => submit("approve")} disabled={decide.isPending || needsInstitution}>
          그대로 승인
        </Button>
      </div>
      {decide.error ? <div className="mt-3"><ErrorNote error={decide.error} /></div> : null}
    </Card>
  );
}

type Status = "open" | "approved" | "edited" | "rejected";

export default function ReviewPage() {
  const [status, setStatus] = useState<Status>("open");
  const queue = useReviewQueue(status);
  return (
    <div className="space-y-6">
      <PageHeader
        title="검토 대기열"
        description="검증기가 근거, 금액, 연도, 기관 중 하나라도 확인하지 못한 신호예요. 여기서 사람이 판단한 결과는 평가용 정답으로도 쌓여요."
        action={
          <Segmented<Status>
            ariaLabel="검토 상태"
            value={status}
            onChange={setStatus}
            options={[
              { key: "open", label: "대기" },
              { key: "approved", label: "승인" },
              { key: "edited", label: "수정" },
              { key: "rejected", label: "기각" },
            ]}
          />
        }
      />
      {queue.error ? <ErrorNote error={queue.error} /> : null}
      {queue.isLoading ? (
        <Skeleton className="h-64" />
      ) : queue.data?.length ? (
        <div className="space-y-4">
          {queue.data.map((item) => (
            <ReviewCard key={item.id} item={item} />
          ))}
        </div>
      ) : (
        <Card>
          <EmptyState icon={<ClipboardCheck className="size-8" aria-hidden />} title="검토할 신호가 없어요" />
        </Card>
      )}
    </div>
  );
}

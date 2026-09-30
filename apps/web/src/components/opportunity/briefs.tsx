"use client";

import { FileSearch } from "lucide-react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";

import { Button } from "@/components/ui/button";
import { Card, CardHeader } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/toast";
import { ApiError, newIdempotencyKey, type Schemas } from "@/lib/api/client";
import { useCreateBrief, useMe } from "@/lib/api/hooks";
import { DEMO_STATIC } from "@/lib/demo/fetch";
import { formatDateTime } from "@/lib/format";

export function Briefs({ detail }: { detail: Schemas["OpportunityDetail"] }) {
  const me = useMe();
  const toast = useToast();
  const create = useCreateBrief(detail.id);
  const balance = me.data?.org.credit_balance ?? 0;
  const latest = detail.briefs[0];
  return (
    <Card id="briefing" className="scroll-mt-20">
      <CardHeader
        title="영업 브리핑"
        description={DEMO_STATIC
          ? "사업의 근거와 제안 전략을 정리한 예시 브리핑을 열어 보세요. 체험에서는 크레딧이 차감되지 않아요."
          : "지금까지 잡힌 신호와 원문, 이 기관의 발주 이력을 모아 한 장짜리 브리핑을 써 드려요. 한 번에 3크레딧이 들어요."}
        action={
          <Button
            size="sm"
            loading={create.isPending}
            disabled={!DEMO_STATIC && balance < 3}
            onClick={() =>
              create.mutate(newIdempotencyKey("brief"), {
                onSuccess: () => toast("good", DEMO_STATIC
                  ? "예시 브리핑을 열었어요. 크레딧은 차감되지 않아요."
                  : "브리핑을 만들었어요. 크레딧 3개를 썼어요."),
                onError: (e) =>
                  toast(
                    "critical",
                    e instanceof ApiError && e.status === 402 ? e.message : "브리핑을 만들지 못했어요. 잠시 후 다시 해 주세요.",
                  ),
              })
            }
          >
            <FileSearch className="size-4" aria-hidden />
            {DEMO_STATIC ? (latest ? "예시 브리핑 다시 보기" : "예시 브리핑 보기") : (latest ? "다시 만들기" : "브리핑 만들기")}
          </Button>
        }
      />
      <div className="px-5 pt-3 pb-5">
        {!DEMO_STATIC && balance < 3 ? (
          <p className="mb-3 text-[13px] text-muted">
            크레딧이 모자라요.{" "}
            <Link href="/app/billing" className="text-accent-text hover:underline">
              충전하기
            </Link>
          </p>
        ) : null}
        {latest ? (
          <article className="prose-brief">
            {DEMO_STATIC ? <p className="mb-3 rounded-lg bg-accent-soft p-3 text-[12px] text-ink-2">가상 문서로 미리 작성한 예시예요. 아래 확률과 일정은 데모용 추정치이며 실제 성능은 검증되지 않았어요. 열람 시 AI 호출이나 결제가 발생하지 않아요.</p> : null}
            <ReactMarkdown>{latest.content_md}</ReactMarkdown>
            <p className="mt-4 text-[11px] text-muted">
              {formatDateTime(latest.created_at)} ·{" "}
              {latest.model.startsWith("heuristic") ? "LLM 없이 템플릿으로 작성" : latest.model}
            </p>
          </article>
        ) : (
          <p className="text-[13px] text-muted">아직 만든 브리핑이 없어요.</p>
        )}
      </div>
    </Card>
  );
}

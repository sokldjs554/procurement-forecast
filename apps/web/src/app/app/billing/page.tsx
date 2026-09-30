"use client";

import { Check, CreditCard } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge, Card, CardHeader, ErrorNote, PageHeader, Skeleton } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/toast";
import { ApiError, newIdempotencyKey, type Schemas } from "@/lib/api/client";
import { useBilling, useBuyCredits, useChangePlan, useRegisterCard } from "@/lib/api/hooks";
import { formatDate, formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { DEMO_STATIC } from "@/lib/demo/fetch";

type Billing = Schemas["BillingOut"];

const REASON_LABEL: Record<string, string> = {
  plan_grant: "플랜 기본 제공",
  purchase: "크레딧 구매",
  brief: "영업 브리핑",
  refund: "환불",
  adjustment: "조정",
  expiry: "기간이 지나 소멸",
};
const CHANNEL_LABEL: Record<string, string> = { email: "이메일", slack: "Slack", kakao: "카카오 알림톡" };
const STATUS_LABEL: Record<string, string> = {
  active: "이용 중",
  trialing: "체험 중",
  past_due: "결제 실패 · 다시 시도할 예정",
  canceled: "해지됨",
};

declare global {
  interface Window {
    TossPayments?: (clientKey: string) => {
      payment: (opts: { customerKey: string }) => {
        requestBillingAuth: (opts: {
          method: "CARD";
          successUrl: string;
          failUrl: string;
          customerEmail?: string;
          customerName?: string;
        }) => Promise<void>;
      };
    };
  }
}

async function loadTossSdk(): Promise<NonNullable<Window["TossPayments"]>> {
  if (window.TossPayments) return window.TossPayments;
  await new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://js.tosspayments.com/v2/standard";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("토스페이먼츠 결제창을 불러오지 못했어요"));
    document.head.appendChild(script);
  });
  if (!window.TossPayments) throw new Error("토스페이먼츠 결제창을 불러오지 못했어요");
  return window.TossPayments;
}

function CardSection({ billing }: { billing: Billing }) {
  const register = useRegisterCard();
  const toast = useToast();
  const sub = billing.subscription;

  const startRegistration = async () => {
    if (billing.payment_provider === "toss" && billing.toss_client_key) {
      // Real flow: Toss hosts the card form and redirects back with authKey.
      const TossPayments = await loadTossSdk();
      await TossPayments(billing.toss_client_key)
        .payment({ customerKey: billing.customer_key })
        .requestBillingAuth({
          method: "CARD",
          successUrl: `${window.location.origin}/app/billing/success`,
          failUrl: `${window.location.origin}/app/billing?fail=1`,
        });
      return;
    }
    // Local/demo: the fake provider issues a test billing key.
    register.mutate(
      { auth_key: newIdempotencyKey("demo"), customer_key: billing.customer_key },
      { onSuccess: () => toast("good", "테스트 카드를 등록했어요") },
    );
  };

  return (
    <Card>
      <CardHeader
        title="결제 수단"
        description={
          billing.payment_provider === "toss"
            ? "매달 토스페이먼츠 자동결제로 결제돼요. 카드 번호는 저장하지 않고, 암호화한 빌링키만 보관해요."
            : "데모라서 실제로 돈이 나가지 않아요. 테스트용 결제사가 결제하는 흉내만 내요."
        }
      />
      <div className="flex flex-wrap items-center justify-between gap-3 p-5">
        <div className="flex items-center gap-2 text-sm text-ink">
          <CreditCard className="size-4 text-muted" aria-hidden />
          {sub.card_summary ?? "등록된 카드가 없어요"}
        </div>
        <Button variant="secondary" size="sm" disabled={DEMO_STATIC} loading={register.isPending} onClick={() => void startRegistration()}>
          {sub.card_summary ? "카드 변경" : "카드 등록"}
        </Button>
      </div>
      {register.error ? <div className="px-5 pb-5"><ErrorNote error={register.error} /></div> : null}
    </Card>
  );
}

function Plans({ billing }: { billing: Billing }) {
  const change = useChangePlan();
  const toast = useToast();
  const current = billing.subscription.plan;
  return (
    <div className="grid gap-4 md:grid-cols-3">
      {billing.plans.map((plan) => {
        const isCurrent = plan.key === current;
        return (
          <Card key={plan.key} className={cn("flex flex-col p-5", isCurrent && "border-accent")}>
            <div className="flex items-center justify-between">
              <h3 className="text-[15px] font-semibold text-ink">{plan.name}</h3>
              {isCurrent ? <Badge tone="accent">현재 플랜</Badge> : null}
            </div>
            <p className="mt-2 text-[24px] font-bold tracking-tight text-ink">
              {plan.monthly_price_krw ? `₩${plan.monthly_price_krw.toLocaleString("ko-KR")}` : "무료"}
              {plan.monthly_price_krw ? <span className="text-sm font-normal text-muted"> / 월</span> : null}
            </p>
            <ul className="mt-4 flex-1 space-y-2 text-[13px] text-ink-2">
              <li className="flex gap-2"><Check className="mt-0.5 size-3.5 text-good" aria-hidden />매월 크레딧 {plan.monthly_credits}개</li>
              <li className="flex gap-2"><Check className="mt-0.5 size-3.5 text-good" aria-hidden />관심 지역 {plan.max_regions ?? "무제한"}{plan.max_regions ? "개" : ""}</li>
              <li className="flex gap-2"><Check className="mt-0.5 size-3.5 text-good" aria-hidden />{plan.channels.map((c) => CHANNEL_LABEL[c] ?? c).join(" · ")}</li>
              <li className="flex gap-2"><Check className="mt-0.5 size-3.5 text-good" aria-hidden />{plan.instant_alerts ? "새 사업 바로 알림" : "매일·매주 요약 알림"}</li>
            </ul>
            <Button
              className="mt-5"
              variant={isCurrent || !plan.monthly_price_krw ? "secondary" : "primary"}
              disabled={DEMO_STATIC || isCurrent || change.isPending}
              loading={change.isPending && change.variables?.plan === plan.key}
              onClick={() =>
                change.mutate(
                  { plan: plan.key as Schemas["PlanChangeIn"]["plan"], idempotencyKey: newIdempotencyKey("plan") },
                  {
                    onSuccess: () =>
                      toast("good", plan.monthly_price_krw ? `${plan.name} 플랜으로 바꿨어요` : "이번 결제 기간이 끝나면 Free로 바뀌어요"),
                    onError: (e) => toast("critical", e instanceof ApiError ? e.message : "플랜을 바꾸지 못했어요"),
                  },
                )
              }
            >
              {isCurrent ? "이용 중" : plan.monthly_price_krw ? "이 플랜으로 바꾸기" : "해지 예약"}
            </Button>
          </Card>
        );
      })}
    </div>
  );
}

function Credits({ billing }: { billing: Billing }) {
  const buy = useBuyCredits();
  const toast = useToast();
  return (
    <Card>
      <CardHeader
        title={`${DEMO_STATIC ? "예시 " : ""}크레딧 ${billing.credit_balance}개`}
        description={DEMO_STATIC
          ? "아래 잔액과 사용 내역은 예시예요. 공개 데모에서 브리핑을 열어도 크레딧은 차감되지 않아요."
          : `영업 브리핑 한 건에 ${billing.brief_cost}크레딧이 들어요. 플랜으로 받은 크레딧은 결제 주기가 끝나면 사라지고, 따로 산 크레딧은 계속 남아요.`}
        action={
          <div className="flex gap-2">
            {billing.credit_packs.map((p) => (
              <Button
                key={p.key}
                size="sm"
                variant="secondary"
                disabled={DEMO_STATIC}
                loading={buy.isPending && buy.variables?.pack === p.key}
                onClick={() =>
                  buy.mutate({ pack: p.key, idempotencyKey: newIdempotencyKey("pack") }, {
                    onSuccess: () => toast("good", `크레딧 ${p.credits}개를 충전했어요`),
                    onError: (e) => toast("critical", e instanceof ApiError ? e.message : "결제하지 못했어요"),
                  })
                }
              >
                +{p.credits} · ₩{p.price_krw.toLocaleString("ko-KR")}
              </Button>
            ))}
          </div>
        }
      />
      {DEMO_STATIC ? <p className="rounded-lg border border-line bg-surface p-4 text-sm text-ink-2">요금과 결제 내역을 살펴보는 예시 화면이에요. 실제 구독·결제·크레딧 차감은 없으며, 카드 변경과 충전은 실행하지 않아요.</p> : null}
      <div className="overflow-x-auto p-5">
        <table className="w-full min-w-[480px] text-[13px]">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="py-2 font-medium">일시</th>
              <th className="py-2 font-medium">내용</th>
              <th className="py-2 text-right font-medium">변동</th>
              <th className="py-2 text-right font-medium">잔액</th>
            </tr>
          </thead>
          <tbody>
            {billing.ledger.map((e) => (
              <tr key={e.id} className="border-b border-line last:border-0">
                <td className="tabular py-2 text-ink-2">{formatDateTime(e.created_at)}</td>
                <td className="py-2 text-ink">{REASON_LABEL[e.reason] ?? e.reason}</td>
                <td className={cn("tabular py-2 text-right font-medium", e.delta > 0 ? "text-good-text" : "text-ink")}>
                  {e.delta > 0 ? `+${e.delta}` : e.delta}
                </td>
                <td className="tabular py-2 text-right text-ink-2">{e.balance_after}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export default function BillingPage() {
  const billing = useBilling();
  if (billing.isLoading) return <Skeleton className="h-96" />;
  if (!billing.data) return <ErrorNote error={billing.error} />;
  const b = billing.data;
  const sub = b.subscription;
  return (
    <div className="space-y-6">
      <PageHeader
        title="요금·크레딧"
        description={
          <>
            {STATUS_LABEL[sub.status] ?? sub.status}
            {sub.next_charge_at ? ` · 다음 결제 ${formatDate(sub.next_charge_at)}` : ""}
            {sub.canceled_at && sub.current_period_end ? ` · ${formatDate(sub.current_period_end)}에 Free로 전환` : ""}
          </>
        }
      />
      {sub.status === "past_due" ? (
        <div role="alert" className="rounded-lg border border-serious/40 bg-serious/10 px-4 py-3 text-sm text-ink">
          정기결제가 {sub.failed_attempts}번 실패했어요. 1일, 3일, 7일 뒤에 다시 시도하고, 그래도 안 되면 Free 플랜으로 바뀌어요. 카드를
          한 번 확인해 주세요.
        </div>
      ) : null}
      <CardSection billing={b} />
      <Plans billing={b} />
      <Credits billing={b} />
      <Card>
        <CardHeader title="결제 내역" />
        <div className="overflow-x-auto p-5">
          <table className="w-full min-w-[520px] text-[13px]">
            <tbody>
              {b.payments.map((p) => (
                <tr key={p.id} className="border-b border-line last:border-0">
                  <td className="tabular py-2 text-ink-2">{formatDateTime(p.paid_at ?? p.created_at)}</td>
                  <td className="py-2 text-ink">{p.order_name}</td>
                  <td className="tabular py-2 text-right text-ink">₩{p.amount.toLocaleString("ko-KR")}</td>
                  <td className="py-2 text-right">
                    <Badge tone={p.status === "paid" ? "good" : p.status === "failed" ? "critical" : "neutral"}>
                      {p.status === "paid" ? "결제 완료" : p.status === "failed" ? (p.failure_message ?? "실패") : p.status}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

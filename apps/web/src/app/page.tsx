import { FileCheck2, GitMerge, Timer } from "lucide-react";
import Link from "next/link";

import { DemoButton } from "@/components/landing/demo-button";
import { DemoShowcase } from "@/components/landing/demo-showcase";
import { Logo } from "@/components/layout/shell";
import { buttonVariants } from "@/components/ui/button";
import { ThemeToggle } from "@/components/ui/controls";
import { cn } from "@/lib/utils";
import { DEMO_STATIC } from "@/lib/demo/fetch";

const JOURNEY = [
  {
    date: "2025.11",
    stage: "의회 발언",
    source: "강남구의회 행정재무위원회 회의록",
    quote: "내년도 본예산에 스마트쉘터 7개소 추가 설치 사업비 3억 5천만원을 반영하겠습니다.",
    us: true,
  },
  { date: "2025.12", stage: "예산 편성", source: "2026년도 세출예산 사업명세서", quote: "세부사업: 스마트쉘터 설치  352,000 (천원)" },
  { date: "2026.03", stage: "발주계획", source: "나라장터 발주계획", quote: "2026년 스마트쉘터 제작·설치 — 발주시기 2026년 6월" },
  { date: "2026.05", stage: "사전규격", source: "나라장터 사전규격 공개", quote: "스마트 버스정류장 조성사업 — 의견등록 마감 5/20" },
  { date: "2026.06", stage: "입찰공고", source: "나라장터 입찰공고", quote: "[긴급] 스마트쉘터 구축사업 (협상에 의한 계약)", them: true },
];

const FEATURES = [
  {
    icon: FileCheck2,
    title: "원문에 있는 것만 보여줘요",
    body: "AI가 뽑아낸 금액, 연도, 근거 문장은 전부 원문과 한 번 더 맞춰 봐요. 원문에 없는 문장은 버리고, 금액이 안 맞으면 사람이 직접 확인해요.",
  },
  {
    icon: GitMerge,
    title: "흩어진 문서도 한 사업으로",
    body: "같은 사업인데 의회에서는 '스쿨존 카메라', 예산서에서는 '어린이보호구역 지능형 CCTV', 공고에서는 '스쿨존 AI 안전카메라'라고 불러요. 공고 번호와 조달 용어 사전으로 이걸 하나로 이어 붙여요.",
  },
  {
    icon: Timer,
    title: "지금 움직일 만한 순서대로",
    body: "회사와의 적합도, 사업 단계, 예상 일정으로 검토 순서를 정해요. 공고 전환 수치는 합성 데이터 기반 추정치이며, 실제 발주 확률과 얼마나 일찍 찾는지는 추가 검증이 필요해요.",
  },
];

export default function Home() {
  return (
    <div className="min-h-dvh">
      <header className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4">
        <Logo />
        <nav className="flex items-center gap-1">
          <a href="#how" className="hidden px-3 text-sm text-ink-2 hover:text-ink sm:inline">
            작동 방식
          </a>
          <ThemeToggle />
          <Link href="/login" className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}>
            로그인
          </Link>
          <Link href={DEMO_STATIC ? "/app" : "/signup"} className={cn(buttonVariants({ size: "sm" }))}>
            {DEMO_STATIC ? "데모 둘러보기" : "무료로 시작"}
          </Link>
        </nav>
      </header>

      <section className="mx-auto max-w-6xl px-4 pt-12 pb-16 md:pt-20">
        <p className="text-sm font-semibold text-accent-text">공공조달 발주 예측 서비스</p>
        <h1 className="mt-3 max-w-3xl text-[34px] leading-[1.15] font-bold tracking-tight text-ink md:text-[52px]">
          입찰공고 전 단계의 공공사업을
          <br />
          우리 회사에 맞게 찾아 드려요
        </h1>
        <p className="mt-5 max-w-2xl text-[17px] leading-relaxed text-ink-2">
          회의록의 추진 계획과 예산서의 사업비를 모아, 우리 회사가 검토할 공공사업을 추천해요.
          흩어진 문서가 같은 사업으로 이어지는 과정과 추천 근거를 직접 확인해 보세요.
        </p>
        <div className="mt-8">
          {DEMO_STATIC ? (
            <DemoShowcase>
              <Link href="/app" className={cn(buttonVariants({ variant: "secondary", size: "lg" }))}>
                추천 목록 둘러보기
              </Link>
            </DemoShowcase>
          ) : (
            <div className="flex flex-wrap gap-3">
              <DemoButton />
              <Link href="/signup" className={cn(buttonVariants({ variant: "secondary", size: "lg" }))}>
                무료로 시작하기
              </Link>
            </div>
          )}
        </div>
        {DEMO_STATIC ? <p className="mt-5 text-sm leading-relaxed text-ink-2">체험 순서: ① 연결된 문서 살펴보기 → ② 예시 원문과 근거 확인 → ③ 영업 브리핑 열기</p> : null}
      </section>

      <section id="how" className="border-y border-line bg-surface">
        <div className="mx-auto max-w-6xl px-4 py-14">
          <h2 className="text-xl font-bold text-ink">한 사업이 공고가 되기까지</h2>
          <p className="mt-1 text-sm text-muted">아래 예시는 데모용으로 만든 가상의 사업이에요.</p>
          <ol className="mt-8 grid gap-4 md:grid-cols-5">
            {JOURNEY.map((step, i) => (
              <li
                key={step.stage}
                className={cn(
                  "relative rounded-xl border bg-bg p-4",
                  step.us ? "border-accent" : step.them ? "border-line-strong" : "border-line",
                )}
              >
                <div className="h-1.5 w-full rounded-full" style={{ background: `var(--stage-${i + 1})` }} aria-hidden />
                <div className="mt-3 flex items-center justify-between">
                  <span className="text-[13px] font-semibold text-ink">{step.stage}</span>
                  <span className="tabular text-[12px] text-muted">{step.date}</span>
                </div>
                <p className="mt-1 text-[12px] text-muted">{step.source}</p>
                <p className="mt-3 text-[13px] leading-relaxed text-ink-2">「{step.quote}」</p>
                {step.us ? (
                  <p className="mt-3 text-[12px] font-semibold text-accent-text">▲ 발주 예측은 여기서 알려 드려요</p>
                ) : null}
                {step.them ? <p className="mt-3 text-[12px] font-semibold text-muted">▲ 보통 입찰 알림은 여기서야 알려줘요</p> : null}
              </li>
            ))}
          </ol>
          <p className="mt-6 text-sm text-ink-2">
            이 가상 사례에서는 첫 발언과 입찰공고 사이가 7개월이에요. 실제 사업을 얼마나 일찍 찾는지는 장기 관측으로 검증해야 해요.
          </p>
        </div>
      </section>

      <section className="mx-auto grid max-w-6xl gap-6 px-4 py-14 md:grid-cols-3">
        {FEATURES.map(({ icon: Icon, title, body }) => (
          <div key={title}>
            <Icon className="size-5 text-accent-text" aria-hidden />
            <h3 className="mt-3 text-[16px] font-semibold text-ink">{title}</h3>
            <p className="mt-2 text-[14px] leading-relaxed text-ink-2">{body}</p>
          </div>
        ))}
      </section>

      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2 px-4 py-6 text-[12px] text-muted">
          <span>발주 예측 · 포트폴리오 프로젝트예요. 데모 데이터는 전부 가상으로 만들었어요.</span>
          <span>국회도서관 지방의정포털 · 조달청 나라장터 · 행정안전부 지방재정365 Open API를 쓰도록 설계했어요</span>
        </div>
      </footer>
    </div>
  );
}

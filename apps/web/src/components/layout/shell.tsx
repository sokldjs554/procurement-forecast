"use client";

import {
  Activity,
  BellRing,
  Bot,
  ClipboardCheck,
  CreditCard,
  Database,
  FlaskConical,
  Gauge,
  ListChecks,
  Link2,
  LogOut,
  Radar,
  UserRound,
  TrendingUp,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { ThemeToggle } from "@/components/ui/controls";
import { Skeleton } from "@/components/ui/primitives";
import { ApiError } from "@/lib/api/client";
import { useLogout, useMe } from "@/lib/api/hooks";
import { DEMO_STATIC } from "@/lib/demo/fetch";
import { cn } from "@/lib/utils";

export function Logo({ className }: { className?: string }) {
  return (
    <Link href="/" className={cn("inline-flex items-center gap-2 font-bold tracking-tight text-ink", className)}>
      <span className="inline-flex size-7 items-center justify-center rounded-lg bg-accent text-accent-ink">
        <TrendingUp className="size-4" aria-hidden />
      </span>
      <span className="text-[17px]">발주 예측</span>
    </Link>
  );
}

function useRequireSession(staff = false) {
  const me = useMe();
  const router = useRouter();
  const pathname = usePathname();
  useEffect(() => {
    if (me.error instanceof ApiError && me.error.status === 401) {
      router.replace(`/login?next=${encodeURIComponent(pathname)}`);
    }
    if (staff && me.data && !me.data.user.is_staff) router.replace("/app");
  }, [me.error, me.data, router, pathname, staff]);
  return me;
}

function UserMenu() {
  const me = useMe();
  const logout = useLogout();
  const router = useRouter();
  if (!me.data) return <Skeleton className="h-8 w-40" />;
  return (
    <div className="flex items-center gap-2">
      <Link
        href="/app/billing"
        className="hidden items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-[12px] text-ink-2 hover:bg-surface-2 sm:inline-flex"
        title={DEMO_STATIC ? "예시 잔액이에요. 데모 체험에서는 차감되지 않아요." : "영업 브리핑을 만들 때 쓰는 크레딧"}
      >
        <CreditCard className="size-3.5" aria-hidden />
        {DEMO_STATIC ? "예시 크레딧" : "크레딧"} <span className="tabular font-semibold text-ink">{me.data.org.credit_balance}</span>
      </Link>
      <span className="hidden text-[13px] text-ink-2 md:inline">
        {me.data.org.name} · {me.data.user.name}
      </span>
      <ThemeToggle />
      <button
        type="button"
        onClick={() => logout.mutate(undefined, { onSuccess: () => router.replace("/login") })}
        className="inline-flex size-9 items-center justify-center rounded-lg text-ink-2 hover:bg-surface-2 hover:text-ink"
        aria-label="로그아웃"
      >
        <LogOut className="size-4" aria-hidden />
      </button>
    </div>
  );
}

const APP_NAV = [
  { href: "/app", label: "기회 피드", icon: Radar, exact: true },
  { href: "/app/profile", label: "회사 프로필", icon: UserRound },
  { href: "/app/alerts", label: "알림", icon: BellRing },
  { href: "/app/billing", label: "요금·크레딧", icon: CreditCard },
];

export function AppShell({ children }: { children: ReactNode }) {
  const me = useRequireSession();
  const pathname = usePathname();
  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-30 border-b border-line bg-bg/90 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between gap-4 px-4">
          <div className="flex items-center gap-6">
            <Logo />
            <nav className="hidden items-center gap-1 md:flex" aria-label="주 메뉴">
              {APP_NAV.map(({ href, label, icon: Icon, exact }) => {
                const active = exact ? pathname === href : pathname.startsWith(href);
                return (
                  <Link
                    key={href}
                    href={href}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-[14px]",
                      active ? "bg-surface-2 font-semibold text-ink" : "text-ink-2 hover:text-ink",
                    )}
                  >
                    <Icon className="size-4" aria-hidden />
                    {label}
                  </Link>
                );
              })}
              {me.data?.user.is_staff ? (
                <Link href="/admin" className="inline-flex h-9 items-center gap-1.5 rounded-lg px-3 text-[14px] text-ink-2 hover:text-ink">
                  <Gauge className="size-4" aria-hidden />
                  운영 콘솔
                </Link>
              ) : null}
            </nav>
          </div>
          <UserMenu />
        </div>
        <nav className="flex gap-1 overflow-x-auto border-t border-line px-3 py-1.5 md:hidden" aria-label="주 메뉴">
          {APP_NAV.map(({ href, label }) => (
            <Link key={href} href={href} className="shrink-0 rounded-md px-2.5 py-1 text-[13px] text-ink-2">
              {label}
            </Link>
          ))}
        </nav>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6 md:py-8">
        {me.data ? children : <Skeleton className="h-64 w-full" />}
      </main>
    </div>
  );
}

const ADMIN_NAV = [
  { href: "/admin", label: "개요", icon: Activity, exact: true },
  { href: "/admin/sources", label: "수집원", icon: Database },
  { href: "/admin/jobs", label: "작업 로그", icon: ListChecks },
  { href: "/admin/review", label: "검토 대기열", icon: ClipboardCheck },
  ...(!DEMO_STATIC ? [{ href: "/admin/relations", label: "사업·계약 관계", icon: Link2 }] : []),
  { href: "/admin/llm", label: "LLM 비용", icon: Bot },
  { href: "/admin/evals", label: "평가·백테스트", icon: FlaskConical },
];

export function AdminShell({ children }: { children: ReactNode }) {
  const me = useRequireSession(true);
  const pathname = usePathname();
  return (
    <div className="flex min-h-dvh">
      {/* The rail's background spans the page; the nav inside it stays pinned while content scrolls. */}
      <div className="hidden w-56 shrink-0 border-r border-line bg-surface md:block">
      <aside className="sticky top-0 flex h-dvh flex-col px-3 py-4">
        <Logo className="px-2" />
        <p className="mt-1 px-2 text-[11px] font-medium tracking-wide text-muted uppercase">운영 콘솔</p>
        <nav className="mt-6 space-y-0.5" aria-label="운영 메뉴">
          {ADMIN_NAV.map(({ href, label, icon: Icon, exact }) => {
            const active = exact ? pathname === href : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex h-9 items-center gap-2 rounded-lg px-2.5 text-[14px]",
                  active ? "bg-surface-2 font-semibold text-ink" : "text-ink-2 hover:bg-surface-2/60 hover:text-ink",
                )}
              >
                <Icon className="size-4" aria-hidden />
                {label}
              </Link>
            );
          })}
        </nav>
        <div className="mt-auto space-y-2 px-2">
          <Link href="/app" className="block text-[13px] text-ink-2 hover:text-ink">
            ← 고객 화면으로
          </Link>
          <ThemeToggle />
        </div>
      </aside>
      </div>
      <div className="min-w-0 flex-1">
        <nav className="flex gap-1 overflow-x-auto border-b border-line px-3 py-2 md:hidden" aria-label="운영 메뉴">
          {ADMIN_NAV.map(({ href, label }) => (
            <Link key={href} href={href} className="shrink-0 rounded-md px-2.5 py-1 text-[13px] text-ink-2">
              {label}
            </Link>
          ))}
        </nav>
        <main className="mx-auto max-w-6xl px-4 py-6 md:px-8 md:py-8">
          {me.data?.user.is_staff ? children : <Skeleton className="h-64 w-full" />}
        </main>
      </div>
    </div>
  );
}

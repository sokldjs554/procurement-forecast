"use client";

import { Sparkles } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { ChipGroup, TagInput } from "@/components/ui/controls";
import { Card, CardHeader, ErrorNote, Field, Input, PageHeader, Skeleton, Textarea } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/toast";
import type { Schemas } from "@/lib/api/client";
import { useCategories, useMe, useProfile, useSaveProfile } from "@/lib/api/hooks";
import { SIDO, currentRegions } from "@/lib/regions";

const PLAN_REGION_LIMIT: Record<string, number | null> = { free: 1, pro: 5, team: null };

const toEok = (won: number | null | undefined) => (won ? String(won / 100_000_000) : "");
const fromEok = (text: string) => {
  const n = Number(text);
  return text.trim() && Number.isFinite(n) && n > 0 ? Math.round(n * 100_000_000) : null;
};

function ProfileForm({ initial }: { initial: Schemas["ProfileIO"] }) {
  const toast = useToast();
  const save = useSaveProfile();
  const categories = useCategories();
  const me = useMe();
  const limit = PLAN_REGION_LIMIT[me.data?.org.plan ?? "free"] ?? 1;
  const [form, setForm] = useState({ ...initial, region_codes: currentRegions(initial.region_codes ?? []) });
  const [budgetMin, setBudgetMin] = useState(toEok(initial.budget_min));
  const [budgetMax, setBudgetMax] = useState(toEok(initial.budget_max));

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate(
      { ...form, budget_min: fromEok(budgetMin), budget_max: fromEok(budgetMax) },
      { onSuccess: () => toast("good", "저장했어요. 추천을 새로 계산하는 중이에요.") },
    );
  };

  return (
    <form onSubmit={onSubmit} className="space-y-6">
      <Card>
        <CardHeader title="무엇을 파나요?" description="이 소개와 비슷한 사업을 찾아서 추천해요. 파는 제품이나 서비스, 대표 납품 실적을 적어 주세요." />
        <div className="space-y-5 p-5">
          <Field label="회사 소개" htmlFor="description">
            <Textarea
              id="description"
              value={form.description ?? ""}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              placeholder="예: 냉난방·공기청정 기능의 스마트쉘터와 스마트폴을 제조·설치하며, 지자체 납품 실적 30건"
              maxLength={2000}
            />
          </Field>
          <Field label="관심 키워드" htmlFor="keywords" hint="Enter로 하나씩 추가해요. 공고명이나 예산서 사업명에 실제로 나올 법한 단어가 잘 맞아요.">
            <TagInput
              id="keywords"
              value={form.keywords ?? []}
              onChange={(keywords) => setForm({ ...form, keywords })}
              placeholder="스마트쉘터, 선별관제, 디지털트윈…"
            />
          </Field>
          <Field label="제외 키워드" htmlFor="excludes" hint="이 단어가 들어간 사업은 순위를 한참 뒤로 내려요.">
            <TagInput
              id="excludes"
              value={form.exclude_keywords ?? []}
              onChange={(exclude_keywords) => setForm({ ...form, exclude_keywords })}
              placeholder="청소용역, 유지보수…"
            />
          </Field>
        </div>
      </Card>

      <Card>
        <CardHeader title="어디에, 얼마 규모로?" />
        <div className="space-y-5 p-5">
          <Field label="관심 분야" htmlFor="categories">
            <ChipGroup
              ariaLabel="관심 분야"
              options={categories.data ?? []}
              value={form.categories ?? []}
              onChange={(next) => setForm({ ...form, categories: next })}
            />
          </Field>
          <Field
            label="관심 지역 (시·도)"
            htmlFor="regions"
            hint={
              limit === null
                ? "안 고르면 전국을 다 봐요."
                : `지금 플랜에서는 ${limit}곳까지 고를 수 있어요. 안 고르면 전국을 다 봐요.`
            }
          >
            <ChipGroup
              ariaLabel="관심 지역"
              options={SIDO}
              value={form.region_codes ?? []}
              onChange={(next) => {
                if (limit !== null && next.length > limit) {
                  toast("critical", `지금 플랜에서는 관심 지역을 ${limit}곳까지만 고를 수 있어요`);
                  return;
                }
                setForm({ ...form, region_codes: next });
              }}
            />
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="최소 사업 규모 (억 원)" htmlFor="bmin">
              <Input id="bmin" inputMode="decimal" value={budgetMin} onChange={(e) => setBudgetMin(e.target.value)} placeholder="1" />
            </Field>
            <Field label="최대 사업 규모 (억 원)" htmlFor="bmax">
              <Input id="bmax" inputMode="decimal" value={budgetMax} onChange={(e) => setBudgetMax(e.target.value)} placeholder="20" />
            </Field>
          </div>
        </div>
      </Card>

      {save.error ? <ErrorNote error={save.error} /> : null}
      <div className="flex justify-end">
        <Button type="submit" loading={save.isPending}>
          저장하고 추천 다시 받기
        </Button>
      </div>
    </form>
  );
}

function ProfileContent() {
  const profile = useProfile();
  const params = useSearchParams();
  const welcome = params.get("welcome") === "1";
  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader title="회사 프로필" description="여기 적은 내용을 보고 우리 회사에 맞는 사업을 골라요." />
      {welcome ? (
        <div className="flex items-start gap-3 rounded-xl border border-accent/30 bg-accent-soft px-4 py-3 text-sm text-ink">
          <Sparkles className="mt-0.5 size-4 shrink-0 text-accent-text" aria-hidden />
          반가워요! 파는 제품과 관심 분야만 알려 주시면, 의회 회의록·예산서·나라장터에서 찾은 사업으로 맞춤 피드를 바로 만들어 드릴게요.
        </div>
      ) : null}
      {profile.isLoading ? <Skeleton className="h-96" /> : profile.data ? <ProfileForm initial={profile.data} /> : <ErrorNote error={profile.error} />}
    </div>
  );
}

export default function ProfilePage() {
  return (
    <Suspense>
      <ProfileContent />
    </Suspense>
  );
}

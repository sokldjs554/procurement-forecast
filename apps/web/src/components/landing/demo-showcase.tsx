"use client";

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { DemoButton } from "./demo-button";

type Showcase = { id: number; title: string; document_count: number };

/**
 * The snapshot selects this case from its current seed; database IDs can change each week.
 * `children` sit beside the button and the case caption goes under the whole row, so the
 * caption cannot widen the button's column and push the neighbouring action away.
 */
export function DemoShowcase({ children }: { children?: ReactNode }) {
  const showcase = useQuery({
    queryKey: ["demo-showcase"],
    staleTime: Infinity,
    retry: false,
    queryFn: async (): Promise<Showcase | null> => {
      const response = await fetch(`${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/demo/showcase.json`);
      if (!response.ok) return null;
      const data = await response.json() as Showcase | null;
      return data && Number.isInteger(data.id) && data.id > 0 && typeof data.title === "string"
        && Number.isInteger(data.document_count) && data.document_count > 0 ? data : null;
    },
  });
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        {showcase.data
          ? <DemoButton href={`/app/opportunities/${showcase.data.id}`} label="대표 사례 체험하기" />
          : <DemoButton label="데모 둘러보기" />}
        {children}
      </div>
      {showcase.data ? <p className="text-[12px] text-ink-2">{showcase.data.title} · 문서 {showcase.data.document_count}개 연결</p> : null}
    </div>
  );
}

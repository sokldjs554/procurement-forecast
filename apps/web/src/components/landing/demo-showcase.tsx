"use client";

import { useQuery } from "@tanstack/react-query";

import { DemoButton } from "./demo-button";

type Showcase = { id: number; title: string; document_count: number };

/** The snapshot selects this case from its current seed; database IDs can change each week. */
export function DemoShowcase() {
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
  if (!showcase.data) return <DemoButton label="데모 둘러보기" />;
  return (
    <div className="space-y-2">
      <DemoButton href={`/app/opportunities/${showcase.data.id}`} label="대표 사례 체험하기" />
      <p className="text-[12px] text-ink-2">{showcase.data.title} · 문서 {showcase.data.document_count}개 연결</p>
    </div>
  );
}

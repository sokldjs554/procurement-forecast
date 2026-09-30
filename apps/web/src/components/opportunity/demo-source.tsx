"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import type { Schemas } from "@/lib/api/client";

import { EvidenceContext } from "./evidence";

type ExampleDocument = { id: number; title: string; text: string | null; synthetic: true };

/** Full parsed example text, loaded only when the visitor opens it. */
export function DemoSource({ documentId, evidence }: { documentId: number; evidence: Schemas["EvidenceOut"][] }) {
  const [open, setOpen] = useState(false);
  const source = useQuery({
    queryKey: ["demo-document", documentId],
    enabled: open,
    retry: false,
    queryFn: async (): Promise<ExampleDocument> => {
      const response = await fetch(`${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/demo/documents/${documentId}.json`);
      if (!response.ok) throw new Error("예시 원문을 불러오지 못했어요. 잠시 후 다시 열어 주세요.");
      const data = await response.json() as ExampleDocument;
      if (data.id !== documentId || data.synthetic !== true || typeof data.text !== "string") {
        throw new Error("이 예시의 전체 원문 텍스트가 준비되지 않았어요.");
      }
      return data;
    },
  });
  return (
    <details className="mt-3 rounded-lg border border-line p-3" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer text-[13px] font-medium text-accent-text">예시 원문 보기</summary>
      {open ? <div className="mt-3 space-y-3">
        <p className="text-[12px] leading-relaxed text-muted">데모용으로 만든 가상 문서의 전체 추출 텍스트예요. 실제 기관 문서가 아니며, 근거 문장은 강조해서 보여드려요.</p>
        {source.isLoading ? <p className="text-sm text-muted" role="status">예시 원문을 불러오고 있어요…</p> : null}
        {source.error ? <p className="text-sm text-critical" role="alert">{source.error.message}</p> : null}
        {source.data ? <>
          <p className="text-sm font-semibold text-ink">{source.data.title}</p>
          <div className="max-h-[32rem] overflow-y-auto break-words">
            <EvidenceContext context={source.data.text} contextOffset={0} evidence={evidence} maxChars={Infinity} />
          </div>
        </> : null}
      </div> : null}
    </details>
  );
}

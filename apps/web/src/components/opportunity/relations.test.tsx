import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "@/lib/api/client";
import { relationFixture } from "@/test/relations";

import { OpportunityRelations } from "./relations";

const flags = vi.hoisted(() => ({ demo: false }));
vi.mock("@/lib/demo/fetch", () => ({ get DEMO_STATIC() { return flags.demo; }, demoFetch: vi.fn() }));
afterEach(() => { vi.restoreAllMocks(); flags.demo = false; });

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><OpportunityRelations opportunityId={1} /></QueryClientProvider>);
}

it("shows stale confirmations as needing review and loads every related contract page", async () => {
  const get = vi.spyOn(api, "GET").mockImplementation(async (_path, options) => {
    const params = options as { params: { query: { after_id: number } } };
    const second = params.params.query.after_id === 7;
    return { data: { items: [second ? { ...relationFixture, id: 8, contract_id: 3, contract: { ...relationFixture.contract!, id: 3, title: "별도 설비 계약" }, status: "proposed", effective_status: "proposed" } : relationFixture], next_after_id: second ? null : 7 }, response: new Response(null, { status: 200 }) } as never;
  });
  show();
  expect(await screen.findByText("근거 변경 · 재검토 필요")).toBeInTheDocument();
  expect(screen.queryByText("확정", { exact: true })).not.toBeInTheDocument();
  expect(screen.getByText("저장 당시 실제 원문")).toBeInTheDocument();
  expect(screen.queryByText("모델 인용")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "관계 더 보기" }));
  expect(await screen.findByRole("link", { name: "별도 설비 계약" })).toHaveAttribute("href", "/app/opportunities/3");
  expect(screen.getByRole("link", { name: "냉난방기 구매 2" })).toBeInTheDocument();
  expect(get).toHaveBeenCalledTimes(2);
});

it("hides the panel and never requests unsupported relation endpoints in the static demo", () => {
  flags.demo = true;
  const get = vi.spyOn(api, "GET");
  const { container } = show();
  expect(container).toBeEmptyDOMElement();
  expect(get).not.toHaveBeenCalled();
});

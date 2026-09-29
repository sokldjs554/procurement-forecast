import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "@/lib/api/client";
import { relationFixture, relationOpportunity } from "@/test/relations";

import RelationsPage from "./page";

afterEach(() => vi.restoreAllMocks());

it("opens versioned history, reloads a conflict, and paginates the mixed-group audit", async () => {
  let version = 3;
  const get = vi.spyOn(api, "GET").mockImplementation(async (path, options) => {
    const params = options as { params?: { path?: { opportunity_id?: number }; query?: { after_id?: number } } } | undefined;
    let data: unknown;
    if (path === "/api/admin/relations") data = { items: [relationFixture], next_after_id: null };
    else if (path === "/api/admin/relations/{relation_id}") data = { ...relationFixture, version };
    else if (path === "/api/opportunities/{opportunity_id}") data = relationOpportunity(params?.params?.path?.opportunity_id ?? 1);
    else if (path === "/api/admin/relations/mixed") {
      const second = params?.params?.query?.after_id === 50;
      data = { items: [{ opportunity: { id: second ? 51 : 50, title: second ? "다음 혼합 기회" : "혼합 사업과 계약", institution_code: "LG-41130", stage: "bid_notice" }, project_signal_ids: [10], contract_signal_ids: [20, 21], bid_notice_numbers: ["B1", "B2"], reason: "mixed_project_contract" }], next_after_id: second ? null : 50 };
    } else throw new Error(`Unexpected GET ${path}`);
    return { data, response: new Response(null, { status: 200 }) } as never;
  });
  vi.spyOn(api, "POST").mockImplementation(async () => {
    version = 4;
    return { error: { detail: "stale_version" }, response: new Response(null, { status: 409 }) } as never;
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retryDelay: 0 } } });
  render(<QueryClientProvider client={client}><RelationsPage /></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: "관계 #7 검토·이력" }));
  expect(await screen.findByText("검토 이력 (1건)")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("관계 판단"), { target: { value: "rejected" } });
  fireEvent.change(screen.getByLabelText("판단 근거"), { target: { value: "관계 기각" } });
  fireEvent.click(screen.getByRole("button", { name: "관계 저장" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("다른 검토자가 관계를 변경했어요");
  fireEvent.click(screen.getByRole("button", { name: "최신 상태 불러오기" }));
  expect(await screen.findByText("관계 #7 다시 판단 · 버전 4")).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "혼합 그룹 감사" }));
  expect(await screen.findByRole("link", { name: "혼합 사업과 계약" })).toHaveAttribute("href", "/app/opportunities/50");
  fireEvent.click(screen.getByRole("button", { name: "혼합 그룹 더 보기" }));
  expect(await screen.findByRole("link", { name: "다음 혼합 기회" })).toBeInTheDocument();
  expect(get.mock.calls.filter((call) => call[0] === "/api/admin/relations/mixed")).toHaveLength(2);
});

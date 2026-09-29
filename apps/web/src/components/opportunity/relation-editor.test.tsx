import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "@/lib/api/client";
import { relationFixture, relationOpportunity } from "@/test/relations";

import { RelationEditor } from "./relation-editor";

afterEach(() => vi.restoreAllMocks());

function setup(relation = undefined as typeof relationFixture | undefined, onReload = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retryDelay: 0 } } });
  vi.spyOn(api, "GET").mockImplementation(async (_path, options) => {
    const params = options as { params: { path: { opportunity_id: number } } };
    return { data: relationOpportunity(params.params.path.opportunity_id), response: new Response(null, { status: 200 }) } as never;
  });
  render(<QueryClientProvider client={qc}><RelationEditor relation={relation} onSaved={vi.fn()} onReload={onReload} /></QueryClientProvider>);
}

it("requires both source selections and keeps a new relation proposed unless explicitly confirmed", async () => {
  const post = vi.spyOn(api, "POST").mockResolvedValue({ data: { ...relationFixture, status: "proposed", effective_status: "proposed", version: 1 }, response: new Response(null, { status: 200 }) } as never);
  setup();
  fireEvent.change(screen.getByLabelText("사업 기회 ID"), { target: { value: "1" } });
  fireEvent.change(screen.getByLabelText("계약 기회 ID"), { target: { value: "2" } });
  fireEvent.click(screen.getByRole("button", { name: "양쪽 기회 불러오기" }));
  expect(await screen.findByText("도서관 개선 예산을 편성합니다.")).toBeInTheDocument();
  expect(screen.getByText("도서관 냉난방기를 구매합니다.")).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "관계 판단" })).toHaveValue("proposed");
  fireEvent.change(screen.getByLabelText("판단 근거"), { target: { value: "예산 사업에서 분리된 구매 계약입니다." } });
  const save = screen.getByRole("button", { name: "관계 저장" });
  expect(save).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /근거 신호 10/ }));
  expect(save).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /근거 신호 20/ }));
  fireEvent.click(save);
  await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
  expect(post.mock.calls[0]![1]).toMatchObject({ body: { project_id: 1, contract_id: 2, expected_version: 0, status: "proposed", evidence_signal_ids: [10, 20] } });
});

it("keeps a stale-version rejection visible and offers an explicit reload instead of overwriting", async () => {
  const reload = vi.fn();
  const post = vi.spyOn(api, "POST").mockResolvedValue({ error: { detail: "stale_version" }, response: new Response(null, { status: 409 }) } as never);
  setup(relationFixture, reload);
  fireEvent.change(screen.getByLabelText("관계 판단"), { target: { value: "rejected" } });
  fireEvent.change(screen.getByLabelText("판단 근거"), { target: { value: "관계 기각" } });
  fireEvent.click(screen.getByRole("button", { name: "관계 저장" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("다른 검토자가 관계를 변경했어요");
  expect(post).toHaveBeenCalledTimes(1);
  expect(post.mock.calls[0]![1]).toMatchObject({ body: { expected_version: 3, status: "rejected" } });
  expect(screen.getByRole("button", { name: "관계 저장" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "최신 상태 불러오기" }));
  expect(reload).toHaveBeenCalledOnce();
});

it("reports backend source-validation failures without retrying or claiming a saved relation", async () => {
  const post = vi.spyOn(api, "POST").mockResolvedValue({ error: { detail: { reason: "invalid_relation", issues: ["source_unavailable:10"] } }, response: new Response(null, { status: 422 }) } as never);
  setup(relationFixture);
  fireEvent.click(await screen.findByRole("checkbox", { name: /근거 신호 10/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /근거 신호 20/ }));
  fireEvent.change(screen.getByLabelText("관계 판단"), { target: { value: "confirmed" } });
  fireEvent.change(screen.getByLabelText("판단 근거"), { target: { value: "양쪽 원문 확인" } });
  fireEvent.click(screen.getByRole("button", { name: "관계 저장" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("원문이 없거나 처리가 완료되지 않았어요. (신호 #10)");
  expect(post).toHaveBeenCalledTimes(1);
  expect(post.mock.calls[0]![1]).toMatchObject({ body: { status: "confirmed", expected_version: 3 } });
});

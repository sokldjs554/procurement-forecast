import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { api } from "./client";
import { useDecideRelation } from "./relations";

afterEach(() => vi.restoreAllMocks());

it("keeps the decision key and expected version across a network retry", async () => {
  const client = new QueryClient({ defaultOptions: { mutations: { retryDelay: 0 } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const post = vi.spyOn(api, "POST")
    .mockRejectedValueOnce(new TypeError("response lost"))
    .mockResolvedValueOnce({ data: { id: 1 }, response: new Response(null, { status: 200 }) } as never);
  const { result } = renderHook(() => useDecideRelation(), { wrapper });
  act(() => result.current.mutate({
    body: { project_id: 1, contract_id: 2, status: "confirmed", expected_version: 3, evidence_signal_ids: [10, 20], note: "원문 대조" },
    idempotencyKey: "relation-click-1",
  }));
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(post.mock.calls).toHaveLength(2);
  for (const call of post.mock.calls) {
    expect(call[0]).toBe("/api/admin/relations");
    expect(call[1]).toMatchObject({ params: { header: { "Idempotency-Key": "relation-click-1" } }, body: { expected_version: 3 } });
  }
});

it("does not retry a stale version decision", async () => {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const post = vi.spyOn(api, "POST").mockResolvedValue({ error: { detail: "stale_version" }, response: new Response(null, { status: 409 }) } as never);
  const { result } = renderHook(() => useDecideRelation(), { wrapper });
  act(() => result.current.mutate({ body: { project_id: 1, contract_id: 2, status: "rejected", expected_version: 1, note: "다른 사업", evidence_signal_ids: [] }, idempotencyKey: "relation-click-2" }));
  await waitFor(() => expect(result.current.isError).toBe(true));
  expect(post).toHaveBeenCalledTimes(1);
});

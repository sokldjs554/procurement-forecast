import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DemoShowcase } from "./demo-showcase";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/api/hooks", () => ({ useMe: () => ({ data: {} }), useLogin: () => ({ isPending: false }) }));
afterEach(() => vi.unstubAllGlobals());

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><DemoShowcase /></QueryClientProvider>);
}

describe("DemoShowcase", () => {
  it("links to the current recorded case rather than a hard-coded database ID", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ id: 991, title: "문서 연결 사례", document_count: 4 })));
    show();
    expect(await screen.findByRole("link", { name: "대표 사례 체험하기" })).toHaveAttribute("href", "/app/opportunities/991");
  });

  it("keeps the feed accessible when there is no usable showcase", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ id: "invalid" })));
    show();
    expect(await screen.findByRole("link", { name: "데모 둘러보기" })).toHaveAttribute("href", "/app");
  });
});

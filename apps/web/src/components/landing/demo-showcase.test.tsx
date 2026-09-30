import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DemoShowcase } from "./demo-showcase";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/lib/api/hooks", () => ({ useMe: () => ({ data: {} }), useLogin: () => ({ isPending: false }) }));
afterEach(() => vi.unstubAllGlobals());

function show(children?: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><DemoShowcase>{children}</DemoShowcase></QueryClientProvider>);
}

describe("DemoShowcase", () => {
  it("links to the current recorded case rather than a hard-coded database ID", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ id: 991, title: "문서 연결 사례", document_count: 4 })));
    show();
    expect(await screen.findByRole("link", { name: "대표 사례 체험하기" })).toHaveAttribute("href", "/app/opportunities/991");
  });

  it("keeps a second action in the button row and the case caption under the row", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ id: 991, title: "문서 연결 사례", document_count: 4 })));
    show(<a href="/app">추천 목록 둘러보기</a>);
    const caption = await screen.findByText("문서 연결 사례 · 문서 4개 연결");
    const row = screen.getByRole("link", { name: "추천 목록 둘러보기" }).parentElement;
    expect(row).toContainElement(screen.getByRole("link", { name: "대표 사례 체험하기" }));
    expect(row).not.toContainElement(caption);
  });

  it("keeps the feed accessible when there is no usable showcase", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ id: "invalid" })));
    show();
    expect(await screen.findByRole("link", { name: "데모 둘러보기" })).toHaveAttribute("href", "/app");
  });
});

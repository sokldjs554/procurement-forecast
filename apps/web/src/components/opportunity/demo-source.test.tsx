import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DemoSource } from "./demo-source";

afterEach(() => vi.unstubAllGlobals());

const evidence = [{ quote: "예산에 반영하겠습니다", start: 6, end: 17, found: true, score: 1, method: "exact" }];

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><DemoSource documentId={12} evidence={evidence} /></QueryClientProvider>);
}

describe("DemoSource", () => {
  it("opens the full example text in place and highlights the verified evidence", async () => {
    const request = vi.fn(async () => new Response(JSON.stringify({
      id: 12, title: "예시 회의록", text: "과장 답변:예산에 반영하겠습니다.\n다음 안건 전체 내용", synthetic: true,
    })));
    vi.stubGlobal("fetch", request);
    const { container } = show();
    expect(request).not.toHaveBeenCalled();
    const details = container.querySelector("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    await screen.findByText(/다음 안건 전체 내용/);
    expect(container.querySelector("mark")?.textContent).toBe("예산에 반영하겠습니다");
    expect(request).toHaveBeenCalledWith("/demo/documents/12.json");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows an explicit error instead of a broken external link when the recorded text is missing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { status: 404 })));
    const { container } = show();
    const details = container.querySelector("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/원문/));
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

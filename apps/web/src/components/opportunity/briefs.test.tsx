import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToastProvider } from "@/components/ui/toast";
import type { Schemas } from "@/lib/api/client";

import { Briefs } from "./briefs";

const demo = vi.hoisted(() => ({ enabled: true }));
vi.mock("@/lib/demo/fetch", async (original) => ({
  ...await original<typeof import("@/lib/demo/fetch")>(),
  get DEMO_STATIC() { return demo.enabled; },
}));
vi.mock("@/lib/api/hooks", () => ({
  useMe: () => ({ data: { org: { credit_balance: 0 } } }),
  useCreateBrief: () => ({ isPending: false, mutate: (_key: string, callbacks: { onSuccess: () => void }) => callbacks.onSuccess() }),
}));
afterEach(() => { demo.enabled = true; });

const detail = { id: 1, briefs: [] } as unknown as Schemas["OpportunityDetail"];

describe("Briefs", () => {
  it("lets a demo visitor view the example with zero credits and never claims a charge", async () => {
    render(<ToastProvider><Briefs detail={detail} /></ToastProvider>);
    const button = screen.getByRole("button", { name: /브리핑/ });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(screen.getByRole("status")).toHaveTextContent(/차감되지/);
    expect(screen.queryByText(/크레딧 3개를 썼어요/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "충전하기" })).not.toBeInTheDocument();
  });

  it("still blocks a real server-backed brief when there are not enough credits", () => {
    demo.enabled = false;
    render(<Briefs detail={detail} />);
    expect(screen.getByRole("button", { name: /브리핑/ })).toBeDisabled();
    expect(screen.getByRole("link", { name: "충전하기" })).toBeInTheDocument();
  });
});

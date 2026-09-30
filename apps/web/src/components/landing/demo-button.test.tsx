import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DemoButton } from "./demo-button";

const push = vi.fn();
const mutate = vi.fn();
let me: { data: unknown } = { data: undefined };

vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/api/hooks", () => ({
  useMe: () => me,
  useLogin: () => ({ mutate, isPending: false }),
}));

beforeEach(() => {
  push.mockReset();
  mutate.mockReset();
  me = { data: undefined };
});

describe("DemoButton", () => {
  it("opens the selected walkthrough after explicit demo sign-in", async () => {
    mutate.mockImplementation((_body, opts: { onSuccess: () => void }) => opts.onSuccess());
    render(<DemoButton href="/app/opportunities/991" label="대표 사례 체험하기" />);
    await userEvent.click(screen.getByRole("button", { name: "대표 사례 체험하기" }));
    expect(push).toHaveBeenCalledWith("/app/opportunities/991");
  });
  it("does not sign anyone in just by rendering", () => {
    render(<DemoButton />);
    expect(screen.getByRole("button", { name: /데모로 둘러보기/ })).toBeInTheDocument();
    expect(mutate).not.toHaveBeenCalled();
  });

  it("signs in to the demo account on click and opens the feed", async () => {
    mutate.mockImplementation((_body, opts: { onSuccess: () => void }) => opts.onSuccess());
    render(<DemoButton />);

    await userEvent.click(screen.getByRole("button", { name: /데모로 둘러보기/ }));

    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate.mock.calls[0]![0]).toEqual({ email: "demo@example.com", password: "demo-pass-1234" });
    expect(push).toHaveBeenCalledWith("/app");
  });

  it("sends people to the login page if the demo sign-in fails", async () => {
    mutate.mockImplementation((_body, opts: { onError: () => void }) => opts.onError());
    render(<DemoButton />);

    await userEvent.click(screen.getByRole("button", { name: /데모로 둘러보기/ }));

    expect(push).toHaveBeenCalledWith("/login");
  });

  it("leaves a signed-in session alone and links to that user's own feed", () => {
    me = { data: { user: { email: "someone@example.com" } } };
    render(<DemoButton />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /내 피드로 가기/ })).toHaveAttribute("href", "/app");
    expect(mutate).not.toHaveBeenCalled();
  });
});

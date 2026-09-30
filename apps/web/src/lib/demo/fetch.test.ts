import { afterEach, describe, expect, it, vi } from "vitest";

import type { components } from "@/lib/api/schema";

import { demoFetch, fileKey, filterFeed } from "./fetch";

type Card = components["schemas"]["OpportunityCard"];

const card = (id: number, over: Partial<Card>): Card =>
  ({ id, title: `사업 ${id}`, status: "open", category: "smart_city", stage: "council", feedback: null, ...over }) as Card;

describe("fileKey", () => {
  // The same names app/demo/snapshot.py writes (tests/unit/test_demo_snapshot.py checks that side).
  it("names a request the way the snapshot names its file", () => {
    expect(fileKey("/api/me", new URLSearchParams())).toBe("me");
    expect(fileKey("/api/admin/jobs", new URLSearchParams("status=failed&limit=100"))).toBe(
      "admin_jobs_limit_100_status_failed",
    );
    expect(fileKey("/api/admin/llm/usage", new URLSearchParams("days=7"))).toBe("admin_llm_usage_days_7");
  });
});

describe("filterFeed", () => {
  const all = [
    card(1, { stage: "council" }),
    card(2, { stage: "budget", category: "cctv" }),
    card(3, { status: "closed" }),
    card(4, { feedback: "dismissed" }),
    card(5, { title: "스마트폴 구축" }),
  ];

  it("keeps open work and hides dismissed cards by default, as the API does", () => {
    const page = filterFeed(all, new URLSearchParams());
    expect(page.items.map((c) => c.id)).toEqual([1, 2, 5]);
    expect(page.stage_counts).toEqual({ council: 2, budget: 1 });
  });

  it("counts stages before the stage filter, so the tabs keep their numbers", () => {
    const page = filterFeed(all, new URLSearchParams("stage=budget"));
    expect(page.items.map((c) => c.id)).toEqual([2]);
    expect(page.total).toBe(1);
    expect(page.stage_counts).toEqual({ council: 2, budget: 1 });
  });

  it("filters by status, category and title, and pages with an offset cursor", () => {
    expect(filterFeed(all, new URLSearchParams("status=closed")).items.map((c) => c.id)).toEqual([3]);
    expect(filterFeed(all, new URLSearchParams("category=cctv")).items.map((c) => c.id)).toEqual([2]);
    expect(filterFeed(all, new URLSearchParams("q=스마트폴")).items.map((c) => c.id)).toEqual([5]);
    const first = filterFeed(all, new URLSearchParams("limit=2"));
    expect(first.next_cursor).toBe("2");
    const second = filterFeed(all, new URLSearchParams("limit=2&cursor=2"));
    expect(second.items.map((c) => c.id)).toEqual([5]);
    expect(second.next_cursor).toBeNull();
  });
});

describe("demoFetch", () => {
  it("opens an example brief without claiming a charge or duplicating it on repeat visits", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.endsWith("briefs/999.json")) return Response.json({
        id: 27, content_md: "예시 브리핑", model: "heuristic-v1", credits_spent: 3, created_at: "2026-09-30T00:00:00Z",
      });
      if (url.endsWith("opportunities/999.json")) return Response.json({ id: 999, briefs: [], feedback: null });
      return new Response("", { status: 404 });
    }));
    const open = () => demoFetch(new Request("http://localhost/api/opportunities/999/briefs", { method: "POST" }));
    expect((await (await open()).json()).credits_spent).toBe(0);
    await open();
    const detail = await demoFetch(new Request("http://localhost/api/opportunities/999"));
    expect((await detail.json()).briefs).toHaveLength(1);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("answers reads from the recorded files and refuses writes", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ email: "demo@example.com" })));
    vi.stubGlobal("fetch", fetchMock);
    const me = await demoFetch(new Request("http://localhost/api/me"));
    expect(me.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith("/demo/demo/me.json");

    const put = await demoFetch(new Request("http://localhost/api/profile", { method: "PUT", body: "{}" }));
    expect(put.status).toBe(403);
    expect((await put.json()).detail).toMatch(/저장되지 않아요/);
  });

  it("signs in only the demo accounts, and signs out", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}")));
    const login = (email: string, password: string) =>
      demoFetch(
        new Request("http://localhost/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
      );
    expect((await login("someone@example.com", "x")).status).toBe(401);
    expect((await login("admin@example.com", "admin-pass-1234")).status).toBe(200);
    await demoFetch(new Request("http://localhost/api/auth/logout", { method: "POST" }));
    expect((await demoFetch(new Request("http://localhost/api/me"))).status).toBe(401);
  });
});

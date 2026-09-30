/**
 * The public demo's stand-in for the API.
 *
 * The public demo is this app exported to static files (GitHub Pages), with no server behind it.
 * When built with NEXT_PUBLIC_DEMO_STATIC=1 the typed client sends every request here instead:
 * reads are answered from files the real API wrote for the seeded demo world
 * (`manage demo snapshot`, public/demo/), and writes are answered as the API would but kept only
 * in this tab. The feed is recorded whole per sort order and filtered here the way the API
 * filters it (apps/api/src/app/api/routers/opportunities.py).
 */

import type { components } from "@/lib/api/schema";

type Card = components["schemas"]["OpportunityCard"];
type Detail = components["schemas"]["OpportunityDetail"];
type Brief = components["schemas"]["BriefOut"];
type FeedFile = { items: Card[] };

export const DEMO_STATIC = process.env.NEXT_PUBLIC_DEMO_STATIC === "1";
const BASE = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
const ACCOUNTS: Record<string, { user: string; password: string }> = {
  "demo@example.com": { user: "demo", password: "demo-pass-1234" },
  "admin@example.com": { user: "admin", password: "admin-pass-1234" },
};
const USER_KEY = "demo-static-user";
const READ_ONLY = "공개 데모에서는 저장되지 않아요. 모든 기능은 README대로 로컬에서 실행해 보세요";

// Kept for this tab only: what a visitor changed, laid over the recorded answers.
const feedback = new Map<number, string | null>();
const briefs = new Map<number, Brief[]>();
const files = new Map<string, Promise<unknown>>();

/** With nothing stored, the visitor is the demo company: any link into the app just opens. */
function currentUser(): string | null {
  try {
    const stored = localStorage.getItem(USER_KEY);
    return stored === "none" ? null : (stored ?? "demo");
  } catch {
    return "demo";
  }
}

function setUser(user: string | null): void {
  try {
    localStorage.setItem(USER_KEY, user ?? "none");
  } catch {
    // private mode: the default (the demo company) applies
  }
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fail = (status: number, detail: string) => json({ detail }, status);

async function load<T>(rel: string): Promise<T | null> {
  if (!files.has(rel)) {
    files.set(
      rel,
      fetch(`${BASE}/demo/${rel}`).then((r) => (r.ok ? r.json() : null)),
    );
  }
  return (await files.get(rel)) as T | null;
}

/** Same name as app/demo/snapshot.py's `file_key` (keep the two in step). */
export function fileKey(path: string, query: URLSearchParams): string {
  let raw = path.replace(/^\/api\//, "");
  const entries = [...query.entries()].sort(([a, x], [b, y]) =>
    a === b ? (x < y ? -1 : x > y ? 1 : 0) : a < b ? -1 : 1,
  );
  if (entries.length) raw += "?" + new URLSearchParams(entries).toString();
  return raw.replace(/[^A-Za-z0-9-]/g, "_");
}

/** GET /api/opportunities: the recorded list for this sort, filtered as the API filters. */
export function filterFeed(all: Card[], query: URLSearchParams) {
  const statuses = query.getAll("status");
  const categories = query.getAll("category");
  const stages = query.getAll("stage");
  const q = (query.get("q") ?? "").trim().toLowerCase();
  const withDismissed = query.get("include_dismissed") === "true";
  const matching = all
    .map((card) => (feedback.has(card.id) ? { ...card, feedback: feedback.get(card.id) ?? null } : card))
    .filter((card) => (statuses.length ? statuses : ["open", "bid_open"]).includes(card.status))
    .filter((card) => !categories.length || categories.includes(card.category))
    .filter((card) => !q || card.title.toLowerCase().includes(q))
    .filter((card) => withDismissed || !["dismissed", "irrelevant"].includes(card.feedback ?? ""));
  const stageCounts: Record<string, number> = {};
  for (const card of matching) stageCounts[card.stage] = (stageCounts[card.stage] ?? 0) + 1;
  const items = stages.length ? matching.filter((card) => stages.includes(card.stage)) : matching;
  const offset = Number(query.get("cursor") ?? 0) || 0;
  const limit = Number(query.get("limit") ?? 20) || 20;
  const page = items.slice(offset, offset + limit);
  return {
    items: page,
    next_cursor: offset + limit < items.length ? String(offset + limit) : null,
    total: items.length,
    stage_counts: stageCounts,
  };
}

async function body(request: Request): Promise<Record<string, unknown>> {
  try {
    return (await request.clone().json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

export async function demoFetch(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname.replace(/^.*?(\/api\/)/, "/api/");
  const method = request.method.toUpperCase();

  if (path === "/api/auth/login" && method === "POST") {
    const { email, password } = await body(request);
    const account = ACCOUNTS[String(email ?? "")];
    if (!account || account.password !== password) {
      return fail(401, "공개 데모에서는 데모 계정(demo@example.com, admin@example.com)만 쓸 수 있어요");
    }
    setUser(account.user);
    return json(await load(`${account.user}/me.json`));
  }
  if (path === "/api/auth/logout") {
    setUser(null);
    return new Response(null, { status: 204 });
  }
  if (path === "/api/auth/signup") return fail(403, "공개 데모에서는 가입할 수 없어요. 데모 계정으로 둘러보세요");

  const user = currentUser();
  if (!user) return fail(401, "로그인이 필요해요");

  const opp = path.match(/^\/api\/opportunities\/(\d+)(\/[a-z]+)?$/);
  if (method === "GET" && path === "/api/opportunities") {
    const feed = await load<FeedFile>(`${user}/feed.${url.searchParams.get("sort") ?? "score"}.json`);
    return feed ? json(filterFeed(feed.items, url.searchParams)) : fail(404, "데모에 없는 목록이에요");
  }
  if (opp && method === "GET" && !opp[2]) {
    const id = Number(opp[1]);
    const detail = await load<Detail>(`${user}/opportunities/${id}.json`);
    if (!detail) return fail(404, "이 사업은 데모에 없어요");
    return json({
      ...detail,
      feedback: feedback.has(id) ? (feedback.get(id) ?? null) : detail.feedback,
      briefs: [...(briefs.get(id) ?? []), ...detail.briefs],
    });
  }
  if (opp && method === "POST" && opp[2] === "/feedback") {
    const { feedback: value } = await body(request);
    feedback.set(Number(opp[1]), (value as string | null) ?? null);
    return new Response(null, { status: 204 });
  }
  if (opp && method === "POST" && opp[2] === "/briefs") {
    const id = Number(opp[1]);
    const brief = await load<Brief>(`${user}/briefs/${id}.json`);
    if (!brief) return fail(403, READ_ONLY);
    const example = { ...brief, credits_spent: 0 };
    briefs.set(id, [example]);
    return json(example, 201);
  }
  if (method === "GET") {
    const data = await load(`${user}/${fileKey(path, url.searchParams)}.json`);
    return data === null ? fail(404, "데모에 없는 화면이에요") : json(data);
  }
  return fail(403, READ_ONLY);
}

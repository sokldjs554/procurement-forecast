import createClient from "openapi-fetch";

import { DEMO_STATIC, demoFetch } from "@/lib/demo/fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

/**
 * Typed API client. Requests go to the same origin (`/api/*`), which the Next.js BFF route
 * forwards to the FastAPI service — so the httpOnly session cookie is first-party and the
 * browser never needs CORS. The static public demo has no server: there it answers from the
 * recorded demo world instead (lib/demo/fetch.ts).
 */
export const api = createClient<paths>({
  baseUrl: "",
  credentials: "include",
  ...(DEMO_STATIC ? { fetch: demoFetch } : {}),
});

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function detailMessage(error: unknown): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: string };
      return first.msg ?? "입력한 내용을 다시 확인해 주세요";
    }
  }
  return "요청을 처리하지 못했어요";
}

/** Unwrap an openapi-fetch result: return data or throw a typed ApiError. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || !result.response.ok) {
    throw new ApiError(result.response.status, detailMessage(result.error));
  }
  return result.data as T;
}

/**
 * Retry policy for requests that charge money (briefs, plan changes, credit packs).
 *
 * Retry only when the failure says nothing about the request itself — the network dropped
 * (fetch throws a TypeError) or the server or the BFF proxy failed (5xx) — and at most twice.
 * This is safe only because those requests carry an Idempotency-Key that is created once per
 * click and stays the same across retries, so the API applies the charge at most once.
 */
export function retryTransient(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false;
  if (error instanceof ApiError) return error.status >= 500;
  return error instanceof TypeError;
}

export function newIdempotencyKey(prefix: string): string {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2);
  return `${prefix}-${random}`;
}

"use client";

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { DEMO_STATIC } from "@/lib/demo/fetch";

import { api, ApiError, retryTransient, unwrap, type Schemas } from "./client";

export type Relation = Schemas["RelationOut"];
export type RelationStatus = Relation["status"];
export type RelationDecision = Schemas["RelationDecisionIn"];

export class RelationError extends ApiError {
  constructor(status: number, message: string, readonly issues: string[] = []) {
    super(status, message);
  }
}

export function useAdminRelations(status?: RelationStatus) {
  return useInfiniteQuery({
    queryKey: ["relations", "admin", status ?? "all"],
    enabled: !DEMO_STATIC,
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => unwrap(await api.GET("/api/admin/relations", {
      params: { query: { status, after_id: pageParam, limit: 25 } },
    })),
    getNextPageParam: (last) => last.next_after_id ?? undefined,
  });
}

export function useMixedRelations() {
  return useInfiniteQuery({
    queryKey: ["relations", "mixed"],
    enabled: !DEMO_STATIC,
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => unwrap(await api.GET("/api/admin/relations/mixed", {
      params: { query: { after_id: pageParam, limit: 25 } },
    })),
    getNextPageParam: (last) => last.next_after_id ?? undefined,
  });
}

export function useRelation(id: number | null) {
  return useQuery({
    queryKey: ["relations", "detail", id],
    enabled: !DEMO_STATIC && id !== null,
    queryFn: async () => unwrap(await api.GET("/api/admin/relations/{relation_id}", {
      params: { path: { relation_id: id as number } },
    })),
  });
}

export function useOpportunityRelations(id: number) {
  return useInfiniteQuery({
    queryKey: ["relations", "opportunity", id],
    enabled: !DEMO_STATIC && Number.isSafeInteger(id) && id > 0,
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => unwrap(await api.GET("/api/opportunities/{opportunity_id}/relations", {
      params: { path: { opportunity_id: id }, query: { after_id: pageParam, limit: 25 } },
    })),
    getNextPageParam: (last) => last.next_after_id ?? undefined,
  });
}

export function useDecideRelation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ body, idempotencyKey }: { body: RelationDecision; idempotencyKey: string }) => {
      if (DEMO_STATIC) throw new ApiError(403, "공개 데모에서는 관계를 저장할 수 없어요.");
      const result = await api.POST("/api/admin/relations", {
        body,
        params: { header: { "Idempotency-Key": idempotencyKey } },
      });
      if (!result.response.ok) {
        const detail = (result.error as { detail?: unknown } | undefined)?.detail;
        if (typeof detail === "string") throw new RelationError(result.response.status, detail);
        if (detail && typeof detail === "object" && "issues" in detail && Array.isArray(detail.issues)) {
          throw new RelationError(result.response.status, "invalid_relation", detail.issues.filter((issue): issue is string => typeof issue === "string"));
        }
      }
      return unwrap(result);
    },
    retry: retryTransient,
    onSuccess: (relation) => {
      qc.setQueryData(["relations", "detail", relation.id], relation);
      return qc.invalidateQueries({ queryKey: ["relations"] });
    },
  });
}

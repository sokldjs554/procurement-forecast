"use client";

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { Button, buttonVariants } from "@/components/ui/button";
import { useLogin, useMe } from "@/lib/api/hooks";
import { cn } from "@/lib/utils";

const DEMO = { email: "demo@example.com", password: "demo-pass-1234" };

/**
 * One click into the demo. Signing in happens only on this click — never from a URL — so a link
 * cannot swap someone's session for the shared demo account, and a signed-in user just goes to
 * their own feed.
 */
export function DemoButton({ href = "/app", label }: { href?: string; label?: string }) {
  const me = useMe();
  const login = useLogin();
  const router = useRouter();
  if (me.data) {
    return (
      <Link href={href} className={cn(buttonVariants({ size: "lg" }))}>
        {label ?? "내 피드로 가기"} <ArrowRight className="size-4" aria-hidden />
      </Link>
    );
  }
  return (
    <Button
      size="lg"
      loading={login.isPending}
      onClick={() => login.mutate(DEMO, { onSuccess: () => router.push(href), onError: () => router.push("/login") })}
    >
      {label ?? "데모로 둘러보기"} <ArrowRight className="size-4" aria-hidden />
    </Button>
  );
}

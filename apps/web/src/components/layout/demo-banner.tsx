import { DEMO_STATIC } from "@/lib/demo/fetch";

/** Says what the static public demo is, on every page of it; renders nothing elsewhere. */
export function DemoBanner() {
  if (!DEMO_STATIC) return null;
  return (
    <div className="border-b border-line bg-surface-2 px-4 py-2 text-center text-[12px] text-ink-2">
      예시 데이터로 체험하는 데모예요. 실제 수집·AI 호출·결제는 실행하지 않아요. 브리핑과 피드백은 새로고침하면 초기화돼요.{" "}
      <a
        className="underline underline-offset-2"
        href="https://github.com/sokldjs554/procurement-forecast"
      >
        저장소와 실행 방법
      </a>
    </div>
  );
}

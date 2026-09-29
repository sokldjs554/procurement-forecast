import { DEMO_STATIC } from "@/lib/demo/fetch";

/** Says what the static public demo is, on every page of it; renders nothing elsewhere. */
export function DemoBanner() {
  if (!DEMO_STATIC) return null;
  return (
    <div className="border-b border-line bg-surface-2 px-4 py-2 text-center text-[12px] text-ink-2">
      공개 데모예요. 합성 데모 세계를 실제 파이프라인과 API로 돌려 녹화한 화면이라, 바꾼 내용은 저장되지
      않아요.{" "}
      <a
        className="underline underline-offset-2"
        href="https://github.com/sokldjs554/procurement-forecast"
      >
        저장소와 실행 방법
      </a>
    </div>
  );
}

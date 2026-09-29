import { readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * The static public demo prerenders one page per recorded opportunity (public/demo/ids.json,
 * written by `manage demo snapshot`); the server build renders each on request.
 */
export function generateStaticParams(): { id: string }[] {
  if (process.env.NEXT_PUBLIC_DEMO_STATIC !== "1") return [];
  const ids = JSON.parse(
    readFileSync(join(process.cwd(), "public", "demo", "ids.json"), "utf-8"),
  ) as number[];
  return ids.map((id) => ({ id: String(id) }));
}

export default function Layout({ children }: LayoutProps<"/app/opportunities/[id]">) {
  return children;
}

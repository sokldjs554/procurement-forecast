import type { Metadata, Viewport } from "next";

// Self-hosted Pretendard (unicode-range subsets, so a page only downloads the glyphs it uses):
// no third-party request, and it renders the same behind proxies and in CI screenshots.
import "pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css";
import { DemoBanner } from "@/components/layout/demo-banner";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: { default: "발주 예측 — 입찰공고 이전의 공공 수요", template: "%s · 발주 예측" },
  description:
    "지방의회 회의록과 예산서에서 입찰공고 6~18개월 전의 공공사업 신호를 찾아 B2G 기업에 알려 드려요.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f4f4f0" },
    { media: "(prefers-color-scheme: dark)", color: "#0d0d0d" },
  ],
};

// Applied before first paint so a stored theme choice never flashes the other theme.
const themeScript = `try{var t=localStorage.getItem("theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ko" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-dvh">
        <DemoBanner />
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

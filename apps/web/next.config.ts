import type { NextConfig } from "next";

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

// The public demo: static files on GitHub Pages under the repository's path, no server, the API
// answered from the recorded demo world (src/lib/demo/fetch.ts, .github/workflows/pages.yml).
const demoStatic = process.env.NEXT_PUBLIC_DEMO_STATIC === "1";

const nextConfig: NextConfig = demoStatic
  ? {
      output: "export",
      basePath: process.env.NEXT_PUBLIC_BASE_PATH || undefined,
      trailingSlash: true,
      images: { unoptimized: true },
      poweredByHeader: false,
      reactStrictMode: true,
    }
  : {
      // Self-contained server bundle for the Cloud Run container (see apps/web/Dockerfile).
      output: "standalone",
      poweredByHeader: false,
      reactStrictMode: true,
      async headers() {
        return [{ source: "/:path*", headers: securityHeaders }];
      },
    };

export default nextConfig;

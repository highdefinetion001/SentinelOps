import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  allowedDevOrigins: ["10.60.132.32"],
  experimental: {
    agentFeedback: true,
  },
  async rewrites() {
    const apiTarget = process.env.CONFIGGUARD_API_TARGET || "http://127.0.0.1:8000";
    return [{ source: "/api/:path*", destination: `${apiTarget}/api/:path*` }];
  },
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    root: process.cwd(),
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;

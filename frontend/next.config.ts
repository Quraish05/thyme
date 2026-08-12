import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emit a self-contained build (.next/standalone with its own minimal
  // node_modules + server.js) so the Docker image can run `node server.js`
  // without the full dependency tree. No effect on `next dev`, and Vercel
  // ignores it — it only shapes the production build output for the container.
  output: "standalone",
};

export default nextConfig;

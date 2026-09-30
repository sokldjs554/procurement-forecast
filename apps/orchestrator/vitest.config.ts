import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // The suites share one database; they create their own tenants but not their own queue.
    fileParallelism: false,
    testTimeout: 30_000,
    hookTimeout: 60_000,
  },
});

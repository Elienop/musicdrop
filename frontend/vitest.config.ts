import path from "node:path";
import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, type ViteUserConfig } from "vitest/config";

// SonarQube's scanner mounts the repo ROOT as its base directory, so every path
// in a report it reads has to be `frontend/...` — not the `src/...` that tools
// run from this directory naturally emit. Both reports below rebase onto this.
const repoRoot = fileURLToPath(new URL("..", import.meta.url));

type TestOptions = NonNullable<ViteUserConfig["test"]>;

// SonarQube's Generic Test Execution XML — test count, pass/fail and duration,
// the "Unit Tests" figures, a channel entirely separate from coverage. Only
// `npm run test:coverage` sets SONAR_REPORT, so a plain `npm run test` never
// loads the reporter and writes no file.
//
// This is a conditional SPREAD, not a `reporters: cond ? x : undefined`, and the
// difference is load-bearing: vitest builds its config as
// `{ ...configDefaults, ...options }` and only afterwards tests `reporters` for
// truthiness, so a key explicitly set to `undefined` overwrites the default
// empty array and crashes resolveConfig on `.length`. The key must be ABSENT for
// vitest's own default reporter selection to apply.
const sonarReporter: Partial<Pick<TestOptions, "reporters">> = process.env.SONAR_REPORT
  ? {
      reporters: [
        "default",
        [
          "vitest-sonar-reporter",
          {
            outputFile: "coverage/sonar-report.xml",
            silent: true,
            // The reporter hands over a cwd-relative path; rebase it on the repo
            // root so the entries read `frontend/src/...` whatever the cwd was.
            onWritePath: (p: string) => path.relative(repoRoot, path.resolve(p)),
          },
        ],
      ],
    }
  : {};

// Separate from vite.config.ts so the dev proxy (which targets a real backend)
// never leaks into the hermetic, msw-mocked test environment.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    // Give jsdom a concrete origin so the client's same-origin relative URLs
    // (e.g. "/api/health") resolve to absolute URLs under Node's fetch/undici,
    // which — unlike the browser — refuses to parse relative URLs.
    environmentOptions: {
      jsdom: { url: "http://localhost" },
    },
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    ...sonarReporter,
    // `vitest run --coverage` (npm run test:coverage, which `make coverage` at
    // the repo root drives) writes coverage/lcov.info for
    // sonar.javascript.lcov.reportPaths. Only application source is coverable
    // code: the tests themselves, the vitest setup, the shadcn primitives under
    // components/ui, the generated API types and the bundle entry point.
    //
    // @vitest/coverage-v8 peers on an EXACT vitest version (4.1.7 here), which
    // is why package.json pins `vitest` exactly rather than with a caret — a
    // floating vitest against an exact coverage-v8 ERESOLVEs on the next patch.
    coverage: {
      provider: "v8",
      // Same rebase as above — every lcov `SF:` line becomes `frontend/src/...`.
      // `lcovonly`, not `lcov`: the latter also emits an HTML report that would
      // then have to be kept out of the scan (and out of the build output).
      reporter: ["text-summary", ["lcovonly", { projectRoot: repoRoot }]],
      reportsDirectory: "./coverage",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/**/*.d.ts",
        "src/test/**",
        "src/components/ui/**",
        "src/main.tsx",
      ],
    },
  },
});

import { screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, test } from "vitest";

import { HealthIndicator } from "@/components/HealthIndicator";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

describe("HealthIndicator", () => {
  test("shows backend status 'ok' and version when /api/health resolves", async () => {
    // The client issues absolute, origin-prefixed URLs (see api/client.ts).
    // Under jsdom the origin is http://localhost, so the handler matches the
    // full absolute URL — msw-node won't match a bare relative "/api/health"
    // pattern against an absolute request.
    server.use(
      http.get(`${window.location.origin}/api/health`, () =>
        HttpResponse.json({ status: "ok", version: "0.1.0" }),
      ),
    );

    renderWithProviders(<HealthIndicator />);

    await waitFor(() => {
      expect(screen.getByText("ok")).toBeInTheDocument();
    });
    expect(screen.getByText(/0\.1\.0/)).toBeInTheDocument();
  });
});

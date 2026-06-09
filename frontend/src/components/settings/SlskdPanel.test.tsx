import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, test, vi } from "vitest";

import { SlskdPanel } from "@/components/settings/SlskdPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/msw-server";

// Spy on navigation while keeping the real MemoryRouter/Routes (the render
// harness imports them from the same module) — only `useNavigate` is swapped.
const { mockNavigate } = vi.hoisted(() => ({ mockNavigate: vi.fn() }));
vi.mock("react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router")>()),
  useNavigate: () => mockNavigate,
}));

const SETTINGS = `${window.location.origin}/api/slskd/settings`;
const TEST_URL = `${window.location.origin}/api/slskd/test`;
const STATUS = `${window.location.origin}/api/acquisition/status`;
const ACTIVE = `${window.location.origin}/api/imports/active`;
const REVIEW = `${window.location.origin}/api/acquisition/review-inbox`;

function activeImport(overrides: Record<string, unknown> = {}) {
  return { active: false, origin: "manual", needs_review_count: 0, ...overrides };
}

function settings(overrides: Record<string, unknown> = {}) {
  return {
    base_url: "",
    downloads_prefix: "",
    auto_import: false,
    has_token: false,
    has_webhook_secret: false,
    ...overrides,
  };
}

function acquisitionStatus(overrides: Record<string, unknown> = {}) {
  return {
    phase: "idle",
    queued: 0,
    current: null,
    processed: 0,
    set_aside: 0,
    failed: 0,
    error: null,
    ...overrides,
  };
}

describe("SlskdPanel", () => {
  // The panel polls the acquisition-status probe for the durable
  // set-aside/failed surface; register an idle default so every test's panel
  // mounts without an unhandled-request error (a per-test server.use can still
  // override it for the activity-surface cases).
  beforeEach(() => {
    mockNavigate.mockClear();
    // The panel polls the acquisition-status probe AND the import-active probe
    // (the "Review inbox" button disables while an import runs). Register idle
    // defaults for both so every panel mounts without an unhandled-request error.
    server.use(
      http.get(STATUS, () => HttpResponse.json(acquisitionStatus())),
      http.get(ACTIVE, () => HttpResponse.json(activeImport())),
    );
  });

  test("renders a real h2 heading (not a CardTitle div)", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);
    expect(await screen.findByRole("heading", { level: 2, name: "slskd" })).toBeInTheDocument();
  });

  test("saves settings, omitting blank secrets, and confirms the save", async () => {
    let body: Record<string, unknown> | null = null;
    let saved = false;
    server.use(
      // The GET reflects the saved value once the PUT lands, matching a real
      // backend round-trip (so the editor reseeds and confirms, not strands).
      http.get(SETTINGS, () =>
        HttpResponse.json(
          saved
            ? settings({ base_url: "http://slskd:9999", has_token: true })
            : settings({ base_url: "http://slskd:5030", has_token: true }),
        ),
      ),
      http.put(SETTINGS, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        saved = true;
        return HttpResponse.json(settings({ base_url: "http://slskd:9999", has_token: true }));
      }),
    );
    renderWithProviders(<SlskdPanel />);

    const input = await screen.findByLabelText(/base url/i);
    await userEvent.clear(input);
    await userEvent.type(input, "http://slskd:9999");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.base_url).toBe("http://slskd:9999");
    // Both secret fields were left blank, so the PUT omits them (keeps the saved
    // ones); auto_import is always sent.
    expect("token" in body!).toBe(false);
    expect("webhook_secret" in body!).toBe(false);
    expect(body!.auto_import).toBe(false);
    // A polite confirmation appears once the reseeded snapshot is clean again.
    expect(await screen.findByText(/slskd settings saved/i)).toBeInTheDocument();
  });

  test("the auto-import toggle is sent in the PUT body", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.put(SETTINGS, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(settings({ auto_import: true }));
      }),
    );
    renderWithProviders(<SlskdPanel />);

    const toggle = await screen.findByRole("switch", { name: /auto-import/i });
    await userEvent.click(toggle);
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.auto_import).toBe(true);
  });

  test("disables Test connection while the form is dirty (Test uses saved settings)", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings({ base_url: "http://slskd:5030" }))),
    );
    renderWithProviders(<SlskdPanel />);

    const input = await screen.findByLabelText(/base url/i);
    const testBtn = screen.getByRole("button", { name: /test connection/i });
    expect(testBtn).toBeEnabled();
    await userEvent.type(input, "9");
    expect(testBtn).toBeDisabled();
    expect(screen.getByText(/save before testing/i)).toBeInTheDocument();
  });

  test("tests the connection and shows the reported version", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.post(TEST_URL, () => HttpResponse.json({ ok: true, version: "0.22.3", error: null })),
    );
    renderWithProviders(<SlskdPanel />);

    await screen.findByLabelText(/base url/i);
    await userEvent.click(screen.getByRole("button", { name: /test connection/i }));

    expect(await screen.findByText(/0\.22\.3/)).toBeInTheDocument();
  });

  test("shows the paste-in webhook snippet", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    await screen.findByLabelText(/base url/i);
    expect(screen.getByText(/api\/slskd\/webhook/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
  });

  test("surfaces the durable 'N set aside for review' signal from the queue status", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(STATUS, () =>
        HttpResponse.json(
          acquisitionStatus({ processed: 3, set_aside: 2, failed: 0 }),
        ),
      ),
    );
    renderWithProviders(<SlskdPanel />);

    expect(
      await screen.findByText(/2 downloads set aside for review/i),
    ).toBeInTheDocument();
    // The lifetime tally separates imported from set-aside/failed.
    expect(screen.getByText(/1 imported · 2 set aside · 0 failed/)).toBeInTheDocument();
  });

  test("shows the last drain error when the queue reports one", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(STATUS, () =>
        HttpResponse.json(
          acquisitionStatus({ processed: 1, failed: 1, error: "disk full" }),
        ),
      ),
    );
    renderWithProviders(<SlskdPanel />);

    expect(await screen.findByText(/last error: disk full/i)).toBeInTheDocument();
  });

  test("reads quiet when nothing has been imported yet", async () => {
    server.use(http.get(SETTINGS, () => HttpResponse.json(settings())));
    renderWithProviders(<SlskdPanel />);

    expect(
      await screen.findByText(/no completed downloads have been imported yet/i),
    ).toBeInTheDocument();
  });

  test("Review inbox navigates into the started import job", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.post(REVIEW, () =>
        HttpResponse.json({ started: true, job_id: "job-xyz", pending: 2 }),
      ),
    );
    renderWithProviders(<SlskdPanel />);

    await userEvent.click(
      await screen.findByRole("button", { name: /review inbox/i }),
    );

    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/import?job=job-xyz"),
    );
  });

  test("Review inbox shows an empty notice (and does not navigate) when nothing is queued", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.post(REVIEW, () =>
        HttpResponse.json({ started: false, job_id: null, pending: 0 }),
      ),
    );
    renderWithProviders(<SlskdPanel />);

    await userEvent.click(
      await screen.findByRole("button", { name: /review inbox/i }),
    );

    expect(
      await screen.findByText(/inbox is empty — nothing to review/i),
    ).toBeInTheDocument();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  test("Review inbox is disabled while an import is already running", async () => {
    server.use(
      http.get(SETTINGS, () => HttpResponse.json(settings())),
      http.get(ACTIVE, () => HttpResponse.json(activeImport({ active: true }))),
    );
    renderWithProviders(<SlskdPanel />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /review inbox/i })).toBeDisabled(),
    );
  });
});

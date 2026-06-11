/** Shared helpers for hooks that bypass the typed openapi-fetch client
 * (api/client.ts) and call `fetch` directly — blob/multipart endpoints the
 * generated client can't express. */

/** Absolute URL for an API path. The explicit origin (rather than a relative
 * path) keeps one code path that works in both the browser and the test
 * runner — Node's fetch (undici) refuses origin-relative URLs under jsdom. */
export function apiUrl(path: string): string {
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  return `${origin}${path}`;
}

/** Read FastAPI's `{ "detail": ... }` error body, falling back to the
 * caller's generic message when the response has no usable detail. */
export async function errorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (
      body !== null &&
      typeof body === "object" &&
      "detail" in body &&
      typeof (body as { detail: unknown }).detail === "string"
    ) {
      return (body as { detail: string }).detail;
    }
  } catch {
    // Non-JSON body — fall through to the generic message.
  }
  return fallback;
}

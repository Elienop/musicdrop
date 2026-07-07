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

/** Unwrap an openapi-fetch result: throw `message` on a transport error, a
 * non-2xx (some endpoints send a bodyless 5xx — `data` is undefined then), or
 * missing data; otherwise return the typed data. The param is the minimal shape
 * of openapi-fetch's `FetchResponse` union so the awaited `client.GET/POST/...`
 * result passes straight through without a cast. */
export function unwrap<T>(
  result: { data?: T; error?: unknown; response: Response },
  message: string,
): T {
  if (result.error || !result.response.ok || result.data == null) {
    throw new Error(message);
  }
  return result.data;
}

/** Read FastAPI's `{ "detail": ... }` error body, falling back to the
 * caller's generic message when the response has no usable detail. Delegates
 * to `detailMessage` so both helpers unwrap the same shapes (string, the 422
 * array, and our structured `{message, recovery}` guards). */
export async function errorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await res.json();
    return detailMessage(body) ?? fallback;
  } catch {
    // Non-JSON body — fall through to the generic message.
  }
  return fallback;
}

/** Best human message from a FastAPI error body, tolerating every shape we
 * emit: our guards' `{detail: string}`, the structured guards'
 * `{detail: {message, recovery}}` (500s from cover install / delete / config
 * apply — the message carries the real cause), and the auto-declared
 * HTTPValidationError `{detail: [{msg, ...}, ...]}` (422 caveat — the OpenAPI
 * schema promises the array, the runtime sometimes sends the string). Null
 * when none match — callers fall back to their own copy. */
export function detailMessage(body: unknown): string | null {
  if (body === null || typeof body !== "object" || !("detail" in body)) {
    return null;
  }
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    if (detail.length > 0) {
      const first: unknown = detail[0];
      if (first !== null && typeof first === "object" && "msg" in first) {
        const msg = (first as { msg: unknown }).msg;
        if (typeof msg === "string") {
          return msg;
        }
      }
    }
    return null;
  }
  if (detail !== null && typeof detail === "object" && "message" in detail) {
    const message = (detail as { message: unknown }).message;
    if (typeof message === "string") {
      return message;
    }
  }
  return null;
}

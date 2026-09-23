/** Shared helpers for hooks that bypass the typed openapi-fetch client
 * (api/client.ts) and call `fetch` directly — blob/multipart endpoints the
 * generated client can't express. */

import { markUnauthenticated, UnauthenticatedError } from "@/api/authStore";

/** Absolute URL for an API path. The explicit origin (rather than a relative
 * path) keeps one code path that works in both the browser and the test
 * runner — Node's fetch (undici) refuses origin-relative URLs under jsdom.
 *
 * NOT exported: `apiFetch` below is the only way into it, which is what makes
 * that function's "no caller can forget it" true rather than merely asserted.
 * While this was exported a new hook could reach for it and call bare `fetch`,
 * reintroducing the pre-gate behaviour — a 401 read as a generic failure, no
 * store flip, no bounce — and the compiler would have said nothing. */
function apiUrl(path: string): string {
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  return `${origin}${path}`;
}

/**
 * `fetch` for the raw-body endpoints, carrying the same session handling the
 * typed client's middleware gives every other call.
 *
 * These hooks never reach openapi-fetch, so without this they would meet the
 * gate's 401 as an ordinary failure and report "Couldn't save the artwork"
 * while the real answer is that the session expired. Throwing (rather than
 * returning the response) also keeps the existing status branches honest: a
 * `res.status === 404` check below the call can never be reached by a 401.
 *
 * `path` is an API path, not a URL — `apiUrl` is applied here so no caller can
 * forget it and no caller can pass a cross-origin URL through the check.
 */
export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(apiUrl(path), init);
  if (res.status === 401) {
    markUnauthenticated();
    throw new UnauthenticatedError();
  }
  return res;
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
 * when none match — callers fall back to their own copy.
 *
 * A BLANK detail is no detail: `""` (or whitespace) used to pass the `??` in
 * every caller and render an empty alert, and on the import panel an empty
 * `aria-describedby` target with it. No route sends one today; the shapes above
 * are read off the wire, so nothing here is guaranteed by a type. */
export function detailMessage(body: unknown): string | null {
  if (body === null || typeof body !== "object" || !("detail" in body)) {
    return null;
  }
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === "string") {
    return nonBlank(detail);
  }
  if (Array.isArray(detail)) {
    return detail.length > 0 ? nonBlank(firstValidationMessage(detail)) : null;
  }
  return nonBlank(structuredDetailMessage(detail));
}

/** The sentence, or null when there is nothing in it to read. */
function nonBlank(message: string | null): string | null {
  return message !== null && message.trim() !== "" ? message : null;
}

/** First entry's `msg` of an HTTPValidationError detail array, when the first
 * entry carries a string one — null otherwise (the caller then falls back). */
function firstValidationMessage(detail: unknown[]): string | null {
  const first: unknown = detail[0];
  if (first === null || typeof first !== "object" || !("msg" in first)) {
    return null;
  }
  const msg = (first as { msg: unknown }).msg;
  return typeof msg === "string" ? msg : null;
}

/** The structured guards' `{detail: {message, recovery}}` shape — the message
 * carries the real cause. Null when the shape doesn't match. */
function structuredDetailMessage(detail: unknown): string | null {
  if (detail === null || typeof detail !== "object" || !("message" in detail)) {
    return null;
  }
  const message = (detail as { message: unknown }).message;
  return typeof message === "string" ? message : null;
}

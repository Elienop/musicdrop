/** The four image formats the artwork endpoints accept.
 *
 * Keep in sync with `sniff_image_mime` in backend/app/artwork/images.py — it
 * matches magic bytes for exactly these four (PNG `\x89PNG\r\n\x1a\n`, JPEG
 * `\xff\xd8\xff`, GIF `GIF87a`/`GIF89a`, WebP `RIFF`+`WEBP`) and returns None
 * for anything else. The playlist route's `_sniff_image_format` is the same
 * check. One backend symbol serves every route, which is why this list is
 * shared here rather than copied per panel — unlike the byte CAPS, which
 * genuinely differ per route (8 MB playlists vs 10 MB albums/artists) and so
 * stay hardcoded beside their single consumer.
 */
const ACCEPTED_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);

/** Declared types that assert NOTHING about a file's contents.
 *
 * `""` is what every browser reports for a file whose extension it does not
 * recognise — or that has no extension at all. `application/octet-stream` is
 * IANA's designated "arbitrary binary data"; Windows reaches it through the
 * registry for a range of extensions. Neither is evidence that the bytes are
 * not a PNG, so neither may be refused. This is a list of NON-CLAIMS, not a
 * list of types we tolerate: nothing belongs here unless it means "unknown".
 */
const UNINFORMATIVE_TYPES = new Set(["", "application/octet-stream"]);

/** True only when `file.type` CONFIDENTLY says this is not a format the server
 * accepts — the one condition under which a client may refuse a pick outright.
 *
 * The asymmetry is the whole point. The server never reads the declared
 * content type; it sniffs magic bytes. So `file.type` is a LOSSY mirror of the
 * server's predicate: it is trustworthy when it names a real format (a
 * `image/bmp` or `application/pdf` pick was always going to 415, and refusing
 * it here saves a pointless upload) and worthless when it is blank or generic
 * (a real PNG with no extension arrives as `""`). Refusing on the worthless
 * case is a FALSE REFUSAL with no override — strictly worse than the round
 * trip it saves, because the server would have taken the file.
 *
 * A guard built on this may therefore only ever be stricter than the server on
 * files the server would also reject. `file.size`, by contrast, mirrors
 * `len(data) > cap` byte-for-byte and can be guarded unconditionally.
 *
 * Deliberately NOT called by the playlist artwork picker in
 * PlaylistDetailPage.tsx, which guards size only; see the comment on
 * `MAX_ARTWORK_BYTES` there. This is an opt-in helper, not a rule that every
 * picker must apply.
 */
export function isConfidentlyUnsupportedImageType(type: string): boolean {
  if (UNINFORMATIVE_TYPES.has(type)) return false;
  return !ACCEPTED_IMAGE_TYPES.has(type);
}

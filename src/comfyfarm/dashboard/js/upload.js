// Uploads in chunks (docs/design/18): a file goes up in pieces; when one does not arrive, the router says what it
// has (409) and the upload goes on from there — after a dropped connection, or after a reload with the same file.
import { ApiError, api, t } from "./core.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// `u`: an upload the router knows (going on with it), else a new one is started. onProgress(bytes, total).
export async function uploadFile(file, kind, meta, onProgress, u = null, cancelled = () => false) {
  u = u || await api("POST", "/api/admin/uploads", { kind, filename: file.name, size: file.size, meta });
  if (u.size !== file.size || u.filename !== file.name) throw new ApiError(t("not the same file: {name}, {size} bytes", { name: u.filename, size: u.size }), 409);
  const chunk = u.chunk || 16 << 20;
  let offset = u.received || 0, tries = 0;
  onProgress(offset, file.size);
  while (offset < file.size) {
    if (cancelled()) throw new ApiError(t("cancelled"), 0);
    let r;
    try {
      r = await fetch(`/api/admin/uploads/${u.id}?offset=${offset}`, {
        method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/octet-stream" },
        body: file.slice(offset, Math.min(offset + chunk, file.size)),
      });
    } catch (e) {                                   // the connection dropped: wait, then go on from what arrived
      if (++tries > 8) throw new ApiError(t("cannot reach the server ({error})", { error: e.message }), 0);
      await sleep(Math.min(30000, 1500 * tries));
      continue;
    }
    let body = null;
    try { body = await r.json(); } catch {}
    const d = body?.detail;
    if (r.status === 409 && d && typeof d.received === "number" && d.state === "uploading") {
      offset = d.received;                          // the router has another amount: go on from there
      continue;
    }
    if (!r.ok) {
      if (r.status >= 500 && ++tries <= 8) { await sleep(Math.min(30000, 1500 * tries)); continue; }
      throw new ApiError(typeof d === "string" ? d : d?.message || `HTTP ${r.status}`, r.status);
    }
    tries = 0;
    offset = body.received;
    onProgress(offset, file.size);
    if (body.done) return body.result;
  }
  return null;
}

export const bytes = (n) => n == null ? "—" : n >= 2 ** 30 ? `${(n / 2 ** 30).toFixed(1)} GB`
  : n >= 2 ** 20 ? `${(n / 2 ** 20).toFixed(0)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;

// A file picker opened from code: resolves to the files picked (none: an empty list).
export function pickFiles({ multiple = false, accept = "" } = {}) {
  return new Promise((resolve) => {
    const inp = Object.assign(document.createElement("input"), { type: "file", multiple, accept });
    inp.style.display = "none";
    inp.onchange = () => { resolve([...inp.files]); inp.remove(); };
    inp.oncancel = () => { resolve([]); inp.remove(); };
    document.body.append(inp);
    inp.click();
  });
}

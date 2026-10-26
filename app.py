"""
YouTube Transcript Extractor
----------------------------
A tiny local web app. Paste a YouTube link, pick a language, get the text.

Run:   python app.py
Then open http://localhost:8765 in your browser.
"""

import json
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
)

PORT = 8765
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def extract_video_id(text: str) -> str | None:
    """Accepts a full YouTube URL (watch, youtu.be, shorts, embed, live) or a bare ID."""
    text = text.strip()
    if VIDEO_ID_RE.match(text):
        return text
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    try:
        url = urlparse(text)
    except ValueError:
        return None
    host = (url.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        candidate = url.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if url.path == "/watch":
            candidate = parse_qs(url.query).get("v", [""])[0]
        else:
            # /shorts/ID, /embed/ID, /live/ID, /v/ID
            parts = [p for p in url.path.split("/") if p]
            candidate = parts[1] if len(parts) >= 2 else ""
    else:
        return None
    return candidate if VIDEO_ID_RE.match(candidate) else None


def list_languages(video_id: str) -> dict:
    """Returns the transcripts available for a video, plus translation targets."""
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)
    available = []
    translation_targets = []
    for t in transcript_list:
        available.append({
            "code": t.language_code,
            "name": t.language,
            "generated": t.is_generated,
            "translatable": t.is_translatable,
        })
        if t.is_translatable and not translation_targets:
            translation_targets = [
                {"code": tl.language_code, "name": tl.language}
                for tl in t.translation_languages
            ]
    # Manually created captions first, then auto-generated
    available.sort(key=lambda x: (x["generated"], x["name"]))
    return {"video_id": video_id, "available": available, "translations": translation_targets}


def get_transcript(video_id: str, code: str, translate_to: str | None, timestamps: bool) -> dict:
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)
    transcript = transcript_list.find_transcript([code])
    if translate_to and translate_to != code:
        transcript = transcript.translate(translate_to)
    fetched = transcript.fetch()

    if timestamps:
        lines = [f"[{_fmt(s.start)}] {s.text.replace(chr(10), ' ')}" for s in fetched]
        text = "\n".join(lines)
    else:
        text = " ".join(s.text.replace("\n", " ") for s in fetched)
        text = re.sub(r"\s+", " ", text).strip()

    return {
        "video_id": video_id,
        "language": fetched.language,
        "language_code": fetched.language_code,
        "generated": fetched.is_generated,
        "segments": len(fetched),
        "text": text,
    }


def _fmt(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, TranscriptsDisabled):
        return "Captions are disabled for this video."
    if isinstance(exc, NoTranscriptFound):
        return "No transcript found in that language."
    if isinstance(exc, VideoUnavailable):
        return "That video is unavailable (private, removed, or wrong ID)."
    if isinstance(exc, (IpBlocked, RequestBlocked)):
        return "YouTube is temporarily blocking requests from this network. Wait a minute and try again."
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter console
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/api/languages":
            q = parse_qs(url.query)
            video_id = extract_video_id(q.get("url", [""])[0])
            if not video_id:
                return self._json(400, {"error": "That doesn't look like a YouTube link or video ID."})
            try:
                self._json(200, list_languages(video_id))
            except Exception as exc:  # noqa: BLE001
                self._json(502, {"error": friendly_error(exc)})
        elif url.path == "/api/transcript":
            q = parse_qs(url.query)
            video_id = extract_video_id(q.get("url", [""])[0])
            code = q.get("lang", [""])[0]
            translate_to = q.get("translate", [""])[0] or None
            timestamps = q.get("timestamps", ["0"])[0] == "1"
            if not video_id or not code:
                return self._json(400, {"error": "Missing video or language."})
            try:
                self._json(200, get_transcript(video_id, code, translate_to, timestamps))
            except Exception as exc:  # noqa: BLE001
                self._json(502, {"error": friendly_error(exc)})
        else:
            self._send(404, b"Not found", "text/plain")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YouTube Transcript Extractor</title>
<style>
  :root { --bg:#f6f7f9; --fg:#1a1a1a; --muted:#6b7280; --card:#fff; --line:#e5e7eb; --accent:#c4302b; --accent-fg:#fff; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111214; --fg:#ececec; --muted:#9aa0a6; --card:#1b1c1f; --line:#2c2e33; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  main { max-width:820px; margin:0 auto; padding:32px 16px 64px; }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 24px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:20px; margin-bottom:16px; }
  label { display:block; font-weight:600; margin-bottom:6px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; }
  input[type=text], select { flex:1 1 240px; padding:10px 12px; font:inherit; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); }
  button { padding:10px 18px; font:inherit; font-weight:600; border:0; border-radius:8px; background:var(--accent); color:var(--accent-fg); cursor:pointer; }
  button.secondary { background:transparent; color:var(--fg); border:1px solid var(--line); }
  button:disabled { opacity:.5; cursor:default; }
  .hint { color:var(--muted); font-size:.9rem; margin-top:8px; }
  .err { color:#d33; margin-top:8px; }
  .opts { display:flex; gap:16px; flex-wrap:wrap; align-items:center; margin-top:12px; color:var(--muted); font-size:.95rem; }
  .opts label { display:inline; font-weight:400; }
  textarea { width:100%; min-height:360px; padding:12px; font:14px/1.5 ui-monospace,Consolas,monospace; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); resize:vertical; }
  .meta { color:var(--muted); font-size:.9rem; margin-bottom:8px; }
  .hidden { display:none; }
</style>
</head>
<body>
<main>
  <h1>YouTube Transcript Extractor</h1>
  <p class="sub">Paste a link, pick a language, get the text.</p>

  <section class="card">
    <label for="url">1. Video link or ID</label>
    <form id="urlForm" class="row">
      <input type="text" id="url" placeholder="https://www.youtube.com/watch?v=..." autofocus>
      <button type="submit" id="loadBtn">Find languages</button>
    </form>
    <div id="urlErr" class="err hidden"></div>
  </section>

  <section class="card hidden" id="langCard">
    <label for="lang">2. Language</label>
    <div class="row">
      <select id="lang"></select>
      <select id="translate"><option value="">No translation</option></select>
      <button id="getBtn">Get transcript</button>
    </div>
    <div class="opts">
      <label><input type="checkbox" id="ts"> Include timestamps</label>
    </div>
    <div class="hint">"auto" means YouTube generated the captions automatically. Translation uses YouTube's machine translation.</div>
    <div id="langErr" class="err hidden"></div>
  </section>

  <section class="card hidden" id="outCard">
    <div class="row" style="justify-content:space-between;align-items:center;margin-bottom:8px">
      <span id="meta" class="meta"></span>
      <span class="row">
        <button class="secondary" id="copyBtn">Copy</button>
        <button class="secondary" id="dlBtn">Download .txt</button>
      </span>
    </div>
    <textarea id="out" readonly></textarea>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);
let state = { url: "", video_id: "" };

function show(el, on) { el.classList.toggle("hidden", !on); }
function setErr(el, msg) { el.textContent = msg || ""; show(el, !!msg); }

async function api(path, params) {
  const r = await fetch(path + "?" + new URLSearchParams(params));
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

$("urlForm").addEventListener("submit", async e => {
  e.preventDefault();
  const url = $("url").value.trim();
  setErr($("urlErr"), ""); show($("langCard"), false); show($("outCard"), false);
  if (!url) return;
  $("loadBtn").disabled = true; $("loadBtn").textContent = "Looking up…";
  try {
    const data = await api("/api/languages", { url });
    state = { url, video_id: data.video_id };
    const lang = $("lang"); lang.innerHTML = "";
    for (const t of data.available) {
      const o = document.createElement("option");
      o.value = t.code;
      o.textContent = `${t.name} (${t.code})${t.generated ? " — auto" : ""}`;
      lang.appendChild(o);
    }
    const tr = $("translate"); tr.innerHTML = '<option value="">No translation</option>';
    for (const t of data.translations) {
      const o = document.createElement("option");
      o.value = t.code; o.textContent = "Translate to " + t.name;
      tr.appendChild(o);
    }
    tr.disabled = data.translations.length === 0;
    show($("langCard"), true);
  } catch (err) {
    setErr($("urlErr"), err.message);
  } finally {
    $("loadBtn").disabled = false; $("loadBtn").textContent = "Find languages";
  }
});

$("getBtn").addEventListener("click", async () => {
  setErr($("langErr"), "");
  $("getBtn").disabled = true; $("getBtn").textContent = "Fetching…";
  try {
    const data = await api("/api/transcript", {
      url: state.url, lang: $("lang").value,
      translate: $("translate").value, timestamps: $("ts").checked ? "1" : "0"
    });
    $("out").value = data.text;
    $("meta").textContent = `${data.language} (${data.language_code}) · ${data.segments} segments · ${data.text.split(/\s+/).length} words`;
    show($("outCard"), true);
    $("out").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    setErr($("langErr"), err.message);
  } finally {
    $("getBtn").disabled = false; $("getBtn").textContent = "Get transcript";
  }
});

$("copyBtn").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("out").value);
  $("copyBtn").textContent = "Copied!"; setTimeout(() => $("copyBtn").textContent = "Copy", 1500);
});

$("dlBtn").addEventListener("click", () => {
  const blob = new Blob([$("out").value], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `transcript-${state.video_id}-${$("lang").value}.txt`;
  a.click(); URL.revokeObjectURL(a.href);
});
</script>
</body>
</html>
"""


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"YouTube Transcript Extractor running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

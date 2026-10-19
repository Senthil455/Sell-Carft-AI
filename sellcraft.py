#!/usr/bin/env python3
"""
SellCraft AI — a 100% local listing writer for handmade sellers.

Runs on your laptop, talks only to a local Ollama server, keeps every photo
and every listing on your machine. No accounts, no API keys, no cloud.

    python sellcraft.py            # http://127.0.0.1:8765

Dependencies: Python 3.10+ and a local Ollama (https://ollama.com).
Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DB_PATH = ROOT / "sellcraft.db"
OLLAMA = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma4:latest"
VISION_HINTS = ("gemma4", "gemma3", "llava", "qwen2.5vl", "qwen2.5-vl", "minicpm", "moondream", "llama3.2-vision", "llama4")

ETSY_TITLE_LIMIT = 140
EBAY_TITLE_LIMIT = 80
ETSY_TAG_COUNT = 13
ETSY_TAG_LIMIT = 20
EBAY_KEYWORDS = 8

_lock = threading.Lock()
_db: sqlite3.Connection | None = None


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    global _db
    with _lock:
        if _db is None:
            _db = sqlite3.connect(DB_PATH, check_same_thread=False)
            _db.row_factory = sqlite3.Row
            _db.executescript(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    model TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    facts TEXT NOT NULL,
                    photos INTEGER NOT NULL DEFAULT 0,
                    seconds REAL NOT NULL DEFAULT 0,
                    feedback TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            _db.commit()
    return _db


def get_setting(key: str, default: str = "") -> str:
    row = db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _lock:
        db().execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        db().commit()


def all_settings() -> dict:
    rows = db().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# --------------------------------------------------------------------------
# ollama
# --------------------------------------------------------------------------

def ollama_json(path: str, timeout: float = 15) -> dict | None:
    try:
        with urllib.request.urlopen(OLLAMA + path, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def ollama_models() -> list[str]:
    data = ollama_json("/api/tags")
    if not data:
        return []
    return [m.get("name", "") for m in data.get("models", [])]


def pick_model() -> str:
    want = get_setting("model", DEFAULT_MODEL)
    installed = ollama_models()
    if want in installed:
        return want
    if want.split(":")[0] in {m.split(":")[0] for m in installed}:
        return want
    for m in installed:
        if any(h in m.lower() for h in VISION_HINTS):
            return m
    return want


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

SECTION_ORDER = ["NOTES", "TITLE", "DESCRIPTION", "TAGS"]


def voice_sample(limit: int = 3) -> str:
    """The seller's own recent listings, fed back in as a style guide."""
    rows = db().execute(
        "SELECT title, description FROM listings ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    if not rows:
        return ""
    out = []
    for r in rows:
        out.append(f"- {r['title']}\n  {r['description'][:220]}...")
    return "\n".join(out)


def build_prompt(payload: dict) -> str:
    platform = payload.get("platform", "etsy").lower()
    facts = payload.get("facts", {}) or {}
    photos = payload.get("photos", []) or []
    n = len(photos)

    if platform == "ebay":
        title_rule = f"one line, {EBAY_TITLE_LIMIT} characters max, keyword-first, no emoji"
        tail_label = "KEYWORDS"
        tail_rule = (
            f"{EBAY_KEYWORDS} comma-separated keywords, lowercase, mix of broad and specific, "
            "no duplicates"
        )
    else:
        title_rule = f"one line, {ETSY_TITLE_LIMIT} characters max, keyword-first, no emoji"
        tail_label = "TAGS"
        tail_rule = (
            f"{ETSY_TAG_COUNT} comma-separated tags, each one {ETSY_TAG_LIMIT} characters or fewer "
            "(count them), lowercase, a mix of broad and specific phrases buyers actually search"
        )

    def fact(label: str, key: str) -> str:
        v = (facts.get(key) or "").strip()
        return f"- {label}: {v}\n" if v else ""

    fact_block = (
        fact("Materials", "materials")
        + fact("Size / dimensions", "size")
        + fact("Price", "price")
        + fact("Keywords the seller wants", "keywords")
        + fact("Extra notes", "notes")
    ) or "- (the seller typed no details — work only from the photos and ask for the rest in NOTES)\n"

    shop = get_setting("shop_name", "").strip()
    voice = get_setting("voice", "").strip()
    sample = voice_sample()
    tone = (payload.get("tone") or get_setting("tone", "warm")).strip()
    feedback = (payload.get("feedback") or "").strip()
    previous = (payload.get("previous") or "").strip()

    shop_line = f'The shop is called "{shop}".' if shop else ""

    revision = ""
    if previous and feedback:
        revision = f"""
REVISION ROUND
You already wrote this listing:
--- previous draft ---
{previous[:2500]}
--- end of previous draft ---
The seller asked for this change: {feedback}
Rewrite the whole listing with that change applied. Keep whatever was already good.
"""

    return f"""You are SellCraft, a copywriter who writes product listings for handmade sellers on {platform.upper()}. {shop_line}

You are looking at {n} photo(s) of one product. Read the photos carefully: shape, colour, material, texture, size cues, styling, anything that makes this item specific.

THE SELLER'S FACTS
{fact_block}
Tone requested: {tone}.

A SAMPLE OF THIS SHOP'S OWN VOICE (match this voice, never copy it):
{sample if sample else "- (no earlier listings yet — this is the shop's first one)"}
{revision}
HARD RULES
- Never invent materials, sizes, prices, origins or claims that are not in the facts above or clearly visible in the photos. If something is missing, ask for it in NOTES.
- Write like a human seller who is proud of their work. No corporate filler, no cliches like "elevate your space", no emoji.
- Plain text only. No markdown symbols, no bullet characters, no headings other than the section labels below.
- Match the language of the seller's facts and voice sample.

Write the listing in EXACTLY this format. Write each label on its own line, then its content on the following lines. Nothing before NOTES or after the final section, no extra section labels.

NOTES
<2-3 short lines: what you can see in the photos, plus any question for the seller about a detail you could not see>
TITLE
<{title_rule}>
DESCRIPTION
<120-180 words. Open with a sensory hook about the object itself. Then materials and size. Then a care or shipping note written like a seller, not a policy page. End with one warm call to action. Plain paragraphs.>
{tail_label}
<{tail_rule}>
"""


def split_sections(text: str) -> dict:
    """Parse the labelled sections out of the model's streamed output."""
    labels = set(SECTION_ORDER) | {"KEYWORDS"}
    current = None
    buf: dict[str, list[str]] = {k: [] for k in labels}
    for line in text.splitlines():
        stripped = line.strip().strip("*#_ ").strip()
        upper = stripped.upper().rstrip(":")
        if upper in labels:
            current = upper
            rest = stripped[len(stripped.rstrip(":")) :]
            continue
        if current:
            buf[current].append(line)
    out = {k: "\n".join(v).strip() for k, v in buf.items()}
    if out.get("KEYWORDS") and not out.get("TAGS"):
        out["TAGS"] = out["KEYWORDS"]
    return out


def tidy_listing(parts: dict, platform: str) -> dict:
    title = re.sub(r"\s+", " ", parts.get("TITLE", "")).strip().strip('"')
    title = re.sub(r"^title\s*[:\-]\s*", "", title, flags=re.I)
    limit = EBAY_TITLE_LIMIT if platform == "ebay" else ETSY_TITLE_LIMIT
    truncated = False
    if len(title) > limit:
        cut = title[:limit]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        title, truncated = cut.rstrip(" ,;:-"), True

    description = parts.get("DESCRIPTION", "").strip()
    description = re.sub(r"\n{3,}", "\n\n", description)

    raw_tags = parts.get("TAGS", "") or parts.get("KEYWORDS", "")
    tags, seen = [], set()
    for t in re.split(r"[,\n;]+", raw_tags):
        t = re.sub(r"^[-\*\d\.\)\s]+", "", t).strip().strip("#\"'").lower()
        t = re.sub(r"\s+", " ", t)
        if not t or t in seen:
            continue
        cap = ETSY_TAG_LIMIT if platform == "etsy" else 32
        if len(t) > cap:  # Etsy hard-caps tags at 20 chars — trim at a word boundary
            cut = t[:cap]
            if " " in cut:
                cut = cut[: cut.rfind(" ")]
            t = cut.rstrip(" ,;-")
            if len(t) < 4:
                continue
        if not t or t in seen:
            continue
        seen.add(t)
        tags.append(t)
    cap_n = EBAY_KEYWORDS if platform == "ebay" else ETSY_TAG_COUNT
    tags = tags[:cap_n]

    return {
        "title": title,
        "title_truncated": truncated,
        "description": description,
        "tags": tags,
        "notes": parts.get("NOTES", "").strip(),
    }


# --------------------------------------------------------------------------
# generation (streaming relay to the browser)
# --------------------------------------------------------------------------

def stream_generation(payload: dict):
    """Yield newline-delimited JSON events relayed from Ollama."""
    model = pick_model()
    prompt = build_prompt(payload)
    photos = payload.get("photos", []) or []
    images = []
    for data_url in photos[:4]:
        if isinstance(data_url, str) and "," in data_url:
            images.append(data_url.split(",", 1)[1])

    body = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "think": False,
        "keep_alive": "15m",
        "options": {
            "temperature": 0.7,
            "top_p": 0.9,
            "num_predict": 900,
            "num_ctx": 8192,
        },
    }
    if images:
        body["images"] = images

    t0 = time.time()
    full: list[str] = []
    stats = {}
    try:
        req = urllib.request.Request(
            OLLAMA + "/api/generate",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=900) as r:
            for raw in r:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                text = chunk.get("response") or ""
                if text:
                    full.append(text)
                    yield json.dumps({"t": text}) + "\n"
                if chunk.get("done"):
                    stats = chunk
    except urllib.error.URLError as e:
        yield json.dumps(
            {
                "error": "Can't reach Ollama on 127.0.0.1:11434. Start the Ollama app "
                "(or run `ollama serve`) and try again. " + str(getattr(e, "reason", e))
            }
        ) + "\n"
        return

    elapsed = time.time() - t0
    text = "".join(full)
    platform = payload.get("platform", "etsy").lower()
    listing = tidy_listing(split_sections(text), platform)
    listing["raw"] = text
    listing["platform"] = platform
    listing["model"] = model
    listing["seconds"] = round(elapsed, 1)
    listing["tok_s"] = round(
        (stats.get("eval_count") or 0) / max(1e-6, (stats.get("eval_duration") or 1) / 1e9), 1
    )
    listing["photos"] = len(images)
    listing["_stats"] = {
        k: stats.get(k)
        for k in (
            "total_duration",
            "load_duration",
            "prompt_eval_count",
            "prompt_eval_duration",
            "eval_count",
            "eval_duration",
        )
    }
    yield json.dumps({"done": True, "listing": listing}) + "\n"


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "SellCraft/1.0"

    def log_message(self, fmt, *args):  # quieter console
        if "/api/" in (self.path or ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    # ---- helpers ---------------------------------------------------------
    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ---- routes ----------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            models = ollama_models()
            return self.send_json(
                {
                    "ollama": bool(models),
                    "endpoint": OLLAMA,
                    "model": pick_model(),
                    "models": models,
                    "library_count": db().execute("SELECT COUNT(*) c FROM listings").fetchone()["c"],
                }
            )
        if path == "/api/settings":
            return self.send_json(all_settings())
        if path == "/api/library":
            rows = db().execute("SELECT * FROM listings ORDER BY id DESC LIMIT 200").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["tags"] = [t for t in (d["tags"] or "").split(",") if t]
                out.append(d)
            return self.send_json(out)
        if path == "/api/export.csv":
            rows = db().execute("SELECT * FROM listings ORDER BY id DESC").fetchall()
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["created_at", "platform", "title", "description", "tags", "model"])
            for r in rows:
                w.writerow(
                    [r["created_at"], r["platform"], r["title"], r["description"], r["tags"], r["model"]]
                )
            data = buf.getvalue().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=sellcraft-listings.csv")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/generate":
            payload = self.body_json()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for event in stream_generation(payload):
                    self.wfile.write(event.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass
            return

        if path == "/api/save":
            p = self.body_json()
            tags = p.get("tags") or []
            tags_str = ",".join(tags) if isinstance(tags, list) else str(tags)
            with _lock:
                cur = db().execute(
                    "INSERT INTO listings(created_at, platform, model, title, description, tags, notes, "
                    "facts, photos, seconds, feedback) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        p.get("platform", "etsy"),
                        p.get("model", ""),
                        p.get("title", ""),
                        p.get("description", ""),
                        tags_str,
                        p.get("notes", ""),
                        json.dumps(p.get("facts", {})),
                        int(p.get("photos") or 0),
                        float(p.get("seconds") or 0),
                        p.get("feedback", ""),
                    ),
                )
                db().commit()
            return self.send_json({"id": cur.lastrowid})

        if path == "/api/settings":
            for k, v in (self.body_json() or {}).items():
                set_setting(str(k), str(v))
            return self.send_json({"ok": True})

        if path == "/api/delete":
            rid = int((self.body_json() or {}).get("id") or 0)
            with _lock:
                db().execute("DELETE FROM listings WHERE id = ?", (rid,))
                db().commit()
            return self.send_json({"ok": True})

        return self.send_json({"error": "not found"}, 404)

    # ---- static ----------------------------------------------------------
    def serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_DIR)) or not target.is_file():
            return self.send_json({"error": "not found"}, 404)
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main():
    ap = argparse.ArgumentParser(description="SellCraft AI — local listing writer")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    db()  # create schema up front
    models = ollama_models()
    model = pick_model()
    print()
    print("  SellCraft AI  ·  local listing studio")
    print("  " + "-" * 46)
    if models:
        print(f"  Ollama       : connected ({len(models)} models)")
        print(f"  Writing with : {model}")
    else:
        print("  Ollama       : NOT reachable on 127.0.0.1:11434")
        print("                 start Ollama, then reload the page")
    print(f"  Your data    : {DB_PATH}")
    print(f"  Open         : http://127.0.0.1:{args.port}")
    print()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye — your listings stay in sellcraft.db\n")


if __name__ == "__main__":
    main()

"""Local web viewer:  py -m mtgo_replay --serve

Serves the viewer (mtgo_replay/viewer/), the generated reviews in output/, and
card image URLs resolved through Scryfall (cached in data/scryfall.json so every
card is looked up only once).

Security: the server only listens on 127.0.0.1, but any web page the user
visits can make their browser send requests to it.  So it
  * only answers requests addressed to 127.0.0.1/localhost on its own port
    (blocks DNS-rebinding pages from reading reviews and notes),
  * rejects writes from other origins and requires a JSON content type, which
    a cross-site page can only send after a CORS preflight this server never
    grants (blocks CSRF against the notes),
  * caps request bodies and validates every path/parameter,
  * sends a strict Content-Security-Policy and anti-framing headers.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VIEWER = Path(__file__).resolve().parent / "viewer"
_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
MAX_BODY = 1_000_000                      # bytes; notes and card-name lists are far smaller
_DIR_NAME = re.compile(r"^[\w-][\w.-]{0,120}$")   # a match folder name: no separators, no leading dot
_MATCH_ID = re.compile(r"^[0-9A-Za-z-]{1,64}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "img-src 'self' https://cards.scryfall.io; object-src 'none'; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'",
}


class Library:
    """Index of output/<match>/gameN.json, re-read only when files change."""

    def __init__(self, out: Path):
        self.out = out
        self._headers: dict[Path, tuple[float, dict]] = {}

    def _header(self, f: Path) -> dict:
        mtime = f.stat().st_mtime
        cached = self._headers.get(f)
        if cached is None or cached[0] != mtime:
            with f.open(encoding="utf-8") as fh:
                data = json.load(fh)
            h = data["header"]
            last = data["steps"][-1]["state"]["players"] if data["steps"] else {}
            h = {**h, "steps": len(data["steps"]),
                 "final_life": {p: v["life"] for p, v in last.items()}}
            self._headers[f] = (mtime, h)
        return self._headers[f][1]

    def matches(self) -> list[dict]:
        out = []
        for marker in self.out.glob("*/.match"):
            d = marker.parent
            games = []
            for f in sorted(d.glob("game*.json"), key=lambda p: int(p.stem[4:] or 0)):
                try:
                    games.append({"n": int(f.stem[4:]), **self._header(f)})
                except (ValueError, KeyError, json.JSONDecodeError, OSError):
                    continue
            if games:
                out.append({"dir": d.name, "match_id": marker.read_text().strip(), "games": games,
                            "date": games[0].get("date", ""), "players": games[0].get("players", [])})
        return sorted(out, key=lambda m: m["date"], reverse=True)

    def game_file(self, dir_name: str, n: int) -> Path | None:
        if not _DIR_NAME.match(dir_name) or ".." in dir_name or not 1 <= n <= 9:
            return None
        root = self.out.resolve()
        d = (root / dir_name).resolve()
        if d.parent != root or not (d / ".match").is_file():       # must be a match folder inside output/
            return None
        f = d / f"game{n}.json"
        return f if f.is_file() else None


class Scryfall:
    """name -> {"small", "normal", "type_line", "oracle"} (None when Scryfall doesn't know it)."""

    def __init__(self, cache_file: Path):
        self.file = cache_file
        self.lock = threading.Lock()
        self.cache: dict[str, dict | None] = {}
        if cache_file.exists():
            try:
                self.cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

    @staticmethod
    def _lookup_name(name: str) -> str:
        return name[: -len(" Token")] if name.endswith(" Token") else name

    @staticmethod
    def _entry(card: dict, wanted: str) -> dict:
        face = card
        for f in card.get("card_faces", []):
            if f.get("name") == wanted:
                face = f
        imgs = face.get("image_uris") or card.get("image_uris") or \
            (card.get("card_faces") or [{}])[0].get("image_uris") or {}
        return {"small": imgs.get("small"), "normal": imgs.get("normal"),
                "type_line": face.get("type_line") or card.get("type_line", ""),
                "oracle": face.get("oracle_text") or card.get("oracle_text", "")}

    def resolve(self, names: list[str]) -> dict:
        with self.lock:
            missing = [n for n in dict.fromkeys(names) if n and n not in self.cache]
            for i in range(0, len(missing), 75):
                chunk = missing[i:i + 75]
                ids = [{"name": self._lookup_name(n)} for n in chunk]
                try:
                    req = urllib.request.Request(
                        "https://api.scryfall.com/cards/collection", data=json.dumps({"identifiers": ids}).encode(),
                        headers={"Content-Type": "application/json", "Accept": "application/json",
                                 "User-Agent": "mtgo-logs/1.0"})
                    data = json.load(urllib.request.urlopen(req, timeout=20))
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                    break                                   # offline: text-only cards, retry next time
                found = {}
                for card in data.get("data", []):
                    faces = [card["name"]] + [f["name"] for f in card.get("card_faces", [])]
                    for n in chunk:
                        if self._lookup_name(n) in faces:
                            found[n] = self._entry(card, self._lookup_name(n))
                for n in chunk:
                    self.cache[n] = found.get(n)
                time.sleep(0.1)                             # Scryfall asks for 50-100 ms between requests
            if missing:
                self.file.parent.mkdir(parents=True, exist_ok=True)
                self.file.write_text(json.dumps(self.cache), encoding="utf-8")
            return {n: self.cache.get(n) for n in names}


class Notes:
    """Per-match tags and review notes, kept in data/notes.json (never overwritten by regenerating).

    {match_id: {"opp_deck": str, "my_deck": str, "notes": [{"id", "game", "step", "turn", "text", "done"}]}}
    """

    def __init__(self, file: Path):
        self.file = file
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {}
        if file.exists():
            try:
                self.data = json.loads(file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                backup = file.with_suffix(".broken.json")
                file.replace(backup)             # keep the unreadable file instead of losing it

    def get(self, match_id: str) -> dict:
        return {"opp_deck": "", "my_deck": "", "notes": [], **self.data.get(match_id, {})}

    def put(self, match_id: str, entry: dict) -> dict:
        clean = {"opp_deck": str(entry.get("opp_deck", ""))[:80].strip(),
                 "my_deck": str(entry.get("my_deck", ""))[:80].strip(), "notes": []}
        raw_notes = entry.get("notes", [])
        for n in (raw_notes if isinstance(raw_notes, list) else [])[:500]:
            try:
                clean["notes"].append({"id": str(n["id"])[:40], "game": int(n["game"]), "step": int(n["step"]),
                                       "turn": int(n.get("turn") or 0), "text": str(n.get("text", ""))[:1000],
                                       "done": bool(n.get("done"))})
            except (KeyError, TypeError, ValueError):
                continue
        with self.lock:
            if clean["opp_deck"] or clean["my_deck"] or clean["notes"]:
                self.data[match_id] = clean
            else:
                self.data.pop(match_id, None)
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.file)                # atomic: a crash never leaves half a file
        return clean


def make_handler(lib: Library, scry: Scryfall, notes: Notes):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):   # keep the console quiet
            pass

        def _send(self, body: bytes, ctype: str, status=HTTPStatus.OK):
            self.send_response(status)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            for k, v in _SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _own_origins(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _host_ok(self) -> bool:
            """Reject DNS rebinding: the request must be addressed to us by IP or localhost."""
            return self.headers.get("Host", "") in self._own_origins()

        def _origin_ok(self) -> bool:
            """Browsers send Origin on every POST; only our own pages may write."""
            origin = self.headers.get("Origin")
            return origin is None or origin in {f"http://{h}" for h in self._own_origins()}

        def _read_json(self):
            """The request body as JSON, or raise ValueError (bad length, too big, not JSON)."""
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(length) or b"null")

        def _json(self, obj, status=HTTPStatus.OK):
            self._send(json.dumps(obj, ensure_ascii=False).encode(), "application/json", status)

        def do_GET(self):
            if not self._host_ok():
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            if url.path == "/api/ping":
                return self._json({"app": "mtgo-replay", "ok": True})
            if url.path == "/api/decks":
                from .decks import saved_deck_names
                return self._json(saved_deck_names())
            if url.path == "/api/matches":
                return self._json([{**m, "tags": notes.get(m["match_id"])} for m in lib.matches()])
            if url.path == "/api/game":
                try:
                    f = lib.game_file(q["dir"][0], int(q["n"][0]))
                except (KeyError, ValueError):
                    f = None
                if f is None:
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return self._send(f.read_bytes(), "application/json")
            name = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            f = (VIEWER / name).resolve()
            if f.parent != VIEWER or not f.exists():
                return self._send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)
            self._send(f.read_bytes(), _TYPES.get(f.suffix, "application/octet-stream"))

        def do_POST(self):
            if not self._host_ok() or not self._origin_ok():
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json({"error": "expected application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            url = urllib.parse.urlparse(self.path)
            try:
                body = self._read_json()
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "bad request"}, HTTPStatus.BAD_REQUEST)
            if url.path == "/api/notes":
                match_id = urllib.parse.parse_qs(url.query).get("match", [""])[0]
                if not _MATCH_ID.match(match_id) or not any(m["match_id"] == match_id for m in lib.matches()):
                    return self._json({"error": "unknown match"}, HTTPStatus.NOT_FOUND)
                return self._json(notes.put(match_id, body if isinstance(body, dict) else {}))
            if url.path == "/api/cards":
                names = body if isinstance(body, list) else []
                return self._json(scry.resolve([n for n in names if isinstance(n, str) and len(n) < 200][:2000]))
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


class _Server(ThreadingHTTPServer):
    # Windows lets SO_REUSEADDR bind a port another server is already listening on;
    # without it a busy port fails and we move on to the next one
    allow_reuse_address = False
    daemon_threads = True


def _handler(out: Path, data: Path):
    return make_handler(Library(out), Scryfall(data / "scryfall.json"), Notes(data / "notes.json"))


def start_in_background(out: Path, data: Path, first_port: int = 8765) -> int:
    """Serve the viewer from a daemon thread on the first free port; record it in data/server.json."""
    import os
    handler = _handler(out, data)
    for port in range(first_port, first_port + 20):
        try:
            server = _Server(("127.0.0.1", port), handler)
            break
        except OSError:
            continue
    else:
        raise OSError("no free port for the viewer")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    data.mkdir(parents=True, exist_ok=True)
    (data / "server.json").write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    return port


def serve(out: Path, data: Path, port: int = 8765, open_browser: bool = True):
    server = _Server(("127.0.0.1", port), _handler(out, data))
    url = f"http://127.0.0.1:{port}/"
    print(f"MTGO viewer running at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

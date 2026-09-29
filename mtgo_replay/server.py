"""Local web viewer:  py -m mtgo_replay --serve

Serves the viewer (mtgo_replay/viewer/), the generated reviews in output/, and
card image URLs resolved through Scryfall (cached in data/scryfall.json so every
card is looked up only once).
"""
from __future__ import annotations

import json
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
        d = self.out / dir_name
        if d.parent != self.out or not (d / ".match").exists():   # no path tricks
            return None
        f = d / f"game{n}.json"
        return f if f.exists() else None


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

    {match_id: {"opp_deck": str, "notes": [{"id", "game", "step", "turn", "text", "done"}]}}
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
        return self.data.get(match_id, {"opp_deck": "", "notes": []})

    def put(self, match_id: str, entry: dict) -> dict:
        clean = {"opp_deck": str(entry.get("opp_deck", ""))[:80].strip(), "notes": []}
        for n in entry.get("notes", [])[:500]:
            try:
                clean["notes"].append({"id": str(n["id"])[:40], "game": int(n["game"]), "step": int(n["step"]),
                                       "turn": int(n.get("turn") or 0), "text": str(n.get("text", ""))[:1000],
                                       "done": bool(n.get("done"))})
            except (KeyError, TypeError, ValueError):
                continue
        with self.lock:
            if clean["opp_deck"] or clean["notes"]:
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
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status=HTTPStatus.OK):
            self._send(json.dumps(obj, ensure_ascii=False).encode(), "application/json", status)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
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
            url = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            if url.path == "/api/notes":
                match_id = urllib.parse.parse_qs(url.query).get("match", [""])[0]
                if not any(m["match_id"] == match_id for m in lib.matches()):
                    return self._json({"error": "unknown match"}, HTTPStatus.NOT_FOUND)
                try:
                    entry = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    return self._json({"error": "bad json"}, HTTPStatus.BAD_REQUEST)
                return self._json(notes.put(match_id, entry if isinstance(entry, dict) else {}))
            if url.path != "/api/cards":
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            try:
                names = json.loads(self.rfile.read(length) or b"[]")
            except json.JSONDecodeError:
                names = []
            self._json(scry.resolve([n for n in names if isinstance(n, str)][:2000]))

    return Handler


def serve(out: Path, data: Path, port: int = 8765, open_browser: bool = True):
    handler = make_handler(Library(out), Scryfall(data / "scryfall.json"), Notes(data / "notes.json"))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
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

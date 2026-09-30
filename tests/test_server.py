"""The local web server: API behaviour and its protections against other web pages."""
import http.client
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from mtgo_replay.server import _Server, _handler

MATCH_ID = "6b8e4e4a-3d2f-4c8b-9e51-7a2c0d5f1b93"


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        out, data = cls.root / "output", cls.root / "data"
        match = out / "2026-01-01_vs_Bob"
        match.mkdir(parents=True)
        data.mkdir()
        (match / ".match").write_text(MATCH_ID)
        (match / "game1.json").write_text(json.dumps({
            "header": {"date": "2026-01-01 12:00", "players": ["Alice", "Bob"], "me": "Alice", "winner": "Alice"},
            "steps": [{"state": {"players": {"Alice": {"life": 20}, "Bob": {"life": 0}}}}]}))
        (data / "scryfall.json").write_text("{}")          # never go online in tests
        cls.srv = _Server(("127.0.0.1", 0), _handler(out, data))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.root)

    def request(self, method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host or f"127.0.0.1:{self.port}")
        headers = dict(headers or {})
        data = body.encode() if isinstance(body, str) else body
        if data is not None and "Content-Length" not in headers:
            headers["Content-Length"] = str(len(data))
        for k, v in headers.items():
            c.putheader(k, v)
        c.endheaders(data)
        r = c.getresponse()
        return r.status, r.getheader("Content-Security-Policy"), r.read()

    def post_notes(self, entry, **headers):
        h = {"Content-Type": "application/json", **headers}
        return self.request("POST", f"/api/notes?match={MATCH_ID}", json.dumps(entry), h)[0]

    def test_serves_viewer_and_api(self):
        status, csp, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", csp)
        status, _, body = self.request("GET", "/api/matches")
        self.assertEqual(json.loads(body)[0]["match_id"], MATCH_ID)
        self.assertEqual(self.request("GET", "/api/game?dir=2026-01-01_vs_Bob&n=1")[0], 200)

    def test_rejects_other_hosts(self):              # DNS rebinding
        self.assertEqual(self.request("GET", "/api/matches", host="evil.example")[0], 403)

    def test_rejects_path_traversal(self):
        for path in ("/api/game?dir=..&n=1", "/api/game?dir=..%2Foutput&n=1", "/api/game?dir=.match&n=1",
                     "/api/game?dir=2026-01-01_vs_Bob&n=-1", "/../server.py"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_notes_writes_are_protected(self):
        self.assertEqual(self.post_notes({"opp_deck": "Burn"}, Origin=f"http://127.0.0.1:{self.port}"), 200)
        self.assertEqual(self.post_notes({"opp_deck": "x"}, Origin="https://evil.example"), 403)   # CSRF
        status = self.request("POST", f"/api/notes?match={MATCH_ID}", "{}", {"Content-Type": "text/plain"})[0]
        self.assertEqual(status, 415)                # simple cross-site requests can't be JSON
        self.assertEqual(self.request("POST", "/api/notes?match=nope", "{}",
                                      {"Content-Type": "application/json"})[0], 404)

    def test_notes_are_validated(self):
        self.assertEqual(self.post_notes({"opp_deck": "D" * 500, "notes": [
            {"id": "a", "game": 1, "step": 3, "text": "t" * 5000}, {"garbage": True}, "x"]}), 200)
        _, _, body = self.request("GET", "/api/matches")
        tags = json.loads(body)[0]["tags"]
        self.assertEqual(len(tags["opp_deck"]), 80)
        self.assertEqual(len(tags["notes"]), 1)
        self.assertEqual(len(tags["notes"][0]["text"]), 1000)
        self.assertEqual(self.post_notes({"notes": "not a list"}), 200)

    def test_my_deck_is_stored(self):
        self.assertEqual(self.post_notes({"my_deck": "Deck 2.0"}), 200)
        _, _, body = self.request("GET", "/api/matches")
        self.assertEqual(json.loads(body)[0]["tags"]["my_deck"], "Deck 2.0")
        status, _, body = self.request("GET", "/api/decks")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body), list)

    def test_body_limits(self):
        h = {"Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/cards", "", {**h, "Content-Length": "-1"})[0], 400)
        self.assertEqual(self.request("POST", "/api/cards", "", {**h, "Content-Length": "50000000"})[0], 400)
        self.assertEqual(self.request("POST", "/api/cards", "{not json", h)[0], 400)


if __name__ == "__main__":
    unittest.main()

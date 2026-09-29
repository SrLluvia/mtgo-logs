"""Background mode: watch MTGO's folders and write reviews for new matches.

Every `interval` seconds:
  * archive the snapshots in mtgo.log (MTGO wipes that file on every start),
  * look for new or changed Match_GameLog_*.dat files (new MTGO install
    folders after an update are found too, since paths are re-scanned),
  * once a match is over ("wins the match", or no changes for `idle_minutes`),
    generate its .txt/.json.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

from . import clientlog, paths
from .gamelog import read_match
from .pipeline import Context, detect_me, process_match

log = logging.getLogger("mtgo_replay.watch")
_MATCH_OVER = re.compile(r"wins the match \d+-\d+")


def _setup_logging(data: Path):
    data.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(data / "watch.log", encoding="utf-8")]
    if sys.stderr is not None:          # pythonw has no console
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=handlers)


def _single_instance(lock_path: Path):
    """Keep an exclusive lock for the life of the process; None if another watcher holds it."""
    import msvcrt
    fh = open(lock_path, "a+")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    return fh


class Watcher:
    def __init__(self, out: Path, data: Path, idle_minutes: float):
        self.out, self.data, self.idle = out, data, idle_minutes * 60
        self.state_path = data / "watch_state.json"
        self.state: dict[str, float] = {}
        self.me: str | None = None

    def load_state(self):
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            return
        # first start: don't regenerate the whole history, only what comes next
        self.state = {f.name: f.stat().st_mtime for f in paths.game_log_files()}
        self.save_state()
        log.info("First start: %d existing match logs marked as already seen", len(self.state))

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def tick(self):
        for p in clientlog.archive(self.data / "clientlogs"):
            log.info("Saved MTGO snapshots: %s", p.name)
        files = paths.game_log_files()
        pending = [f for f in files if self.state.get(f.name) != f.stat().st_mtime]
        if not pending:
            return
        if self.me is None:
            self.me = detect_me(files)
        ctx = None
        for f in sorted(pending, key=lambda f: f.stat().st_mtime):
            mtime = f.stat().st_mtime
            idle = time.time() - mtime >= self.idle
            try:
                m = read_match(f)
            except Exception as e:           # half-written file: try again next tick
                log.info("Could not read %s yet (%s)", f.name, e)
                continue
            over = any(_MATCH_OVER.search(r.text) for r in m.records[-5:])
            if not m.games:
                if idle:
                    self.state[f.name] = mtime          # joined but never played
                continue
            if not (over or idle):
                continue                               # match still in progress
            if ctx is None:
                ctx = Context.load(self.out, self.data, self.me)
            out_dir = process_match(ctx, m)
            self.state[f.name] = mtime
            log.info("Generated %s (%d game%s)", out_dir.name, len(m.games), "s" if len(m.games) > 1 else "")
        self.save_state()


def run(out: Path, data: Path, interval: float = 30, idle_minutes: float = 10):
    _setup_logging(data)
    lock = _single_instance(data / "watch.lock")
    if lock is None:
        log.info("Another watcher is already running; exiting.")
        return
    w = Watcher(out, data, idle_minutes)
    w.load_state()
    log.info("Watching MTGO folders every %ss (output: %s)", interval, out)
    while True:
        try:
            w.tick()
        except Exception:
            log.exception("Error while checking for new matches")
        time.sleep(interval)

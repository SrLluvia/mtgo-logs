"""Command line entry point:  py -m mtgo_replay [--last N] [--watch] [--serve] [--launch]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import clientlog, paths
from .gamelog import read_match
from .launcher import data_home, frozen, launch
from .pipeline import Context, detect_me, process_match

ROOT = Path(__file__).resolve().parent.parent
# the installed app keeps its files in %LOCALAPPDATA%\MTGO Replay; a source checkout next to the code
HOME = data_home() if frozen() else ROOT


def main(argv=None):
    ap = argparse.ArgumentParser(description="Per-action game reviews from MTGO game logs")
    ap.add_argument("--last", type=int, default=10, help="number of most recent matches to process (default 10)")
    ap.add_argument("--out", type=Path, default=HOME / "output", help="output folder (default ./output)")
    ap.add_argument("--data", type=Path, default=HOME / "data", help="cache and archive folder")
    ap.add_argument("--match", help="process only the match whose id starts with this")
    ap.add_argument("--archive-only", action="store_true",
                    help="only save MTGO's client-log snapshots, generate nothing")
    ap.add_argument("--watch", action="store_true",
                    help="keep running and generate reviews automatically when a match ends")
    ap.add_argument("--interval", type=float, default=30, help="seconds between checks in --watch mode")
    ap.add_argument("--idle", type=float, default=10,
                    help="minutes without changes after which an unfinished match is processed anyway")
    ap.add_argument("--serve", action="store_true", help="open the web viewer for the generated reviews")
    ap.add_argument("--port", type=int, default=8765, help="port for --serve (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="with --serve: don't open the browser")
    ap.add_argument("--background", action="store_true",
                    help="watcher + web viewer in one process, no window (what the installed app runs)")
    ap.add_argument("--launch", action="store_true",
                    help="start the background process if needed and open the viewer (the app's double-click)")
    argv = sys.argv[1:] if argv is None else argv
    if frozen() and not argv:
        argv = ["--launch"]                       # double-clicking MTGO Replay.exe
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    if args.serve:
        from .server import serve
        serve(args.out, args.data, args.port, not args.no_browser)
        return

    if args.launch:
        launch(args.data, args.out)
        return

    if args.watch or args.background:
        from .watch import run
        run(args.out, args.data, args.interval, args.idle, with_server=args.background,
            first_batch=10 if args.background else 0)
        return

    archived = clientlog.archive(args.data / "clientlogs")
    if archived:
        print(f"Archived MTGO client log snapshots: {', '.join(p.name for p in archived)}")
    if args.archive_only:
        return

    files = paths.game_log_files()
    if not files:
        sys.exit("No Match_GameLog_*.dat files found under " + str(paths.APPS_ROOT))
    ctx = Context.load(args.out, args.data, detect_me(files))
    print(f"Player detected: {ctx.me}  ·  {len(files)} match logs found  ·  {len(ctx.decks)} saved decks")

    matches = []
    for f in files:
        try:
            m = read_match(f)
        except Exception as e:
            print(f"  skipped {f.name}: {e}")
            continue
        if not m.games or (args.match and not m.match_id.startswith(args.match)):
            continue
        matches.append(m)
        if len(matches) >= args.last:
            break

    for m in reversed(matches):
        out_dir = process_match(ctx, m)
        print(f"  {out_dir.name}: {len(m.games)} game(s)")
    print(f"Done. Output in {args.out}")


if __name__ == "__main__":
    main()

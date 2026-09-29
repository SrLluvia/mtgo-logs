# MTGO Replay Logs

For every Magic Online game, writes an **action-by-action** log with the board state after each
action: permanents (with counters and attackers), graveyards, exile, life totals, hand/library
counts, known cards in hand and the stack.

## Usage

Double-click `generate_logs.bat`, or from a terminal in this folder:

```
py -m mtgo_replay                 # the 10 most recent matches
py -m mtgo_replay --last 25       # the 25 most recent
py -m mtgo_replay --match 78a8    # only the match whose id starts with 78a8
py -m mtgo_replay --archive-only  # only save the mtgo.log snapshots (see below)
```

Requires Python 3.10+ (no dependencies) on Windows. MTGO's folders are found automatically.

## Automatic mode

```
py -m mtgo_replay --watch         # or double-click watch.bat (with a console)
```

`watch_mtgo.pyw` does the same without a window (handy to start with Windows). Every 30 s it:

* saves the `mtgo.log` snapshots before MTGO erases them,
* looks for new or changed matches (including new folders MTGO creates when it updates),
* once a match is over ("wins the match", or 10 minutes without changes if someone disconnects),
  writes its logs to `output/`.

On its first start it marks the existing history as already seen and only processes what is played
afterwards. Only one copy can run at a time. Activity log: `data/watch.log`.
Options: `--interval 30` (seconds between checks), `--idle 10` (minutes).

To start it with Windows, put a shortcut in the Startup folder (`Win+R` → `shell:startup`) whose
target is `pythonw.exe "<path>\watch_mtgo.pyw"`.

## Web viewer

```
py -m mtgo_replay --serve         # or double-click viewer.bat
```

Opens `http://127.0.0.1:8765/` in your browser with every generated match:

* the board after each action (opponent on top, you at the bottom): creatures/other permanents and
  lands (grouped), counters, P/T, attackers, known hand, graveyard / exile / known-library piles
  (click to see them all) and the stack; cards that just arrived in a zone are highlighted,
  estimates are outlined with `⚠`,
* card images from Scryfall (resolved once and cached in `data/scryfall.json`; hover a card for a
  large preview and its notes; without internet cards are shown as text),
* navigation: `←`/`→` previous/next action, `↑`/`↓` previous/next turn, `Home`/`End`, a timeline with
  turn marks, and a clickable log. The URL keeps the current position, so it can be bookmarked.

Options: `--port 8765`, `--no-browser`.

## Output

`output/<date>_vs_<opponent>/gameN.txt` and `gameN.json` (one file per game of the match).

* `.txt`: for reading. Each log line followed by the resulting state.
* `.json`: the same data, structured (meant for a frontend).

About hidden information:

* **Your hand.** The game log never names what anyone draws. With `mtgo.log` snapshots (see below)
  your hand is exact at every action (~99 % of states). Without them, every card you later cast,
  play, discard or exile from hand is shown from the moment it was *certainly* in your hand
  (dated by its object id, or the latest draw it can have come from); the rest is "unknown".
* **Library.** Only cards whose position is known are shown: put on top by an effect (in order)
  or sent to the bottom (e.g. Atraxa). A shuffle forgets them. Scry/surveil don't name the cards.

Legend used in the `.txt`:

| Mark         | Meaning |
|--------------|---------|
| `(inferred)` | Not in the log, but follows from the rules (e.g. Fatal Push resolved → creature destroyed). |
| `⚠`          | Estimate or guess (combat damage, which creature was sacrificed, late corrections). |
| `≈`          | Approximate value (life after combat or shocklands; opponent's library shown as `~`). |
| `[exact]`    | State copied from MTGO's own exact snapshot (see below). |

## Where the data comes from

1. **`Match_GameLog_*.dat`** (always available): MTGO's event log. It does not say what you draw,
   when a spell resolves, when a creature dies, which land a fetchland found, or life totals.
   The program reconstructs them:
   * stack resolution inferred from what happens next,
   * spell/ability effects from their oracle text (destroy, exile, bounce, damage, wipes, edicts, searches…),
   * combat damage estimated from power/toughness and counters,
   * MTGO gives each card a new, increasing object id every time it changes zone: when a card later
     shows up with an "unexpected" id, past states are corrected (e.g. which land a fetchland found).
2. **MTGO's card database** (`CardDataSource`, offline): names, types, P/T, loyalty and oracle text,
   keyed by the same ids the game uses. Cached in `data/cards.json`.
3. **Your saved decks** (`grouping *.xml`): used to guess which deck you played in each match.
4. **`mtgo.log`** (optional but very valuable): MTGO writes exact state snapshots there (life,
   your hand, every zone) plus your exact decklist. **MTGO erases this file every time it starts**,
   so the program archives it to `data/clientlogs/` whenever it runs. Games with archived
   snapshots are much more accurate (exact life and hand).
   → Run the program (or `--archive-only`, or keep `--watch` running) **before reopening MTGO**
   so they are not lost.

## Accuracy (measured against MTGO's exact snapshots, 5 games)

From the `.dat` alone: library count 97 %, hand count 88 %, public zones identical in 80 % of the
states (0.5 cards off on average). Life is the hardest part (fetched shocklands that are never
named, exact timing of combat damage): exact in ~34 % of the states, hence the `≈` mark.
With `mtgo.log` snapshots the values become exact.

## Code layout

```
mtgo_replay/
  paths.py      locate MTGO's folders
  gamelog.py    read the binary .dat files and split a match into games
  events.py     turn each log line into a structured event
  carddb.py     MTGO's offline card database
  engine.py     game-state reconstruction engine
  clientlog.py  archive/read mtgo.log (exact snapshots and decklists)
  snapshots.py  merge exact snapshots into the reconstruction
  decks.py      saved decks and guessing the deck used
  pipeline.py   process one match into per-game files
  watch.py      automatic mode (--watch)
  render.py     write the .txt and .json files
  server.py     local web server for the viewer (--serve)
  viewer/       the web viewer (plain HTML/CSS/JS, no build step)
```

## License

[MIT](LICENSE): anyone may use, copy, modify and distribute this code freely, as long as the
copyright notice is kept.

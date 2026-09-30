"use strict";
/* Per-match tags: the opponent's deck, and review notes pinned to a game position.
   Stored by the local server in data/notes.json. Uses S, $, el, goTo, loadGame, loadMatch from app.js. */

const matchTags = (m) => m.tags || (m.tags = { opp_deck: "", my_deck: "", notes: [] });
const pendingCount = (m) => matchTags(m).notes.filter((n) => !n.done).length;

function matchLabel(m) {
  const me = m.games[0].me;
  const opp = m.players.find((p) => p !== me) || "?";
  const wins = m.games.filter((g) => g.winner === me).length;
  const losses = m.games.filter((g) => g.winner && g.winner !== me).length;
  const t = matchTags(m);
  let label = `${m.date.slice(0, 16)} · vs ${opp}${t.opp_deck ? ` (${t.opp_deck})` : ""} · ${wins}-${losses}`;
  const pending = pendingCount(m);
  if (pending) label += ` · ✎ ${pending}`;
  return label;
}

function passesFilter(m) {
  const f = $("filterSel").value || "all";
  if (f === "pending") return pendingCount(m) > 0;
  if (f === "untagged") return !matchTags(m).opp_deck;
  if (f.startsWith("deck:")) return matchTags(m).opp_deck === f.slice(5);
  return true;
}

function buildMatchOptions() {
  const sel = $("matchSel");
  const current = S.match ? S.match.dir : sel.value;
  sel.replaceChildren();
  for (const m of S.matches) {
    if (!passesFilter(m) && m.dir !== current) continue;
    const o = el("option", "", matchLabel(m));
    o.value = m.dir;
    sel.append(o);
  }
  if (current) sel.value = current;
}

function buildFilterOptions() {
  const sel = $("filterSel");
  const current = sel.value || "all";
  const decks = [...new Set(S.matches.map((m) => matchTags(m).opp_deck).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b));
  sel.replaceChildren();
  const add = (value, text) => { const o = el("option", "", text); o.value = value; sel.append(o); };
  add("all", "All matches");
  add("pending", "With pending notes");
  add("untagged", "No opponent deck yet");
  for (const d of decks) add(`deck:${d}`, `vs ${d}`);
  sel.value = [...sel.options].some((o) => o.value === current) ? current : "all";
  $("deckList").replaceChildren(...decks.map((d) => { const o = el("option"); o.value = d; return o; }));
}

async function saveTags() {
  const m = S.match;
  try {
    const res = await fetch(`/api/notes?match=${encodeURIComponent(m.match_id)}`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(matchTags(m)) });
    if (res.ok) m.tags = await res.json();
    else alert("Could not save the note (is the viewer server still running?)");
  } catch (e) {
    alert("Could not save the note (is the viewer server still running?)");
  }
  buildFilterOptions();
  buildMatchOptions();
  refreshNotes();
}

function setupNotes() {
  buildFilterOptions();
  buildMatchOptions();
  $("filterSel").addEventListener("change", () => {
    buildMatchOptions();
    if (S.match && !passesFilter(S.match)) {
      const first = S.matches.find(passesFilter);
      if (first) loadMatch(first.dir, 1, 0);
    }
  });
  $("oppDeck").addEventListener("change", () => {
    matchTags(S.match).opp_deck = $("oppDeck").value.trim();
    saveTags();
  });
  $("noteForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("noteText").value.trim();
    if (!text || !S.data) return;
    const st = S.data.steps[S.idx];
    matchTags(S.match).notes.push({ id: Date.now().toString(36), game: S.gameN, step: S.idx,
      turn: st.turn, text, done: false });
    $("noteText").value = "";
    $("noteText").blur();
    saveTags();
  });
}

/** Called when a match is shown. */
function showMatchTags() {
  $("oppDeck").value = matchTags(S.match).opp_deck;
}

function jumpToNote(n) {
  if (n.game === S.gameN) goTo(n.step);
  else loadGame(n.game, n.step);
}

/** Notes panel, timeline marks and log marks for the current match/game. */
function refreshNotes() {
  renderNotes();
  renderNoteTicks();
  markLogNotes();
}

function renderNotes() {
  if (!S.match) return;
  const notes = [...matchTags(S.match).notes].sort((a, b) => a.game - b.game || a.step - b.step);
  const ul = $("notesList");
  ul.replaceChildren();
  const pending = notes.filter((n) => !n.done).length;
  $("notesCount").textContent = notes.length ? `${pending} to review · ${notes.length} total` : "";
  for (const n of notes) {
    const here = n.game === S.gameN && n.step === S.idx;
    const li = el("li", `note${n.done ? " done" : ""}${here ? " here" : ""}`);
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = n.done;
    cb.title = "Reviewed";
    cb.addEventListener("change", () => { n.done = cb.checked; saveTags(); });
    const pos = el("button", "npos", `G${n.game} · T${n.turn} · #${n.step}`);
    pos.title = "Go to this action";
    pos.addEventListener("click", () => jumpToNote(n));
    const txt = el("span", "ntext", n.text);
    txt.addEventListener("click", () => jumpToNote(n));
    const del = el("button", "ndel", "✕");
    del.title = "Delete note";
    del.addEventListener("click", () => {
      const t = matchTags(S.match);
      t.notes = t.notes.filter((x) => x.id !== n.id);
      saveTags();
    });
    li.append(cb, pos, txt, del);
    ul.append(li);
  }
  if (!notes.length) ul.append(el("li", "note empty-notes", "No notes yet: go to an action and add one below (N)."));
}

function renderNoteTicks() {
  document.querySelectorAll(".note-tick").forEach((t) => t.remove());
  if (!S.data || !S.match) return;
  const total = Math.max(1, S.data.steps.length - 1);
  for (const n of matchTags(S.match).notes) {
    if (n.game !== S.gameN) continue;
    const t = el("div", `note-tick${n.done ? " done" : ""}`);
    t.style.left = `${(n.step / total) * 100}%`;
    t.title = n.text;
    $("ticks").append(t);
  }
}

function markLogNotes() {
  if (!S.data || !S.match) return;
  const items = $("log").children;
  for (const m of $("log").querySelectorAll(".nmark")) m.remove();
  for (const n of matchTags(S.match).notes) {
    if (n.game !== S.gameN || !items[n.step]) continue;
    const mark = el("span", "nmark", "✎");
    mark.title = n.text;
    items[n.step].append(mark);
  }
}

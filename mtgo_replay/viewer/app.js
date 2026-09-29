"use strict";
/* MTGO Replay viewer: renders the per-action states written by `py -m mtgo_replay`. */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

const S = {
  matches: [], match: null, gameN: 1, data: null, idx: 0,
  me: null, opp: null, turnStarts: [], images: {}, cardOf: new WeakMap(),
};

// ------------------------------------------------------------------ loading
async function init() {
  S.matches = await (await fetch("/api/matches")).json();
  if (!S.matches.length) {                       // first run: the background app is still generating
    $("empty").hidden = false;
    setTimeout(() => location.reload(), 5000);
    return;
  }
  const sel = $("matchSel");
  setupNotes();                                        // tags, filters and notes (notes.js)
  sel.addEventListener("change", () => loadMatch(sel.value, 1, 0));
  const h = new URLSearchParams(location.hash.slice(1));
  const dir = S.matches.some((m) => m.dir === h.get("m")) ? h.get("m") : S.matches[0].dir;
  await loadMatch(dir, +h.get("g") || 1, +h.get("s") || 0);
}

async function loadMatch(dir, gameN, stepIdx) {
  S.match = S.matches.find((m) => m.dir === dir);
  buildMatchOptions();
  $("matchSel").value = dir;
  showMatchTags();
  const tabs = $("gameTabs");
  tabs.replaceChildren();
  for (const g of S.match.games) {
    const b = el("button", "", `Game ${g.n}`);
    if (g.winner) {
      const won = g.winner === g.me;
      b.append(el("span", `res ${won ? "win" : "loss"}`, won ? "W" : "L"));
    }
    b.addEventListener("click", () => loadGame(g.n, 0));
    b.dataset.n = g.n;
    tabs.append(b);
  }
  const n = S.match.games.some((g) => g.n === gameN) ? gameN : S.match.games[0].n;
  await loadGame(n, stepIdx);
}

async function loadGame(n, stepIdx) {
  S.gameN = n;
  for (const b of $("gameTabs").children) b.classList.toggle("active", +b.dataset.n === n);
  const res = await fetch(`/api/game?dir=${encodeURIComponent(S.match.dir)}&n=${n}`);
  S.data = await res.json();
  const h = S.data.header;
  [S.me, S.opp] = [h.players[0], h.players[1]];
  S.turnStarts = S.data.steps.map((s, i) => (s.kind === "turn" ? i : -1)).filter((i) => i >= 0);
  renderInfo();
  buildLog();
  buildTimeline();
  goTo(Math.min(stepIdx, S.data.steps.length - 1));
  refreshNotes();
  loadImages();
}

async function loadImages() {
  const names = new Set();
  const add = (c) => c.card && !(c.card in S.images) && names.add(c.card);
  for (const st of S.data.steps) {
    for (const p of Object.values(st.state.players)) {
      for (const z of ["battlefield", "graveyard", "exile", "hand_known", "library_top", "library_bottom"]) (p[z] || []).forEach(add);
    }
    for (const it of st.state.stack) if (it.source && !(it.source in S.images)) names.add(it.source);
  }
  if (!names.size) return;
  try {
    const res = await fetch("/api/cards", { method: "POST", body: JSON.stringify([...names]) });
    Object.assign(S.images, await res.json());
    render();
  } catch (e) { /* offline: text tiles stay */ }
}

// --------------------------------------------------------------- navigation
function goTo(i) {
  if (!S.data) return;
  S.idx = Math.max(0, Math.min(S.data.steps.length - 1, i));
  render();
}
const prevTurn = () => {
  const earlier = S.turnStarts.filter((t) => t < S.idx);
  goTo(earlier.length ? earlier[earlier.length - 1] : 0);
};
const nextTurn = () => {
  const later = S.turnStarts.find((t) => t > S.idx);
  goTo(later !== undefined ? later : S.data.steps.length - 1);
};

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeModal();
    if (e.target.tagName === "INPUT") e.target.blur();
    return;
  }
  if (e.target.tagName === "INPUT") {                  // typing a note or a deck name
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (e.key === "n" || e.key === "N") { e.preventDefault(); $("noteText").focus(); return; }
  if (e.target.tagName === "SELECT" || !S.data) return;
  const k = { ArrowLeft: () => goTo(S.idx - 1), ArrowRight: () => goTo(S.idx + 1),
    ArrowUp: prevTurn, ArrowDown: nextTurn, PageUp: prevTurn, PageDown: nextTurn,
    Home: () => goTo(0), End: () => goTo(S.data.steps.length - 1) }[e.key];
  if (k) { e.preventDefault(); k(); }
});
$("btnFirst").onclick = () => goTo(0);
$("btnPrev").onclick = () => goTo(S.idx - 1);
$("btnNext").onclick = () => goTo(S.idx + 1);
$("btnLast").onclick = () => goTo(S.data.steps.length - 1);
$("btnPrevTurn").onclick = prevTurn;
$("btnNextTurn").onclick = nextTurn;
$("slider").addEventListener("input", (e) => goTo(+e.target.value));

// ------------------------------------------------------------ static parts
function renderInfo() {
  const h = S.data.header;
  const info = $("gameInfo");
  info.replaceChildren();
  const item = (label, value) => {
    const s = el("span", "", `${label} `);
    s.append(el("b", "", value));
    info.append(s);
  };
  item("On the play:", h.on_play || "?");
  item("Winner:", h.winner || "?");
  for (const [p, d] of Object.entries(h.decks || {})) item(`${p}'s deck:`, d.split(" (")[0]);
  const src = el("span", "", h.source.startsWith("game log +") ? "✔ exact MTGO snapshots" : "Reconstructed from the game log");
  src.title = h.source;
  info.append(src);
}

function buildLog() {
  const log = $("log");
  log.replaceChildren();
  S.data.steps.forEach((st, i) => {
    let li;
    if (st.kind === "turn") {
      li = el("li", `turn ${st.active === S.me ? "of-me" : "of-opp"}`, `Turn ${st.turn} · ${st.active}`);
    } else {
      li = el("li", st.text.startsWith(S.me) ? "by-me" : st.text.startsWith(S.opp) ? "by-opp" : "");
      li.append(el("span", "n", String(i)), document.createTextNode(st.text));
      if (st.notes.some((n) => n.includes("⚠"))) li.append(el("span", "w", "⚠"));
      if (st.notes.length) li.title = st.notes.join("\n");
    }
    li.addEventListener("click", () => goTo(i));
    log.append(li);
  });
  $("logCount").textContent = `${S.data.steps.length} actions`;
}

function buildTimeline() {
  const n = S.data.steps.length;
  $("slider").max = n - 1;
  const ticks = $("ticks");
  ticks.replaceChildren();
  let lastPct = -10, lastTurn = -1;
  for (const i of S.turnStarts) {
    const st = S.data.steps[i];
    const pct = (i / Math.max(1, n - 1)) * 100;
    const label = st.turn !== lastTurn && pct - lastPct > 2.5 ? `T${st.turn}` : "";
    const t = el("div", `tick ${st.active === S.me ? "of-me" : "of-opp"}`, label);
    if (label) { lastPct = pct; lastTurn = st.turn; }
    t.style.left = `${pct}%`;
    t.title = `Turn ${st.turn} · ${st.active}`;
    ticks.append(t);
  }
}

// ----------------------------------------------------------------- render
function render() {
  const st = S.data.steps[S.idx];
  const prev = S.data.steps[S.idx - 1];
  // equipment / auras are drawn with the creature they are attached to
  S.attached = new Map();
  const onBattlefield = new Set();
  for (const p of Object.values(st.state.players)) for (const c of p.battlefield) onBattlefield.add(String(c.id));
  for (const p of Object.values(st.state.players)) {
    for (const c of p.battlefield) {
      const host = c.attached_to_id !== undefined ? String(c.attached_to_id) : null;
      if (host && onBattlefield.has(host)) {
        if (!S.attached.has(host)) S.attached.set(host, []);
        S.attached.get(host).push(c);
      }
    }
  }
  renderPlayer($("pTop"), S.opp, st, prev, false);
  renderPlayer($("pBottom"), S.me, st, prev, true);
  renderAction(st);
  renderStack(st.state.stack);
  fitTable();

  const log = $("log");
  log.querySelector(".current")?.classList.remove("current");
  const li = log.children[S.idx];
  li.classList.add("current");
  li.scrollIntoView({ block: "nearest" });

  $("slider").value = S.idx;
  const pos = $("position");
  pos.replaceChildren();
  pos.append("Turn ", el("b", "", String(st.turn)), ` · ${st.active || "—"} · action `,
    el("b", "", String(S.idx)), ` / ${S.data.steps.length - 1}`);
  history.replaceState(null, "", `#m=${encodeURIComponent(S.match.dir)}&g=${S.gameN}&s=${S.idx}`);
  renderNotes();
}

/** Crowded boards: shrink every card just enough for the table to fit without scrolling. */
function fitTable() {
  const root = document.documentElement;
  const tbl = $("table");
  let fit = 1;
  root.style.setProperty("--fit", "1");
  tbl.style.overflow = "";
  const SLACK = 4;               // sub-pixel rounding: less than the table's own padding, nothing is hidden
  const over = () => tbl.scrollHeight > tbl.clientHeight + SLACK || tbl.scrollWidth > tbl.clientWidth + SLACK;
  for (let k = 0; k < 8 && over() && fit > 0.5; k++) {
    const ratio = Math.min(tbl.clientHeight / tbl.scrollHeight, tbl.clientWidth / tbl.scrollWidth);
    fit = Math.max(0.5, fit * Math.max(0.85, ratio));
    root.style.setProperty("--fit", fit.toFixed(3));
  }
  // everything fits (give or take a rounding pixel): no scrollbar
  if (!over()) tbl.style.overflow = "hidden";
}
window.addEventListener("resize", () => { if (S.data) fitTable(); });

/** Cards that were not in that zone before this action get highlighted. */
function newFlags(cards, prevCards) {
  const before = new Map();
  for (const c of prevCards || []) before.set(c.name, (before.get(c.name) || 0) + 1);
  const seen = new Map();
  return cards.map((c) => {
    const k = (seen.get(c.name) || 0) + 1;
    seen.set(c.name, k);
    return k > (before.get(c.name) || 0);
  });
}

function renderPlayer(root, name, st, prev, isMe) {
  const p = st.state.players[name];
  const pp = prev ? prev.state.players[name] : null;
  root.replaceChildren();

  // --- status bar
  const bar = el("div", "pbar");
  const nm = el("div", "pname");
  if (st.active === name) nm.append(el("span", "active-dot"));
  nm.append(name);
  bar.append(nm);
  const stat = (label, value, cls = "") => {
    const s = el("div", `stat ${cls}`);
    s.append(el("b", "", value), label);
    bar.append(s);
  };
  stat("life", `${p.life_approx ? "≈" : ""}${p.life}`, `life ${p.life_approx ? "approx" : ""}`);
  stat("in hand", String(p.hand_count));
  stat("library", `${p.library_approx ? "~" : ""}${p.library_count}`);
  for (const [k, v] of Object.entries(p.counters || {})) bar.append(el("span", "pcounter", `${k} ${v}`));
  if (p.exact) bar.append(el("span", "badge-exact", "exact"));
  root.append(bar);

  // --- battlefield: non-lands and lands
  const bf = p.battlefield;
  const isLand = (c) => (c.types || []).includes("Land") || (!c.card && /land|plains|island|swamp|mountain|forest/i.test(c.name));
  const isAttachment = (c) => c.attached_to_id !== undefined && [...S.attached.values()].some((l) => l.includes(c));
  const nonlands = bf.filter((c) => !isLand(c) && !isAttachment(c));
  const lands = bf.filter(isLand);
  const prevBf = pp ? pp.battlefield : null;
  const rowOf = (cards, cls, groupSame) => {
    const row = el("div", `row ${cls}`);
    const flags = newFlags(cards, prevBf);
    if (groupSame) {
      const groups = new Map();
      cards.forEach((c, i) => {
        const key = c.counters || c.note || c.attacking || c.uncertain ? `${c.id}`
          : `${c.name}|${c.tapped ? (c.tap_guess ? "tap?" : "tap") : ""}`;   // tapped and untapped lands apart
        const g = groups.get(key) || { card: c, n: 0, isNew: false };
        g.n += 1; g.isNew = g.isNew || flags[i];
        groups.set(key, g);
      });
      for (const g of groups.values()) row.append(cardEl(g.card, { qty: g.n, isNew: g.isNew }));
    } else {
      cards.forEach((c, i) => row.append(withAttachments(c, cardEl(c, { isNew: flags[i] }))));
    }
    return row;
  };
  // lands in a compact block on the left, the other permanents large next to them
  root.append(rowOf(lands, "lands", true), rowOf(nonlands, "creatures", false));

  // --- piles
  const piles = el("div", "piles");
  piles.append(pileEl("Graveyard", p.graveyard, name), pileEl("Exile", p.exile, name));
  // only library cards whose position is known (put on top, or sent to the bottom)
  if ((p.library_top || []).length) piles.append(pileEl("Library top", p.library_top, name, true));
  if ((p.library_bottom || []).length) piles.append(pileEl("Library bottom", p.library_bottom, name));
  root.append(piles);

  // --- hand
  const hand = el("div", "hand");
  hand.append(el("div", "hand-label", `Hand · ${p.hand_count}`));
  const row = el("div", "row");
  const flags = newFlags(p.hand_known, pp ? pp.hand_known : null);
  p.hand_known.forEach((c, i) => row.append(cardEl(c, { isNew: flags[i] })));
  const unknown = p.hand_count - p.hand_known.length;
  if (unknown > 0) row.append(el("div", "hand-note", `${p.hand_known.length ? "+ " : ""}${unknown} unknown card${unknown > 1 ? "s" : ""} in hand`));
  if (!p.hand_count) row.append(el("div", "hand-note", "Empty hand"));
  hand.append(row);
  root.append(hand);
}

/** Identical cards (same name, counters and note) shown once with a ×N count. */
function groupCards(cards) {
  const groups = new Map();
  for (const c of cards) {
    const key = `${c.name}|${JSON.stringify(c.counters || {})}|${c.note || ""}|${c.uncertain ? 1 : 0}`;
    const g = groups.get(key) || { card: c, n: 0 };
    g.n += 1;
    groups.set(key, g);
  }
  return [...groups.values()];
}

function pileEl(label, cards, owner, firstIsTop = false) {
  const pile = el("div", `pile ${cards.length ? "" : "empty-pile"}`);
  const top = firstIsTop ? cards[0] : cards[cards.length - 1];
  pile.append(top ? cardEl(top, {}) : el("div", "card"));
  const lab = el("div", "plabel", `${label} `);
  lab.append(el("b", "", String(cards.length)));
  pile.append(lab);
  if (cards.length) pile.addEventListener("click", () => openModal(`${owner}'s ${label.toLowerCase()} (${cards.length})`, cards, !firstIsTop));
  return pile;
}

/** A creature with its equipment/auras peeking out behind it. */
function withAttachments(c, hostEl) {
  const atts = S.attached.get(String(c.id));
  if (!atts || !atts.length) return hostEl;
  const group = el("div", "attach-group");
  group.style.setProperty("--n", atts.length);
  atts.forEach((a, i) => {
    const e = cardEl(a, {});
    e.classList.add("attached");
    e.style.setProperty("--i", i + 1);
    group.append(e);
  });
  hostEl.classList.add("host");
  group.append(hostEl);
  return group;
}

function counterText(k, n) {
  if (k === "+1/+1") return `+${n}/+${n}`;
  if (k === "-1/-1") return `-${n}/-${n}`;
  if (k === "loyalty") return `◆${n}`;
  if (k === "lore") return `lore ${n}`;
  return `${k} ${n}`;
}

function cardEl(c, { qty = 1, isNew = false, big = false } = {}) {
  const d = el("div", "card");
  const face = el("div", "face");            // rotated when tapped; badges stay upright on the card
  const img = c.card ? S.images[c.card] : null;
  if (img && img.small) {
    const i = el("img");
    i.src = big && img.normal ? img.normal : img.small;       // big views get the high-res image
    i.alt = c.name; i.loading = "lazy";
    face.append(i);
  } else {
    const t = el("div", "tile");
    t.append(el("div", "tname", c.name));
    if (c.types) t.append(el("div", "ttype", c.types.join(" ")));
    face.append(t);
    if (!c.card) d.classList.add("unknown");
  }
  d.append(face);
  if (c.tapped) {
    d.classList.add("tapped");
    if (c.tap_guess) { d.classList.add("tap-guess"); d.append(el("span", "b tapq", "⚠")); }
  }
  if (c.token) d.classList.add("token");
  if (isNew) d.classList.add("new");
  if (c.attacking) { d.classList.add("attacking"); d.append(el("span", "b atk", "ATK")); }
  if (c.uncertain) { d.classList.add("uncertain"); d.append(el("span", "b warn", "⚠")); }
  if (c.counters) {
    const txt = Object.entries(c.counters).map(([k, n]) => counterText(k, n)).join(" ");
    if (txt) d.append(el("span", "b cnt", txt));
  }
  if (c.pt) {
    const [p, t] = c.pt.split("/").map(Number);
    const [bp, bt] = (c.pt_base || c.pt).split("/").map(Number);
    const cls = p > bp || t > bt ? " up" : p < bp || t < bt ? " down" : "";
    const b = el("span", `b pt${cls}`, c.pt);
    if (c.pt_mods) b.title = `base ${c.pt_base || c.pt}\n${c.pt_mods.join("\n")}`;
    d.append(b);
  }
  if (qty > 1) d.append(el("span", "b qty", `×${qty}`));
  if (c.attached_to) d.append(el("span", "b on", `on ${c.attached_to}`));
  S.cardOf.set(d, c);
  d.addEventListener("mouseenter", showPreview);
  d.addEventListener("mousemove", movePreview);
  d.addEventListener("mouseleave", hidePreview);
  return d;
}

function renderAction(st) {
  const a = $("action");
  a.replaceChildren();
  const line = el("div");
  line.append(el("span", "aturn", st.turn ? `T${st.turn}` : "Pre-game"), el("span", "atext", st.text));
  a.append(line);
  if (st.notes.length) {
    const ul = el("ul");
    for (const n of st.notes) ul.append(el("li", n.includes("⚠") ? "warn" : "", n));
    a.append(ul);
  }
}

function renderStack(stack) {
  const s = $("stack");
  s.replaceChildren();
  if (!stack.length) return;
  s.append(el("div", "slabel", "Stack"));
  for (const it of [...stack].reverse()) {           // top of the stack first
    const box = el("div", `stack-item ${it.kind}`);
    const c = { name: it.kind === "ability" ? `${it.source} (ability)` : it.source, card: it.source,
      note: [it.text, it.targets.length ? `→ ${it.targets.join(", ")}` : ""].filter(Boolean).join("\n") };
    box.append(cardEl(c, {}), el("div", "sby", it.targets.length ? `→ ${it.targets.join(", ")}` : it.controller));
    s.append(box);
  }
}

// ------------------------------------------------------ preview & modal
function showPreview(e) {
  const c = S.cardOf.get(e.currentTarget);
  if (!c) return;
  const pv = $("preview");
  pv.replaceChildren();
  const img = c.card ? S.images[c.card] : null;
  if (img && img.normal) {
    const i = el("img"); i.src = img.normal; i.alt = c.name;
    pv.append(i);
  } else {
    pv.append(el("div", "pv-name", c.name));
    if (img) pv.append(el("div", "pv-type", img.type_line), el("div", "pv-text", img.oracle));
    else if (c.types) pv.append(el("div", "pv-type", c.types.join(" ")));
  }
  const meta = [];
  if (c.pt) meta.push(`P/T ${c.pt}` + (c.pt_base ? ` (base ${c.pt_base})` : ""));
  if (c.pt_mods) meta.push(...c.pt_mods);
  const atts = S.attached && S.attached.get(String(c.id));
  if (atts && atts.length) meta.push(`equipped/enchanted with ${atts.map((a) => a.name).join(", ")}`);
  if (c.tapped) meta.push(c.tap_guess ? "tapped for mana (estimate: the log never says which lands paid)" : "tapped");
  if (c.counters) meta.push(Object.entries(c.counters).map(([k, n]) => `${k}: ${n}`).join(", "));
  if (c.owner) meta.push(`owned by ${c.owner}`);
  if (c.attached_to) meta.push(`attached to ${c.attached_to}`);
  if (meta.length) pv.append(el("div", "pv-meta", meta.join("\n")));
  if (c.note) pv.append(el("div", "pv-note", c.note));
  pv.hidden = false;
  movePreview(e);
}
function movePreview(e) {
  const pv = $("preview");
  const w = pv.offsetWidth, h = pv.offsetHeight;
  let x = e.clientX + 18, y = e.clientY - h / 2;
  if (x + w > innerWidth - 8) x = e.clientX - w - 18;
  y = Math.max(8, Math.min(innerHeight - h - 8, y));
  pv.style.left = `${x}px`; pv.style.top = `${y}px`;
}
function hidePreview() { $("preview").hidden = true; }

function openModal(title, cards, group = true) {
  $("modalTitle").textContent = title;
  const body = $("modalBody");
  // piles are grouped (×N); the top of the library keeps its order
  const items = group ? groupCards(cards).map((g) => cardEl(g.card, { qty: g.n, big: true }))
    : cards.map((c) => cardEl(c, { big: true }));
  body.replaceChildren(...items);
  $("modal").hidden = false;
}
function closeModal() { $("modal").hidden = true; hidePreview(); }
$("modalClose").onclick = closeModal;
$("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

window.addEventListener("hashchange", () => {
  const h = new URLSearchParams(location.hash.slice(1));
  if (!S.match || h.get("m") !== S.match.dir || +h.get("g") !== S.gameN) {
    if (S.matches.some((m) => m.dir === h.get("m"))) loadMatch(h.get("m"), +h.get("g") || 1, +h.get("s") || 0);
  } else if (+h.get("s") !== S.idx) goTo(+h.get("s") || 0);
});

init();

// MTGO's game log doesn't say which phase an action happened in, so it is inferred from what the
// log does show: the turn line, the draw, lands and sorcery-speed spells (main phases), attacks and
// blocks (combat), and triggers that name a phase ("at the beginning of your upkeep / end step").
// Casting an instant or activating an ability never moves the phase, so those stay where they were.
const PHASES = [
  { key: "untap", label: "Untap" },
  { key: "upkeep", label: "Upkeep" },
  { key: "draw", label: "Draw" },
  { key: "main1", label: "Main 1" },
  { key: "begin", label: "Begin", combat: true, title: "Beginning of combat" },
  { key: "attackers", label: "Attackers", combat: true, title: "Declare attackers" },
  { key: "blockers", label: "Blockers", combat: true, title: "Declare blockers" },
  { key: "damage", label: "Damage", combat: true, title: "Combat damage" },
  { key: "endcombat", label: "End", combat: true, title: "End of combat" },
  { key: "main2", label: "Main 2" },
  { key: "end", label: "End step" },
  { key: "cleanup", label: "Cleanup" },
];
const PH = Object.fromEntries(PHASES.map((p, i) => [p.key, i]));

/** Phase index (into PHASES) of every step, or -1 before the first turn. */
function inferPhases(steps, images) {
  const out = [];
  let ph = -1;
  for (const st of steps) {
    if (st.kind === "turn") ph = PH.untap;
    else if (ph >= 0) ph = nextPhase(ph, st, images);
    out.push(ph);
  }
  return out;
}

function nextPhase(ph, st, images) {
  const text = st.text.toLowerCase();
  const byActive = st.active && st.text.startsWith(st.active);
  const inMain = () => (ph >= PH.attackers ? PH.main2 : PH.main1);   // sorcery-speed: before or after combat
  const inCombat = ph >= PH.attackers && ph <= PH.damage;
  // "whenever it deals combat damage" goes on the stack in the damage step itself
  if (st.kind === "trigger" && inCombat && /combat damage/.test(text)) return PH.damage;
  // otherwise damage was dealt just before this action (the engine notes it on the next step)
  if (ph >= PH.attackers && ph < PH.endcombat && st.notes.some((n) => /combat damage|died in combat/.test(n))) ph = PH.endcombat;
  switch (st.kind) {
    case "skip_draw": return Math.max(ph, PH.draw);
    case "draw":
      // "X draws a card." at the start of the turn; draws caused by a spell or ability say "with ..."
      return byActive && ph < PH.main1 && /draws \w+ cards?\.$/.test(text) ? PH.draw : ph;
    case "attack": return PH.attackers;
    case "block": return PH.blockers;
    case "play_land": return byActive ? inMain() : ph;
    case "cast": return byActive && sorcerySpeed(st, images, ph) ? inMain() : ph;
    case "trigger":
      if (/upkeep/.test(text) && ph < PH.draw) return PH.upkeep;
      if (/beginning of (your |each )?combat/.test(text) && ph <= PH.begin) return PH.begin;
      if (/beginning of (your |the next |each |the )?end step/.test(text) && ph >= PH.main1) return PH.end;
      return ph;
    default: return ph;
  }
}

function sorcerySpeed(st, images, ph) {
  const top = st.state.stack[st.state.stack.length - 1];
  const info = top && images[top.source];
  if (!info) return ph < PH.main1;                       // unknown card: before combat, assume it was cast in the main phase
  const types = info.type_line || "";
  if (/Instant/.test(types) || /^flash$/im.test(info.oracle || "")) return false;
  return /Creature|Land|Artifact|Enchantment|Planeswalker|Sorcery|Battle/.test(types);
}

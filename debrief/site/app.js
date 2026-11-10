"use strict";

// ---------- vocabulary ----------

const SIGNALS = {
  correction: { label: "Said it was wrong", plural: "Said it was wrong", tip: "The person told the agent something it did or said was wrong." },
  reversal: { label: "Had it redone", plural: "Had it redone", tip: "The person rejected an approach the agent had already built and asked for it to be redone." },
  could_find: { label: "Agent overlooked", plural: "Agent overlooked", tip: "The person pointed the agent to information it already had access to but didn't find." },
  defect: { label: "Caught a bug", plural: "Caught a bug", tip: "The person pointed out a bug or unwanted output in the agent's work." },
  not_filed: { label: "Found, not filed", plural: "Found, not filed", tip: "The agent noticed a problem outside its task and left it." },
};
const SIG_ORDER = ["correction", "reversal", "could_find", "defect"];
// The three kinds of correction that make up "Agent corrected".
const CORRECTION_KINDS = ["correction", "reversal", "defect"];
const pctText = (rate) => (rate == null ? "—" : `${pct1(rate)}%`);
const SIG_FIELD = { correction: "corrections", reversal: "reversals", could_find: "could_find", defect: "defects" };
// A human message counts toward "Agent corrected" when it carries any of these.
const CORRECTING = ["correction", "reversal", "defect"];
// Glossary entry for each signal (method.js).
const LABEL_FIELD = { correction: "turn_kind", reversal: "design_reversal", could_find: "agent_could_have_found_it", defect: "human_caught_defect", not_filed: "found_not_filed" };
const OUTCOMES = { shipped_or_pr_ready: "Shipped", partial: "Partial", abandoned: "Abandoned", investigation_only: "Investigation", unclear: "Unclear" };
const TURN_KINDS = {
  question_to_agent: "Question to agent", new_request: "New request", approval_or_go_ahead: "Approval / go ahead",
  correction: "Correction", answer_to_agent_question: "Answer to agent", status_check: "Status check", other: "Other", unlabeled: "Unlabeled",
};
const PROMPT_STYLES = {
  links_issue_or_pr: "Links issue or PR", detailed_instructions: "Detailed instructions", pasted_spec: "Pasted spec",
  one_line_ask: "One-line ask", question: "Question", asks_for_plan_first: "Asks for a plan",
};
const CORRECTION_TYPES = {
  scope: "Scope: too much or too little", wrong_assumption: "Wrong assumption", process: "Skipped a step",
  missed_requirement: "Missed requirement", factual_error: "Factual error", style_or_convention: "Convention", unclassified: "Unclassified",
};
const CI_CLASSES = { infra_or_env: "Infra / env", test_regression: "Test regression", build_error: "Build error", flaky_test: "Flaky test", timeout: "Timeout", lint_or_format: "Lint / format", other: "Other", null: "Unlabeled" };
const HELD = { valid_and_fixed: "Valid, fixed", valid_open: "Valid, open", outdated: "Outdated", wrong: "Wrong", unclear: "Unclear", null: "Unlabeled" };
const HELD_COLOR = { valid_and_fixed: "var(--text-default)", valid_open: "var(--text-muted)", outdated: "var(--border-default)", wrong: "var(--destructive)", unclear: "var(--surface-selected)", null: "var(--surface-hover)" };
const PERIODS = { all: "All time", 30: "Last 30 days", 14: "Last 14 days" };
const DAY = 864e5;

const ICONS = {
  overview: '<rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/>',
  recs: '<path d="M2 4l1.5 1.5L6 3M2 10l1.5 1.5L6 9M8.5 4.5H14M8.5 10.5H14"/>',
  corrections: '<path d="M8 2.2l6 10.8H2z"/><path d="M8 6.5v3M8 11.3v.2"/>',
  adoption: '<circle cx="6" cy="5.5" r="2.5"/><path d="M1.5 13.5c.5-2.5 2.3-3.8 4.5-3.8s4 1.3 4.5 3.8M11 3.2a2.3 2.3 0 010 4.6M12.5 10c1.2.5 1.9 1.7 2 3.5"/>',
  delivery: '<circle cx="4" cy="3.5" r="1.5"/><circle cx="4" cy="12.5" r="1.5"/><circle cx="12" cy="12.5" r="1.5"/><path d="M4 5v6M12 11V6.5a2 2 0 00-2-2H7M8.5 3L7 4.5 8.5 6"/>',
  sessions: '<rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><path d="M4.5 6.5l2 1.5-2 1.5M8 9.5h3"/>',
  method: '<path d="M3 2.5h7.5a2 2 0 012 2v9H5a2 2 0 01-2-2z"/><path d="M3 11.5a2 2 0 012-2h7.5M6 5.5h4"/>',
};
const PAGES = [
  { id: "overview", label: "Overview" },
  { id: "recs", label: "Ideas" },
  { id: "corrections", label: "Corrections" },
  { id: "adoption", label: "Adoption" },
  { id: "delivery", label: "CI & review", needs: "github" },
  { id: "sessions", label: "Sessions" },
  { id: "method", label: "How it works", secondary: true },
];

// ---------- state ----------

const state = {
  data: null,
  sessionsById: new Map(),
  recsBySession: new Map(),
  sessionCache: new Map(),
  shardCache: new Map(),
  memo: new Map(),
  scope: { period: "all", agent: "" },
  recFilter: { q: "", lever: "", sort: "sessions", dir: -1 },
  correctionsFilter: { signal: "", ctype: "", dir: "" },
  sessionFilter: { q: "", author: "", project: "", outcome: "", corrected: false, sort: "started", dir: -1 },
  evTab: "all",
  overviewGroup: "agent",
  lastPage: null,
};


// ---------- helpers ----------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
// Not scrollIntoView: inside an iframe it also scrolls the page that embeds this one.
function scrollToEl(el, block = "start") {
  if (!el) return;
  const r = el.getBoundingClientRect();
  const off = block === "center" ? (innerHeight - r.height) / 2 : parseFloat(getComputedStyle(el).scrollMarginTop) || 0;
  window.scrollTo(0, scrollY + r.top - off);
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => Number(n).toLocaleString("en-US");
const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
const pct1 = (x) => (x == null ? "—" : `${(100 * x).toFixed(1)}`);
const sum = (xs, f = (x) => x) => xs.reduce((a, x) => a + (f(x) || 0), 0);
const groupBy = (xs, f) => xs.reduce((m, x) => { const k = f(x); (m.get(k) || m.set(k, []).get(k)).push(x); return m; }, new Map());
const short = (id) => String(id).slice(0, 8);
const time = (ts) => new Date(ts).getTime();
// A bare date like "2026-08-11" is a calendar day, so show it in UTC rather than shifting it a day west.
const day = (ts) => (ts ? new Date(ts).toLocaleDateString("en-US", { month: "short", day: "numeric", ...(/^\d{4}-\d{2}-\d{2}$/.test(ts) ? { timeZone: "UTC" } : {}) }) : "");
const dayTime = (ts) => (ts ? new Date(ts).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");
const plural = (n, one, many = one + "s") => `${fmt(n)} ${n === 1 ? one : many}`;
const dirOf = (p) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "(repo root)");
const DEF_BY_ID = new Map(DEFS.map((d) => [d.id, d]));
// Fill {placeholders} in glossary text from the data's meta block.
const fill = (text) => String(text || "").replace(/\{(\w+)\}/g, (_, k) => {
  const d = state.data;
  const v = { findings: d && Object.keys(d.findings).length, recommendations: d?.recommendations.length }[k] ?? d?.meta?.[k];
  return v == null ? "?" : k === "from" || k === "to" ? day(v) : typeof v === "number" ? fmt(v) : v;
});
const defTip = (id) => fill(DEF_BY_ID.get(id)?.short);
// An ⓘ icon: hover for the short definition; with a glossary id, click to open the full entry.
function info(text, def) {
  const tip = esc(fill(text || defTip(def)));
  return def
    ? `<a class="info" href="#method~${DEF_BY_ID.get(def)?.anchor || def}" data-tip="${tip}" aria-label="Definition: ${tip}">i</a>`
    : `<span class="info" data-tip="${tip}" tabindex="0" aria-label="${tip}">i</span>`;
}
const glyph = (sig) => `<span class="glyph ${sig}" aria-hidden="true"></span>`;
const sigChip = (sig, extra = "") => `<span class="sig">${glyph(sig)}${esc(SIGNALS[sig].label)}${extra}</span>`;
const outcomeChip = (o) => (o ? `<span class="outcome ${esc(o)}">${esc(OUTCOMES[o] || o)}</span>` : `<span class="muted">—</span>`);
// Optional data (GitHub, people, checkpoints, projects, labels): the export says which exists, and
// pages, columns and tiles for the rest stay hidden.
const has = (k) => Boolean(state.data?.meta?.has?.[k]);
const pages = () => PAGES.filter((p) => !p.needs || has(p.needs));
// Lever keys and their descriptions come from the data. The label is the description up to its colon,
// or the key in words when there is no short one.
const lever = (k) => {
  const desc = state.data.levers?.[k] || "";
  const head = desc.split(":")[0];
  return desc.includes(":") && head.length <= 32 ? head : String(k).replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
};
// A PR's key is "owner/name#12". The repo's name shows only when the report spans several repos.
const prLabel = (k) => { const [repo, n] = String(k).split("#"); return esc(state.data.meta.repo ? `#${n}` : `${repo.split("/").pop()}#${n}`); };
const prLink = (k) => { const url = state.data.prs[k]?.url; return url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${prLabel(k)}</a>` : prLabel(k); };
// "3 of 40 messages", or "No messages" rather than "0 of 0".
const ofText = (a, b, unit) => (b ? `${fmt(a)} of ${fmt(b)} ${unit}` : `No ${unit}`);
const sessionHref = (id, idx) => `#session~${id}${idx != null ? "~" + idx : ""}`;
// Filter value for sessions whose harness didn't record who ran them.
const NO_PERSON = "(no person)";
const personLabel = (author, none = "Unknown") => (!author || author === NO_PERSON ? none : author);
// A session's project is the folder it ran in: show the last part, filter on the whole path.
const NO_PROJECT = "(no project)";
const pathTail = (p, n = 1) => String(p).replace(/[\\/]+$/, "").split(/[\\/]/).slice(-n).join("/") || p;
const projectName = (p) => (p ? pathTail(p) : "Unknown");
// Who ran a session and with what, for a row's second line.
const ranBy = (s) => [has("people") && personLabel(s.author), s.agent].filter(Boolean).join(" · ");
// Shown in the side bar and the intro: the GitHub repo, the one project, or how many projects.
function workspaceLabel() {
  const d = state.data;
  if (d.meta.repo) return d.meta.repo;
  const projects = new Set(d.sessions.map((s) => s.project).filter(Boolean));
  return projects.size === 1 ? projectName([...projects][0]) : projects.size ? plural(projects.size, "project") : "";
}
// A card that says what is missing and how to get it, in place of empty tables.
const notice = (title, text) => `<section class="card"><div class="card-body"><h2>${esc(title)}</h2><p>${text}</p></div></section>`;
const NOTICES = {
  sessions: () => notice("No sessions yet", "debrief found no agent sessions to report on. Run <code>debrief sync</code> to import them."),
  labels: () => notice("Sessions imported, not analysed yet", `${plural(state.data.sessions.length, "session")} to <a href="#sessions">browse</a>, but none is labelled yet, so there are no correction rates or ideas. Run <code>debrief sync</code> to label them.`),
  ideas: () => notice("No ideas yet", "The messages are labelled, but no findings or ideas have been written from them. Run <code>debrief sync</code> to finish the analysis."),
};
const correctedTurns = (s) => s.friction.filter(([, sig]) => sig.some((x) => CORRECTING.includes(x))).length;

const agentKey = (agent) => ({ "Claude Code": "claude", Codex: "codex", Pi: "pi" }[agent] || "other");
const agentColor = (agent) => `var(--ag-${agentKey(agent)})`;
const agentChip = (a) => `<span class="agent-chip ${agentKey(a)}">${esc(a)}</span>`;

// Group key for a model: "[1m]" and the like only widen the context window, so they count with the base model.
const modelName = (m) => String(m || "").replace(/\[\d+[km]\]$/i, "").trim() || "Unknown model";
// Models with fewer sessions than this are left out of the model tables.
const MIN_RATED = 10;
// One row per model with enough sessions: metrics, the one-session check and sessions per harness, by sessions.
// `hidden` counts the models left out.
function modelRows(S) {
  const all = [...groupBy(S, (s) => modelName(s.model))];
  const rows = all.filter(([, ss]) => ss.length >= MIN_RATED)
    .map(([name, ss]) => ({ name, mm: metrics(ss), one: restsOnOne(ss),
      agents: [...groupBy(ss, (s) => s.agent)].map(([a, xs]) => [a, xs.length]).sort((x, y) => y[1] - x[1]) }))
    .sort((x, y) => y.mm.sessions - x.mm.sessions);
  return Object.assign(rows, { hidden: all.length - rows.length });
}
const hiddenNote = (rows) => (rows.hidden ? `${plural(rows.hidden, "model")} with fewer than ${MIN_RATED} sessions not shown.` : "");
const harnessDots = (agents) => `<span class="mini" data-tip="${esc(agents.map(([a, n]) => `${a}: ${n}`).join(", "))}" tabindex="0">${agents.map(([a]) => `<span class="dot" style="--c:${agentColor(a)}"></span>`).join("")}</span>`;

// One item per key in turn, so no single session crowds out the rest.
function interleave(xs, key) {
  const queues = [...groupBy(xs, key).values()];
  const out = [];
  for (let i = 0; out.length < xs.length; i++) queues.forEach((q) => { if (q[i]) out.push(q[i]); });
  return out;
}

// Minimal markdown: headings, fenced code, lists, paragraphs, inline code, bold, links. Escapes first.
function md(src) {
  const out = [];
  String(src || "").split(/```[\w-]*\n?/).forEach((part, i) => {
    if (i % 2) { out.push(`<pre>${esc(part.replace(/\n$/, ""))}</pre>`); return; }
    for (const block of part.split(/\n\s*\n/)) {
      const lines = block.split("\n").filter((l) => l.trim());
      if (lines.length && /^#{1,6}\s/.test(lines[0])) out.push(`<p><strong>${inline(lines.shift().replace(/^#+\s*/, ""))}</strong></p>`);
      if (!lines.length) continue;
      if (lines.every((l) => /^\s*([-*]|\d+[.)])\s+/.test(l))) {
        const items = lines.map((l) => `<li>${inline(l.replace(/^\s*([-*]|\d+[.)])\s+/, ""))}</li>`).join("");
        out.push(/^\s*\d/.test(lines[0]) ? `<ol>${items}</ol>` : `<ul>${items}</ul>`);
      } else {
        out.push(`<p>${lines.map(inline).join("<br>")}</p>`);
      }
    }
  });
  return out.join("");
}
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

// ---------- scope (global period + agent filter) ----------

function windowFor(offset) {
  if (state.scope.period === "all") return offset ? null : [-Infinity, Infinity];
  const end = time(state.data.meta.to) + DAY;
  const len = Number(state.scope.period) * DAY;
  return [end - len * (offset + 1), end - len * offset];
}

// When a session counts for periods and weeks: its start, or the window start if it began earlier.
const startedAt = (s) => Math.max(time(s.started), time(state.data.meta.from));

// Sessions and everything tied to them, for the current scope (offset 1 = the previous window).
function scoped(offset = 0) {
  const key = `${state.scope.period}|${state.scope.agent}|${offset}`;
  if (state.memo.has(key)) return state.memo.get(key);
  const w = windowFor(offset);
  let out = null;
  if (w) {
    const d = state.data;
    const S = d.sessions.filter((s) => (!state.scope.agent || s.agent === state.scope.agent) && startedAt(s) >= w[0] && startedAt(s) < w[1]);
    const ids = new Set(S.map((s) => s.id));
    const prs = new Set(S.flatMap((s) => s.prs.map((p) => p.n)));
    out = {
      S, ids, prs, window: w,
      flagged: d.flagged.filter((t) => ids.has(t.s)),
      fails: d.ci.failures.filter((f) => prs.has(f.pr)),
      reviews: d.reviews.filter((r) => prs.has(r.pr)),
      skills: d.skills.filter((u) => ids.has(u.s)),
      files: d.files.map((f) => ({ ...f, turns: f.turns.filter((t) => ids.has(t.s)) })).filter((f) => f.turns.length)
        .map((f) => ({ ...f, sessions: [...new Set(f.turns.map((t) => t.s))] })),
    };
  }
  state.memo.set(key, out);
  return out;
}

function metrics(S) {
  const turns = sum(S, (s) => s.human_turns);
  const labeled = S.filter((s) => s.outcome);
  const shipped = labeled.filter((s) => s.outcome === "shipped_or_pr_ready").length;
  const corrected = sum(S, correctedTurns);
  const m = {
    sessions: S.length, people: new Set(S.map((s) => s.author).filter(Boolean)).size, turns, corrected,
    labeled: labeled.length, shipped, shipRate: labeled.length ? shipped / labeled.length : null,
    correctedRate: turns ? corrected / turns : null,
  };
  for (const g of SIG_ORDER) { m[g] = sum(S, (s) => s[SIG_FIELD[g]]); m[`${g}Rate`] = turns ? m[g] / turns : null; }
  return m;
}

// Agent corrected without the one session that moves it most. Returns that session and the rate
// without it when dropping it shifts the rate by more than a third; null when the rate holds.
function restsOnOne(S) {
  const turns = sum(S, (s) => s.human_turns);
  const corrected = sum(S, correctedTurns);
  if (!turns) return null;
  const rate = corrected / turns;
  let worst = null;
  for (const s of S) {
    const rest = turns - s.human_turns;
    if (!rest) continue;
    const r = (corrected - correctedTurns(s)) / rest;
    if (!worst || Math.abs(r - rate) > Math.abs(worst.rate - rate)) worst = { s, rate: r };
  }
  return worst && Math.abs(worst.rate - rate) > rate / 3 ? worst : null;
}
const restsPill = (w) => (w ? ` <span class="pill" data-tip="Without its biggest session (${plural(w.s.human_turns, "message")}) this is ${pctText(w.rate)}.">rests on one session</span>` : "");

function weekStart(ts) {
  const d = new Date(ts);
  d.setUTCHours(0, 0, 0, 0);
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
  return d.getTime();
}

// Every week in the scope, empty ones included, with metrics per week.
function weekly(S) {
  if (!S.length) return [];
  const by = groupBy(S, (s) => weekStart(startedAt(s)));
  const first = Math.min(...by.keys());
  const last = Math.max(...by.keys());
  const out = [];
  for (let w = first; w <= last; w += 7 * DAY) out.push({ week: w, S: by.get(w) || [], m: metrics(by.get(w) || []) });
  return out;
}

// ---------- charts ----------

function spark(values) {
  const pts = values.map((v, i) => [i, v]).filter(([, v]) => v != null);
  if (pts.length < 2) return "";
  const W = 84, H = 30, lo = Math.min(...pts.map((p) => p[1])), hi = Math.max(...pts.map((p) => p[1]));
  const x = (i) => 2 + (i * (W - 4)) / (values.length - 1 || 1);
  const y = (v) => H - 3 - ((v - lo) / (hi - lo || 1)) * (H - 6);
  const line = pts.map(([i, v]) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const [li, lv] = pts[pts.length - 1];
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" aria-hidden="true">
    <polygon points="${x(pts[0][0]).toFixed(1)},${H} ${line} ${x(li).toFixed(1)},${H}" fill="var(--surface-selected)"/>
    <polyline points="${line}" fill="none" stroke="var(--text-muted)" stroke-width="1.5" stroke-linejoin="round"/>
    <circle cx="${x(li).toFixed(1)}" cy="${y(lv).toFixed(1)}" r="2.5" fill="var(--text-muted)"/></svg>`;
}

// weeks: [{week, S, m}]; series: [{label, color, value: (week) => number|null}]
function lineChart(weeks, series, { fmtY = (v) => v.toFixed(1), unit = "" } = {}) {
  if (weeks.length < 2) return `<div class="empty">Not enough weeks in this period for a trend.</div>`;
  const W = 640, H = 200, L = 34, R = 10, T = 10, B = 24;
  const vals = series.flatMap((s) => weeks.map(s.value)).filter((v) => v != null);
  const step = niceStep(Math.max(...vals, 0.001) / 4);
  const top = Math.ceil(Math.max(...vals, 0.001) / step) * step;
  const x = (i) => L + (i * (W - L - R)) / (weeks.length - 1);
  const y = (v) => T + (1 - v / top) * (H - T - B);
  const grid = [];
  for (let v = 0; v <= top + 1e-9; v += step) grid.push(`<line class="grid-line" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis-text" x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${fmtY(v)}</text>`);
  const labelEvery = Math.ceil(weeks.length / 8);
  const xl = weeks.map((w, i) => (i % labelEvery ? "" : `<text class="axis-text" x="${x(i)}" y="${H - 6}" text-anchor="middle">${day(w.week)}</text>`)).join("");
  // A week still in progress at the end of the data, or with few sessions, isn't comparable: draw it dotted.
  const dataEnd = time(state.data.meta.to);
  const partial = (w) => w.week + 7 * DAY > dataEnd;
  const weak = (w) => w.m.sessions < 5 || partial(w);
  const lines = series.map((s) => {
    const segs = weeks.slice(1).map((w, n) => {
      const a = s.value(weeks[n]), b = s.value(w);
      if (a == null || b == null) return "";
      return `<path d="M${x(n).toFixed(1)},${y(a).toFixed(1)}L${x(n + 1).toFixed(1)},${y(b).toFixed(1)}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linecap="round"${weak(weeks[n]) || weak(w) ? ' stroke-dasharray="2 4"' : ""}/>`;
    }).join("");
    const dots = weeks.map((w, i) => { const v = s.value(w); if (v == null) return ""; const thin = weak(w);
      return `<circle cx="${x(i)}" cy="${y(v)}" r="${thin ? 3 : 3.5}" fill="var(--surface-floating)" stroke="${s.color}" stroke-width="${thin ? 1.5 : 2}"/>`; }).join("");
    return `${segs}${dots}`;
  }).join("");
  const band = (W - L - R) / (weeks.length - 1);
  const hover = weeks.map((w, i) => {
    const tip = [`Week of ${day(w.week)} · ${plural(w.m.sessions, "session")}, ${plural(w.m.turns, "message")}`, ...series.map((s) => { const v = s.value(w); return `${s.label}: ${v == null ? "—" : fmtY(v) + unit}`; }), ...(partial(w) ? [`Partial week: data ends ${day(state.data.meta.to)}`] : w.m.sessions < 5 ? ["Few sessions: read as noise"] : [])].join("\n");
    return `<g class="hover-band" data-tip="${esc(tip)}"><rect x="${x(i) - band / 2}" y="${T}" width="${band}" height="${H - T - B}" fill="transparent"/><line class="cross" x1="${x(i)}" x2="${x(i)}" y1="${T}" y2="${H - B}"/></g>`;
  }).join("");
  const note = weeks.some(weak) ? `<p class="note small">Dotted: a week that's only partly over or has fewer than 5 sessions.</p>` : "";
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(series.map((s) => s.label).join(" and "))} by week">${grid.join("")}${xl}${lines}${hover}</svg>${note}</div>`;
}
function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((s) => s >= raw) || raw;
}

// rows: [{label, value, valText?, color?, segs?: [{v, c, tip}], href?, tip?}]
function bars(rows, { max, labelWidth } = {}) {
  if (!rows.length) return `<div class="empty">No data in this period.</div>`;
  const top = max ?? Math.max(1, ...rows.map((r) => r.value));
  const style = labelWidth ? ` style="grid-template-columns:${labelWidth} minmax(0,1fr) 72px"` : "";
  return `<div class="bars">${rows.map((r) => {
    const segs = r.segs || [{ v: r.value, c: r.color, tip: r.tip }];
    const track = segs.filter((s) => s.v > 0).map((s) => `<span class="seg" style="width:${(100 * s.v) / top}%;${s.c ? `--c:${s.c}` : ""}" data-tip="${esc(s.tip || `${r.label}: ${fmt(s.v)}`)}"></span>`).join("");
    const attrs = r.href ? ` class="bar-row link" data-href="${esc(r.href)}" role="link" tabindex="0"` : ` class="bar-row"`;
    return `<div${attrs}${style}><span class="lab" title="${esc(r.label)}">${esc(r.label)}</span><span class="track">${track}</span><span class="val">${r.valText ?? fmt(r.value)}</span></div>`;
  }).join("")}</div>`;
}
const legend = (items) => `<div class="legend">${items.map((i) => `<span>${i.glyph ? glyph(i.glyph) : `<span class="dot" style="--c:${i.c}"></span>`}${esc(i.label)}</span>`).join("")}</div>`;
const inlineBar = (value, max, color, text, tip) => `<div class="inline-bar"><span class="track"><span class="seg" style="width:${max ? (100 * value) / max : 0}%;--c:${color}" data-tip="${esc(tip || "")}"></span></span><span class="num" style="min-width:36px;text-align:right">${text}</span></div>`;

// ---------- building blocks ----------

function kpi({ label, tip, def, desc, value, unit = "", sub = "", sparkValues, delta, href }) {
  // A div, not a link: the ⓘ inside is its own link, and links can't nest.
  return `<div class="card kpi${href ? " link" : ""}"${href ? ` data-href="${href.slice(1)}" role="link" tabindex="0"` : ""}>
    <div class="kpi-label"><span>${esc(label)}</span>${tip || def ? info(tip, def) : ""}</div>
    <div class="kpi-row"><span class="kpi-value">${value}${unit ? `<small>${unit}</small>` : ""}</span>${sparkValues ? spark(sparkValues) : ""}</div>
    ${desc ? `<div class="kpi-desc">${esc(desc)}</div>` : ""}
    <div class="kpi-sub">${delta || ""}<span>${sub}</span></div>
  </div>`;
}

// Change against the previous window; better = "up" | "down".
function delta(cur, prev, { better, pts = false }) {
  if (cur == null || prev == null) return "";
  const diff = pts ? 100 * (cur - prev) : prev ? (100 * (cur - prev)) / prev : null;
  if (diff == null) return "";
  const flat = Math.abs(diff) < (pts ? 0.5 : 5);
  const good = better === "up" ? diff > 0 : diff < 0;
  const cls = flat ? "flat" : good ? "good" : "bad";
  const arrow = flat ? "→" : diff > 0 ? "▲" : "▼";
  return `<span class="delta ${cls}" data-tip="vs previous ${state.scope.period} days">${arrow} ${Math.abs(diff).toFixed(1)}${pts ? " pts" : "%"}</span>`;
}

function card({ title, tip, def, aside = "", body, foot = "", flush = false, cls = "" }) {
  return `<section class="card ${cls}"><div class="card-head"><h2>${esc(title)}${tip || def ? " " + info(tip, def) : ""}</h2>${aside ? `<div class="aside">${aside}</div>` : ""}</div>
    <div class="card-body${flush ? " flush" : ""}">${body}</div>${foot ? `<div class="card-foot">${foot}</div>` : ""}</section>`;
}

// Recommendations are ordered by reach: sessions touched, then people. Ties keep the export's order.
// Ideas left with no supporting evidence (weak) sort last whatever the sort.
const REC_SORTS = {
  sessions: (r) => r.sessions.length * 1e3 + r.people,
  people: (r) => r.people * 1e3 + r.sessions.length,
};
const weakLast = (a, b) => Number(!!a.weak) - Number(!!b.weak);
const byReach = (a, b) => weakLast(a, b) || REC_SORTS.sessions(b) - REC_SORTS.sessions(a);
function sortRecs(recs, { sort, dir } = state.recFilter) {
  return recs.slice().sort((a, b) => weakLast(a, b) || (REC_SORTS[sort](a) - REC_SORTS[sort](b)) * dir);
}
const WEAK_TIP = "A second model pass found none of this idea's cited items support it.";
const weakPill = (r) => (r.weak ? ` <span class="pill" data-tip="${WEAK_TIP}">weak evidence</span>` : "");

// sortable: headers become clickable and show the current sort (the Recommendations page).
function recTable(recs, { compact = false, sortable = false } = {}) {
  if (!recs.length) return `<div class="empty">No ideas match.</div>`;
  const f = state.recFilter;
  const th = (k, label, cls = "") => (sortable
    ? `<th class="sortable ${cls}" data-rec-sort="${k}" aria-sort="${f.sort === k ? (f.dir > 0 ? "ascending" : "descending") : "none"}">${label}${f.sort === k ? (f.dir > 0 ? " ↑" : " ↓") : ""}</th>`
    : `<th class="${cls}">${label}</th>`);
  return `<div class="table-wrap"><table><thead><tr><th>Idea</th><th>Type</th>
    ${th("sessions", "Sessions", "r")}${has("people") ? th("people", "People", "r") : ""}</tr></thead>
    <tbody>${recs.map((r) => `<tr class="click" data-href="rec~${r.id}">
      <td style="min-width:260px"><div class="cell-title">${esc(r.title)}${weakPill(r)}</div>${!compact && r.target ? `<div class="cell-sub mono">${esc(r.target)}</div>` : ""}</td>
      <td class="nowrap">${esc(lever(r.lever))}</td>
      <td class="r">${r.sessions.length}</td>${has("people") ? `<td class="r">${r.people}</td>` : ""}</tr>`).join("")}</tbody></table></div>`;
}

function sessionRows(S) {
  const col = { project: has("projects"), person: has("people"), pr: has("github") };
  return S.map((s) => {
    const c = Object.fromEntries(SIG_ORDER.map((g) => [g, s[SIG_FIELD[g]]]));
    return `<tr class="click" data-href="session~${s.id}">
      <td class="num nowrap">${day(s.started)}</td>
      <td style="min-width:220px"><div class="cell-title">${esc(s.name || short(s.id))}</div><div class="cell-sub">${esc(s.prompt)}</div></td>
      ${col.project ? `<td class="nowrap${s.project ? "" : " muted"}" style="max-width:220px;overflow:hidden;text-overflow:ellipsis" title="${esc(s.project || "")}">${esc(projectName(s.project))}</td>` : ""}
      <td class="nowrap">${agentChip(s.agent)}<div class="cell-sub">${esc(s.model || "")}</div></td>
      ${col.person ? `<td class="nowrap${s.author ? "" : " muted"}">${esc(personLabel(s.author))}</td>` : ""}
      <td class="r">${s.human_turns}</td>
      <td><span class="mini">${SIG_ORDER.filter((g) => c[g]).map((g) => `<span class="sig" data-tip="${c[g]} ${SIGNALS[g].plural.toLowerCase()}">${glyph(g)}${c[g]}</span>`).join("") || `<span class="muted">—</span>`}</span></td>
      <td>${outcomeChip(s.outcome)}</td>${col.pr ? `<td class="nowrap">${s.prs.map((p) => `${prLink(p.n)} <span class="pr-state">${esc(p.state)}</span>`).join("<br>") || `<span class="muted">—</span>`}</td>` : ""}
    </tr>`;
  }).join("");
}

function turnRows(turns) {
  return turns.map((t) => {
    const s = state.sessionsById.get(t.s) || {};
    return `<tr class="click" data-href="session~${t.s}~${t.i}">
      <td class="num nowrap top">${day(t.ts || s.started)}</td>
      <td class="top" style="min-width:180px"><div class="cell-title">${esc(s.name || short(t.s))}</div><div class="cell-sub">${esc(ranBy(s))}</div></td>
      <td class="top"><div class="chips">${t.sig.map((g) => `<span class="sig">${glyph(g)}${esc(g === "correction" && t.ctype ? CORRECTION_TYPES[t.ctype] || t.ctype : SIGNALS[g].label)}</span>`).join("")}</div></td>
      <td class="top" style="min-width:280px;color:var(--text-default);overflow-wrap:anywhere">${esc(t.text)}</td></tr>`;
  }).join("");
}

// ---------- chrome ----------

function renderNav(active) {
  const counts = { recs: state.data.recommendations.length, sessions: state.data.sessions.length };
  $("#nav").innerHTML = pages().map((p, i, shown) => `${p.secondary && !shown[i - 1]?.secondary ? '<div class="nav-label">About</div>' : ""}<a href="#${p.id}"${p.id === active ? ' aria-current="page"' : ""}>
    <svg viewBox="0 0 16 16" aria-hidden="true">${ICONS[p.id]}</svg><span>${esc(p.label)}</span>${counts[p.id] ? `<span class="count">${counts[p.id]}</span>` : ""}</a>`).join("");
}

function renderTopbar({ crumbs, scope = false, right = "" }) {
  const m = state.data.meta;
  const agents = [...new Set(state.data.sessions.map((s) => s.agent))].sort();
  const trail = crumbs.map((c, i) => (i < crumbs.length - 1 ? `<a href="${c.href}">${esc(c.label)}</a><span class="sep">/</span>` : `<h1>${esc(c.label)}</h1>`)).join("");
  const controls = scope ? `<label for="scope-period">Period <select id="scope-period">${Object.entries(PERIODS).map(([k, l]) => `<option value="${k}"${state.scope.period === k ? " selected" : ""}>${l}</option>`).join("")}</select></label>
    <label for="scope-agent">Harness <select id="scope-agent"><option value="">All harnesses</option>${agents.map((a) => `<option${state.scope.agent === a ? " selected" : ""}>${esc(a)}</option>`).join("")}</select></label>` : "";
  $("#topbar").innerHTML = `<nav class="crumbs" aria-label="Breadcrumb">${trail}</nav>${right}
    <div class="scope">${controls}<span class="fresh" data-tip="Data exported ${esc(dayTime(m.generated))}. Analysis run ${esc(m.run_id)}."><span class="live"></span>Updated ${esc(day(m.generated))}</span></div>`;
  $("#scope-period")?.addEventListener("change", (e) => { state.scope.period = e.target.value; rerender(); });
  $("#scope-agent")?.addEventListener("change", (e) => { state.scope.agent = e.target.value; rerender(); });
}

function scopeLabel() {
  const sc = scoped();
  const w = sc.window;
  const from = w[0] > time(state.data.meta.from) ? day(w[0]) : day(state.data.meta.from);
  return `${from} – ${day(state.data.meta.to)} · ${plural(sc.S.length, "session")}${state.scope.agent ? ` · ${esc(state.scope.agent)}` : ""}`;
}

// ---------- routing ----------

function route() {
  const [page, a, b] = (location.hash.slice(1) || "overview").split("~");
  const views = { overview: viewOverview, recs: viewRecs, rec: () => viewRec(a), corrections: viewCorrections, adoption: viewAdoption,
    delivery: viewDelivery, sessions: viewSessions, session: () => viewSession(a, b), method: () => viewMethod(a) };
  if (!has("github")) delete views.delivery;
  // Nothing imported: every page would be empty tables, so say so once.
  const view = state.data.sessions.length ? views[page] || viewOverview : () => { renderTopbar({ crumbs: [{ label: "Overview" }] }); return NOTICES.sessions(); };
  renderNav(page === "rec" ? "recs" : page === "session" ? "sessions" : views[page] ? page : "overview");
  $("#content").innerHTML = view();
  const key = location.hash;
  if (state.lastPage !== key && !((page === "session" || page === "method") && a)) window.scrollTo(0, 0);
  state.lastPage = key;
}
function rerender() {
  const y = window.scrollY;
  const focusId = document.activeElement?.id;
  const caret = document.activeElement?.selectionStart;
  route();
  window.scrollTo(0, y);
  const el = focusId && document.getElementById(focusId);
  if (el) { el.focus({ preventScroll: true }); if (caret != null && el.setSelectionRange) el.setSelectionRange(caret, caret); }
}

// ---------- Overview ----------

function viewOverview() {
  renderTopbar({ crumbs: [{ label: "Overview" }], scope: has("labels") });
  const d = state.data;
  const sc = scoped();
  const prev = scoped(1);
  const m = metrics(sc.S);
  const pm = prev && prev.S.length >= 5 ? metrics(prev.S) : null;
  const wk = weekly(sc.S);
  const recs = d.recommendations;
  if (!has("labels")) return NOTICES.labels();

  const kpis = [
    kpi({ label: "Agent corrected", def: "correction_rate", desc: "Person said it was wrong, had it redo work, or caught a bug", value: pct1(m.correctedRate), unit: "%", sub: ofText(m.corrected, m.turns, "messages"), sparkValues: wk.map((w) => w.m.correctedRate), delta: pm && delta(m.correctedRate, pm.correctedRate, { better: "down", pts: true }), href: "#corrections" }),
    kpi({ label: "Agent overlooked", def: "overlooked_info", desc: "Person pointed it to info it could have found itself", value: pct1(m.could_findRate), unit: "%", sub: ofText(m.could_find, m.turns, "messages"), sparkValues: wk.map((w) => w.m.could_findRate), delta: pm && delta(m.could_findRate, pm.could_findRate, { better: "down", pts: true }), href: "#corrections" }),
    kpi({ label: "Shipped rate", def: "shipped_rate", desc: "Sessions that ended with a commit or a PR ready", value: m.shipRate == null ? "—" : Math.round(100 * m.shipRate), unit: "%", sub: m.labeled ? ofText(m.shipped, m.labeled, "sessions") : "No session outcomes labelled", sparkValues: wk.map((w) => w.m.shipRate), delta: pm && delta(m.shipRate, pm.shipRate, { better: "up", pts: true }), href: "#sessions" }),
  ].join("");

  const trend = card({
    title: "Agent corrected and overlooked, by week", def: "correction_rate", tip: "Both as a share of human messages, by the week each session started. Dotted lines and hollow points mark a week that is only partly over or has fewer than 5 sessions.",
    aside: legend([{ c: "var(--sig-corrected)", label: "Agent corrected (%)" }, { c: "var(--sig-could_find)", label: "Agent overlooked (%)" }]),
    body: lineChart(wk, [
      { label: "Agent corrected", color: "var(--sig-corrected)", value: (w) => (w.m.correctedRate == null ? null : 100 * w.m.correctedRate) },
      { label: "Agent overlooked", color: "var(--sig-could_find)", value: (w) => (w.m.could_findRate == null ? null : 100 * w.m.could_findRate) },
    ], { fmtY: (v) => v.toFixed(v < 10 && v % 1 ? 1 : 0), unit: "%" }),
  });

  const top = card({ title: "Top ideas", def: "reach", tip: "The ideas touching the most sessions. Built from all sessions; the period filter does not apply.", flush: true,
    body: recTable(recs.slice(0, 8), { compact: true }), foot: `<a href="#recs">View all ${recs.length} ideas →</a>` });

  const agentRows = [...groupBy(sc.S, (s) => s.agent)].map(([a, ss]) => [a, metrics(ss), restsOnOne(ss)]).sort((x, y) => y[1].sessions - x[1].sessions);
  const maxRate = Math.max(0.0001, ...agentRows.flatMap(([, mm]) => [mm.correctedRate || 0, mm.could_findRate || 0]));
  const byModel = state.overviewGroup === "model";
  const rateCells = (mm, max) => `<td>${inlineBar(mm.correctedRate || 0, max, "var(--sig-corrected)", pctText(mm.correctedRate), `${mm.corrected} of ${mm.turns} messages`)}</td>
        <td>${inlineBar(mm.could_findRate || 0, max, "var(--sig-could_find)", pctText(mm.could_findRate), `${mm.could_find} of ${mm.turns} messages`)}</td>`;
  const mRows = byModel ? modelRows(sc.S) : [];
  const maxModelRate = Math.max(0.0001, ...mRows.flatMap((r) => [r.mm.correctedRate || 0, r.mm.could_findRate || 0]));
  const rows = byModel
    ? mRows.map((r) => `<tr class="click" data-href="corrections"><td class="nowrap"><span class="mono">${esc(r.name)}</span>${restsPill(r.one)}</td><td class="r">${r.mm.sessions}</td>
        ${rateCells(r.mm, maxModelRate)}</tr>`)
    : agentRows.map(([a, mm, one]) => `<tr class="click" data-href="corrections" data-agent="${esc(a)}"><td class="nowrap">${agentChip(a)}${restsPill(one)}</td><td class="r">${mm.sessions}</td>
        ${rateCells(mm, maxRate)}</tr>`);
  const groupTabs = `<div class="tabs" role="group" aria-label="Group by">${[["agent", "Harness"], ["model", "Model"]].map(([k, l]) => `<button type="button" data-ov-group="${k}" aria-pressed="${state.overviewGroup === k}">${l}</button>`).join("")}</div>`;
  const byAgent = card({ title: byModel ? "By model" : "By harness", def: byModel ? "by_model" : "correction_rate", aside: groupTabs, flush: true,
    foot: byModel && hiddenNote(mRows) ? `<span class="muted">${hiddenNote(mRows)}</span>` : "",
    body: `<div class="table-wrap tight"><table><thead><tr><th>${byModel ? "Model" : "Harness"}</th><th class="r">Sessions</th><th>Agent corrected</th><th>Agent overlooked</th></tr></thead><tbody>
      ${rows.join("") || `<tr><td colspan="4" class="empty">No sessions</td></tr>`}
      </tbody></table></div>` });
  setTimeout(() => $$("[data-ov-group]").forEach((b) => b.addEventListener("click", () => { state.overviewGroup = b.dataset.ovGroup; rerender(); })), 0);

  const dirs = hotspotDirs(sc).slice(0, 6);
  const hot = card({ title: "Where corrections happen", def: "hotspot", flush: true,
    body: dirs.length ? `<div class="table-wrap"><table><thead><tr><th>Directory</th><th class="r">Sessions</th><th class="r">Flagged turns</th></tr></thead><tbody>
      ${dirs.map((g) => `<tr class="click" data-dir="${esc(g.dir)}"><td class="mono">${esc(g.dir)}</td><td class="r">${g.sessions.size}</td><td class="r">${g.turns.size}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No hotspots in this period.</div>`,
    foot: `<a href="#corrections">Open corrections →</a>` });

  const section = (title, sub) => `<div class="section-title"><h2>${esc(title)}</h2>${sub ? `<span>${sub}</span>` : ""}</div>`;
  return `${intro()}
    ${section("Statistics", scopeLabel())}
    <div class="kpis cols-3">${kpis}</div>
    <div class="grid"><div class="span-7">${trend}</div><div class="span-5">${byAgent}</div></div>
    ${section("Corrections", scopeLabel())}
    <div class="grid"><div class="span-12">${hot}</div></div>
    ${section("Ideas", `From all ${fmt(d.sessions.length)} sessions; the period and harness filters don't apply`)}
    <div class="grid"><div class="span-12">${recs.length ? top : NOTICES.ideas()}</div></div>`;
}

// A backlog title names the fix ("Fix macOS path failure in …"); in a list, name the bug itself.
const bugName = (title) => title.replace(/^(check and file:\s*|(fix|track|make|reject|unify|validate)\s+(the\s+)?)/i, "");

// The first thing a new viewer reads: what this is, and three findings computed from all sessions.
function intro() {
  const d = state.data;
  const all = d.sessions;
  const m = metrics(all);
  const prs = new Set(all.flatMap((s) => s.prs.filter((p) => p.state !== "open").map((p) => p.n))).size;
  const people = has("people") ? ` from ${plural(d.recommendations[0]?.people, "person", "people")}` : "";
  const stats = [`${fmt(all.length)} sessions`, has("people") && plural(m.people, "person", "people"), `${fmt(m.turns)} human messages`,
    has("github") && `${fmt(prs)} merged or closed PRs with CI and review`, `${day(d.meta.from)} – ${day(d.meta.to)}`].filter(Boolean).join(" · ");

  // Compare only harnesses with 20+ sessions whose rate doesn't rest on one session; name any
  // harness left out for resting on one session when its rate sits outside the compared ones.
  const agents = [...groupBy(all, (s) => s.agent)].filter(([, ss]) => ss.length >= 5)
    .map(([a, ss]) => ({ a, n: ss.length, rate: metrics(ss).correctedRate, one: restsOnOne(ss) })).sort((x, y) => y.rate - x.rate);
  const solid = agents.filter((x) => x.n >= 20 && !x.one);
  const hi = solid[0];
  const lo = solid[solid.length - 1];
  const compared = hi && lo && hi !== lo ? `${esc(hi.a)} was corrected in ${pctText(hi.rate)} of messages, ${esc(lo.a)} in ${pctText(lo.rate)}.` : "";
  const caveats = hi ? agents.filter((x) => x.one && (x.rate > hi.rate || x.rate < lo.rate))
    .map((x) => `${esc(x.a)}'s ${x.rate > hi.rate ? "higher" : "lower"} rate comes mostly from one session.`) : [];
  const agentLine = [compared, ...caveats].filter(Boolean).join(" ");

  const top = d.recommendations[0];
  // Bug ideas whose evidence held up; weak ones stay on the Ideas page but aren't counted here.
  const bugs = d.recommendations.filter((r) => !r.weak && r.findings.some((i) => d.findings[i]?.kind === "found_not_filed"));

  const findings = [
    m.corrected && { big: `1 in ${Math.round(1 / m.correctedRate)}`, what: "messages people send an agent correct its work.", detail: agentLine, go: "See the messages", href: "#corrections" },
    top && { eyebrow: "Top idea", title: top.title, detail: `Backed by ${plural(top.evidence.length, "cited item")} across ${plural(top.sessions.length, "session")}${people}.`, go: "See the change and evidence", href: `#rec~${top.id}` },
    bugs.length && { big: fmt(bugs.length), what: "possible bugs agents flagged outside their task and left unfiled in the session.", detail: `For example: ${bugs.slice(0, 2).map((r) => `“${esc(bugName(r.title))}”`).join(" and ")}.`, go: "See the list", href: "#recs", lever: bugs[0]?.lever },
  ].filter(Boolean);

  return `<section class="card intro">
    <div class="intro-head">
      <h2>Session debrief</h2>
      ${workspaceLabel() ? `<p class="intro-repo mono">${esc(workspaceLabel())}</p>` : ""}
      <p class="intro-stats">${stats}</p>
    </div>
    <div class="findings">${findings.map((f) => `<div class="finding-cell">
      ${f.eyebrow ? `<span class="finding-eyebrow">${esc(f.eyebrow)}</span><span class="finding-title">${esc(f.title)}</span>` : `<span class="finding-big num">${f.big}</span><span class="finding-title">${esc(f.what)}</span>`}
      ${f.detail ? `<p class="finding-detail">${f.detail}</p>` : ""}
      <a class="finding-go" href="${f.href}"${f.lever ? ` data-lever="${esc(f.lever)}"` : ""}>${esc(f.go)} →</a>
    </div>`).join("")}</div>
  </section>`;
}

function hotspotDirs(sc) {
  const dirs = new Map();
  for (const f of sc.files) {
    const dir = dirOf(f.path);
    const g = dirs.get(dir) || dirs.set(dir, { dir, sessions: new Set(), turns: new Set(), files: [] }).get(dir);
    f.sessions.forEach((s) => g.sessions.add(s));
    f.turns.forEach((t) => g.turns.add(`${t.s}~${t.i}`));
    g.files.push(f);
  }
  return [...dirs.values()].sort((a, b) => b.sessions.size - a.sessions.size || b.turns.size - a.turns.size);
}

// ---------- Recommendations ----------

function filteredRecs() {
  const f = state.recFilter;
  const q = f.q.trim().toLowerCase();
  return state.data.recommendations.filter((r) => (!f.lever || r.lever === f.lever) &&
    (!q || `${r.title} ${r.target} ${r.problem}`.toLowerCase().includes(q)));
}

function viewRecs() {
  const d = state.data;
  renderTopbar({ crumbs: [{ label: "Ideas" }] });
  const f = state.recFilter;
  const recs = filteredRecs();
  if (!d.recommendations.length) return has("labels") ? NOTICES.ideas() : NOTICES.labels();
  const levers = [...new Set([...Object.keys(d.levers || {}), ...d.recommendations.map((r) => r.lever)])];
  const segs = [["", "All", d.recommendations.length], ...levers.map((k) => [k, lever(k), d.recommendations.filter((r) => r.lever === k).length]).filter(([, , n]) => n)];
  return `<p class="page-sub">${d.recommendations.length} ideas from all ${d.meta.sessions} sessions, sorted by how many sessions each touches. Each is a lead to check against its evidence, not a verdict.</p>
    <section class="card">
      <div class="toolbar">
        <input type="search" id="rec-q" placeholder="Search ideas" value="${esc(f.q)}" aria-label="Search ideas">
        <div class="seg-ctl" role="group" aria-label="Type">${segs.map(([k, l, n]) => `<button type="button" data-rec-lever="${esc(k)}" aria-pressed="${f.lever === k}">${esc(l)}<span class="n">${n}</span></button>`).join("")}</div>
        <span class="spacer"></span><span class="muted small num">${recs.length} shown</span>
      </div>
      ${recTable(sortRecs(recs), { sortable: true })}
    </section>`;
}

function viewRec(id) {
  const d = state.data;
  const r = d.recommendations.find((x) => String(x.id) === String(id));
  if (!r) { renderTopbar({ crumbs: [{ label: "Ideas", href: "#recs" }, { label: "Not found" }] }); return `<div class="empty">This idea is not in the current run.</div>`; }
  const order = sortRecs(d.recommendations);
  const i = order.indexOf(r);
  const pager = `<div class="pager"><a href="#rec~${order[i - 1]?.id}" aria-label="Previous idea" aria-disabled="${i === 0}">↑</a><a href="#rec~${order[i + 1]?.id}" aria-label="Next idea" aria-disabled="${i === order.length - 1}">↓</a></div>`;
  renderTopbar({ crumbs: [{ label: "Ideas", href: "#recs" }, { label: r.title }], right: pager });

  const ev = r.evidence.map((k) => d.refs[k]).filter((x) => x && x.type !== "missing");
  const counts = { msg: ev.filter((x) => x.type === "msg").length, check: ev.filter((x) => x.type === "check").length, review: ev.filter((x) => x.type === "review").length };
  const tabs = [["all", "All", ev.length], ["msg", "Messages", counts.msg], ["check", "CI runs", counts.check], ["review", "Review comments", counts.review]].filter(([, , n], j) => j === 0 || n);
  if (!tabs.some(([k]) => k === state.evTab)) state.evTab = "all";
  const shown = ev.filter((x) => state.evTab === "all" || x.type === state.evTab);

  const main = `<div class="detail-main">
    <section class="card"><div class="card-body" style="gap:14px">
      <div class="issue-title">${esc(r.title)}</div>
      ${r.impact ? `<div class="summary">${esc(r.impact)}</div>` : ""}
      <div><h3 style="margin-bottom:6px">Problem</h3><div class="prose">${md(r.problem)}</div></div>
      <div><div class="change-head" style="margin-bottom:6px"><h3>Suggested change</h3><button type="button" id="copy-change">Copy</button></div>
        <div class="change-body" id="change-body">${md(r.change)}</div></div>
    </div></section>
    ${card({ title: "Evidence", aside: `<div class="tabs" role="group" aria-label="Evidence type">${tabs.map(([k, l, n]) => `<button type="button" data-ev-tab="${k}" aria-pressed="${state.evTab === k}">${l} <span class="num">${n}</span></button>`).join("")}</div>`, flush: true,
      body: `<div class="evlist">${shown.map(evidenceItem).join("")}</div>` })}
  </div>`;

  const props = `<aside class="card props">
    <dl>
      <dt>Type</dt><dd>${esc(lever(r.lever))}</dd>
      ${r.target ? `<dt>Lands in</dt><dd class="mono small">${esc(r.target)}</dd>` : ""}
    </dl>
    <div class="section"><h3>Reach</h3>
      <div class="num">${plural(r.sessions.length, "session")}${has("people") ? ` · ${plural(r.people, "person", "people")}` : ""}</div>
      ${r.checked ? `<div class="small" data-tip="A second model pass checked each cited item against this idea; items that didn't support it are hidden and don't count toward reach.">${r.evidence.length} of ${plural(r.evidence_all, "cited item")} support this idea</div>` : ""}
      ${ev.length ? `<div class="muted small">${[counts.msg && plural(counts.msg, "message"), counts.check && plural(counts.check, "CI run"), counts.review && plural(counts.review, "review comment")].filter(Boolean).join(" · ")}${r.checked ? "" : " cited"}</div>` : ""}
      ${r.weak ? `<div class="small" style="color:var(--text-muted)">Weak evidence. ${WEAK_TIP}</div>` : r.sessions.length <= 1 ? `<div class="small" style="color:var(--text-muted)">Single session. Check the evidence before acting.</div>` : ""}
    </div>
  </aside>`;

  setTimeout(() => wireRec(r), 0);
  return `<div class="detail">${main}${props}</div>`;
}

function evidenceItem(x) {
  if (x.type === "msg") {
    const s = state.sessionsById.get(x.session_id) || {};
    const who = x.role === "assistant" ? "Agent" : "Human";
    const sigs = (x.signals || []).filter((g) => g !== "not_filed" || x.role === "assistant");
    return `<a class="ev" href="${sessionHref(x.session_id, x.msg_idx)}">
      <div class="ev-head"><span class="src">${esc(s.name || short(x.session_id))}</span><span>${esc(ranBy(s))} · turn ${x.msg_idx}</span></div>
      <div class="quote${x.role === "assistant" ? " agent" : ""}"><span class="muted small">${who}: </span>${esc(x.role === "assistant" ? x.agent_before || x.text : x.text)}</div>
      ${sigs.length ? `<div class="chips">${sigs.map((g) => sigChip(g, g === "correction" && x.correction_type ? ` · ${esc(CORRECTION_TYPES[x.correction_type] || x.correction_type)}` : "")).join("")}</div>` : ""}
    </a>`;
  }
  if (x.type === "check") {
    return `<div class="ev">
      <div class="ev-head"><span class="src">CI · <span class="mono">${esc(x.name)}</span> failed</span><span>${esc(CI_CLASSES[x.failure_class] || "Unlabeled")} · ${dayTime(x.at)} · ${x.prs.map(prLink).join(", ") || "no PR"}${x.sessions.length ? ` · <a href="${sessionHref(x.sessions[0])}">session</a>` : ""}</span></div>
      <details><summary>Log tail</summary><pre style="margin-top:6px">${esc(x.log)}</pre></details>
    </div>`;
  }
  if (x.type === "review") {
    const loc = x.path ? `${x.path}${x.line ? ":" + x.line : ""}` : "PR";
    return `<div class="ev">
      <div class="ev-head"><span class="src">Review · ${esc(x.author)}${x.is_bot ? " (bot)" : ""}</span><span><span class="mono">${esc(loc)}</span> · ${prLink(x.pr)} · ${esc(HELD[x.held_up] || "Unlabeled")} · thread ${x.resolved ? "resolved" : x.outdated ? "outdated" : "open"}</span></div>
      <div class="quote agent">${esc(x.body)}</div>
    </div>`;
  }
  return "";
}

function wireRec(r) {
  $$("[data-ev-tab]").forEach((b) => b.addEventListener("click", () => { state.evTab = b.dataset.evTab; rerender(); }));
  $("#copy-change")?.addEventListener("click", async (e) => {
    try { await navigator.clipboard.writeText(r.change || ""); e.target.textContent = "Copied"; } catch {
      const range = document.createRange();
      range.selectNodeContents($("#change-body"));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      e.target.textContent = "Selected";
    }
    setTimeout(() => { e.target.textContent = "Copy"; }, 2000);
  });
}

// ---------- Corrections ----------

function viewCorrections() {
  renderTopbar({ crumbs: [{ label: "Corrections" }], scope: has("labels") });
  const sc = scoped();
  const prev = scoped(1);
  const m = metrics(sc.S);
  const pm = prev && prev.S.length >= 5 ? metrics(prev.S) : null;
  const wk = weekly(sc.S);
  const f = state.correctionsFilter;
  if (!has("labels")) return NOTICES.labels();

  const headline = kpi({ label: "Agent corrected", def: "correction_rate", value: pct1(m.correctedRate), unit: "%", sub: ofText(m.corrected, m.turns, "messages"),
    sparkValues: wk.map((w) => w.m.correctedRate), delta: pm && delta(m.correctedRate, pm.correctedRate, { better: "down", pts: true }) })
    + kpi({ label: "Agent overlooked", def: "overlooked_info", value: pct1(m.could_findRate), unit: "%", sub: ofText(m.could_find, m.turns, "messages"),
      sparkValues: wk.map((w) => w.m.could_findRate), delta: pm && delta(m.could_findRate, pm.could_findRate, { better: "down", pts: true }) });

  const agentRows = [...groupBy(sc.S, (s) => s.agent)].map(([a, ss]) => ({ a, mm: metrics(ss), one: restsOnOne(ss), tools: sum(ss, (s) => s.tool_calls), err: sum(ss, (s) => s.tool_errors) }))
    .sort((x, y) => y.mm.sessions - x.mm.sessions);
  const cols = [
    { label: "Agent corrected", rate: (mm) => mm.correctedRate, n: (mm) => mm.corrected, color: "var(--sig-corrected)" },
    ...CORRECTION_KINDS.map((g) => ({ label: SIGNALS[g].plural, rate: (mm) => mm[`${g}Rate`], n: (mm) => mm[g], color: `var(--sig-${g})`, sub: true })),
    { label: "Agent overlooked", rate: (mm) => mm.could_findRate, n: (mm) => mm.could_find, color: "var(--sig-could_find)" },
  ];
  const maxRate = Math.max(0.0001, ...agentRows.flatMap((r) => cols.map((c) => c.rate(r.mm) || 0)));
  const byAgent = card({ title: "By harness", def: "correction_rate", tip: "Each as a share of the agent's human messages. The three middle columns break down Agent corrected; a message can count in more than one. Codex runs everything through one shell tool, so its failed-call rate includes test runs that fail on purpose.", flush: true,
    body: `<div class="table-wrap"><table><thead><tr><th>Harness</th><th class="r">Sessions</th><th class="r">Human messages</th>${cols.map((c) => `<th${c.sub ? ' class="muted"' : ""}>${esc(c.label)}</th>`).join("")}<th class="r">Failed tool calls</th></tr></thead><tbody>
      ${agentRows.map((r) => `<tr><td class="nowrap">${agentChip(r.a)}${r.mm.sessions < 10 ? ` <span class="pill" data-tip="Fewer than 10 sessions: treat the rates as anecdotes">low n</span>` : restsPill(r.one)}</td><td class="r">${r.mm.sessions}</td><td class="r">${fmt(r.mm.turns)}</td>
        ${cols.map((c) => `<td>${inlineBar(c.rate(r.mm) || 0, maxRate, c.color, pctText(c.rate(r.mm)), `${r.a}: ${c.n(r.mm)} of ${r.mm.turns} messages`)}</td>`).join("")}
        <td class="r">${r.tools ? `${pct(r.err, r.tools)}% <span class="muted">of ${fmt(r.tools)}</span>` : `<span class="muted">—</span>`}</td></tr>`).join("")}</tbody></table></div>` });

  const mRows = modelRows(sc.S);
  const maxModelRate = Math.max(0.0001, ...mRows.flatMap((r) => [r.mm.correctedRate || 0, r.mm.could_findRate || 0]));
  const byModel = card({ title: "By model", def: "by_model", flush: true, foot: hiddenNote(mRows) && `<span class="muted">${hiddenNote(mRows)}</span>`,
    body: `<div class="table-wrap"><table><thead><tr><th>Model</th><th>Harnesses</th><th class="r">Sessions</th><th class="r">Human messages</th><th>Agent corrected</th><th>Agent overlooked</th></tr></thead><tbody>
      ${mRows.map((r) => `<tr><td class="nowrap"><span class="mono">${esc(r.name)}</span>${restsPill(r.one)}</td><td>${harnessDots(r.agents)}</td><td class="r">${r.mm.sessions}</td><td class="r">${fmt(r.mm.turns)}</td>
        ${`<td>${inlineBar(r.mm.correctedRate || 0, maxModelRate, "var(--sig-corrected)", pctText(r.mm.correctedRate), `${r.name}: ${r.mm.corrected} of ${r.mm.turns} messages`)}</td>
        <td>${inlineBar(r.mm.could_findRate || 0, maxModelRate, "var(--sig-could_find)", pctText(r.mm.could_findRate), `${r.name}: ${r.mm.could_find} of ${r.mm.turns} messages`)}</td>`}</tr>`).join("") || `<tr><td colspan="6" class="empty">No sessions</td></tr>`}</tbody></table></div>` });

  const corrections = sc.flagged.filter((t) => t.sig.includes("correction"));
  const types = [...groupBy(corrections, (t) => t.ctype || "unclassified")].sort((a, b) => b[1].length - a[1].length);
  const typeCard = card({ title: "What the agent got wrong", def: "mistake_types", tip: "For messages that said the agent was wrong, what kind of mistake it was. Click a row to filter the messages below.",
    aside: plural(corrections.length, "message"),
    body: bars(types.map(([k, ts]) => ({ label: CORRECTION_TYPES[k] || k, value: ts.length, color: "var(--sig-corrected)", href: `corrections~ctype~${k}`, tip: `${ts.length} messages in ${new Set(ts.map((t) => t.s)).size} sessions` })), { labelWidth: "minmax(110px,190px)" }) });

  const dirs = hotspotDirs(sc).slice(0, 10);
  const dirCard = card({ title: "Where corrections happen", def: "hotspot", flush: true,
    body: dirs.length ? `<div class="table-wrap"><table><thead><tr><th>Directory</th><th class="r">Sessions</th><th class="r">Flagged turns</th><th class="r">Files</th></tr></thead><tbody>
      ${dirs.map((g) => `<tr class="click" data-dir="${esc(g.dir)}"${f.dir === g.dir ? ' style="background:var(--surface-selected)"' : ""}><td class="mono">${esc(g.dir)}</td><td class="r">${g.sessions.size}</td><td class="r">${g.turns.size}</td><td class="r">${g.files.length}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No hotspots in this period.</div>` });

  // Flagged turns, filtered by signal, correction type or directory.
  let turns = sc.flagged;
  if (f.signal) turns = turns.filter((t) => t.sig.includes(f.signal));
  if (f.ctype) turns = turns.filter((t) => t.sig.includes("correction") && (t.ctype || "unclassified") === f.ctype);
  if (f.dir) {
    const keys = new Set(sc.files.filter((x) => dirOf(x.path) === f.dir).flatMap((x) => x.turns.map((t) => `${t.s}~${t.i}`)));
    turns = turns.filter((t) => keys.has(`${t.s}~${t.i}`));
  }
  turns = interleave(turns.slice().sort((a, b) => time(b.ts || 0) - time(a.ts || 0)), (t) => t.s);
  const sigCounts = Object.fromEntries(SIG_ORDER.map((g) => [g, sc.flagged.filter((t) => t.sig.includes(g)).length]));
  const active = [f.ctype && `Type: ${CORRECTION_TYPES[f.ctype] || f.ctype}`, f.dir && `Directory: ${f.dir}`].filter(Boolean);
  const turnCard = `<section class="card">
    <div class="card-head"><h2>Flagged messages ${info("Every human message that corrected the agent or pointed it to info it overlooked. Click a row to open the session at that message.")}</h2><span class="aside num">${plural(turns.length, "message")}</span></div>
    <div class="toolbar">
      <div class="seg-ctl" role="group" aria-label="Signal"><button type="button" data-signal="" aria-pressed="${!f.signal}">All<span class="n">${sc.flagged.length}</span></button>${SIG_ORDER.map((g) => `<button type="button" data-signal="${g}" aria-pressed="${f.signal === g}">${glyph(g)} ${SIGNALS[g].label}<span class="n">${sigCounts[g]}</span></button>`).join("")}</div>
      ${active.map((a) => `<span class="pill accent">${esc(a)}</span>`).join("")}${active.length ? `<button type="button" class="linklike" id="clear-corrections">Clear</button>` : ""}
    </div>
    ${turns.length ? `<div class="table-wrap" style="max-height:560px;overflow-y:auto"><table><thead><tr><th>Date</th><th>Session</th><th>Signals</th><th>What the person said</th></tr></thead><tbody>${turnRows(turns.slice(0, 150))}</tbody></table></div>
      ${turns.length > 150 ? `<div class="card-foot muted">Showing 150 of ${turns.length}. Narrow with the filters above.</div>` : ""}` : `<div class="empty">No turns match.</div>`}
  </section>`;

  setTimeout(wireCorrections, 0);
  return `<p class="page-sub">${scopeLabel()} · ${fmt(m.turns)} human messages</p>
    <div class="kpis cols-2">${headline}</div>
    <div class="grid"><div class="span-12">${byAgent}</div><div class="span-12">${byModel}</div><div class="span-5">${typeCard}</div><div class="span-7">${dirCard}</div><div class="span-12" id="turns">${turnCard}</div></div>`;
}

function wireCorrections() {
  const f = state.correctionsFilter;
  $$("[data-signal]").forEach((b) => b.addEventListener("click", () => { f.signal = b.dataset.signal; rerender(); }));
  $("#clear-corrections")?.addEventListener("click", () => { f.ctype = ""; f.dir = ""; if (location.hash === "#corrections") rerender(); else location.hash = "corrections"; });
}

// ---------- Adoption ----------

function viewAdoption() {
  renderTopbar({ crumbs: [{ label: "Adoption" }], scope: true });
  const sc = scoped();
  const S = sc.S;
  const all = state.data.sessions;
  const agents = [...new Set(all.map((s) => s.agent))].sort((a, b) => all.filter((s) => s.agent === b).length - all.filter((s) => s.agent === a).length);
  const wk = weekly(S);
  const labelled = has("labels");
  const habits = habitCard(S, new Set(sc.skills.map((u) => u.s)));

  const maxWeek = Math.max(1, ...wk.map((w) => w.S.length));
  const weekChart = wk.length ? `<div class="col-chart"><div class="cols">${wk.map((w) => `<div class="col">${agents.map((a) => { const n = w.S.filter((s) => s.agent === a).length;
    return n ? `<span class="seg" style="height:${(100 * n) / maxWeek}%;--c:${agentColor(a)}" data-tip="Week of ${day(w.week)}\n${a}: ${n}"></span>` : ""; }).join("")}</div>`).join("")}</div>
    <div class="col-labels">${wk.map((w) => `<span>${day(w.week)}</span>`).join("")}</div></div>` : `<div class="empty">No sessions.</div>`;

  const models = [...groupBy(S, (s) => `${s.agent}|${s.model || "unknown"}`)].sort((a, b) => b[1].length - a[1].length);
  const moreModels = models.length - 10;
  const people = [...groupBy(S.filter((s) => s.author), (s) => s.author)].sort((a, b) => b[1].length - a[1].length);
  const nobody = S.filter((s) => !s.author);
  if (nobody.length) people.push([NO_PERSON, nobody]);

  const kinds = {};
  S.forEach((s) => Object.entries(s.turn_kinds).forEach(([k, n]) => { kinds[k] = (kinds[k] || 0) + n; }));
  const totalTurns = sum(Object.values(kinds));
  const kindRows = Object.entries(kinds).sort((a, b) => b[1] - a[1]).map(([k, n]) => ({ label: TURN_KINDS[k] || k, value: n, valText: `${pct(n, totalTurns)}%`, color: k === "correction" ? "var(--sig-corrected)" : undefined, tip: `${fmt(n)} messages` }));

  // Without GitHub no session has a settled PR, so compare all of them.
  const settled = has("github") ? S.filter((s) => s.settled) : S;
  const styles = [...groupBy(settled.filter((s) => s.prompt_style), (s) => s.prompt_style)].map(([k, ss]) => ({ k, n: ss.length, f: sum(ss, correctedTurns) / ss.length, shipped: ss.filter((s) => s.outcome === "shipped_or_pr_ready").length }))
    .sort((a, b) => a.f - b.f);
  const maxF = Math.max(0.0001, ...styles.map((r) => r.f));

  const skills = [...groupBy(sc.skills, (u) => u.skill)].map(([k, us]) => ({ k, sessions: new Set(us.map((u) => u.s)).size, human: us.filter((u) => u.by === "human").length, agent: us.filter((u) => u.by === "agent").length }))
    .sort((a, b) => b.sessions - a.sessions || b.human + b.agent - a.human - a.agent).slice(0, 12);

  return `<p class="page-sub">${scopeLabel()}</p>
    <div class="grid">
      ${labelled ? `<div class="span-12">${habits}</div>` : ""}
      <div class="span-8">${card({ title: "Sessions by week", def: "sessions", tip: "Sessions by the week they started.", aside: legend(agents.map((a) => ({ c: agentColor(a), label: a }))), body: weekChart })}</div>
      <div class="span-4">${card({ title: "Harnesses and models", flush: true, foot: moreModels > 0 ? `<span class="muted">+${plural(moreModels, "more model")}</span>` : "",
        body: `<div class="table-wrap"><table><thead><tr><th>Harness</th><th>Model</th><th class="r">Sessions</th></tr></thead><tbody>${models.slice(0, 10).map(([k, ss]) => { const [a, mo] = k.split("|"); return `<tr><td class="nowrap">${agentChip(a)}</td><td class="mono small">${esc(mo)}</td><td class="r">${ss.length}</td></tr>`; }).join("")}</tbody></table></div>` })}</div>
      ${has("people") ? `<div class="span-6">${card({ title: "People", def: "per_person", tip: `${defTip("per_person")}${nobody.length ? ` ${plural(nobody.length, "session")} ha${nobody.length === 1 ? "s" : "ve"} no person: the harness didn't record who ran them.` : ""}`, flush: true,
        body: `<div class="table-wrap"><table><thead><tr><th>Person</th><th class="r">Sessions</th><th>Harnesses</th><th class="r">Human messages</th><th>Last session</th></tr></thead><tbody>
        ${people.map(([p, ss]) => `<tr class="click" data-href="sessions" data-author="${esc(p)}"><td${p === NO_PERSON ? ' class="muted"' : ""}>${esc(personLabel(p, "No person recorded"))}</td><td class="r">${ss.length}</td><td><span class="mini">${[...new Set(ss.map((s) => s.agent))].map((a) => `<span class="dot" style="--c:${agentColor(a)}" data-tip="${esc(a)}"></span>`).join("")}</span></td><td class="r">${fmt(sum(ss, (s) => s.human_turns))}</td><td class="num">${day(ss.reduce((x, s) => (time(s.started) > time(x) ? s.started : x), ss[0].started))}</td></tr>`).join("")}
        </tbody></table></div>` })}</div>` : ""}
      ${labelled ? `<div class="span-6">${card({ title: "How human messages are spent", def: "turn_kinds", aside: `${fmt(totalTurns)} messages`, body: bars(kindRows, { labelWidth: "minmax(120px,170px)" }) })}</div>
      <div class="span-6">${card({ title: "Opening prompt vs corrections", def: "prompt_styles", tip: has("github") ? "" : "How the person opened the session. Small groups: read a gap as a lead, not proof.",
        aside: `${has("github") ? "Sessions with a merged or closed PR" : "All sessions"} (${fmt(settled.length)})`, flush: true,
        body: `<div class="table-wrap"><table><thead><tr><th>Opening prompt</th><th class="r">Sessions</th><th>Corrections / session</th><th class="r">Shipped</th></tr></thead><tbody>
        ${styles.map((r) => `<tr><td>${esc(PROMPT_STYLES[r.k] || r.k)}</td><td class="r">${r.n}</td><td>${inlineBar(r.f, maxF, "var(--sig-corrected)", r.f.toFixed(2))}</td><td class="r">${pct(r.shipped, r.n)}%</td></tr>`).join("") || `<tr><td colspan="4" class="empty">No ${has("github") ? "settled sessions" : "sessions with a labelled opening prompt"} in this period.</td></tr>`}
        </tbody></table></div>` })}</div>` : ""}
      <div class="span-6">${card({ title: "Skills and slash commands", def: "skills_use", flush: true,
        body: skills.length ? `<div class="table-wrap"><table><thead><tr><th>Skill</th><th class="r">Sessions</th><th class="r">Typed</th><th class="r">Auto-loaded</th></tr></thead><tbody>
        ${skills.map((k) => `<tr><td class="mono small">${esc(k.k)}</td><td class="r">${k.sessions}</td><td class="r">${k.human}</td><td class="r">${k.agent}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No skill use in this period.</div>` })}</div>
    </div>`;
}

// Agent corrected in sessions with each habit vs without. Sessions with no label for a habit sit out that row.
const HABITS = [
  { label: "Agent planned before editing", has: (s) => (s.planned_first ? s.planned_first === "true" : null) },
  { label: "Session changed tests", has: (s) => (s.tests_added ? s.tests_added === "true" : null) },
  { label: "Used a skill or slash command", has: (s, skilled) => skilled.has(s.id) },
  { label: "Opened with an issue or PR link", has: (s) => (s.prompt_style ? s.prompt_style === "links_issue_or_pr" : null) },
];
function habitCard(S, skilled) {
  const rows = HABITS.map((h) => {
    const by = groupBy(S, (s) => h.has(s, skilled));
    const yes = by.get(true) || [];
    const no = by.get(false) || [];
    return { label: h.label, yes: metrics(yes), no: metrics(no), yesOne: restsOnOne(yes), noOne: restsOnOne(no) };
  });
  const max = Math.max(0.0001, ...rows.flatMap((r) => [r.yes.correctedRate || 0, r.no.correctedRate || 0]));
  const cell = (mm, one) => `<td>${inlineBar(mm.correctedRate || 0, max, "var(--sig-corrected)", pctText(mm.correctedRate), `${mm.corrected} of ${mm.turns} messages`)}<div class="cell-sub">${plural(mm.sessions, "session")}${restsPill(one)}</div></td>`;
  // Under 2 points, or a side that rests on one session, is too close to call.
  const diff = (r) => {
    if (r.yes.sessions < 20 || r.no.sessions < 20) return `<span class="muted small">too few sessions</span>`;
    const d = 100 * (r.yes.correctedRate - r.no.correctedRate);
    if (Math.abs(d) < 2 || r.yesOne || r.noOne) {
      return `<span class="muted small" data-tip="${Math.abs(d).toFixed(1)} pts ${d < 0 ? "fewer" : "more"} with the habit">no clear difference</span>`;
    }
    return `<span class="delta ${d < 0 ? "good" : "bad"}">${Math.abs(d).toFixed(1)} pts ${d < 0 ? "fewer" : "more"}</span>`;
  };
  return card({ title: "Habits and corrections", tip: "Share of human messages that corrected the agent, in sessions with and without each habit. A difference is a lead, not proof: harder tasks may draw both the habit and the corrections. Gaps under 2 points, or where one side rests on a single session, show as no clear difference.",
    aside: `All sessions (${fmt(S.length)})`, flush: true,
    body: `<div class="table-wrap"><table><thead><tr><th>Habit</th><th>With</th><th>Without</th><th class="r">Difference</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${esc(r.label)}</td>${cell(r.yes, r.yesOne)}${cell(r.no, r.noOne)}<td class="r nowrap">${diff(r)}</td></tr>`).join("")}</tbody></table></div>` });
}

// ---------- CI & review ----------

function viewDelivery() {
  renderTopbar({ crumbs: [{ label: "CI & review" }], scope: true });
  const sc = scoped();
  const fails = sc.fails;
  const R = sc.reviews;
  const bots = R.filter((r) => r.bot);
  // Validity counts inline comments only: review summaries can't be right or wrong, and they repeat inline findings.
  const inlineR = R.filter((r) => r.kind === "inline");
  const botInline = inlineR.filter((r) => r.bot);
  const botWrong = botInline.filter((r) => r.held === "wrong").length;
  const heldTip = "Inline review comments only; review summaries can't be right or wrong.";
  const infra = fails.filter((f) => f.class === "infra_or_env").length;
  // Checks data holds only each check's latest attempt, so re-run passes show up only once data has retries.
  const reruns = fails.filter((f) => f.flaky).length;
  const rerunTip = "Seen, not judged: the same check failed, then passed later on the same commit. Flaky test is a cause read from the log, so the two counts needn't match.";
  const failNote = `${plural(fails.length, "failed run")} on merged or closed PRs in this window. The rest of the ${fmt(state.data.meta.check_failures)} failed runs on record are on open PRs or an approval gate, or before the window.`;
  // A share of nothing is a dash, not 0%.
  const share100 = (a, b) => (b ? pct(a, b) : "—");
  const unit = (b) => (b ? "%" : "");

  const kpis = [
    kpi({ label: "Failures from infra", def: "ci_causes", tip: `Runner, network, setup or missing-secret failures: not the code's fault. ${failNote}`, value: share100(infra, fails.length), unit: unit(fails.length), sub: ofText(infra, fails.length, "failed runs") }),
    kpi({ label: "Review comments", def: "settled_pr", tip: "Top-level review comments on merged or closed PRs.", value: fmt(R.length), sub: R.length ? `${fmt(inlineR.length)} inline · ${pct(bots.length, R.length)}% from bots` : "" }),
    kpi({ label: "Bot comments valid", def: "held_up", tip: `Bot comments labeled valid (fixed or left open). ${heldTip}`, value: share100(botInline.filter((r) => r.held?.startsWith("valid")).length, botInline.length), unit: unit(botInline.length), sub: botInline.length ? `of ${botInline.length} inline` : "No inline bot comments" }),
    kpi({ label: "Bot comments wrong", def: "held_up", tip: `Bot comments where the thread shows the reviewer was wrong. Each one costs a human rebuttal. ${heldTip}`, value: share100(botWrong, botInline.length), unit: unit(botInline.length), sub: botInline.length ? plural(botWrong, "comment") : "No inline bot comments" }),
  ].join("");

  const byClass = [...groupBy(fails, (f) => f.class)].sort((a, b) => b[1].length - a[1].length);
  const byCheck = [...groupBy(fails, (f) => f.name)].sort((a, b) => b[1].length - a[1].length).slice(0, 10);
  const heldOrder = ["valid_and_fixed", "valid_open", "outdated", "wrong", "unclear", null];
  const who = [["Bots", botInline], ["People", inlineR.filter((r) => !r.bot)]];
  const botRows = [...groupBy(botInline, (r) => r.author)].sort((a, b) => b[1].length - a[1].length);
  // Ideas that cite a CI run or a review comment.
  const related = state.data.recommendations.filter((r) => r.evidence.some((k) => ["check", "review"].includes(state.data.refs[k]?.type)));
  // Infra failures aren't the code's fault; a module-download error names whatever file did the import.
  const placed = fails.filter((f) => f.area && f.class !== "infra_or_env");
  const byArea = [...groupBy(placed, (f) => f.area)].sort((a, b) => b[1].length - a[1].length).slice(0, 10);
  const inline = R.filter((r) => r.path);
  const share = (rs, f) => rs.filter(f).length / rs.length;
  const isWrong = (r) => r.held === "wrong";
  const isValid = (r) => r.held === "valid_and_fixed" || r.held === "valid_open";
  const byDir = [...groupBy(inline, (r) => dirOf(r.path))].filter(([, rs]) => rs.length >= 5)
    .sort((a, b) => share(b[1], isWrong) - share(a[1], isWrong) || b[1].length - a[1].length);
  const extOf = (p) => /\.\w+$/.exec(p.slice(p.lastIndexOf("/") + 1))?.[0];
  const byExt = [...groupBy(inline.filter((r) => extOf(r.path)), (r) => extOf(r.path))].sort((a, b) => b[1].length - a[1].length).slice(0, 4);
  const otherExt = inline.length - byExt.reduce((n, [, rs]) => n + rs.length, 0);
  const extLine = [...byExt.map(([k, rs]) => `<span class="mono">${esc(k)}</span> ${rs.length}`), ...(otherExt ? [`other ${otherExt}`] : [])].join(" · ");
  // Up to three failing test names under the area; the rest in a tooltip.
  const areaTests = (fs) => {
    const names = [...new Set(fs.flatMap((f) => f.tests || []))];
    if (!names.length) return "";
    const more = names.length > 3 ? ` <span data-tip="${esc(names.slice(3).join(", "))}">+${names.length - 3} more</span>` : "";
    return `<div class="muted test-names">${names.slice(0, 3).map(esc).join("<br>")}${more}</div>`;
  };

  return `<p class="page-sub">${scopeLabel()} · merged and closed PRs only</p>
    <div class="kpis cols-4">${kpis}</div>
    <div class="grid">
      <div class="span-5">${card({ title: "CI failures by cause", def: "ci_causes", aside: reruns ? `<span data-tip="${esc(rerunTip)}">${reruns} passed on re-run</span>` : "",
        body: bars(byClass.map(([k, fs]) => ({ label: CI_CLASSES[k] ?? k, value: fs.length,
          tip: `${fs.length} failures on ${new Set(fs.map((f) => f.pr)).size} PRs. Cause read from the log.${k === "flaky_test" ? " Passed on re-run counts what happened next instead." : ""}` }))),
        foot: `<span class="muted">${esc(failNote)}</span>` })}</div>
      <div class="span-7">${card({ title: "Failing checks", flush: true, body: byCheck.length ? `<div class="table-wrap"><table><thead><tr><th>Check</th><th class="r">Failures</th><th>Causes</th><th>PRs</th></tr></thead><tbody>
        ${byCheck.map(([k, fs]) => `<tr><td class="mono small">${esc(k)}</td><td class="r">${fs.length}</td><td class="small">${[...groupBy(fs, (f) => f.class)].map(([c, xs]) => `${esc(CI_CLASSES[c] ?? c)} ${xs.length}`).join(", ")}</td><td class="small">${[...new Set(fs.map((f) => f.pr))].map(prLink).join(", ")}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No CI failures in this period.</div>` })}</div>
      <div class="span-7">${card({ title: "Did review comments hold up?", def: "held_up", tip: `Validity from a classifier reading the thread. ${heldTip}`,
        aside: legend(heldOrder.map((k) => ({ c: HELD_COLOR[k], label: HELD[k] }))),
        body: bars(who.map(([k, rs]) => ({ label: `${k} · ${rs.length}`, value: rs.length, valText: `${pct(rs.filter((r) => r.held === "wrong").length, rs.length)}% wrong`,
          segs: heldOrder.map((h) => ({ v: rs.filter((r) => (r.held ?? null) === h).length, c: HELD_COLOR[h], tip: `${k}, ${HELD[h]}: ${rs.filter((r) => (r.held ?? null) === h).length}` })) })), { max: Math.max(1, ...who.map(([, rs]) => rs.length)) }) })}</div>
      <div class="span-5">${card({ title: "Review bots", tip: heldTip, flush: true, body: botRows.length ? `<div class="table-wrap"><table><thead><tr><th>Bot</th><th class="r">Comments</th><th class="r">Fixed</th><th class="r">Wrong</th></tr></thead><tbody>
        ${botRows.map(([k, rs]) => `<tr><td class="mono small">${esc(k)}</td><td class="r">${rs.length}</td><td class="r">${pct(rs.filter((r) => r.held === "valid_and_fixed").length, rs.length)}%</td><td class="r">${pct(rs.filter((r) => r.held === "wrong").length, rs.length)}%</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No inline bot comments.</div>` })}</div>
      <div class="span-6">${card({ title: "Where CI fails", def: "ci_where", flush: true,
        body: byArea.length ? `<div class="table-wrap"><table><thead><tr><th>Area</th><th class="r">Failures</th><th>Causes</th>${reruns ? `<th class="r" data-tip="${esc(rerunTip)}">Passed on re-run</th>` : ""}</tr></thead><tbody>
        ${byArea.map(([k, fs]) => `<tr><td class="mono small">${esc(k)}${areaTests(fs)}</td><td class="r">${fs.length}</td><td class="small">${[...groupBy(fs, (f) => f.class)].map(([c, xs]) => `${esc(CI_CLASSES[c] ?? c)} ${xs.length}`).join(", ")}</td>${reruns ? `<td class="r">${fs.filter((f) => f.flaky).length}</td>` : ""}</tr>`).join("")}</tbody></table></div>` : `<div class="empty">No CI failures name a package or file in this period.</div>`,
        foot: `<span class="muted">${placed.length} of ${fails.length} failures placed by package or file. ${infra} infra failures are left out; the rest name no file.</span>` })}</div>
      <div class="span-6">${card({ title: "Where review comments land", def: "review_where", flush: true,
        body: inline.length ? `<div class="card-note">${extLine}</div>${byDir.length ? `<div class="table-wrap"><table><thead><tr><th>Directory</th><th class="r">Comments</th><th class="r">Valid</th><th class="r">Wrong</th></tr></thead><tbody>
        ${byDir.map(([k, rs]) => `<tr><td class="mono small">${esc(k)}</td><td class="r muted small">${rs.length}</td><td class="r">${Math.round(100 * share(rs, isValid))}%</td><td class="r">${Math.round(100 * share(rs, isWrong))}%</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No directory has 5 or more comments in this period.</div>`}` : `<div class="empty">No inline review comments in this period.</div>`,
        foot: `<span class="muted">${inline.length} of ${R.length} comments point at a file; review bodies and PR-level comments don't.</span>` })}</div>
      <div class="span-12">${card({ title: "Related ideas", flush: true, body: recTable(related, { compact: true }) })}</div>
    </div>`;
}

// ---------- Sessions ----------

function filteredSessions() {
  const f = state.sessionFilter;
  const q = f.q.trim().toLowerCase();
  const S = scoped().S.filter((s) => (!f.author || (f.author === NO_PERSON ? !s.author : s.author === f.author)) &&
    (!f.project || (f.project === NO_PROJECT ? !s.project : s.project === f.project)) && (!f.outcome || s.outcome === f.outcome) && (!f.corrected || correctedTurns(s) > 0) &&
    (!q || `${s.name} ${s.prompt} ${s.id} ${s.project || ""} ${s.prs.map((p) => p.n).join(" ")}`.toLowerCase().includes(q)));
  const key = { started: (s) => s.started, turns: (s) => s.human_turns, corrections: correctedTurns, name: (s) => (s.name || "").toLowerCase() }[f.sort];
  return S.slice().sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * f.dir);
}

function viewSessions() {
  renderTopbar({ crumbs: [{ label: "Sessions" }], scope: true });
  const f = state.sessionFilter;
  const S = filteredSessions();
  const all = state.data.sessions;
  const authors = [...new Set(all.map((s) => s.author).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  // Two folders with the same name show their parent too, so the options stay distinct.
  const projects = [...new Set(all.map((s) => s.project).filter(Boolean))];
  const sameName = groupBy(projects, projectName);
  const projectOpts = projects.map((p) => [p, pathTail(p, sameName.get(projectName(p)).length > 1 ? 2 : 1)]).sort((a, b) => a[1].localeCompare(b[1]));
  const noProject = all.some((s) => !s.project);
  const col = { project: has("projects"), person: has("people"), pr: has("github") };
  const th = (k, l, cls = "") => `<th class="sortable ${cls}" data-sort="${k}" aria-sort="${f.sort === k ? (f.dir > 0 ? "ascending" : "descending") : "none"}">${l}${f.sort === k ? (f.dir > 0 ? " ↑" : " ↓") : ""}</th>`;
  setTimeout(wireSessions, 0);
  return `<p class="page-sub">${scopeLabel()}</p>
    <section class="card">
      <div class="toolbar">
        <input type="search" id="s-q" placeholder="Search name${col.project ? ", project" : ""}${col.pr ? ", prompt or PR" : " or prompt"}" value="${esc(f.q)}" aria-label="Search sessions">
        ${col.project && projects.length + noProject > 1 ? `<select id="s-project" aria-label="Project" style="max-width:220px"><option value="">All projects</option>${projectOpts.map(([p, l]) => `<option value="${esc(p)}" title="${esc(p)}"${p === f.project ? " selected" : ""}>${esc(l)}</option>`).join("")}${noProject ? `<option value="${esc(NO_PROJECT)}"${f.project === NO_PROJECT ? " selected" : ""}>Unknown</option>` : ""}</select>` : ""}
        ${col.person ? `<select id="s-author" aria-label="Person"><option value="">All people</option>${authors.map((a) => `<option${a === f.author ? " selected" : ""}>${esc(a)}</option>`).join("")}${all.some((s) => !s.author) ? `<option value="${esc(NO_PERSON)}"${f.author === NO_PERSON ? " selected" : ""}>Unknown</option>` : ""}</select>` : ""}
        <select id="s-outcome" aria-label="Outcome"><option value="">Any outcome</option>${Object.entries(OUTCOMES).map(([k, l]) => `<option value="${k}"${k === f.outcome ? " selected" : ""}>${l}</option>`).join("")}</select>
        <label class="check"><input type="checkbox" id="s-corrected"${f.corrected ? " checked" : ""}> Has corrections</label>
        <span class="spacer"></span><span class="muted small num">${S.length} of ${scoped().S.length}</span>
      </div>
      <div class="table-wrap"><table>
        <thead><tr>${th("started", "Started")}${th("name", "Session")}${col.project ? "<th>Project</th>" : ""}<th>Harness</th>${col.person ? "<th>Person</th>" : ""}${th("turns", "Messages", "r")}${th("corrections", "Signals")}<th>Outcome</th>${col.pr ? "<th>PR</th>" : ""}</tr></thead>
        <tbody>${sessionRows(S) || `<tr><td colspan="${6 + col.project + col.person + col.pr}" class="empty">No sessions match.</td></tr>`}</tbody>
      </table></div>
    </section>`;
}

function wireSessions() {
  const f = state.sessionFilter;
  $("#s-q")?.addEventListener("input", (e) => { f.q = e.target.value; rerender(); });
  $("#s-author")?.addEventListener("change", (e) => { f.author = e.target.value; rerender(); });
  $("#s-project")?.addEventListener("change", (e) => { f.project = e.target.value; rerender(); });
  $("#s-outcome")?.addEventListener("change", (e) => { f.outcome = e.target.value; rerender(); });
  $("#s-corrected")?.addEventListener("change", (e) => { f.corrected = e.target.checked; rerender(); });
  $$("th[data-sort]").forEach((th) => th.addEventListener("click", () => { const k = th.dataset.sort; f.dir = f.sort === k ? -f.dir : -1; f.sort = k; rerender(); }));
}

// ---------- Session detail ----------

// Transcripts ship in shard files of many sessions each; keep each shard's promise so
// sessions from the same shard share one fetch.
function loadShard(name) {
  let p = state.shardCache.get(name);
  if (!p) {
    p = fetch(`data/sessions/${name}.json`).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
    p.catch(() => state.shardCache.delete(name));
    state.shardCache.set(name, p);
  }
  return p;
}

function viewSession(id, focusIdx) {
  const s = state.sessionsById.get(id);
  if (!s) { renderTopbar({ crumbs: [{ label: "Sessions", href: "#sessions" }, { label: "Not found" }] }); return `<div class="empty">Session ${esc(id)} is not in this dataset.</div>`; }
  renderTopbar({ crumbs: [{ label: "Sessions", href: "#sessions" }, { label: s.name || short(s.id) }] });
  const cached = state.sessionCache.get(id);
  if (!cached) {
    loadShard(s.shard).then((shard) => {
      if (!shard[id]) throw new Error(`session ${id} missing from shard ${s.shard}`);
      state.sessionCache.set(id, shard[id]);
      if (location.hash.startsWith(`#session~${id}`)) route();
    }).catch(() => { const t = $("#transcript"); if (t) t.innerHTML = `<div class="empty">Could not load the transcript.</div>`; });
  }
  const focus = focusIdx != null ? Number(focusIdx) : null;
  const recs = state.recsBySession.get(id) || [];
  const dur = time(s.last) - time(s.started);
  const durText = Number.isNaN(dur) ? "—" : dur > DAY ? `${(dur / DAY).toFixed(1)} days` : `${Math.max(1, Math.round(dur / 36e5))} h`;

  const main = `<section class="card">
    <div class="card-head"><h2>Transcript</h2></div>
    ${cached ? `<div class="strip-bar">${stripLegend(cached)}${strip(cached)}</div>` : ""}
    <div class="transcript" id="transcript">${cached ? transcript(cached, focus) : `<div class="empty">Loading transcript…</div>`}</div>
  </section>`;

  const props = `<aside class="card props">
    <dl>
      <dt>Harness</dt><dd>${agentChip(s.agent)}</dd>
      <dt>Model</dt><dd class="mono small">${esc(s.model || "unknown")}</dd>
      ${s.project ? `<dt>Project</dt><dd class="mono small" title="${esc(s.project)}">${esc(projectName(s.project))}</dd>` : ""}
      ${has("people") ? `<dt>Person</dt><dd>${esc(personLabel(s.author))}</dd>` : ""}
      <dt>Started</dt><dd>${dayTime(s.started)}</dd>
      <dt>Duration</dt><dd class="num">${durText}</dd>
      <dt>Outcome</dt><dd>${outcomeChip(s.outcome)}</dd>
      <dt>Human turns</dt><dd class="num">${s.human_turns}</dd>
      <dt>Tool calls</dt><dd class="num">${fmt(s.tool_calls)} <span class="muted">(${fmt(s.tool_errors)} failed)</span></dd>
      ${has("checkpoints") ? `<dt>Checkpoints</dt><dd class="num">${s.checkpoints ?? "—"}</dd>` : ""}
      ${has("github") ? `<dt>PR</dt><dd>${s.prs.map((p) => `${prLink(p.n)} <span class="pr-state">${esc(p.state)}</span>`).join("<br>") || "—"}</dd>` : ""}
    </dl>
    <div class="section"><h3>Signals</h3><div class="chips">${SIG_ORDER.map((g) => `<span class="sig" data-tip="${esc(SIGNALS[g].tip)}">${glyph(g)}${s[SIG_FIELD[g]]} ${esc(SIGNALS[g].label.toLowerCase())}</span>`).join("")}</div>
      ${s.not_filed ? `<div class="small muted">${glyph("not_filed")} ${plural(s.not_filed, "problem")} found and left unfiled in the session</div>` : ""}</div>
    ${recs.length ? `<div class="section"><h3>Cited by</h3>${recs.map((r) => `<a href="#rec~${r.id}" class="small">${esc(r.title)}</a>`).join("")}</div>` : ""}
  </aside>`;

  // A timeout, not requestAnimationFrame, which never fires in a background tab.
  if (cached) setTimeout(() => { const el = focus != null && document.getElementById(`m${focus}`); scrollToEl(el, "center"); }, 0);
  return `<div class="detail"><div class="detail-main">${main}</div>${props}</div>`;
}

const STRIP_SIGS = [...SIG_ORDER, "not_filed"];
function stripLegend(j) {
  const present = new Set(j.messages.flatMap((m) => m.sig || []));
  return legend(STRIP_SIGS.filter((g) => present.has(g)).map((g) => ({ glyph: g, label: SIGNALS[g].label })));
}
function strip(j) {
  const last = j.messages.length ? j.messages[j.messages.length - 1].i : 1;
  const marks = j.messages.filter((m) => (m.sig || []).length);
  if (!marks.length) return `<p class="muted small">Nothing flagged in this session.</p>`;
  return `<div class="strip">${marks.map((m) => {
    const g = STRIP_SIGS.find((x) => m.sig.includes(x));
    return `<button class="mark" type="button" style="left:${2 + (96 * m.i) / (last || 1)}%" data-jump="${m.i}" data-tip="Turn ${m.i}: ${m.sig.map((x) => SIGNALS[x].label).join(", ")}" aria-label="Jump to turn ${m.i}">${glyph(g)}</button>`;
  }).join("")}</div>`;
}

function transcript(j, focus) {
  const out = [];
  let run = [];
  const flush = () => { if (run.length) out.push(agentRun(run, focus)); run = []; };
  for (const m of j.messages) {
    if (m.r === "user" && m.k === "human") { flush(); out.push(humanMsg(m, focus)); } else run.push(m);
  }
  flush();
  return out.join("");
}

function humanMsg(m, focus) {
  const sigs = (m.sig || []).filter((g) => g !== "not_filed");
  const l = m.l || {};
  const extra = [l.turn_kind && `Turn: ${l.turn_kind[0].replace(/_/g, " ")}`, l.correction_type && sigs.includes("correction") && `Type: ${CORRECTION_TYPES[l.correction_type[0]] || l.correction_type[0]}`, l.resource_pointed_to && `Pointed to: ${l.resource_pointed_to[0].replace(/_/g, " ")}`].filter(Boolean);
  return `<div class="msg human${m.i === focus ? " focus" : ""}" id="m${m.i}">
    <div class="msg-head"><span class="who">Human · turn ${m.i}</span><span>${dayTime(m.ts)}</span></div>
    <div class="msg-body">${esc(m.t)}</div>
    ${sigs.length || extra.length ? `<div class="chips">${sigs.map((g) => sigChip(g, l[LABEL_FIELD[g]] ? ` <span class="muted num" data-tip="Classifier confidence">${l[LABEL_FIELD[g]][1]}</span>` : "")).join("")}${extra.map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</div>` : ""}
  </div>`;
}

function agentRun(msgs, focus) {
  const parts = [];
  let calls = [];
  const flushCalls = () => {
    if (!calls.length) return;
    const errs = calls.filter((c) => c.err).length;
    const byName = [...groupBy(calls, (c) => c.n)].map(([n, cs]) => `${n} ×${cs.length}`).join(" · ");
    parts.push(`<details class="tools"><summary>${calls.length} tool calls: ${esc(byName)}${errs ? ` · <span class="errtxt">${errs} failed</span>` : ""}</summary>
      <div class="table-wrap"><table><tbody>${calls.map((c) => `<tr${c.err ? ' class="err"' : ""}><td class="nowrap">${esc(c.n)}</td><td class="in">${esc(c.in)}${c.w ? `<div class="muted">wrote ${esc(c.w.join(", "))}</div>` : ""}${c.err ? `<pre style="margin-top:4px">${esc(c.err)}</pre>` : ""}</td></tr>`).join("")}</tbody></table></div></details>`);
    calls = [];
  };
  for (const m of msgs) {
    if (m.r === "user") {
      flushCalls();
      parts.push(`<div class="msg injected" id="m${m.i}">Injected context · turn ${m.i}: ${esc(m.t.slice(0, 200))}${m.t.length > 200 ? "…" : ""}</div>`);
      continue;
    }
    if ((m.t || "").trim()) {
      flushCalls();
      const clamp = m.t.length > 600 && m.i !== focus;
      const notFiled = (m.sig || []).includes("not_filed");
      parts.push(`<div class="msg agent${m.i === focus ? " focus" : ""}" id="m${m.i}">
        <div class="msg-head"><span class="who">Agent · turn ${m.i}</span><span>${dayTime(m.ts)}</span></div>
        ${notFiled ? `<div class="flagnote">${glyph("not_filed")} Noticed a problem outside its task and left it</div>` : ""}
        <div class="msg-body md${clamp ? " clamp" : ""}">${md(m.t)}</div>
        ${clamp ? `<button type="button" class="linklike small" data-unclamp>Show all</button>` : ""}
      </div>`);
    } else if (m.i === focus) {
      parts.push(`<div class="msg agent focus" id="m${m.i}"><div class="msg-head"><span class="who">Agent · turn ${m.i}</span></div><span class="muted">Tool calls only</span></div>`);
    }
    if (m.c) calls.push(...m.c);
  }
  flushCalls();
  return `<div class="agent-run">${parts.join("")}</div>`;
}

// ---------- How it works ----------

function viewMethod(focus) {
  renderTopbar({ crumbs: [{ label: "How it works" }] });
  const better = { lower: "Lower is better", higher: "Higher is better" };
  const num = (v) => fill(v).replace(/^\d+$/, (n) => fmt(Number(n)));
  // Lines marked [github] in method.js describe steps that only run on GitHub data.
  const text = (t) => md(fill(has("github") ? t.replace(/\[github\] /g, "") : t.split("\n").filter((l) => !l.includes("[github]")).join("\n")));
  const glance = `<section class="card glance" aria-label="The method at a glance">${METHOD.steps.map((st, i) => `<a class="glance-step" href="#method~${st.id}">
    <span class="glance-name"><span class="step-n">${i + 1}</span>${esc(st.short)}</span>
    <span class="glance-num num">${esc(num(st.glance[0]))}</span><span class="glance-label">${esc(st.glance[1])}</span></a>`).join("")}</section>`;
  const steps = METHOD.steps.map((st, i) => `<section class="card step${st.id === focus ? " focus" : ""}" id="def-${st.id}">
    <div class="step-head"><span class="step-n">${i + 1}</span><h2>${esc(st.title)}</h2></div>
    <div class="step-body"><div class="step-lede">${text(st.lede)}</div>${st.text.map(text).join("")}
      ${st.facts?.length ? `<div class="facts">${st.facts.filter(([v]) => fill(v) !== "0").map(([v, l]) => `<span><b class="num">${esc(num(v))}</b>${esc(l)}</span>`).join("")}</div>` : ""}</div>
  </section>`).join("");
  const metrics = DEFS.filter((d) => d.page).map((d) => `<article class="def${d.id === focus ? " focus" : ""}" id="def-${d.id}">
    <div class="def-head"><h3>${esc(d.term)}</h3>${d.better ? `<span class="pill">${better[d.better]}</span>` : ""}</div>
    <p>${esc(fill(d.def))}</p>
    ${d.list ? `<dl class="def-list">${d.list.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>` : ""}
    ${d.example ? `<p class="def-meta"><span>Example</span>${esc(d.example)}</p>` : ""}
  </article>`).join("");
  const toc = `<nav class="card toc" aria-label="On this page">
    <div class="toc-group"><div class="nav-label">Method</div>${METHOD.steps.map((st, i) => `<a href="#method~${st.id}"${st.id === focus ? ' aria-current="true"' : ""}>${i + 1}. ${esc(st.title)}</a>`).join("")}</div>
    <div class="toc-group"><div class="nav-label">Metrics</div>${DEFS.filter((d) => d.page).map((d) => `<a href="#method~${d.id}"${d.id === focus ? ' aria-current="true"' : ""}>${esc(d.term)}</a>`).join("")}</div></nav>`;
  if (focus) setTimeout(() => scrollToEl(document.getElementById(`def-${focus}`)), 0);
  return `<section class="card method-intro"><h2>How this analysis works</h2><p>${esc(fill(METHOD.question))}</p></section>
    ${glance}
    <div class="defs">${toc}<div class="defs-main">
      ${steps}
      ${card({ title: "The metrics", body: metrics })}
    </div></div>`;
}

// ---------- global events ----------

function correctionsFromHash() {
  const [page, key, val] = location.hash.slice(1).split("~");
  if (page !== "corrections") return false;
  if (key === "ctype") state.correctionsFilter = { ...state.correctionsFilter, ctype: val, dir: "" };
  if (key === "dir") state.correctionsFilter = { ...state.correctionsFilter, dir: decodeURIComponent(val), ctype: "" };
  return Boolean(key);
}

function initEvents() {
  const tip = $("#tip");
  const show = (t) => { tip.textContent = t.dataset.tip; tip.hidden = false; };
  document.addEventListener("pointerover", (e) => { const t = e.target.closest("[data-tip]"); if (t) show(t); else tip.hidden = true; });
  document.addEventListener("focusin", (e) => { const t = e.target.closest?.("[data-tip]"); if (t) { show(t); const r = t.getBoundingClientRect(); tip.style.left = `${r.left}px`; tip.style.top = `${r.bottom + 6}px`; } });
  document.addEventListener("focusout", () => { tip.hidden = true; });
  document.addEventListener("pointermove", (e) => {
    if (tip.hidden) return;
    tip.style.left = `${Math.max(8, Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8))}px`;
    tip.style.top = `${e.clientY + 18 + tip.offsetHeight > window.innerHeight ? e.clientY - tip.offsetHeight - 10 : e.clientY + 18}px`;
  });

  document.addEventListener("click", (e) => {
    // Rows and links that carry a filter set it before navigating.
    const el = e.target.closest("[data-lever],[data-agent],[data-author],[data-dir]");
    if (el) {
      if (el.dataset.lever) state.recFilter = { ...state.recFilter, lever: el.dataset.lever };
      if (el.dataset.agent) { state.scope.agent = el.dataset.agent; }
      if (el.dataset.author) state.sessionFilter.author = el.dataset.author;
      if (el.dataset.dir) { location.hash = `corrections~dir~${encodeURIComponent(el.dataset.dir)}`; return; }
    }
    const row = e.target.closest("[data-href]");
    if (row && !e.target.closest("a,button,select,input")) location.hash = row.dataset.href;
    const jump = e.target.closest("[data-jump]");
    if (jump) {
      $$(".msg.focus").forEach((x) => x.classList.remove("focus"));
      const m = document.getElementById(`m${jump.dataset.jump}`);
      if (m) { m.classList.add("focus"); scrollToEl(m, "center"); /* instant: a smooth scroll across a long transcript is slow */ }
    }
    const un = e.target.closest("[data-unclamp]");
    if (un) { un.previousElementSibling.classList.remove("clamp"); un.remove(); }
    const rsort = e.target.closest("[data-rec-sort]");
    if (rsort) {
      const f = state.recFilter;
      const k = rsort.dataset.recSort;
      f.dir = f.sort === k ? -f.dir : -1;
      f.sort = k;
      rerender();
    }
    const rl = e.target.closest("[data-rec-lever]");
    if (rl) { state.recFilter.lever = rl.dataset.recLever; rerender(); }
  });
  document.addEventListener("keydown", (e) => {
    const row = e.target.closest?.("[data-href]");
    if (row && row === e.target && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); location.hash = row.dataset.href; }
  });
  document.addEventListener("input", (e) => { if (e.target.id === "rec-q") { state.recFilter.q = e.target.value; rerender(); } });
  window.addEventListener("hashchange", () => {
    const deep = correctionsFromHash();
    route();
    if (deep) setTimeout(() => scrollToEl($("#turns")), 0);
  });
}

// ---------- boot ----------

async function boot() {
  initEvents();
  try {
    state.data = await fetch("data/index.json").then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
  } catch (e) {
    $("#content").innerHTML = `<section class="card"><div class="card-body"><h2>No data</h2><p>Could not load data/index.json (${esc(e.message)}). Run <code>debrief sync</code> to build it, and open this page over http, not as a file.</p></div></section>`;
    return;
  }
  const d = state.data;
  d.recommendations.sort(byReach);
  for (const s of d.sessions) state.sessionsById.set(s.id, s);
  for (const r of d.recommendations) for (const sid of r.sessions) (state.recsBySession.get(sid) || state.recsBySession.set(sid, []).get(sid)).push(r);
  const m = d.meta;
  $("#workspace").innerHTML = `<span>${esc(workspaceLabel())}</span>`;
  $("#side-foot").innerHTML = `<div class="row"><span>Sessions</span><span>${fmt(m.sessions)}</span></div>
    ${m.to ? `<div class="row"><span>Window</span><span>${esc(day(m.from))} – ${esc(day(m.to))}</span></div>` : ""}
    <div class="row"><span>Label confidence</span><span>≥ ${m.min_conf}</span></div>`;
  correctionsFromHash();
  route();
}
boot();

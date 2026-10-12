/* FactLock reviewer screen: plain JavaScript, no build step. Read web/DESIGN.md first.
   The paper is a series of A4 pages. Blocks are laid into a page until it is full, then the next page starts. */
(() => {
  "use strict";

  const F = window.FactLockFormat;
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false || child === "") continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  };
  const T = (lang, en, ja) => (lang === "ja" ? ja : en);
  // Names of the four phases in the conventional reading of the cycle: 4 liquidity-driven -> 1 earnings-driven
  // -> 2 reverse liquidity -> 3 reverse earnings (approved 2026-09-23).
  const PHASE_NAMES = { 1: ["Earnings-driven", "業績相場"], 2: ["Reverse liquidity", "逆金融相場"], 3: ["Reverse earnings", "逆業績相場"], 4: ["Liquidity-driven", "金融相場"] };
  const phaseLabel = (L, n) => (L === "ja" ? PHASE_NAMES[n][1] : PHASE_NAMES[n][0]);
  const REVIEWER_JA = { ran: "実施", "skipped (rules already failed)": "省略（規則で不合格のため）" };

  const state = { health: null, sheet: null, result: null, lang: "both", selected: null, approval: null,
    working: false, error: null };

  // ------------------------------------------------------------------ sign-in
  // "firebase": the visitor is signed in anonymously with Firebase Authentication and every API call carries
  // the ID token. "passphrase": the cookie set by /api/login. "none": nothing.
  const auth = { mode: "none", firebase: null, user: null };

  async function signIn() {
    const config = await (await fetch("/api/config")).json();
    auth.mode = config.auth;
    if (auth.mode !== "firebase") return;
    const SDK = "https://www.gstatic.com/firebasejs/11.6.1/";
    const [{ initializeApp }, { getAuth, signInAnonymously }] = await Promise.all([
      import(`${SDK}firebase-app.js`), import(`${SDK}firebase-auth.js`)]);
    auth.firebase = getAuth(initializeApp(config.firebase));
    auth.user = (await signInAnonymously(auth.firebase)).user;
  }

  async function authHeaders() {
    if (auth.mode !== "firebase" || !auth.user) return {};
    return { Authorization: `Bearer ${await auth.user.getIdToken()}` };   // refreshed by the SDK when it expires
  }

  async function api(path, options) {
    const headers = Object.assign({}, (options && options.headers) || {}, await authHeaders());
    const response = await fetch(path, Object.assign({}, options || {}, { headers }));
    let body = null;
    try { body = await response.json(); } catch (_) { /* empty body */ }
    if (!response.ok) {
      if (response.status === 401 && auth.mode !== "firebase") window.location.replace("/login");
      const error = new Error(body && body.detail ? body.detail : `Request failed (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  // ------------------------------------------------------------------ setup
  async function init() {
    document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => setLanguage(b.dataset.lang)));
    $("#generate").addEventListener("click", generate);
    $("#report-select").addEventListener("change", loadReport);
    $("#approve").addEventListener("click", openApproval);
    $("#approve-cancel").addEventListener("click", closeApproval);
    $("#approve-form").addEventListener("submit", recordApproval);
    $("#copy-md").addEventListener("click", copyMarkdown);
    $("#print").addEventListener("click", () => { prepareForPrint(); window.print(); });
    window.addEventListener("beforeprint", prepareForPrint);
    window.addEventListener("resize", fitPages);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.selected) select(null); });

    await signIn();
    state.health = await api("/api/health");
    const live = state.health.mode === "gemini";
    $("#generate").textContent = generateLabel();
    $("#scenario-field").hidden = live;
    $("#signout").hidden = !state.health.gated;
    $("#signout").addEventListener("click", signOut);

    const { reports } = await api("/api/reports");
    const menu = $("#report-select");
    menu.replaceChildren(...reports.map((r) => el("option", { value: `${r.fund_id}|${r.month}`, text: `${r.fund_id}, ${F.monthText(r.month, "en")}` })));
    if (reports.length) menu.value = `${reports[reports.length - 1].fund_id}|${reports[reports.length - 1].month}`;
    await loadReport();
    // Layout depends on the real glyph widths; lay out again once the web fonts have arrived.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => layout());
  }

  async function signOut() {
    $("#signout").disabled = true;
    try {
      await fetch("/api/logout", { method: "POST" });
    } finally {
      window.location.replace("/login?signed_out=1");
    }
  }

  function current() {
    const [fundId, month] = $("#report-select").value.split("|");
    return { fundId, month };
  }

  function setLanguage(lang) {
    state.lang = lang;
    document.querySelectorAll(".seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.lang === lang)));
    document.documentElement.lang = lang === "ja" ? "ja" : "en";
    layout();
  }

  async function loadReport() {
    const { fundId, month } = current();
    state.sheet = await api(`/api/facts?fund_id=${encodeURIComponent(fundId)}&month=${encodeURIComponent(month)}`);
    state.result = null; state.selected = null; state.approval = null; state.error = null; state.errorKind = null;
    const latest = await api(`/api/latest?fund_id=${encodeURIComponent(fundId)}&month=${encodeURIComponent(month)}`);
    state.result = latest.result;
    state.approval = approvalOf(state.result);
    refresh();
  }

  function refresh() {
    renderOps();
    layout();
  }

  // ---------------------------------------------------------------- ops row
  /** The label of the drafting button for this mode (not its current text, which reads "Drafting…" while busy). */
  function generateLabel() {
    return state.health && state.health.mode === "gemini" ? "Draft it live with Gemini" : "Run the offline demo";
  }

  function ruleSentence() {
    const streak = state.sheet && state.sheet.facts["cli.G7.streak"];
    const m = streak && /(risen|fallen) in each of the last (\d+) months/.exec(streak.text_en || "");
    if (!m) return ["Rules, not prompts, decide what ships: a draft with an untraced number is sent back before you see it."];
    const verb = m[1] === "risen" ? "rose" : "fell";
    return ["Rules, not prompts, decide what ships. A typical draft says ",
      el("span", { class: "struck", text: `\u201c${verb} for ${m[2]}\u00A0months in a row\u201d` }),
      ". The data does not reach the start of that run, so FactLock writes ",
      el("span", { class: "kept", text: `\u201chas ${m[1]} in each of the last ${m[2]}\u00A0months\u201d` }),
      " and explains the series in the margin."];
  }

  // The API is one request with no progress events, so the highlighted step is estimated from the
  // elapsed time (a live run takes about 70 s: writing and reviewing are the long parts).
  const STEPS = [["Computing facts", 0], ["Writing EN + JA", 2], ["Checking rules", 40], ["Reviewing", 42], ["Deciding", 66]];
  let stepTimer = null, startedAt = 0;
  const estimatedStep = (sec) => STEPS.reduce((k, [, at], i) => (sec >= at ? i : k), 0);

  function renderOps() {
    const text = $("#ops-text");
    const result = state.result;
    if (state.working) {
      const sec = Math.floor((Date.now() - startedAt) / 1000);
      const now = estimatedStep(sec);
      const live = state.health && state.health.mode === "gemini";
      text.replaceChildren(el("div", { class: "prog", role: "status" },
        el("div", { class: "prog-head" },
          el("span", { class: "prog-title", text: live ? "Drafting with Gemini" : "Running the offline demo" }),
          el("span", { class: "prog-time", text: `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}` }),
          el("span", { class: "prog-note", text: "About a minute. The paper below updates when the draft has passed its checks." })),
        el("div", { class: "prog-bar" }, el("span")),
        el("ol", { class: "prog-steps" }, ...STEPS.map(([name], k) =>
          el("li", { class: k < now ? "done" : k === now ? "now" : "" }, el("span", { class: "prog-dot" }), name)))));
    } else if (state.error) {
      text.replaceChildren(el("span", { class: "mark fail" }),
        el("strong", { text: state.errorKind === "load" ? "The page did not load. " : "Drafting stopped. " }), state.error);
    } else if (result && result.status !== "passed") {
      const n = (result.report?.errors || []).length;
      text.replaceChildren(el("span", { class: "mark fail" }), el("strong", { text: "Needs human review. " }),
        `${n} issue${n === 1 ? "" : "s"} remained after ${result.iterations} draft${result.iterations === 1 ? "" : "s"}. The sentences are marked in the text and listed under \u201cHow this draft was checked\u201d.`);
    } else if (!result) {
      // Before the first draft, the ops row says what to do first (DESIGN.md §7, the empty state).
      text.replaceChildren(el("strong", { text: `To begin, press \u201c${generateLabel()}\u201d at the top right.` }),
        " The writer agent drafts in English and Japanese; every number is inserted from the fact sheet and checked before you see it. About a minute.");
    } else if (result.served_from === "ledger" && result.served_reason) {
      // Gemini could not answer; the ledger's latest verified draft is shown instead, and says so.
      text.replaceChildren(el("span", { class: "mark warn" }), el("strong", { text: "Gemini is busy. " }),
        `Showing the latest verified draft from the ledger (run ${F.timeText(servedAt(result), "en")}). `, ...ruleSentence());
    } else {
      text.replaceChildren(...ruleSentence());
    }
    const passed = Boolean(result && result.status === "passed");
    $("#approve").disabled = !passed || state.working || Boolean(state.approval);
    $("#approve").textContent = state.approval ? "Approved" : "Approve draft";
    $("#approve").title = result && !passed ? "Only a draft that passed the checks can be approved." : "";
    $("#copy-md").disabled = !result || state.working;
    $("#generate").disabled = state.working;
    $("#generate").textContent = state.working ? "Drafting\u2026" : generateLabel();
    document.querySelector(".desk").classList.toggle("working", state.working);
  }

  // ------------------------------------------------------------ the document
  /** Walk the commentary once, in reading order, and number the notes. */
  function numberNotes(result, primary) {
    const facts = state.sheet.facts;
    const groups = new Map(); // group -> {n, ids[], ref (sentence where the note first appears)}
    const byFact = new Map();
    let next = 1, count = 0;
    if (!result) return { groups, byFact, count };
    for (const section of result.commentary) {
      for (const sentence of section.sentences) {
        for (const seg of sentence[primary]) {
          if (seg.type !== "fact" || !F.isLocked(seg.fact_id, facts[seg.fact_id])) continue;
          count += 1;
          const g = F.noteGroup(seg.fact_id);
          if (!groups.has(g)) groups.set(g, { n: next++, ids: [], ref: sentence.ref });
          const entry = groups.get(g);
          if (!entry.ids.includes(seg.fact_id)) entry.ids.push(seg.fact_id);
          byFact.set(seg.fact_id, entry.n);
        }
      }
    }
    // Each note knows the source of the note before it ("same source as note n").
    let prev = null;
    for (const entry of groups.values()) {
      entry.prevSource = prev;
      const src = facts[entry.ids[0]].source;
      prev = src === "derived" ? prev : { source: src, n: entry.n };
    }
    return { groups, byFact, count };
  }

  function lockNode(seg, fact, noteN, lang) {
    const selected = state.selected === seg.fact_id;
    const label = lang === "ja" ? fact.label_ja : fact.label_en;
    const common = {
      role: "button", tabindex: "0", "data-fact": seg.fact_id, "aria-pressed": String(selected),
      "aria-label": `${F.displayMinus(seg.text)}. ${label}${noteN ? `, note ${noteN}` : ""}. Select to open its note.`,
      onclick: () => select(seg.fact_id),
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(seg.fact_id); } },
    };
    const sup = noteN ? el("sup", { class: "nref", text: String(noteN) }) : null;
    const text = F.displayMinus(seg.text);
    if (fact.unit === "text") {
      // A text fact carrying a figure: dotted underline over the phrase; the last word stays with its note mark.
      const cut = lang === "ja" ? Math.max(0, text.length - 2) : text.lastIndexOf(" ") + 1;
      return el("span", { ...common, class: `txtfact${selected ? " selected" : ""}` },
        F.nb(text.slice(0, cut)), el("span", { class: "nw" }, text.slice(cut), sup));
    }
    const parts = F.splitNumber(text) || ["", text, ""];
    return el("span", {}, parts[0],
      el("span", { ...common, class: `lock${selected ? " selected" : ""}` }, el("span", { class: "num", text: F.nb(parts[1]) }), sup),
      parts[2]);
  }

  function paragraph(sentences, lang, notes, primary) {
    const facts = state.sheet.facts;
    const flagged = new Set((state.result?.report?.errors || []).map((i) => i.ref).filter(Boolean));
    const nodes = [];
    sentences.forEach((sentence, i) => {
      const segs = sentence[lang].map((seg) => {
        if (seg.type === "fact") {
          const fact = facts[seg.fact_id];
          if (F.isLocked(seg.fact_id, fact)) return lockNode(seg, fact, lang === primary ? notes.byFact.get(seg.fact_id) : null, lang);
          return F.nb(F.displayMinus(seg.text));
        }
        if (seg.type === "error") return el("span", { class: "bad", text: seg.text });
        return F.nb(seg.text);
      });
      if (i && lang === "en") nodes.push(" ");
      nodes.push(el("span", { class: `sentence${flagged.has(sentence.ref) ? " flagged" : ""}`, "data-ref": sentence.ref }, ...segs));
    });
    return el("p", { class: lang === "ja" ? "p-ja" : "p-en", lang }, ...nodes);
  }

  function heading(section, lang) {
    if (lang === "both") return el("h2", { class: "h2" }, section.heading_en, el("span", { class: "h2-sub", lang: "ja", text: section.heading_ja }));
    return el("h2", { class: "h2", lang }, lang === "ja" ? section.heading_ja : section.heading_en);
  }

  function noteNode(group, entry, lang, notes) {
    const t = F.noteText(group, entry.ids, state.sheet, lang, (id) => notes.byFact.get(id) || null, entry.prevSource);
    const open = entry.ids.includes(state.selected);
    const refLinks = (text) => {
      // "note 1" / "注記1" becomes a button that opens that note on screen; it prints as plain text.
      const out = [];
      const re = /(notes? |注記)(\d+)/g;
      let pos = 0, m;
      while ((m = re.exec(text))) {
        out.push(F.nb(text.slice(pos, m.index)), m[1]);
        const target = [...notes.groups.values()].find((g) => g.n === Number(m[2]));
        out.push(target ? el("button", { type: "button", class: "note-link", text: m[2], onclick: () => select(target.ids[0]) }) : m[2]);
        pos = m.index + m[0].length;
      }
      out.push(F.nb(text.slice(pos)));
      return out;
    };
    const lead = lang === "ja" ? `${t.value}：` : `${t.value}${entry.ids.length > 1 ? ":" : ","} `;
    // Lower-case the first letter after "2.71%, " only for an ordinary word ("Monthly" -> "monthly"),
    // never for a ticker or acronym ("SPY", "OECD").
    const body = lang === "ja" || entry.ids.length > 1 || !/^[A-Z][a-z]/.test(t.body) ? t.body : t.body.charAt(0).toLowerCase() + t.body.slice(1);
    return el("div", { class: `note${open ? " open" : ""}`, "data-note": String(entry.n), "data-facts": entry.ids.join(" ") },
      el("div", { class: "note-plain", style: "display: flex; gap: 6px;" },
        el("span", { class: "note-n", text: String(entry.n) }), el("span", {}, F.nb(lead), ...refLinks(body))),
      el("div", { class: "note-open" },
        el("div", { class: "note-headline" }, el("span", { style: "font-weight: 700;", text: String(entry.n) }),
          el("span", { class: "note-value", text: F.nb(t.value) })),
        el("p", {}, ...refLinks(t.body)),
        el("p", { class: "note-id", text: `${T(lang, "Fact", "データID")} ${entry.ids.join(", ")}` })));
  }

  function notesHead(list, lang) {
    const ns = list.map((e) => e.n);
    const first = ns[0], last = ns[ns.length - 1];
    const span = ns.length === 1 ? T(lang, `Note ${first}`, `注記${first}`)
      : ns.length === 2 ? T(lang, `Notes ${first} and ${last}`, `注記${first}・${last}`)
        : T(lang, `Notes ${first} to ${last}`, `注記${first}〜${last}`);
    return el("p", { class: "notes-head" }, F.nb(span),
      el("span", { class: "screen-only", text: T(lang, ". Select a number in the text to open its note.", "。数値をクリックすると詳細を表示します") }));
  }

  /** One text row: heading (optional), paragraphs for the chosen sentences, their notes, and an exhibit. */
  function sectionRow(section, sentences, opts) {
    const { lang, primary, notes, withHeading, exhibit, first } = opts;
    const langs = lang === "both" ? ["en", "ja"] : [lang];
    const refs = new Set(sentences.map((s) => s.ref));
    const own = [...notes.groups.entries()].filter(([, e]) => refs.has(e.ref));
    const aside = el("aside", { class: "notes" });
    if (own.length) {
      aside.append(notesHead(own.map(([, e]) => e), primary), ...own.map(([g, e]) => noteNode(g, e, primary, notes)));
    } else if (section.key === "outlook" && withHeading) {
      aside.append(el("p", { class: "notes-head", text: T(primary, "Disclaimer", "ディスクレーマー") }),
        el("p", { class: "note", text: T(primary,
          "The outlook reflects the view at the time of writing and may change without notice; it does not guarantee or imply future results. Companies named are for reference only, not recommendations to buy or sell.",
          "今後の見通しは作成時点のものであり、市場の環境やその他の状況によって予告なく変更することがあります。また、いずれも将来の傾向、数値、運用結果等を保証もしくは示唆するものではありません。本資料に記載されている個別の銘柄・企業名については、あくまでも参考として申し述べたものであり、その銘柄又は企業の株式等の売買を推奨するものではありません。") }));
    }
    return el("div", { class: `row${first ? " first" : ""}`, "data-block": `section:${section.key}` },
      el("section", { class: "col-text" },
        withHeading ? heading(section, lang) : null,
        ...(sentences.length ? langs.map((l) => paragraph(sentences, l, notes, primary)) : []),
        exhibit || null),
      aside);
  }

  // ---------------------------------------------------------------- exhibits
  function caption(lang, en, ja) {
    if (lang === "ja") return el("figcaption", { class: "fig-cap" }, el("span", { class: "fig-title", text: ja[0] }), el("span", { class: "fig-sub", text: ja[1] }));
    return el("figcaption", { class: "fig-cap" }, el("span", { class: "fig-title", text: F.nb(en) }), el("span", { class: "fig-sub", lang: "ja", text: ja[1] }));
  }

  function cycleExhibit(lang) {
    const L = lang === "ja" ? "ja" : "en";
    const ex = state.sheet.exhibits.cycle;
    const nameOf = (code) => ex.rows.find((r) => r.code === code)[L === "ja" ? "name_ja" : "name_en"];
    const cell = (phase, right) => el("td", { class: `${ex.cells[phase].length ? "current" : ""}${right ? " edge-r" : ""}` },
      el("span", { class: "ph", text: F.nb(phaseLabel(L, phase)) }),
      el("p", { class: "cell-body", text: ex.cells[phase].length ? ex.cells[phase].map(nameOf).join(T(L, ", ", "、")) : T(L, "none this month", "該当なし") }));
    const matrix = el("table", { class: "tbl matrix" },
      el("tr", { class: "mhead" },
        el("td", { class: "axis", style: "width: 20%;" }),
        el("td", { style: "width: 40%;", text: T(L, "Indicator rising", "景気指標：上昇") }),
        el("td", { class: "edge-r", style: "width: 40%;", text: T(L, "Indicator falling", "景気指標：低下") })),
      el("tr", {}, el("td", { class: "axis", text: T(L, "Rate rising", "金利：上昇") }), cell(1), cell(2, true)),
      el("tr", {}, el("td", { class: "axis", text: T(L, "Rate falling", "金利：低下") }), cell(4), cell(3, true)));
    const reason = (r) => (r.reason === "no_rate" ? T(L, "not placed", "対象外") : T(L, "unchanged", "変化なし"));
    const ledger = el("table", { class: "tbl", style: "margin-top: 4px;" },
      el("tr", { class: "head" },
        el("td", { style: "width: 30%;", text: T(L, "Area", "地域") }),
        el("td", { class: "r", style: "width: 20%;", text: T(L, "Indicator, pt", "先行指数、pt") }),
        el("td", { class: "r", style: "width: 20%;", text: T(L, "Rate, pp", "金利、pp") }),
        el("td", { class: "r", style: "width: 30%;", text: T(L, "Phase", "局面") })),
      ...ex.rows.map((r) => el("tr", {},
        el("td", { text: L === "ja" ? r.name_ja : r.name_en }),
        el("td", { class: "r v", text: F.num(r.cli_chg, r.cli_decimals, true) }),
        r.rate_chg === null ? el("td", { class: "r soft", text: T(L, "no series", "系列なし") })
          : el("td", { class: "r v", text: F.num(r.rate_chg, r.rate_decimals, true) }),
        el("td", { class: `r${r.phase ? "" : " soft"}`, text: r.phase ? phaseLabel(L, r.phase) : reason(r) }))));
    return el("figure", { class: "fig", "data-block": "exhibit:cycle" },
      caption(lang, `Where the cycle stands, ${F.monthText(state.sheet.month, "en")}`,
        [`景気サイクル（${F.monthText(state.sheet.month, "ja")}）`, "景気先行指数と長期金利の前月差による4局面"]),
      matrix, ledger,
      el("p", { class: "src", text: F.nb(T(L,
        "Indicator: OECD composite leading indicator, change on the month. Rate: long-term interest rate, monthly average, change on the month. An area is placed only when both series exist and both moved. In the usual reading of the cycle the phases run liquidity-driven, earnings-driven, reverse liquidity, reverse earnings. Source: OECD, CC BY 4.0; each phase computed by FactLock.",
        "先行指数：OECD景気先行指数の前月差。金利：長期金利（月平均）の前月差。両方の系列があり、両方が動いた地域だけを配置する。局面は通常 金融相場→業績相場→逆金融相場→逆業績相場の順に巡る。出所：OECD（CC BY 4.0）。各局面はFactLockが算出。")) }));
  }

  function contributionExhibit(lang) {
    const L = lang === "ja" ? "ja" : "en";
    const ex = state.sheet.exhibits.contribution;
    const { min, max, ticks } = ex.axis;
    const pos = (v) => ((v - min) / (max - min)) * 100;
    const gridAt = ticks.filter((t) => t !== 0 || min < 0);
    const bar = (a, b, total) => el("span", { class: `bar${total ? " total" : ""}`,
      style: `left: ${pos(Math.min(a, b)).toFixed(2)}%; width: ${Math.max(0.4, Math.abs(pos(b) - pos(a))).toFixed(2)}%;` });
    const row = (label, soft, w, r, a, b, total) => el("tr", { class: total ? "total" : "" },
      el("td", {}, el("span", { style: "font-weight: 600;", text: label }), soft ? [" ", el("span", { class: "soft", text: soft })] : null),
      el("td", { class: "r t", text: `${F.num(w, 1)}%` }),
      el("td", { class: "r t", text: `${F.num(r, 2)}%` }),
      el("td", { class: "barcell" }, el("div", { class: "bars" },
        ...gridAt.map((g) => el("span", { class: "grid", style: `left: ${pos(g).toFixed(2)}%;` })), bar(a, b, total))),
      el("td", { class: "r v", text: F.num(b - a, 2) }));
    const tickRow = el("div", { class: "ticks" }, ...ticks.map((t) =>
      el("span", { class: pos(t) === 0 ? "zero" : "", style: `left: ${pos(t).toFixed(2)}%;`, text: t === 0 ? "0" : F.num(t, 1) })));
    const table = el("table", { class: "tbl" },
      el("tr", { class: "head" },
        el("td", { style: "width: 150px;", text: T(L, "Holding", "資産") }),
        el("td", { class: "r", style: "width: 50px;", text: T(L, "Weight", "配分") }),
        el("td", { class: "r", style: "width: 52px;", text: T(L, "Return", "騰落率") }),
        el("td", { class: "barcell", style: "width: 136px;" }, tickRow),
        el("td", { class: "r", style: "width: 52px;", text: T(L, "pp", "%") })),
      ...ex.rows.map((r) => row(r.ticker, L === "ja" ? r.name_ja : r.name_en, r.weight, r.ret, r.start, r.end, false)),
      row(T(L, "Fund", "ファンド"), "", ex.total.weight, ex.total.ret, 0, ex.total.ret, true));
    const m = Number(state.sheet.month.split("-")[1]);
    return el("figure", { class: "fig", "data-block": "exhibit:contribution" },
      caption(lang, `Contribution to the ${F.monthText(state.sheet.month, "en").split(" ")[0]} return, by holding`,
        [`要因分析（${m}月）`, `${m}月　資産クラス別寄与度（費用控除前）`]),
      table,
      el("p", { class: "src", text: F.nb(T(L,
        `Contribution is target weight times the 1-month total return. Each blue bar starts where the previous one ends, so the last reaches the navy fund total. Hypothetical, gross of fees. Source: SPDR ETF product data, State Street Global Advisors, as of ${F.dateText(ex.as_of, "en")}.`,
        `要因分析：目標配分×1か月トータルリターン。青の棒は直前の棒の終点から始まり、最後の資産クラスがファンド合計（紺の棒）に一致する。仮想ポートフォリオの数値であり、費用控除前。出所：State Street Global Advisors「SPDR ETF product data」（${F.dateText(ex.as_of, "ja")}時点）。`)) }));
  }

  // ------------------------------------------------------ full-width blocks
  const STATUS_WORD = { pass: ["passed", "合格"], warn: ["note", "注意"], fail: ["failed", "不合格"] };

  /** How to use the demo, on the paper before the first draft. On screen only: never printed. */
  function guideBlock(lang) {
    const L = lang === "ja" ? "ja" : "en";
    const label = generateLabel();
    const steps = L === "ja" ? [
      `右上の「${label}」を押します。下書きには約1分かかります。`,
      "下線の付いた数値を選ぶと、余白に出所・基準日・算出式が表示されます。",
      "下書きを承認し、Markdownで複写するか、A4のPDFとして印刷できます。",
    ] : [
      `Press \u201c${label}\u201d at the top right. The draft takes about a minute.`,
      "Select any underlined number to read its source, date and calculation in the margin.",
      "Approve the draft, copy it as Markdown, or print it as an A4 PDF.",
    ];
    return el("section", { class: "wide screen-only", "data-block": "guide" },
      el("h2", { class: "h3", text: T(L, "How to use this demo", "このデモの使い方") }),
      el("ol", { class: "guide-steps" }, ...steps.map((t) => el("li", { class: L === "ja" ? "p-ja" : "p-en", text: t }))));
  }

  function checksBlock(lang) {
    const L = lang === "ja" ? "ja" : "en";
    return el("section", { class: "wide", "data-block": "checks" },
      el("h2", { class: "h3", text: T(L, "Data checks before drafting", "発行前データチェック") }),
      el("table", { class: "ledger" }, ...state.sheet.exhibits.checks.rows.map((r) => el("tr", {},
        el("td", { class: "dot" }, el("span", { class: `mark ${r.status}` })),
        el("td", { text: F.nb(L === "ja" ? r.text_ja : r.text_en) }),
        el("td", { class: "res", text: STATUS_WORD[r.status][L === "ja" ? 1 : 0] })))));
  }

  function sourcesBlock(lang) {
    const L = lang === "ja" ? "ja" : "en";
    const src = state.sheet.exhibits.sources;
    const when = src.retrieved.map((d) => F.dateText(d, L));
    const whenText = when.length > 1 ? T(L, `${when[0]} to ${when[when.length - 1]}`, `${when[0]}〜${when[when.length - 1]}`) : when[0];
    return el("section", { class: "wide", "data-block": "sources", style: "padding-top: 20px;" },
      el("h2", { class: "h3", text: F.nb(T(L, `Sources, retrieved ${whenText}`, `出所（${whenText}取得）`)) }),
      el("table", { class: "ledger" }, ...src.rows.map((r) => el("tr", {},
        el("td", { class: "pub", style: "width: 170px;", text: r.publisher }),
        el("td", {}, el("a", { href: r.url, target: "_blank", rel: "noopener", text: F.nb(r.title) }),
          r.citation ? el("span", { class: "cite", text: F.nb(r.citation) }) : null),
        el("td", { class: "res", style: "width: 110px;", text: F.nb(r.terms_short === "public disclosure" ? T(L, "public disclosure", "公開情報") : r.terms_short) })))),
      el("p", { class: "src", text: T(L,
        "Changes on the month, cycle phases, contributions and look-through weights were computed by FactLock from these data.",
        "前月差・局面・寄与・ルックスルー比率は、これらのデータからFactLockが算出した。") }));
  }

  function trailBlock(lang, notes) {
    const L = lang === "ja" ? "ja" : "en";
    const r = state.result;
    const counts = state.sheet.exhibits.checks.counts;
    const warnRows = state.sheet.exhibits.checks.rows.filter((x) => x.status !== "pass");
    const step = (n, head, ...body) => el("tr", {},
      el("td", { class: "step-n", text: String(n) }), el("td", { class: "step-h", text: head }), el("td", {}, ...body));
    const total = state.sheet.checks.length;
    const checksLine = T(L,
      `${total} checks before drafting: ${counts.pass} passed${counts.warn ? `, ${counts.warn} note${counts.warn === 1 ? "" : "s"}` : ""}${counts.fail ? `, ${counts.fail} failed` : ""}. Listed on page `,
      `発行前に${total}件を検査：合格${counts.pass}件${counts.warn ? `、注意${counts.warn}件` : ""}${counts.fail ? `、不合格${counts.fail}件` : ""}。一覧は`);
    const engine = r.engine || {};
    const at = r.history.length ? r.history[r.history.length - 1].at : null;
    const when = at ? F.timeText(at, L) : "";
    // The endpoints that actually answered (after a 429 the fallback location), else the configured one.
    const where = (engine.locations && engine.locations.length ? engine.locations : [state.health.location]).join(", ");
    const writer = engine.mode === "gemini"
      ? T(L, `${engine.model} on Google Cloud (${where}) drafted the English and Japanese text together, inserting every number from the fact sheet rather than typing it. Run ${when}, ${(r.elapsed_ms / 1000).toFixed(1)} s in all.`,
        `${engine.model}（Google Cloud、${where}）が英文と和文を同時に執筆。数値はAIが入力せず、全て出所データから自動で差し込み。実行：${when}、所要${(r.elapsed_ms / 1000).toFixed(1)}秒。`)
      : T(L, `Offline demo: no AI model was called. A scripted writer (scenario \u201c${engine.scenario}\u201d) shows how the checks work. Run ${when}.`,
        `オフラインデモ：AIモデル未使用。台本の書き手（シナリオ「${engine.scenario}」）でチェック機能を実演。実行：${when}。`);
    // A draft served from the ledger because Gemini was busy says so on paper too, not only on the screen.
    const served = r.served_from === "ledger" && r.served_reason
      ? T(L, ` Served from the audit ledger on ${F.timeText(r.served_at || new Date().toISOString(), "en")} because Gemini was busy; this is the latest draft that passed its checks, made ${when}.`,
        `${F.timeText(r.served_at || new Date().toISOString(), "ja")}に、Geminiの混雑のため監査台帳から提供。${when}に作成された、検査に合格した最新の下書き。`)
      : "";
    const drafts = r.history.map((h) => {
      const head = h.passed
        ? T(L, `Draft ${h.iteration} passed.`, `第${h.iteration}版：合格。`)
        : T(L, `Draft ${h.iteration} was sent back with ${h.errors.length} issue${h.errors.length === 1 ? "" : "s"}.`, `第${h.iteration}版：${h.errors.length}件の指摘で差し戻し。`);
      const items = [...h.errors, ...h.warnings].map((i) => el("span", { class: "issue", text: F.nb(F.displayMinus(
        `${i.severity === "warning" ? T(L, "Caution", "注意") : T(L, "Issue", "指摘")}${i.ref ? ` [${i.ref}${i.lang && i.lang !== "both" ? `, ${i.lang.toUpperCase()}` : ""}]` : ""}: ${i.message}`)) }));
      return [el("span", { style: "display: block;" }, F.nb(head), L === "ja" ? "" : " ", T(L, `Reviewer agent: ${h.reviewer}.`, `AI審査：${REVIEWER_JA[h.reviewer] || h.reviewer}。`)), ...items];
    });
    const holdings = Boolean(state.sheet.facts["holdings.as_of"]);
    const closing = r.status === "passed"
      ? T(L, `All ${notes.count} numbers in the text matched their facts in both languages${holdings ? ", and every holding sentence states its date" : ""}.`,
        `本文の数値${notes.count}件は全て日英とも出所データと一致${holdings ? "。組入銘柄に関連する場合には日付を明記" : ""}。`)
      : T(L, "Issues remained after the last draft; a person must resolve them before release.", "最終版でも指摘が残ったため、公開前に担当者の目視確認が必要。");
    const approval = state.approval
      ? T(L, `Approved by ${state.approval.name} on ${F.timeText(state.approval.at.toISOString(), "en")}.`, `${state.approval.name}が${F.timeText(state.approval.at.toISOString(), "ja")}に承認。`)
      : [T(L, "Reviewer", "確認者"), el("span", { class: "blank" }), T(L, "Date", "日付"), el("span", { class: "blank last" })];
    return el("section", { class: "wide", "data-block": "trail", style: "padding-top: 12px;" },
      el("h2", { class: "h3", text: T(L, "How this draft was checked", "検証結果") }),
      el("table", { class: "ledger" },
        step(1, T(L, "Data checks", "データ検査"), F.nb(checksLine), el("span", { "data-page-of": "checks", text: "?" }), T(L, ".", "ページ。"),
          warnRows.length ? el("span", { class: "issue", text: F.nb(warnRows.map((w) => (L === "ja" ? w.text_ja : w.text_en)).join(T(L, "; ", "。"))) }) : null),
        step(2, T(L, "Writer agent", "執筆AI情報"), F.nb(writer + served)),
        step(3, T(L, "Rule review", "ルールベースチェック"), ...drafts, el("span", { style: "display: block;", text: F.nb(closing) })),
        step(4, T(L, "Approval", "最終承認"), approval)));
  }

  function disclaimerBlock(lang) {
    return el("p", { class: "disclaimer", "data-block": "disclaimer", text: F.nb(T(lang === "ja" ? "ja" : "en",
      "Demonstration by SemanticBase Co., Ltd., using only public data: OECD statistics (CC BY 4.0) and ETF issuers' public disclosures (SPDR data, State Street Global Advisors). The fund is hypothetical and returns are gross of fees. Nothing here predicts or guarantees future results; SemanticBase accepts no liability for any loss arising from its use, and investment decisions remain your own responsibility.",
      "本資料は株式会社セマンティックベースによるデモです。公開データ（OECD統計〈CC BY 4.0〉、ETF発行体の公開情報〈State Street Global AdvisorsのSPDRデータ〉）のみを使用しています。掲載したファンドは仮想のものであり、騰落率は費用控除前です。本資料のいかなる内容も、将来の結果を予測し、保証するものではありません。本資料及び掲載された情報を利用することで生じるいかなる損害（直接的、間接的を問わず）についても、当社が責任を負うものではありません。実際の資産運用や投資判断に当たっては、必ずご自身の責任において最終的に判断してください。")) });
  }

  function seal(count, date) {
    return el("div", { class: "seal", role: "img", "aria-label": `Verified, ${count} of ${count} numbers, ${date}` },
      el("span", { class: "seal-ja", text: "検証済" }), el("span", { class: "seal-en", text: "VERIFIED" }),
      el("span", { class: "seal-n first", text: `${count} of ${count}` }), el("span", { class: "seal-n", text: date }));
  }

  function titleBlock(lang, notes) {
    const s = state.sheet, r = state.result;
    const L = lang === "ja" ? "ja" : "en";
    const at = r && r.history.length ? r.history[r.history.length - 1].at : null;
    let status;
    if (!r) {
      status = T(L, "No draft for this month yet. The data below has been checked and is ready for the writer agent.",
        "今月分は作成前です。以下データをAIへ送付すれば執筆可能な状態になっています。");
    } else if (r.status !== "passed") {
      const n = (r.report?.errors || []).length;
      status = [el("span", { class: "mark fail" }), T(L,
        `Needs human review: ${n} issue${n === 1 ? "" : "s"} remained after ${r.iterations} draft${r.iterations === 1 ? "" : "s"}.`,
        `要確認：第${r.iterations}版でも${n}件の修正箇所が残っています。`)];
    } else if (state.approval) {
      status = T(L, `Approved by ${state.approval.name} on ${F.timeText(state.approval.at.toISOString(), "en")}. All ${notes.count} numbers in the text trace to a public source.`,
        `${state.approval.name}が${F.timeText(state.approval.at.toISOString(), "ja")}に承認。本文の数値${notes.count}件は全て出所に元データが掲載されています。`);
    } else {
      const which = r.iterations === 1 ? T(L, "the first draft", "初版") : T(L, `draft ${r.iterations}`, `第${r.iterations}版`);
      status = T(L, `Ready for review: all ${notes.count} numbers in the text trace to a public source, and ${which} passed the rule review on ${F.dateText(at, "en")}.`,
        `確認待ち：本文の数値${notes.count}件は全て出所に元データが掲載されており、${which}を${F.dateText(at, "ja")}にチェックしました。`);
    }
    const meta = L === "ja"
      ? [`${F.monthText(s.month, "ja")}のマンスリーコメント（月末日：${F.dateText(s.period_end, "ja")}）`, s.fund_note_ja]
      : [`Monthly commentary for ${F.monthText(s.month, "en")}, month-end ${F.dateText(s.period_end, "en")}. ${s.fund_note_en}`];
    return el("header", { class: "title", "data-block": "title" },
      el("div", { class: "title-main" },
        el("h1", { lang: L, text: L === "ja" ? s.fund_name_ja : s.fund_name_en }),
        el("p", { class: "title-sub", lang: L === "ja" ? "en" : "ja", text: L === "ja" ? s.fund_name_en : s.fund_name_ja }),
        ...meta.map((m, i) => el("p", { class: "title-meta", style: i ? "margin-top: 0;" : "", text: F.nb(m) })),
        el("p", { class: "title-status" }, typeof status === "string" ? F.nb(status) : status)),
      el("div", { class: "title-seal" }, r && r.status === "passed" && at ? seal(notes.count, at.slice(0, 10)) : null));
  }

  // ------------------------------------------------------------------ layout
  function newPage(pagesEl, index, lang) {
    const L = lang === "ja" ? "ja" : "en";
    const s = state.sheet;
    const body = el("div", { class: "page-body" });
    pagesEl.append(el("article", { class: "page", "aria-label": `Page ${index + 1}` },
      el("div", { class: "page-inner" },
        index > 0 ? el("div", { class: "runhead" },
          el("span", { class: "runhead-fund", text: L === "ja" ? s.fund_name_ja : s.fund_name_en }),
          el("span", { class: "runhead-month", text: F.nb(T(L, `Monthly commentary, ${F.monthText(s.month, "en")}`, `${F.monthText(s.month, "ja")}のマンスリーコメント`)) })) : null,
        body,
        el("footer", { class: "pagefoot" },
          el("span", { text: F.nb(T(L, `FactLock, monthly commentary for ${F.monthText(s.month, "en")}`, `FactLock　${F.monthText(s.month, "ja")}のマンスリーコメント`)) }),
          el("span", { class: "n" })))));
    return body;
  }

  const fits = (body) => body.scrollHeight <= body.clientHeight + 0.5;
  /** Share of the page body already used (scrollHeight cannot tell: it is never below clientHeight). */
  const usedShare = (body) => {
    const last = body.lastElementChild;
    if (!last) return 0;
    return (last.getBoundingClientRect().bottom - body.getBoundingClientRect().top) / body.getBoundingClientRect().height;
  };

  function layout() {
    if (!state.sheet) return;
    const pagesEl = $("#pages");
    const lang = state.lang;
    const primary = lang === "ja" ? "ja" : "en";
    const r = state.result;
    const notes = numberNotes(r, primary);
    const scrollY = window.scrollY;
    pagesEl.style.zoom = "";
    pagesEl.replaceChildren();

    let pageIndex = 0;
    let body = newPage(pagesEl, pageIndex, lang);
    const isEmpty = () => body.children.length === 0;
    const nextPage = () => { pageIndex += 1; body = newPage(pagesEl, pageIndex, lang); };
    const tryHere = (node) => { body.append(node); if (fits(body)) return true; node.remove(); return false; };
    // Place a block here, or at the top of the next page. A block taller than a page is placed anyway.
    const place = (node) => {
      if (tryHere(node)) return;
      if (!isEmpty()) nextPage();
      body.append(node);
    };

    place(titleBlock(lang, notes));

    if (!r) {
      place(guideBlock(primary));
      place(checksBlock(lang));
      place(sourcesBlock(lang));
      place(disclaimerBlock(lang));
    } else {
      let firstRow = true;
      for (const section of r.commentary) {
        const exhibitFor = () => (section.key === "market" ? cycleExhibit(lang) : section.key === "performance" ? contributionExhibit(lang) : null);
        const opts = (withHeading, sentences, exhibit) => ({ lang, primary, notes, withHeading, first: firstRow, exhibit });
        const hasExhibit = section.key === "market" || section.key === "performance";
        if (!tryHere(sectionRow(section, section.sentences, opts(true, null, exhibitFor())))) {
          let exhibitPlaced = false;
          let placed = false;
          // 1) The whole text here, the exhibit on the next page.
          if (hasExhibit && tryHere(sectionRow(section, section.sentences, opts(true, null, null)))) placed = true;
          // 2) Text and exhibit together on a fresh page, when this page is mostly used.
          if (!placed && !isEmpty() && usedShare(body) > 0.6) {
            nextPage();
            firstRow = false;
            placed = exhibitPlaced = tryHere(sectionRow(section, section.sentences, opts(true, null, exhibitFor())));
          }
          if (!placed) {
            // 3) Split: as many sentences as fit here; the rest on the following pages.
            let rest = section.sentences.slice();
            let withHeading = true;
            while (rest.length) {
              let k = rest.length;
              for (; k >= 1; k -= 1) {
                const withEx = k === rest.length && hasExhibit;
                if (tryHere(sectionRow(section, rest.slice(0, k), { ...opts(withHeading), first: false, exhibit: withEx ? exhibitFor() : null }))) {
                  exhibitPlaced = withEx;
                  break;
                }
                if (withEx && tryHere(sectionRow(section, rest.slice(0, k), { ...opts(withHeading), first: false, exhibit: null }))) break;
              }
              if (k === 0) {
                if (isEmpty()) { body.append(sectionRow(section, rest.slice(0, 1), { ...opts(withHeading), first: false })); k = 1; }
                else { nextPage(); continue; }
              }
              rest = rest.slice(k);
              withHeading = false;
              if (rest.length) nextPage();
            }
          }
          if (hasExhibit && !exhibitPlaced) place(sectionRow(section, [], { ...opts(false), first: false, exhibit: exhibitFor() }));
        }
        firstRow = false;
        if (section.key === "performance") { place(checksBlock(lang)); place(sourcesBlock(lang)); }
      }
      if (!pagesEl.querySelector('[data-block="checks"]')) { place(checksBlock(lang)); place(sourcesBlock(lang)); }
      place(trailBlock(lang, notes));
      // Never leave the short disclaimer alone on the last page: take the review record with it.
      const disclaimer = disclaimerBlock(lang);
      if (!tryHere(disclaimer)) {
        const trail = body.querySelector('[data-block="trail"]');
        if (trail && body.children.length > 1) { trail.remove(); nextPage(); body.append(trail); }
        else nextPage();
        place(disclaimer);
      }
    }

    // Page numbers, and the page the data checks landed on.
    const pages = [...pagesEl.querySelectorAll(".page")];
    pages.forEach((p, i) => { p.querySelector(".pagefoot .n").textContent = F.nb(T(primary, `${i + 1} of ${pages.length}`, `${i + 1} / ${pages.length}`)); });
    const checksPage = pages.findIndex((p) => p.querySelector('[data-block="checks"]')) + 1;
    pagesEl.querySelectorAll('[data-page-of="checks"]').forEach((n) => { n.textContent = String(checksPage); });
    pagesEl.dataset.pages = String(pages.length);
    pagesEl.dataset.overflow = String(pages.filter((p) => !fits(p.querySelector(".page-body"))).length);
    fitPages();
    window.scrollTo(0, scrollY);
    applySelection(false);
  }

  /** Mark the selected number (all its occurrences) and open its note, without touching the layout. */
  function applySelection(scroll) {
    const id = state.selected;
    document.querySelectorAll("#pages [data-fact]").forEach((chip) => {
      const on = chip.dataset.fact === id;
      chip.classList.toggle("selected", on);
      chip.setAttribute("aria-pressed", String(on));
    });
    let opened = null;
    document.querySelectorAll("#pages .note[data-facts]").forEach((note) => {
      const on = Boolean(id) && note.dataset.facts.split(" ").includes(id);
      note.classList.toggle("open", on);
      if (on) opened = note;
    });
    // Scroll the window vertically only; scrollIntoView could also scroll the page box sideways.
    if (scroll && opened) {
      const r = opened.getBoundingClientRect();
      const vh = window.innerHeight;
      if (r.top < 0) window.scrollBy(0, r.top - 16);
      else if (r.bottom > vh) window.scrollBy(0, Math.min(r.top - 16, r.bottom - vh + 16));
    }
    document.querySelectorAll("#pages .page-body").forEach((b) => { if (b.scrollLeft) b.scrollLeft = 0; });
  }

  /** On a phone the A4 page is scaled to the screen width; print resets this (see styles.css). */
  function fitPages() {
    const pagesEl = $("#pages");
    const page = pagesEl.querySelector(".page");
    if (!page) return;
    pagesEl.style.zoom = "";
    const available = document.documentElement.clientWidth - 16;
    const width = page.offsetWidth;
    if (width > available) pagesEl.style.zoom = String(available / width);
  }

  function select(factId) {
    state.selected = state.selected === factId ? null : factId;
    applySelection(true);
  }

  function prepareForPrint() {
    // Print shows every note in the same closed form.
    state.selected = null;
    applySelection(false);
    $("#pages").style.zoom = "";
  }

  // ---------------------------------------------------------------- generate
  async function generate() {
    const { fundId, month } = current();
    state.working = true; state.error = null; state.errorKind = "draft"; startedAt = Date.now();
    renderOps();
    stepTimer = setInterval(renderOps, 1000);
    try {
      state.result = await api("/api/generate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fund_id: fundId, month, scenario: $("#scenario-select").value }),
      });
      state.approval = approvalOf(state.result); state.selected = null;
    } catch (error) {
      state.error = error.message;
    } finally {
      clearInterval(stepTimer);
      state.working = false;
      refresh();
    }
  }

  // ------------------------------------------------------- approve & export
  function openApproval() {
    $("#approve").hidden = true;
    $("#approve-form").hidden = false;
    $("#approve-name").focus();
  }
  function closeApproval() {
    $("#approve-form").hidden = true;
    $("#approve").hidden = false;
  }
  async function recordApproval(event) {
    event.preventDefault();
    const name = $("#approve-name").value.trim();
    if (!name) return;
    state.approval = { name, at: new Date() };
    if (state.result && state.result.run_id) {
      try {
        const { approval } = await api("/api/approve", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ run_id: state.result.run_id, name }) });
        state.approval = { name: approval.name, at: new Date(approval.at) };   // the ledger's record, not the browser's clock
        state.result.approval = approval;
      } catch (error) {
        console.warn("The approval could not be recorded on the ledger:", error.message);   // kept on this page only
      }
    }
    closeApproval();
    refresh();
  }

  /** The approval recorded on the ledger for a draft, as the page keeps it, else null. */
  function approvalOf(result) {
    const a = result && result.approval;
    return a && a.name && a.at ? { name: a.name, at: new Date(a.at) } : null;
  }

  /** When a draft served from the ledger was made: the time its last draft was checked. */
  function servedAt(result) {
    const h = result && result.history;
    return h && h.length ? h[h.length - 1].at : new Date().toISOString();
  }

  function toMarkdown() {
    const r = state.result, s = state.sheet;
    const text = (segs) => F.displayMinus(segs.map((x) => x.text).join(""));
    const lines = [`# ${s.fund_name_en} / ${s.fund_name_ja}`, `Monthly commentary, ${F.monthText(s.month, "en")}`, ""];
    r.commentary.forEach((section) => {
      lines.push(`## ${section.heading_en} / ${section.heading_ja}`, "");
      if (state.lang !== "ja") lines.push(section.sentences.map((x) => text(x.en)).join(" "), "");
      if (state.lang !== "en") lines.push(section.sentences.map((x) => text(x.ja)).join(""), "");
    });
    lines.push("---", s.fund_note_en, s.fund_note_ja, "");
    lines.push(`Verification: ${r.status} after ${r.iterations} draft(s); engine ${r.engine?.model || "n/a"}.`);
    lines.push(state.approval ? `Approved by ${state.approval.name} on ${state.approval.at.toISOString()}.` : "Not yet approved.");
    lines.push("", "Sources:", ...s.sources.map((x) => `- ${x.publisher}, ${x.title}. ${x.terms}${x.citation ? ` ${x.citation}` : ""}`));
    lines.push("", "Changes on the month, cycle phases, contributions and look-through weights were computed by FactLock from these data.");
    return lines.join("\n");
  }

  async function copyMarkdown() {
    const md = toMarkdown();
    try {
      await navigator.clipboard.writeText(md);
      flash("Markdown copied.");
    } catch (_) {
      const blob = new Blob([md], { type: "text/markdown" });
      const link = el("a", { href: URL.createObjectURL(blob), download: `factlock_${state.sheet.fund_id.toLowerCase()}_${state.sheet.month}.md` });
      document.body.append(link); link.click(); link.remove();
      flash("The browser blocked copying, so the Markdown was downloaded instead.");
    }
  }

  function flash(message) {
    $("#ops-text").replaceChildren(el("span", { class: "mark pass" }), message);
    setTimeout(() => { if (!state.working) renderOps(); }, 2500);
  }

  init().catch((error) => {
    console.error("FactLock: the page did not load.", error);
    state.errorKind = "load";
    state.error = "Please reload the page (\u2318R on a Mac, F5 on Windows). If it happens again, tell the team what the browser console shows.";
    renderOps();
  });
})();

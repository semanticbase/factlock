/* FactLock display helpers: pure functions, no DOM. Loaded by the page and by the tests (Node). */
(function (root) {
  "use strict";

  const MINUS = "\u2212";
  const NBSP = "\u00A0";
  const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December"];

  // ---------------------------------------------------------------- numbers
  /** Show a minus as the mathematical sign (U+2212). Only a hyphen that precedes a digit is a minus;
   *  "ex-U.S." and "2026-08" (a digit before the hyphen) are left alone. */
  function displayMinus(text) {
    return String(text).replace(/(^|[^\w.\-])-(?=\d)/g, `$1${MINUS}`);
  }

  function isZero(value, decimals) { return Number(Math.abs(value).toFixed(decimals)) === 0; }

  /** Port of app/render.py format_fact, then displayMinus. Keep the two in step (a test compares them). */
  function formatFact(fact, fmt, lang) {
    if (fact.unit === "text") return lang === "en" ? fact.text_en : fact.text_ja;
    const v = fact.value, d = fact.decimals ?? 2;
    const n = Math.abs(v).toFixed(d);
    const up = v > 0 && !isZero(v, d), down = v < 0 && !isZero(v, d);
    const sign = up ? "+" : down ? "-" : "";
    const dw = fact.down_word_ja || "下落";
    let out;
    if (["pct", "pct_pa", "weight"].includes(fact.unit)) {
      if (fmt === "value") out = `${down ? "-" : ""}${n}%`;
      else if (fmt === "signed") out = `${sign}${n}%`;
      else if (fmt === "abs") out = `${n}%`;
      else if (lang === "en") out = up ? `rose ${n}%` : down ? `fell ${n}%` : "was unchanged";
      else out = up ? `${n}%の上昇` : down ? `${n}%の${dw}` : "横ばい";
    } else if (fact.unit === "contrib") {
      const u = lang === "en" ? " pp" : "%";
      if (fmt === "value" || fmt === "signed") out = `${sign}${n}${u}`;
      else if (fmt === "abs") out = `${n}${u}`;
      else if (lang === "en") out = up ? `added ${n} percentage points` : down ? `subtracted ${n} percentage points` : "had no effect";
      else out = up ? `${n}%のプラス寄与` : down ? `${n}%のマイナス寄与` : "寄与なし";
    } else if (fact.unit === "pp") {
      const u = lang === "en" ? " pp" : "%ポイント";
      if (fmt === "value" || fmt === "signed") out = `${sign}${n}${u}`;
      else if (fmt === "abs") out = `${n}${u}`;
      else if (lang === "en") out = up ? `rose ${n} percentage points` : down ? `fell ${n} percentage points` : "was unchanged";
      else out = up ? `${n}%ポイントの上昇` : down ? `${n}%ポイントの${dw}` : "横ばい";
    } else if (fact.unit === "pt") {
      const u = lang === "en" ? " pt" : "ポイント";
      if (fmt === "value" || fmt === "signed") out = `${sign}${n}${u}`;
      else if (fmt === "abs") out = `${n}${u}`;
      else if (lang === "en") out = up ? `rose ${n} points` : down ? `fell ${n} points` : "was unchanged";
      else out = up ? `${n}ポイントの上昇` : down ? `${n}ポイントの${dw}` : "横ばい";
    } else if (fact.unit === "index") {
      out = v.toFixed(d);
    } else {
      throw new Error(`unsupported unit ${fact.unit}`);
    }
    return displayMinus(out);
  }

  /** A plain number for exhibits: fixed decimals, "+" optional, U+2212 for minus. */
  function num(value, decimals, signed) {
    const n = Math.abs(value).toFixed(decimals);
    if (isZero(value, decimals)) return n;
    if (value < 0) return MINUS + n;
    return signed ? `+${n}` : n;
  }

  /** Split a rendered fact segment into [before, number, after] so the underline marks the number only. */
  function splitNumber(text) {
    const m = /[+\u2212-]?\d[\d,]*(?:\.\d+)?(?:\s?(?:%ポイント|%|pp|pt|percentage points|points|ポイント))?/.exec(text);
    if (!m) return null;
    return [text.slice(0, m.index), m[0], text.slice(m.index + m[0].length)];
  }

  // ------------------------------------------------------------------ dates
  function dateText(iso, lang) {
    const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
    return lang === "ja" ? `${y}年${m}月${d}日` : `${MONTHS[m - 1]} ${d}, ${y}`;
  }
  function monthText(period, lang) {
    const [y, m] = period.split("-").map(Number);
    return lang === "ja" ? `${y}年${m}月` : `${MONTHS[m - 1]} ${y}`;
  }
  function timeText(iso, lang) {
    const t = new Date(iso);
    const hh = String(t.getUTCHours()).padStart(2, "0"), mm = String(t.getUTCMinutes()).padStart(2, "0");
    return `${dateText(t.toISOString(), lang)}${lang === "ja" ? " " : ", "}${hh}:${mm} UTC`;
  }

  // ---------------------------------------------------------- non-breaking
  /** Keep a number with its unit, dates, and fixed phrases on one line (DESIGN.md §5.4). */
  function nb(text) {
    const mo = MONTHS.join("|");
    return String(text)
      .replace(/(\d) (pt|pp|bp|months?|points?|percentage points|ETFs|series|of \d)/g, `$1${NBSP}$2`)
      .replace(new RegExp(`(${mo}) (\\d{1,2}), (\\d{4})`, "g"), `$1${NBSP}$2,${NBSP}$3`)
      .replace(new RegExp(`(\\d{1,2}) (${mo}) (\\d{4})`, "g"), `$1${NBSP}$2${NBSP}$3`)
      .replace(new RegExp(`(${mo}) (\\d{4})`, "g"), `$1${NBSP}$2`)
      .replace(/\b(note|notes|page|Phase|Draft) (\d)/g, `$1${NBSP}$2`)
      .replace(/CC BY 4\.0/g, `CC${NBSP}BY${NBSP}4.0`)
      .replace(/trend = 100/g, `trend${NBSP}=${NBSP}100`)
      .replace(/(\d{2}):(\d{2}) UTC/g, `$1:$2${NBSP}UTC`);
  }

  // ------------------------------------------------------------ locked facts
  /** Numbers are always locked. A text fact is locked (dotted underline and a note) when it carries a
   *  figure derived from data, e.g. "for 16 consecutive months"; names, labels and the report
   *  month are not. */
  function isLocked(factId, fact) {
    if (!fact) return false;
    if (fact.unit !== "text") return true;
    if (factId.startsWith("report.") || /\.(label|name)$/.test(factId)) return false;
    return /\d/.test(`${fact.text_en || ""}${fact.text_ja || ""}`);
  }

  /** Facts that share one note (DESIGN.md §5: e.g. the four target weights share a note). */
  function noteGroup(factId) {
    if (/^etf\.[^.]+\.weight$/.test(factId)) return "group:weights";
    if (/^lookthrough\.\d+\.weight$/.test(factId)) return "group:lookthrough";
    return factId;
  }

  function joinList(items, lang) {
    if (lang === "ja") return items.join("、");
    if (items.length <= 1) return items.join("");
    if (items.length === 2) return `${items[0]} and ${items[1]}`;
    return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
  }

  const SOURCE_TEXT = {
    derived: { en: "Calculated by FactLock", ja: "FactLockが算出" },
    fund_definition: { en: "Fund definition (hypothetical demo fund)", ja: "ファンド定義（デモ用の仮想ファンド）" },
    report: { en: "Report settings", ja: "レポート設定" },
  };

  function sourceText(sheet, key, lang, short) {
    if (SOURCE_TEXT[key]) return SOURCE_TEXT[key][lang];
    const s = (sheet.sources || []).find((x) => x.key === key);
    if (!s) return key;
    const title = s.title.replace(" (public download)", "").replace(/;.*$/, "");
    const terms = s.terms.startsWith("CC BY") ? s.terms : "";
    if (short) return lang === "ja" ? `出所：${s.publisher}${terms ? `（${terms}）` : ""}` : `${s.publisher}${terms ? `, ${terms}` : ""}`;
    return lang === "ja" ? `出所：${s.publisher}「${title}」` : `Source: ${s.publisher}, ${title}`;
  }

  /** Japanese wording for the derivations written in app/facts.py (19 kinds as of v1.11). Unknown ones
   *  fall back to the English text, and a test fails if any derivation in the fact sheet is not covered. */
  function derivationJa(d) {
    const ym = (y, m) => `${Number(y)}年${Number(m)}月`;
    const fixed = {
      "target weight × 1-month total return": "目標配分×1か月トータルリターン",
      "Σ target weight × component 1-month total return": "Σ（目標配分×各資産の1か月トータルリターン）",
      "argmax of contributions": "寄与度が最大の資産クラス",
      "contribution of the largest contributor": "資産クラス別寄与度の最大値",
      "argmin of contributions": "寄与度が最小の資産クラス",
      "contribution of the smallest contributor": "資産クラス別寄与度の最小値",
      "components with a negative contribution": "寄与度がマイナスの資産",
      "Amplitude-adjusted CLI; long-term trend = 100": "振幅調整済みの景気先行指数（長期趨勢＝100）",
      "level compared with 100": "水準と100の比較",
      "level vs 100 and month-on-month direction": "水準（100との比較）と前月からの方向",
      "Σ fund weight × weight within ETF, largest holdings only": "Σ（ファンドの配分×ETF内の組入比率）、上位銘柄のみ",
      "Σ fund weight × weight within ETF": "Σ（ファンドの配分×ETF内の組入比率）",
      "Issuer-published NAV total return (distributions reinvested)": "発行体公表のトータルリターン（分配金再投資）",
    };
    if (fixed[d]) return fixed[d];
    let m = /^level (\d{4})-(\d{2}) − level (\d{4})-(\d{2})$/.exec(d);
    if (m) return "当月の水準−前月の水準";
    m = /^average (\d{4})-(\d{2}) − average (\d{4})-(\d{2})$/.exec(d);
    if (m) return "当月平均−前月平均";
    m = /^(\d+) (rises|falls) in a row, changes (\d{4})-(\d{2}) to (\d{4})-(\d{2}); data from (\d{4})-(\d{2})$/.exec(d);
    if (m) return `${m[1]}か月連続の${m[2] === "rises" ? "上昇" : "低下"}（${ym(m[3], m[4])}〜${ym(m[5], m[6])}、データは${ym(m[7], m[8])}から）`;
    return d;
  }

  function headline(fact, lang) {
    if (fact.unit === "text") return lang === "en" ? fact.text_en : fact.text_ja;
    const fmt = fact.unit === "contrib" || fact.unit === "pp" || fact.unit === "pt" ? "abs" : "value";
    return formatFact(fact, fmt, lang);
  }

  /**
   * Text of one margin note. prevSource is {source, n} of the note before it, so a repeated source can say
   * "same source as note n". ids are the facts sharing the note, in reading order; noteOf(id) returns the
   * note number of another fact if it has one (used for "derived from note 1").
   * Returns { value, body, refs } where refs lists the note numbers mentioned in body.
   */
  function noteText(group, ids, sheet, lang, noteOf, prevSource) {
    const facts = sheet.facts;
    const ja = lang === "ja";
    const end = (s) => (ja ? (s.endsWith("。") ? s : `${s}。`) : (/[.?!]$/.test(s) ? s : `${s}.`));
    const sentences = [];
    const refs = [];

    if (group === "group:weights" || group === "group:lookthrough") {
      const value = joinList(ids.map((id) => headline(facts[id], lang)), lang);
      const asOf = facts[ids[0]].as_of;
      if (group === "group:weights") {
        const check = (sheet.checks || []).find((c) => c.name === "Target weights add up to 100%");
        sentences.push(ja ? "ファンド定義の目標配分" : "Target weights from the fund definition");
        if (check && check.status === "pass") {
          sentences.push(ja ? "合計が100%であることを発行前に確認済み" : "They add up to 100%, checked before drafting");
        }
      } else {
        sentences.push(ja
          ? `ルックスルー比率＝ファンドの目標配分×各ETF内の組入比率（${dateText(asOf, "ja")}時点）`
          : `Look-through weights: fund target weight × the holding's weight in its ETF, as of ${dateText(asOf, "en")}`);
        if (asOf > sheet.period_end) {
          sentences.push(ja
            ? "組入データの基準日が月末より後のため、規則により文中に日付を明記している"
            : "Holdings are dated after the month end, so the rules require the date in the sentence");
        }
        sentences.push(sourceText(sheet, facts[ids[0]].source, lang, true));
      }
      return { value, body: sentences.map(end).join(ja ? "" : " "), refs };
    }

    const id = ids[0];
    const fact = facts[id];
    const value = headline(fact, lang);
    const label = ja ? fact.label_ja : fact.label_en;
    // The month-end date is on the title; a note states a date only when it differs from it.
    if (fact.discloses_as_of || fact.as_of === sheet.period_end) sentences.push(label);
    else sentences.push(ja ? `${label}（${dateText(fact.as_of, "ja")}時点）` : `${label}, as of ${dateText(fact.as_of, "en")}`);
    // An input that has no note of its own may still be in the text under another name with the same
    // value (SPY's contribution appears as "the largest contribution"); that note counts for it.
    const noteFor = (i) => noteOf(i) || Object.keys(facts).map((k) => facts[k])
      .filter((f) => f.id !== id && (f.inputs || []).length === 1 && f.inputs[0] === i && f.value === facts[i]?.value)
      .map((f) => noteOf(f.id)).find(Boolean) || null;
    const inputs = fact.inputs || [];
    const inputNotes = [...new Set(inputs.map(noteFor).filter(Boolean))];
    const uncited = inputs.filter((i) => !noteFor(i)).length;
    refs.push(...inputNotes);
    let from = "";
    if (inputNotes.length) {
      // Say so when part of the calculation is not in the text, so the note never implies a shorter sum.
      from = ja ? `注記${inputNotes.join("・")}${uncited ? `と本文にない${uncited}件の入力` : ""}から`
        : `from note${inputNotes.length > 1 ? "s" : ""} ${joinList(inputNotes.map(String), "en")}` +
          (uncited ? ` and ${uncited} input${uncited > 1 ? "s" : ""} not cited in the text` : "");
    }
    if (fact.source === "derived" && fact.derivation) {
      sentences.push(ja ? `${from}算出：${derivationJa(fact.derivation)}` : `${fact.derivation.charAt(0).toUpperCase()}${fact.derivation.slice(1)}${from ? `, ${from}` : ""}`);
    } else if (from) {
      sentences.push(ja ? `${from}算出` : `Derived ${from}`);
    }
    const nav = /^etf\.([^.]+)\.ret_1m$/.exec(id);
    if (nav) {
      const check = (sheet.checks || []).find((c) => c.name === `${nav[1]}: published return vs NAV change`);
      const m = check && (/the ([+-]?\d+\.\d+) pt gap reflects the monthly distribution/.exec(check.detail)
        || /gap ([+-]?\d+\.\d+) pt/.exec(check.detail));
      // The gap is a difference between two returns in percentage points; show it in basis points
      // (1 bp = 0.01 pp) so that −0.0030 pp reads as −0.3 bp.
      const bp = m ? displayMinus(`${Number(m[1]) >= 0 ? "+" : ""}${(Number(m[1]) * 100).toFixed(1)}`) : "";
      if (m && /monthly distribution/.test(check.detail)) {
        sentences.push(ja ? `基準価額の変動との差${bp}bpは分配金分`
          : `Gap to NAV change ${bp} bp, the distribution`);
      } else if (m) {
        sentences.push(ja ? `基準価額から算出した騰落率との差：${bp}bp`
          : `NAV cross-check gap ${bp} bp`);
      }
    }
    const run = fact.unit === "text" ? /in each of the last (\d+) months?/.exec(fact.text_en || "") : null;
    if (run) {
      const k = run[1];
      sentences.push(ja
        ? `算出開始月より古いデータが存在する可能性があるため、「${k}か月連続」ではなく「直近${k}か月にわたる」と表記`
        : `The run reaches the first month of the data, so a longer run cannot be ruled out; the text says \u201ceach of the last ${k} months\u201d, not \u201c${k} months in a row\u201d`);
    }
    if (fact.source !== "derived") {
      // "Same source" when the previous note already named it (DESIGN.md: notes 10 and 11 of the specimen).
      if (prevSource && prevSource.source === fact.source) {
        refs.push(prevSource.n);
        sentences.push(ja ? `出所は注記${prevSource.n}と同じ` : `Same source as note ${prevSource.n}`);
      } else {
        sentences.push(sourceText(sheet, fact.source, lang, true));
      }
    }
    return { value, body: sentences.map(end).join(ja ? "" : " "), refs };
  }

  const api = { MINUS, NBSP, displayMinus, formatFact, num, splitNumber, dateText, monthText, timeText, nb,
    isLocked, noteGroup, noteText, joinList, sourceText, headline, derivationJa };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FactLockFormat = api;
})(typeof window !== "undefined" ? window : globalThis);

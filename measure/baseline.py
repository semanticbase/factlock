"""M6 — FactLock against ordinary use of the same model.

The comparison writer gets the same fact sheet, the same rules about what may be said, the same
model and settings, and the same ADK configuration (app.agents.make_writer). The one difference:
it types every number itself from the values listed in FACTS, and nothing checks or returns its
draft. Its instruction is FactLock's own writer rules with the number rules replaced; each
replacement is asserted, so a change to the production rules cannot silently change the baseline.

The evaluator below reads plain text only, and is applied in the same way to FactLock's rendered
drafts and to the comparison drafts, so both are measured with one yardstick.
"""
from __future__ import annotations

import json
import re

from app import policy
from app.models import FactSheet
from app.prompts import WRITER_RULES, facts_for_prompt, writer_instruction

BASELINE_DESCRIPTION = "Drafts the bilingual commentary."

_REPLACEMENTS = [
    ("You are the drafting agent of FactLock. You prepare", "You prepare"),
    ("""HOW NUMBERS WORK
- Never type a number yourself: no digits, percentages, dates, counts, or names that contain digits (such as G7).
- Every number, date and data-derived phrase comes from a fact placeholder written as {{fact_id|format}}, for example {{fund.ret_1m|verb}}.
- Use only the fact IDs listed in FACTS, and only the formats listed for each fact.
- Formats: value = the number as is; signed = with its sign; abs = the size without sign; verb = the movement with its direction (English "rose 1.23%", Japanese "1.23%の上昇"); name = a text fact.
- To describe a movement, use the verb format instead of typing words such as rose or fell. In Japanese the verb format is a noun phrase, so write for example "{{fund.ret_1m|verb}}となりました". Text facts about runs of months are noun phrases too: write "{{cli.G7.streak|name}}となっています", never "{{cli.G7.streak|name}}一方".""",
     """HOW NUMBERS WORK
- Take every number, date and data-derived phrase from FACTS and type it into the text yourself: value_en in English, value_ja in Japanese.
- Describe movements in your own words, for example English "rose 1.23%", Japanese "1.23%の上昇"."""),
    ("in the same sentence, for example {{holdings.as_of|name}}.", "in the same sentence."),
]


def baseline_rules() -> str:
    text = WRITER_RULES
    for old, new in _REPLACEMENTS:
        if text.count(old) != 1:
            raise ValueError(f"The production writer rules changed; update the baseline: {old[:60]!r}")
        text = text.replace(old, new)
    return text


def baseline_instruction(sheet: FactSheet) -> str:
    production = writer_instruction(sheet, [], None)
    header = re.search(r"REPORT\nFund:.*?\n(?=\nFACTS\n)", production, re.S)
    if header is None:
        raise ValueError("The production instruction layout changed; update the baseline.")
    rows = [{k: v for k, v in row.items() if k != "formats"} for row in facts_for_prompt(sheet)]
    facts = json.dumps(rows, ensure_ascii=False, indent=0)
    return "\n\n".join([baseline_rules(), header.group(0).rstrip("\n") + "\n", f"FACTS\n<FACTS_JSON>\n{facts}\n</FACTS_JSON>"])


# ------------------------------------------------------------------------------------ evaluator
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December"], start=1)}
NUMBER = re.compile(r"(?<![A-Za-z0-9.,])([+\-\u2212]?)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")
# Numbers that are names, not figures: index names, area codes, bond maturities ("10-year", 「10年物」).
NAME_NUMBERS = re.compile(r"S&P\s*\d+|MSCI\s+\w+\s*\d*|\b[A-Z]\d\b|\b\d+[- ]year\b|\d+年(?:物|債)")
CLAUSE = re.compile(r"[,;、。；]")
AREA_ALIASES = {"G7": ["G7"], "A5M": ["Asia", "アジア"], "USA": ["United States", "U.S.", "US ", "米国"],
                "JPN": ["Japan", "日本"], "CHN": ["China", "中国"]}
DIRECTIONAL = {"pct", "contrib", "pp", "pt"}
UNIT_WORDS = ("%", "％", "pp", "pt", "point", "percent", "bp", "basis point", "ポイント", "ベーシス")
# A sentence such as "the US, Japan and the G7 rose to 100.96, 100.30 and 100.88, respectively"
# names its entities before its values; comma-splitting for clause-level attribution then pairs a
# value with whichever name happens to sit in the same clause, not the name it actually belongs to
# (2026-09-25, m6b false positives). RESPECTIVELY marks a sentence as this kind of ordered list, so
# its clauses are checked against sentence-wide mentions instead of only their own clause's.
RESPECTIVELY_EN = re.compile(r"\brespectively\b", re.I)


def _numbers(text: str) -> list[dict]:
    masked = NAME_NUMBERS.sub(lambda m: " " * len(m.group(0)), text)
    out = []
    for m in NUMBER.finditer(masked):
        digits = m.group(2).replace(",", "")
        dec = m.group(3) or ""
        after = masked[m.end():m.end() + 20]
        out.append({"text": m.group(0), "value": float(f"{digits}.{dec}" if dec else digits), "decimals": len(dec),
                    "sign": {"+": 1, "-": -1, "\u2212": -1}.get(m.group(1), 0), "after": after, "pos": m.start()})
    return out


def _mask_names(text: str, sheet: dict) -> str:
    """Hide names and labels that contain digits (e.g. アジア主要5カ国) before comparing English and Japanese."""
    for fid, f in sheet["facts"].items():
        if fid.endswith((".label", ".name")):
            for key in ("text_en", "text_ja"):
                name = f.get(key) or ""
                if re.search(r"\d", name) and name in text:
                    text = text.replace(name, " ")
    return text


def _entities(fact_id: str, sheet: dict) -> set[str]:
    parts = fact_id.split(".")
    if parts[0] == "etf" or fact_id.startswith("fund.contrib."):
        return {"T:" + parts[-1] if fact_id.startswith("fund.contrib.") else "T:" + parts[1]}
    if parts[0] == "cli":
        return {"A:" + parts[1]}
    if parts[0] == "rate":
        return {"A:" + parts[1]}
    if fact_id in ("fund.top_contributor_value", "fund.smallest_contributor_value"):
        name = sheet["facts"][fact_id.replace("_value", "")]["text_en"]
        ticker = re.search(r"\(([A-Z0-9]+)\)", name)
        return {"F:fund"} | ({"T:" + ticker.group(1)} if ticker else set())
    if parts[0] == "fund":
        return {"F:fund"}
    if parts[0] == "lookthrough":
        name = sheet["facts"].get(f"lookthrough.{parts[1]}.name", {}).get("text_en", "")
        return {"H:" + name.split()[0].upper()} if name else set()
    return set()


def _mentions(text: str, sheet: dict) -> set[str]:
    found = set()
    for fid, f in sheet["facts"].items():
        if re.fullmatch(r"etf\.[A-Z0-9]+\.label", fid):
            ticker = fid.split(".")[1]
            label_en = re.sub(r"\s*\([A-Z0-9]+\)$", "", f["text_en"])
            label_ja = re.sub(r"（[A-Z0-9]+）$", "", f["text_ja"])
            # Case-insensitive: a label sitting at the start of a sentence is capitalized by the
            # writer ("Emerging-market equities...") but stored lower-case ("emerging-market
            # equities (SPEM)"), so an exact-case check misses it (2026-09-25, m6b false positives).
            if re.search(rf"\b{ticker}\b", text) or label_en.lower() in text.lower() or label_ja in text:
                found.add("T:" + ticker)
        elif re.fullmatch(r"lookthrough\.\d+\.name", fid):
            first = f["text_en"].split()[0]
            if first.lower() in text.lower():
                found.add("H:" + first.upper())
    for area, aliases in AREA_ALIASES.items():
        if any(a in text for a in aliases):
            found.add("A:" + area)
    if re.search(r"\bfund\b|Fund|ファンド", text):
        found.add("F:fund")
    return found


def _allowed_integers(sheet: dict) -> set[int]:
    ints: set[int] = set()
    for f in sheet["facts"].values():
        for key in ("text_en", "text_ja", "label_en", "label_ja", "as_of"):
            ints |= {int(x) for x in re.findall(r"\d+", str(f.get(key) or ""))}
    for key in ("month", "period_end"):
        ints |= {int(x) for x in re.findall(r"\d+", sheet[key])}
    tickers = [fid.split(".")[1] for fid in sheet["facts"] if re.fullmatch(r"etf\.[A-Z0-9]+\.label", fid)]
    looks = [fid for fid in sheet["facts"] if re.fullmatch(r"lookthrough\.\d+\.name", fid)]
    areas = {fid.split(".")[1] for fid in sheet["facts"] if fid.startswith(("cli.", "rate."))}
    ints |= {len(tickers), len(looks), len({a for a in areas if f"cli.{a}.level" in sheet["facts"]})}
    return ints


def _candidates(n: dict, sheet: dict) -> list[tuple[str, dict]]:
    tol = 0.5 * 10 ** (-n["decimals"]) + 1e-9
    value = n["value"] / 100 if re.match(r"\s*(bp|basis point|ベーシス)", n["after"]) else n["value"]
    out = []
    for fid, f in sheet["facts"].items():
        if f["unit"] == "text" or f.get("value") is None:
            continue
        v = f["value"]
        if abs(value - abs(v)) > tol:
            continue
        if n["sign"] and ((n["sign"] > 0 and v <= 0) or (n["sign"] < 0 and v >= 0)):
            continue
        out.append((fid, f))
    return out


def published_values(snapshot: dict) -> list[float]:
    """Every value the publishers provide (series, returns, weights, holdings). A draft that quotes one of
    them, such as last month's level, is quoting data, even if FactLock's fact sheet does not carry it."""
    vals = []
    for e in snapshot["etfs"].values():
        vals.append(e["tr_1m"])
        vals += [h["weight"] for h in (e.get("holdings") or {}).get("top", [])]
    vals += [c["weight"] * 100 for c in snapshot["fund"]["components"]]
    for section in ("cli", "lt_rate"):
        for entry in snapshot["macro"][section].values():
            vals += [v for _, v in entry["series"]]
    return vals


def _matches_published(n: dict, published: list[float]) -> bool:
    tol = 0.5 * 10 ** (-n["decimals"]) + 1e-9
    return any(abs(n["value"] - abs(v)) <= tol and not (n["sign"] < 0 <= v) for v in published)


def evaluate(sentences: list[dict], sheet: dict, published: list[float] | None = None) -> dict:
    """sentences: [{"ref", "section", "en", "ja"}] as plain text. Returns counts and details.
    published: values from the publishers' data (see published_values); a figure matching one is supported."""
    allowed = _allowed_integers(sheet)
    found = {k: [] for k in ("unsupported", "unit", "direction", "prohibited", "misattribution", "parity", "caution",
                             "placeholder")}
    for s in sentences:
        per_lang_values = {}
        for lang in ("en", "ja"):
            text = s.get(lang) or ""
            if "{{" in text:
                found["placeholder"].append({"ref": s["ref"], "lang": lang, "text": text})
            values = [round(n["value"], 4) for n in _numbers(_mask_names(text, sheet))]
            if lang == "en":
                values += [float(MONTHS[m]) for m in re.findall(r"\b(" + "|".join(MONTHS) + r")\b", text)]
            per_lang_values[lang] = sorted(values)
            sentence_mentions = _mentions(text, sheet)
            distributive = bool(RESPECTIVELY_EN.search(text)) if lang == "en" else ("それぞれ" in text)
            for clause in [c for c in CLAUSE.split(text) if c.strip()]:
                # Names are looked for in the clause as written; figures are read after names with digits
                # (G7, アジア主要5カ国) are masked, so a name is never read as a figure and never hidden.
                clause_mentions = _mentions(clause, sheet)
                ups = policy.DIRECTION_EN["up"] if lang == "en" else policy.DIRECTION_JA["up"]
                downs = policy.DIRECTION_EN["down"] if lang == "en" else policy.DIRECTION_JA["down"]
                has_up, has_down = bool(ups.search(clause)), bool(downs.search(clause))
                signs = set()
                for n in _numbers(_mask_names(clause, sheet)):
                    unitful = n["after"].lstrip().lower().startswith(UNIT_WORDS)
                    if not unitful and n["decimals"] == 0 and int(n["value"]) in allowed:
                        continue  # a count or a date part taken from the facts
                    cands = _candidates(n, sheet)
                    if not cands and published and _matches_published(n, published):
                        continue  # a publisher's value that the fact sheet does not carry, e.g. last month's level
                    if not cands:
                        found["unsupported"].append({"ref": s["ref"], "lang": lang, "number": n["text"], "clause": clause.strip()})
                        continue
                    units = {f["unit"] for _, f in cands}
                    after = n["after"].lstrip()
                    if units <= {"pp", "pt"} and after.startswith("%") and not after.startswith(("%ポイント", "% point")):
                        found["unit"].append({"ref": s["ref"], "lang": lang, "number": n["text"], "clause": clause.strip()})
                    ents = set().union(*(_entities(fid, sheet) for fid, _ in cands))
                    if lang == "ja" and ents and all(e.startswith("H:") for e in ents):
                        ents = set()  # holdings are often written in katakana in Japanese; names cannot be matched
                    ref_mentions = sentence_mentions if distributive else clause_mentions
                    if ents and not (ents & ref_mentions):
                        if (not distributive and clause_mentions) or not (ents & sentence_mentions):
                            found["misattribution"].append({"ref": s["ref"], "lang": lang, "number": n["text"],
                                                            "clause": clause.strip(), "could_be": [fid for fid, _ in cands]})
                    dir_signs = {(f["value"] > 0) - (f["value"] < 0) for _, f in cands if f["unit"] in DIRECTIONAL}
                    if len(dir_signs) == 1:
                        signs |= dir_signs
                if s.get("section") != "outlook" and signs and len(signs) == 1:
                    sign = signs.pop()
                    if (sign > 0 and has_down and not has_up) or (sign < 0 and has_up and not has_down):
                        found["direction"].append({"ref": s["ref"], "lang": lang, "clause": clause.strip()})
            for code, _, pattern in (policy.PROHIBITED if lang == "en" else policy.PROHIBITED_JA):
                if pattern.search(text):
                    found["prohibited"].append({"ref": s["ref"], "lang": lang, "code": code})
            for code, _, pattern in (policy.CAUTION if lang == "en" else policy.CAUTION_JA):
                if pattern.search(text):
                    found["caution"].append({"ref": s["ref"], "lang": lang, "code": code})
        if per_lang_values["en"] != per_lang_values["ja"]:
            found["parity"].append({"ref": s["ref"], "en": per_lang_values["en"], "ja": per_lang_values["ja"]})
    certain = sum(len(found[k]) for k in ("unsupported", "unit", "direction", "prohibited", "placeholder"))
    review = sum(len(found[k]) for k in ("misattribution", "parity", "caution"))
    return {"numbers": sum(len(_numbers(s.get(l) or "")) for s in sentences for l in ("en", "ja")),
            "certain_errors": certain, "needs_review": review,
            "counts": {k: len(v) for k, v in found.items()}, "details": found}


def sentences_from_commentary(commentary: list[dict]) -> list[dict]:
    return [{"ref": s["ref"], "section": sec["key"],
             "en": "".join(x["text"] for x in s["en"]), "ja": "".join(x["text"] for x in s["ja"])}
            for sec in commentary for s in sec["sentences"]]


def sentences_from_draft(draft) -> list[dict]:
    return [{"ref": f"{sec.key}.{i}", "section": sec.key, "en": st.en, "ja": st.ja}
            for sec in draft.sections for i, st in enumerate(sec.sentences, start=1)]


# ------------------------------------------------------------------ ordinary use, inputs only (P6)
INPUTS_METHOD = """CALCULATIONS
The DATA below are the published inputs only. Compute what you need yourself, with these definitions:
- Contribution of an asset, in percentage points = target weight × its 1-month total return. Fund return = the sum of the contributions.
- Look-through weight of a holding = target weight of its ETF × the holding's weight in that ETF, added up across the ETFs that hold it. Rank the holdings by this weight.
- Monthly change of an indicator or rate = latest value − value of the month before. A run = the number of consecutive months, ending with the latest, in which the change had the same direction.
- Cycle phase of a leading indicator (OECD reading): above 100 and rising = expansion; above 100 and falling = downturn; below 100 and falling = slowdown; below 100 and rising = recovery. The long-term trend level is 100.
State holdings with their as-of date whenever you cite them."""


def inputs_data(snapshot: dict) -> dict:
    """The published inputs of the snapshot, without anything FactLock computes from them."""
    etfs = snapshot["etfs"]
    return {
        "report_month": snapshot["month"], "period_end": snapshot["period_end"],
        "fund": {"name_en": snapshot["fund"]["name_en"], "name_ja": snapshot["fund"]["name_ja"],
                 "components": [{"ticker": c["ticker"], "label_en": c["label_en"], "label_ja": c["label_ja"],
                                 "target_weight": c["weight"], "total_return_1m_pct": etfs[c["ticker"]]["tr_1m"],
                                 "returns_as_of": etfs[c["ticker"]]["returns_as_of"]} for c in snapshot["fund"]["components"]]},
        "holdings": {t: {"as_of": e["holdings"]["as_of"],
                         "top": [{"name": h["name"], "identifier": h.get("identifier"), "weight_pct": h["weight"]}
                                 for h in e["holdings"]["top"]]}
                     for t, e in etfs.items() if e.get("holdings")},
        "leading_indicator": {a: {"label_en": e["label_en"], "label_ja": e["label_ja"], "series": e["series"]}
                              for a, e in snapshot["macro"]["cli"].items()},
        "long_term_rate_pct": {a: {"label_en": e["label_en"], "label_ja": e["label_ja"], "series": e["series"]}
                               for a, e in snapshot["macro"]["lt_rate"].items()},
    }


def inputs_instruction(sheet: FactSheet, snapshot: dict) -> str:
    """The same rules as the comparison writer, but FACTS is replaced by the published inputs and the
    definitions of the calculations. The model computes contributions, the fund return, look-through
    weights, monthly changes, runs and phases itself."""
    production = writer_instruction(sheet, [], None)
    header = re.search(r"REPORT\nFund:.*?\n(?=\nFACTS\n)", production, re.S)
    if header is None:
        raise ValueError("The production instruction layout changed; update the baseline.")
    rules = baseline_rules().replace("type it into the text yourself: value_en in English, value_ja in Japanese.",
                                     "type it into the text yourself, computing derived figures as described under CALCULATIONS.")
    if "CALCULATIONS" not in rules:
        raise ValueError("The number rule of the baseline changed; update inputs_instruction.")
    data = json.dumps(inputs_data(snapshot), ensure_ascii=False, indent=0)
    return "\n\n".join([rules, header.group(0).rstrip("\n") + "\n", INPUTS_METHOD, f"DATA\n<DATA_JSON>\n{data}\n</DATA_JSON>"])

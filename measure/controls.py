"""Known mistakes planted in a copy of the commentary, to show that M3 can see them.

A report of "0 problems" means nothing unless the same code, on the same draft, finds planted
mistakes. Controls are chosen by the kind of token the draft contains, so they apply to any
draft (the offline demo or each Gemini run). A control whose kind of token is absent is
reported as not applicable. Two negative controls check that correct renderings are accepted.
"""
from __future__ import annotations

import copy
import re

from measure.independent import NUMBER, check_commentary, expected_facts

SWAP_WORDS = {"en": [("rose", "fell"), ("fell", "rose"), ("added", "subtracted"), ("subtracted", "added")],
              "ja": [("プラス寄与", "マイナス寄与"), ("マイナス寄与", "プラス寄与"), ("上昇", "下落"), ("下落", "上昇"),
                     ("低下", "上昇")]}
UNIT_SWAPS = [("%ポイント", "%"), ("percentage points", "percent"), (" pp", "%"), (" pt", "%"), (" points", "%"),
              ("ポイント", "%")]


def _tokens(commentary):
    for si, section in enumerate(commentary):
        for ti, sentence in enumerate(section["sentences"]):
            for lang in ("en", "ja"):
                for gi, seg in enumerate(sentence[lang]):
                    if seg.get("type") == "fact":
                        yield (si, ti, lang, gi), seg


def _seg(commentary, where):
    si, ti, lang, gi = where
    return commentary[si]["sentences"][ti][lang][gi]


DATES = ("holdings.as_of", "report.period_end", "report.month")


def _pickers(exp, tokens):
    def number(seg, kind=None):
        e = exp.get(seg["fact_id"])
        return bool(e and e["kind"] == "number" and (kind is None or e["quantity"] == kind)
                    and NUMBER.search(seg["text"]))

    def digits_changed(seg, lang):
        if not number(seg):
            return None
        m = NUMBER.search(seg["text"])
        d = m.group(2)
        return seg["text"][:m.start(2)] + d[:-1] + str((int(d[-1]) + 5) % 10) + seg["text"][m.end(2):]

    def last_decimal(seg, lang):
        """Move the last shown decimal one step away from the true value (the smallest visible error)."""
        if not number(seg):
            return None
        m = NUMBER.search(seg["text"])
        if "." not in m.group(2):
            return None
        places = len(m.group(2).split(".")[1])
        shown = float(m.group(2))
        step = 10 ** (-places) * (1 if shown >= abs(exp[seg["fact_id"]]["value"]) else -1)
        value = round(shown + step, places)
        if value < 0:
            return None
        return seg["text"][:m.start(2)] + f"{value:.{places}f}" + seg["text"][m.end(2):]

    def sign_flipped(seg, lang):
        return seg["text"].replace("+", "-", 1) if seg.get("format") == "signed" and "+" in seg["text"] else None

    def direction_reversed(seg, lang):
        if seg.get("format") != "verb":
            return None
        for old, new in SWAP_WORDS[lang]:
            if old in seg["text"]:
                return seg["text"].replace(old, new, 1)
        return None

    def unit_changed(seg, lang):
        if not number(seg) or exp[seg["fact_id"]]["quantity"] not in ("rate_change", "index_change"):
            return None
        for old, new in UNIT_SWAPS:
            if old in seg["text"]:
                return seg["text"].replace(old, new, 1)
        return None

    def figure_in_phrase(seg, lang):
        fid = seg["fact_id"]
        e = exp.get(fid)
        if not e or e["kind"] != "text" or fid in DATES or fid.startswith("report.") or fid.endswith((".label", ".name")):
            return None
        m = re.search(r"\d+", seg["text"])
        return seg["text"][:m.start()] + str(int(m.group(0)) + 1) + seg["text"][m.end():] if m else None

    def wrong_asset(seg, lang):
        if not re.fullmatch(r"fund\.(top|smallest)_contributor|etf\.[A-Z0-9]+\.label", seg["fact_id"]):
            return None
        tickers = [k.split(".")[1] for k in exp if re.fullmatch(r"etf\.[A-Z0-9]+\.label", k)]
        m = re.search(r"\b(" + "|".join(map(re.escape, tickers)) + r")\b", seg["text"])
        if not m:
            return None
        other = next(t for t in tickers if t != m.group(1))
        return seg["text"].replace(m.group(1), other)

    def index_as_percent(seg, lang):
        return seg["text"] + "%" if number(seg, "index") else None

    def date_changed(seg, lang):
        if seg["fact_id"] not in DATES:
            return None
        m = re.search(r"\d+", seg["text"])
        return seg["text"][:m.start()] + str(int(m.group(0)) + 1) + seg["text"][m.end():] if m else None

    def lookthrough_swapped(seg, lang):
        if not re.fullmatch(r"lookthrough\.\d+\.name", seg["fact_id"]):
            return None
        others = [t["text"] for _, t in tokens if re.fullmatch(r"lookthrough\.\d+\.name", t["fact_id"])
                  and t["text"] != seg["text"]]
        return others[0] if others else None

    return [("digits changed", digits_changed), ("last decimal off by one", last_decimal),
            ("sign flipped", sign_flipped), ("direction word reversed", direction_reversed),
            ("percentage points or points shown as percent", unit_changed),
            ("figure inside a phrase changed (run length, date, count)", figure_in_phrase),
            ("wrong asset named", wrong_asset), ("index level shown as percent", index_as_percent),
            ("date changed", date_changed), ("look-through ranking swapped", lookthrough_swapped)]


def _append_token(commentary, fact_id, text):
    planted = copy.deepcopy(commentary)
    planted[0]["sentences"][0]["en"] += [{"type": "text", "text": " "},
                                          {"type": "fact", "fact_id": fact_id, "format": "signed", "text": text}]
    return planted


def run_controls(commentary: list[dict], snapshot: dict) -> dict:
    """Each control is judged by what it adds to the draft's own result, so a problem already present in
    the draft can neither make a planted mistake look found nor a correct rendering look rejected."""
    exp = expected_facts(snapshot)
    tokens = list(_tokens(commentary))
    base = check_commentary(commentary, snapshot)

    def added(planted, key="problems"):
        return check_commentary(planted, snapshot)[key] > base[key]

    # Plant only into tokens that are correct now: changing a token that is already wrong adds nothing.
    broken = {(t["ref"], t["lang"], t["fact_id"]) for t in base["failed_tokens"]}
    clean = [(w, seg) for w, seg in tokens
             if (commentary[w[0]]["sentences"][w[1]]["ref"], w[2], seg["fact_id"]) not in broken]

    results = []
    for name, make in _pickers(exp, clean):
        where, planted_text = None, None
        for w, seg in clean:
            candidate = make(seg, w[2])
            if candidate and candidate != seg["text"]:
                where, planted_text = w, candidate
                break
        if where is None:
            results.append({"control": name, "expect": "detected", "applicable": False, "detected": None})
            continue
        planted = copy.deepcopy(commentary)
        _seg(planted, where)["text"] = planted_text
        results.append({"control": name, "expect": "detected", "applicable": True, "detected": added(planted)})

    planted = copy.deepcopy(commentary)
    planted[0]["sentences"][0]["en"].append({"type": "text", "text": " (about 2.2%)"})
    results.append({"control": "digit typed between tokens", "expect": "detected", "applicable": True,
                    "detected": added(planted, "digits_outside_tokens")})

    if tokens:
        planted = copy.deepcopy(commentary)
        seg = _seg(planted, tokens[0][0])
        seg.clear()
        seg.update({"type": "error", "text": "{{no.such.fact|value}}"})
        results.append({"control": "placeholder left unrendered", "expect": "detected", "applicable": True,
                        "detected": added(planted, "unrendered_placeholders")})

    negative = next(((fid, e) for fid, e in exp.items() if e["kind"] == "number"
                     and e["quantity"] in ("index_change", "rate_change") and round(e["value"], 2) < 0), None)
    if negative:
        fid, e = negative
        unit = " pt" if e["quantity"] == "index_change" else " pp"
        results.append({"control": "negative change shown with a plus sign", "expect": "detected", "applicable": True,
                        "detected": added(_append_token(commentary, fid, f"+{abs(e['value']):.2f}{unit}"))})
        results.append({"control": "minus sign U+2212 on a negative change (correct)", "expect": "accepted",
                        "applicable": True,
                        "detected": added(_append_token(commentary, fid, f"\u2212{abs(e['value']):.2f}{unit}"))})
    contrib = next(((fid, e) for fid, e in exp.items() if fid.startswith("fund.contrib.")), None)
    if contrib:
        fid, e = contrib
        sign = "+" if e["value"] > 0 else "\u2212"
        results.append({"control": "contribution in words 'percentage points' (correct)", "expect": "accepted",
                        "applicable": True,
                        "detected": added(_append_token(commentary, fid, f"{sign}{abs(e['value']):.2f} percentage points"))})

    applied = [c for c in results if c["applicable"]]
    ok = all((c["expect"] == "detected") == c["detected"] for c in applied)
    return {"all_as_expected": ok, "applicable": len(applied), "controls": results}

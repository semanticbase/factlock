"""M3 — every figure in the commentary, recomputed independently from the public-data snapshot.

This module must not import anything from app/ (a test enforces it). It reads two plain JSON
objects: the snapshot (the values taken from the publishers) and the rendered commentary as the
reader sees it. It recomputes each quantity with its own code and checks, for every locked token:

* the number shown is a correct rounding of the recomputed value, at the precision shown;
* the sign, and the direction word of a "verb" token, agree with the recomputed value;
* the unit is the right one for the quantity (a change in yield is in percentage points, etc.);
* text tokens that carry figures (runs of months, dates, rankings, phases) say what the data says.

It also scans the text between tokens: any digit there was typed by the writer, not locked.
"""
from __future__ import annotations

import datetime as _dt
import math
import re

MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December"]
MINUS_SIGNS = "-\u2212"
NUMBER = re.compile(r"([+\-\u2212])?(\d+(?:\.\d+)?)")

# Units accepted for each kind of quantity, per language (the text right after the number).
UNITS = {
    "return": {"en": ("%",), "ja": ("%",)},
    "weight": {"en": ("%",), "ja": ("%",)},
    "rate": {"en": ("%",), "ja": ("%",)},
    "contribution": {"en": ("pp", "percentage points"), "ja": ("%", "%ポイント")},
    "rate_change": {"en": ("pp", "percentage points"), "ja": ("%ポイント",)},
    "index_change": {"en": ("pt", "points"), "ja": ("ポイント",)},
    "index": {"en": ("",), "ja": ("",)},
}
# A percentage must not be written as percentage points, and the reverse.
NOT_UNITS = {"return": ("%ポイント", "pp", "points"), "weight": ("%ポイント", "pp"), "rate": ("%ポイント", "pp")}
UP_WORDS = {"en": ("rose", "added"), "ja": ("上昇", "プラス寄与")}
DOWN_WORDS = {"en": ("fell", "subtracted"), "ja": ("下落", "低下", "マイナス寄与")}
FLAT_WORDS = {"en": ("was unchanged", "had no effect"), "ja": ("横ばい", "寄与なし")}

EN_NUMBER_WORDS = re.compile(
    r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|"
    r"thousand|million|billion|dozen|half|third|quarter|twice|thrice)\b", re.I)
JA_NUMERALS = re.compile(r"(?<!十)(?!十分)[〇一二三四五六七八九十百千万億兆]+(?=\s*(か月|ヶ月|カ月|ヵ月|年|倍|割|分|%|％|"
                         r"パーセント|ポイント|日|週|四半期|位|本|社|件|銘柄|資産|期|回))")
DIGITS = re.compile(r"[0-9０-９]")


# ----------------------------------------------------------------------------- expected values
def _num(kind: str, value: float) -> dict:
    return {"kind": "number", "quantity": kind, "value": value}


def _text(check, **info) -> dict:
    return {"kind": "text", "check": check, "info": info}


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _month_text(period: str, lang: str) -> str:
    year, month = (int(p) for p in period.split("-")[:2])
    return f"{MONTHS_EN[month - 1]} {year}" if lang == "en" else f"{year}年{month}月"


def _date_text(iso: str, lang: str) -> str:
    d = _dt.date.fromisoformat(iso[:10])
    return f"{MONTHS_EN[d.month - 1]} {d.day}, {d.year}" if lang == "en" else f"{d.year}年{d.month}月{d.day}日"


def _exact(en: str, ja: str):
    def check(text, lang):
        want = en if lang == "en" else ja
        return [] if text == want else [f"text is “{text}”, the data says “{want}”"]
    return check


def _ticker_label(ticker: str, label_en: str, label_ja: str):
    def check(text, lang):
        problems = []
        if not re.search(rf"\b{re.escape(ticker)}\b", text):
            problems.append(f"ticker {ticker} missing")
        label = label_en if lang == "en" else label_ja
        if label not in text:
            problems.append(f"label “{label}” missing")
        return problems
    return check


def _one_of_tickers(expected: set[str], all_tickers: set[str]):
    def check(text, lang):
        named = {t for t in all_tickers if re.search(rf"\b{re.escape(t)}\b", text)}
        if named != expected:
            return [f"names {sorted(named) or 'no asset'}, the data says {sorted(expected)}"]
        return []
    return check


def _detractors(negatives: set[str], all_tickers: set[str]):
    def check(text, lang):
        named = {t for t in all_tickers if re.search(rf"\b{re.escape(t)}\b", text)}
        problems = []
        if named != negatives:
            problems.append(f"names {sorted(named) or 'no asset'} as detractors; the data says {sorted(negatives) or 'none'}")
        numbers = {int(n) for n in re.findall(r"\d+", text)}
        if negatives and len(negatives) not in numbers:
            problems.append(f"count {len(negatives)} not stated")
        if not negatives and not re.search(r"\bno\b|ありません|なし|ない", text):
            problems.append("does not say that nothing detracted")
        return problems
    return check


def _trend(level: float):
    above = level >= 100

    def check(text, lang):
        problems = []
        if "100" not in text:
            problems.append("trend level 100 not stated")
        if lang == "en":
            ok = ("above" in text and "below" not in text) if above else ("below" in text and "above" not in text)
        else:
            ok = ("上回" in text and "下回" not in text) if above else ("下回" in text and "上回" not in text)
        if not ok:
            problems.append(f"level {level} is {'above' if above else 'below'} 100")
        return problems
    return check


def _phase(level: float, change: float):
    # OECD reading of an amplitude-adjusted CLI: level against the trend (100) and the direction.
    above, rising = level >= 100, change > 0
    name = {(True, True): "expansion", (True, False): "downturn",
            (False, False): "slowdown", (False, True): "recovery"}[(above, rising)]

    def check(text, lang):
        problems = []
        if lang == "en":
            if name not in text:
                problems.append(f"phase should be {name}")
            if ("above" in text) != above:
                problems.append("above/below trend is wrong")
            if ("rising" in text) != rising or ("falling" in text) == rising:
                problems.append("rising/falling is wrong")
        else:
            if ("上回" in text) != above:
                problems.append("趨勢との上下が逆")
            if ("上昇" in text) != rising or ("低下" in text) == rising:
                problems.append("方向が逆")
        return problems
    return check


def run_of_changes(values: list[float]) -> tuple[int, int, bool]:
    """(length, sign, complete) of the latest run of same-direction monthly changes.
    complete is False when the run reaches the first change in the data."""
    changes = [b - a for a, b in zip(values, values[1:])]
    if not changes:
        return 0, 0, True
    sign = _sign(changes[-1])
    length = 0
    for c in reversed(changes):
        if sign == 0 or _sign(c) != sign:
            break
        length += 1
    return length, sign, length < len(changes)


def _streak(values: list[float]):
    n, sign, complete = run_of_changes(values)

    def check(text, lang):
        problems = []
        numbers = {int(x) for x in re.findall(r"\d+", text)}
        if sign != 0 and numbers != {n}:
            problems.append(f"states {sorted(numbers)}, the run is {n}")
        up = sign > 0
        if lang == "en":
            if sign == 0:
                return [] if "unchanged" in text else ["run is flat"]
            if ("ris" in text) != up or ("fall" in text) == up:
                problems.append("direction of the run is wrong")
            if complete and n > 1 and "consecutive" not in text:
                problems.append("a complete run should be stated as consecutive months")
            if not complete and "each of the last" not in text:
                problems.append("the run reaches the start of the data; only “each of the last N months” may be said")
        else:
            if sign == 0:
                return [] if "横ばい" in text else ["run is flat"]
            if ("上昇" in text) != up or ("低下" in text) == up:
                problems.append("方向が逆")
            if complete and n > 1 and "連続" not in text:
                problems.append("連続の表記がない")
            if not complete and "直近" not in text:
                problems.append("データの端に達しているのに連続と表記")
        return problems
    return check


def _contains(*needles_by_lang):
    en_needles, ja_needles = needles_by_lang

    def check(text, lang):
        needles = en_needles if lang == "en" else ja_needles
        return [f"“{n}” missing" for n in needles if n not in text]
    return check


def expected_facts(snapshot: dict) -> dict[str, dict]:
    """Every quantity the commentary may cite, computed from the snapshot with this module's code."""
    exp: dict[str, dict] = {}
    fund, etfs = snapshot["fund"], snapshot["etfs"]
    comps = fund["components"]
    tickers = {c["ticker"] for c in comps}

    exp["report.month"] = _text(_exact(_month_text(snapshot["month"], "en"), _month_text(snapshot["month"], "ja")))
    exp["report.period_end"] = _text(_exact(_date_text(snapshot["period_end"], "en"),
                                            _date_text(snapshot["period_end"], "ja")))
    exp["fund.name"] = _text(_exact(fund["name_en"], fund["name_ja"]))

    contribution = {}
    for c in comps:
        t = c["ticker"]
        r = etfs[t]["tr_1m"]
        exp[f"etf.{t}.ret_1m"] = _num("return", r)
        exp[f"etf.{t}.weight"] = _num("weight", c["weight"] * 100)
        exp[f"etf.{t}.label"] = _text(_ticker_label(t, c["label_en"], c["label_ja"]))
        contribution[t] = c["weight"] * r
        exp[f"fund.contrib.{t}"] = _num("contribution", contribution[t])
    exp["fund.ret_1m"] = _num("return", math.fsum(contribution.values()))
    best, worst = max(contribution.values()), min(contribution.values())
    tops = {t for t, v in contribution.items() if v == best}
    bottoms = {t for t, v in contribution.items() if v == worst}
    exp["fund.top_contributor"] = _text(_one_of_tickers(tops, tickers))
    exp["fund.top_contributor_value"] = _num("contribution", best)
    exp["fund.smallest_contributor"] = _text(_one_of_tickers(bottoms, tickers))
    exp["fund.smallest_contributor_value"] = _num("contribution", worst)
    exp["fund.detractors"] = _text(_detractors({t for t, v in contribution.items() if v < 0}, tickers))

    for code, entry in snapshot["macro"]["cli"].items():
        series = entry["series"]
        if len(series) < 2:
            continue
        values = [v for _, v in series]
        last, prev = values[-1], values[-2]
        exp[f"cli.{code}.label"] = _text(_exact(entry["label_en"], entry["label_ja"]))
        exp[f"cli.{code}.level"] = _num("index", last)
        exp[f"cli.{code}.chg_1m"] = _num("index_change", last - prev)
        exp[f"cli.{code}.trend"] = _text(_trend(last))
        exp[f"cli.{code}.phase"] = _text(_phase(last, last - prev))
        exp[f"cli.{code}.streak"] = _text(_streak(values))

    for code, entry in snapshot["macro"]["lt_rate"].items():
        series = entry["series"]
        if len(series) < 2:
            continue
        last, prev = series[-1][1], series[-2][1]
        exp[f"rate.{code}.label"] = _text(_contains((entry["label_en"], "long-term"), (entry["label_ja"], "長期金利")))
        exp[f"rate.{code}.level"] = _num("rate", last)
        exp[f"rate.{code}.chg_1m"] = _num("rate_change", last - prev)

    combined: dict[str, float] = {}
    names: dict[str, str] = {}
    dates = []
    for c in comps:
        holdings = etfs[c["ticker"]].get("holdings")
        if not holdings:
            continue
        dates.append(holdings["as_of"])
        for row in holdings["top"]:
            key = row.get("identifier") or row["name"]
            combined[key] = combined.get(key, 0.0) + c["weight"] * row["weight"]
            names[key] = row["name"]
    if combined:
        exp["holdings.as_of"] = _text(_exact(_date_text(max(dates), "en"), _date_text(max(dates), "ja")))
        ranked = sorted(combined.items(), key=lambda kv: kv[1], reverse=True)
        for i, (key, weight) in enumerate(ranked[:5], start=1):
            exp[f"lookthrough.{i}.name"] = _text(_exact(names[key], names[key]))
            exp[f"lookthrough.{i}.weight"] = _num("weight", weight)
    return exp


# ----------------------------------------------------------------------------- checking tokens
def _check_number(exp: dict, seg: dict, lang: str) -> list[str]:
    text, fmt = seg["text"], seg.get("format", "")
    true = exp["value"]
    kind = exp["quantity"]
    found = list(NUMBER.finditer(text))
    problems: list[str] = []
    if not found:
        flat = any(w in text for w in FLAT_WORDS[lang])
        if fmt == "verb" and flat and round(abs(true), 2) == 0:
            return []
        return [f"no number shown in “{text}”"]
    if len(found) > 1:
        problems.append(f"more than one number in “{text}”")
    m = found[0]
    sign_char, digits = m.group(1), m.group(2)
    decimals = len(digits.split(".")[1]) if "." in digits else 0
    shown = float(digits)
    tolerance = 0.5 * 10 ** (-decimals) + 1e-9
    if abs(shown - abs(true)) > tolerance:
        problems.append(f"shows {digits}, recomputed value is {true:.6f}")
    rounded_zero = abs(true) < tolerance and shown == 0
    true_sign = 0 if rounded_zero else _sign(true)

    after = text[m.end():].lstrip()
    if kind == "index":
        if after and not after.startswith(("、", "。", ")", "）")):
            unit_ok = False
        else:
            unit_ok = True
    else:
        unit_ok = any(after.startswith(u) for u in UNITS[kind][lang] if u)
    wrong_unit = any(after.startswith(u) for u in NOT_UNITS.get(kind, ()))
    if not unit_ok or wrong_unit:
        problems.append(f"unit “{after[:12]}” is not right for a {kind.replace('_', ' ')}")

    if fmt == "verb":
        up = any(w in text for w in UP_WORDS[lang])
        down = any(w in text for w in DOWN_WORDS[lang])
        said = 1 if up and not down else (-1 if down and not up else 0)
        if said != true_sign:
            problems.append(f"direction word does not match the recomputed sign ({true_sign:+d})")
        if sign_char:
            problems.append("a verb phrase should not carry a sign")
    elif fmt in ("signed", "value"):
        # Accuracy only: the sign shown must not contradict the recomputed sign. Whether a positive change
        # carries "+" is a display convention (a change's value is shown signed), not an error.
        shown_sign = 0 if not sign_char else (1 if sign_char == "+" else -1)
        if true_sign < 0 and shown_sign != -1:
            problems.append("negative value shown without a minus sign")
        if true_sign > 0 and shown_sign == -1:
            problems.append("positive value shown with a minus sign")
    elif fmt == "abs" and sign_char:
        problems.append("abs format should not carry a sign")
    return problems


def check_commentary(commentary: list[dict], snapshot: dict) -> dict:
    """Check every token and every stretch of text between tokens. Returns a JSON-able record."""
    exp = expected_facts(snapshot)
    tokens, outside, problems_total = [], [], 0
    for section in commentary:
        for sentence in section["sentences"]:
            for lang in ("en", "ja"):
                for seg in sentence[lang]:
                    kind = seg.get("type")
                    if kind == "fact":
                        fid = seg["fact_id"]
                        e = exp.get(fid)
                        if e is None:
                            problems = ["no independent computation for this figure"]
                            k = "uncovered"
                        elif e["kind"] == "number":
                            problems = _check_number(e, seg, lang)
                            k = "number"
                        else:
                            problems = e["check"](seg["text"], lang)
                            k = "text_with_figure" if DIGITS.search(seg["text"]) else "text"
                        tokens.append({"ref": sentence["ref"], "lang": lang, "fact_id": fid,
                                       "format": seg.get("format"), "shown": seg["text"], "kind": k,
                                       "ok": not problems, "problems": problems})
                        problems_total += len(problems)
                    else:
                        text = seg.get("text", "")
                        hits = [m.group(0) for m in DIGITS.finditer(text)]
                        words = [m.group(0) for m in (EN_NUMBER_WORDS if lang == "en" else JA_NUMERALS).finditer(text)]
                        if kind == "error" or hits or words:
                            outside.append({"ref": sentence["ref"], "lang": lang, "text": text,
                                            "digits": hits, "number_words": words, "unrendered": kind == "error"})
    numbers = [t for t in tokens if t["kind"] == "number"]
    text_fig = [t for t in tokens if t["kind"] == "text_with_figure"]
    return {
        "tokens_total": len(tokens),
        "numbers": len(numbers),
        "numbers_ok": sum(t["ok"] for t in numbers),
        "text_with_figures": len(text_fig),
        "text_with_figures_ok": sum(t["ok"] for t in text_fig),
        "other_text_tokens": len([t for t in tokens if t["kind"] == "text"]),
        "other_text_tokens_ok": sum(t["ok"] for t in tokens if t["kind"] == "text"),
        "uncovered": sorted({t["fact_id"] for t in tokens if t["kind"] == "uncovered"}),
        "digits_outside_tokens": sum(len(o["digits"]) for o in outside),
        "number_words_outside_tokens": [o for o in outside if o["number_words"]],
        "unrendered_placeholders": sum(o["unrendered"] for o in outside),
        "problems": problems_total,
        "failed_tokens": [t for t in tokens if not t["ok"]],
        "outside": outside,
        "tokens": tokens,
    }

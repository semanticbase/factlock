"""Data for the two exhibits, the data-check summary and the source list on the paper.

Everything here is computed from the fact sheet by code, so the page never shows
a figure the checks have not seen. The browser only lays these values out.
"""
from __future__ import annotations

import datetime as dt
import math

from app.models import FactSheet

# Short area names for the exhibit (the fact labels are written for sentences:
# "the G7 economies"). Unknown codes fall back to the fact label.
AREA_NAMES = {
    "USA": ("United States", "米国"),
    "JPN": ("Japan", "日本"),
    "G7": ("G7", "G7"),
    "CHN": ("China", "中国"),
    "A5M": ("Asia, major five", "アジア主要5カ国"),
}
# Short holding names for the contribution exhibit.
HOLDING_NAMES = {
    "SPY": ("U.S. large-cap", "米国大型株"),
    "SPDW": ("Developed ex-U.S.", "先進国株式"),
    "SPEM": ("Emerging", "新興国株式"),
    "SPAB": ("U.S. bonds", "米国債券"),
}
# Phase numbers: rows are the rate direction, columns the indicator direction.
PHASES = {(1, 1): 1, (-1, 1): 2, (-1, -1): 3, (1, -1): 4}   # (indicator, rate) -> phase


def _sign(value: float, decimals: int) -> int:
    rounded = round(value, decimals)
    return (rounded > 0) - (rounded < 0)


def cycle_exhibit(sheet: FactSheet) -> dict:
    """Four-phase matrix: indicator change on the month × long-term rate change on the month."""
    facts = sheet.facts
    codes = [fid.split(".")[1] for fid in facts if fid.startswith("cli.") and fid.endswith(".chg_1m")]
    placed, unplaced = [], []
    for code in codes:
        cli = facts[f"cli.{code}.chg_1m"]
        rate = facts.get(f"rate.{code}.chg_1m")
        label = facts[f"cli.{code}.label"]
        name_en, name_ja = AREA_NAMES.get(code, (label.text_en.removeprefix("the "), label.text_ja))
        row = {"code": code, "name_en": name_en, "name_ja": name_ja,
               "cli_chg": cli.value, "cli_decimals": cli.decimals,
               "rate_chg": rate.value if rate else None, "rate_decimals": rate.decimals if rate else None,
               "phase": None, "reason": None}
        if rate is None:
            row["reason"] = "no_rate"
        else:
            key = (_sign(cli.value, cli.decimals), _sign(rate.value, rate.decimals))
            if 0 in key:
                row["reason"] = "unchanged"
            else:
                row["phase"] = PHASES[key]
        (placed if row["phase"] else unplaced).append(row)
    cells = {p: [r["code"] for r in placed if r["phase"] == p] for p in (1, 2, 3, 4)}
    return {"rows": placed + unplaced, "cells": cells, "as_of": sheet.period_end}


def _nice_step(span: float) -> float:
    for step in (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0):
        if span / step <= 4:
            return step
    return 10.0 ** math.ceil(math.log10(span))


def contribution_exhibit(sheet: FactSheet) -> dict:
    """Stacked contributions: each bar starts where the previous one ended."""
    facts = sheet.facts
    tickers = [fid.split(".")[2] for fid in facts if fid.startswith("fund.contrib.")]
    rows, start = [], 0.0
    for t in tickers:
        c = facts[f"fund.contrib.{t}"].value
        label = facts[f"etf.{t}.label"]
        name_en, name_ja = HOLDING_NAMES.get(t, (label.text_en, label.text_ja))
        rows.append({"ticker": t, "name_en": name_en, "name_ja": name_ja,
                     "weight": facts[f"etf.{t}.weight"].value, "ret": facts[f"etf.{t}.ret_1m"].value,
                     "start": start, "contrib": c, "end": start + c})
        start += c
    total = facts["fund.ret_1m"].value
    ends = [0.0, total] + [r["start"] for r in rows] + [r["end"] for r in rows]
    lo, hi = min(ends), max(ends)
    span = (hi - lo) or 1.0
    step = _nice_step(span)
    # Leave a little room past the longest bar so its end is visible.
    axis_hi = math.ceil((hi + span * 0.05) / (step / 5)) * (step / 5) if hi > 0 else 0.0
    axis_lo = math.floor((lo - span * 0.05) / (step / 5)) * (step / 5) if lo < 0 else 0.0
    ticks = [round(k * step, 10) for k in range(math.ceil(axis_lo / step), math.floor(axis_hi / step) + 1)]
    return {"rows": rows, "total": {"weight": sum(r["weight"] for r in rows), "ret": total},
            "axis": {"min": round(axis_lo, 10), "max": round(axis_hi, 10), "ticks": ticks},
            "as_of": sheet.period_end, "reconciles": abs(start - total) < 1e-9}


_WORST = {"pass": 0, "warn": 1, "fail": 2}


def _single_check_text(sheet: FactSheet, check) -> tuple[str, str]:
    """English and Japanese wording for a check that is not folded into a family."""
    holdings = sheet.facts.get("holdings.as_of")
    if check.name == "Holdings date matches the report month end" and holdings and check.status != "pass":
        return (f"Holdings are dated {holdings.text_en}; a sentence that cites holdings must state this date",
                f"組入銘柄は{holdings.text_ja}時点のデータのため、組入銘柄に関連する場合には日付を明記")
    known = {
        "Target weights add up to 100%": "目標配分の合計が100%であることを確認",
        "Contributions add up to the fund return": "要因分析の合計値がファンド騰落率に一致することを確認",
        "Holdings date matches the report month end": "組入銘柄の基準日が月末と一致することを確認",
    }
    return check.name, known.get(check.name, check.name)


def check_summary(sheet: FactSheet) -> dict:
    """The pre-drafting checks, with families of identical checks folded into one row."""
    families = {
        "nav": (lambda n: n.endswith(": published return vs NAV change"),
                "Published returns agree with month-end NAV changes, {n} ETFs",
                "公表騰落率と月末基準価額の変化を照合（ETF {n}本）"),
        "coverage": (lambda n: n.endswith("covers the report month"),
                     "OECD series cover the report month, {n} series",
                     "OECD系列が対象月を含むことを確認（{n}系列）"),
    }
    rows: list[dict] = []
    index: dict[str, dict] = {}
    for check in sheet.checks:
        family = next((k for k, (test, _, _) in families.items() if test(check.name)), None)
        if family is None:
            text_en, text_ja = _single_check_text(sheet, check)
            rows.append({"status": check.status, "text_en": text_en, "text_ja": text_ja,
                         "detail": check.detail, "members": [check.name]})
            continue
        row = index.get(family)
        if row is None:
            row = {"family": family, "status": "pass", "members": [], "detail": None}
            index[family] = row
            rows.append(row)
        row["members"].append(check.name)
        if _WORST[check.status] > _WORST[row["status"]]:
            row["status"] = check.status
    for family, row in index.items():
        _, en, ja = families[family]
        row["text_en"], row["text_ja"] = en.format(n=len(row["members"])), ja.format(n=len(row["members"]))
    counts = {s: sum(1 for c in sheet.checks if c.status == s) for s in ("pass", "warn", "fail")}
    return {"rows": rows, "counts": counts, "total": len(sheet.checks)}


def source_list(sheet: FactSheet) -> dict:
    rows = []
    for s in sheet.sources:
        terms = s["terms"]
        rows.append({"key": s["key"], "publisher": s["publisher"],
                     "title": s["title"].replace(" (public download)", ""),
                     "terms_short": terms if terms.startswith("CC BY") else "public disclosure",
                     "url": s["url"] if "{" not in s["url"] else s["url"].split("/")[0] + "//" + s["url"].split("/")[2],
                     "citation": s.get("citation")})
    # One row per publisher and terms (the three SPDR files become one row, as in the design specimen).
    folded: list[dict] = []
    for row in rows:
        same = next((f for f in folded if f["publisher"] == row["publisher"] and f["terms_short"] == row["terms_short"]
                     and not f["citation"] and not row["citation"]), None)
        if same is None:
            folded.append({**row, "keys": [row["key"]], "titles": [row["title"]]})
        else:
            same["keys"].append(row["key"])
            same["titles"].append(row["title"])
    for f in folded:
        f["title"] = _join_titles(f.pop("titles"))
    dates = sorted({dt.datetime.fromisoformat(s["retrieved_at"]).date().isoformat() for s in sheet.sources})
    return {"rows": folded, "retrieved": dates}


def _join_titles(titles: list[str]) -> str:
    """'SPDR ETF product data', 'SPDR ETF daily NAV history' -> 'SPDR ETF product data, daily NAV history'."""
    if len(titles) == 1:
        return titles[0]
    first = titles[0]
    words = first.split()
    prefix = ""
    for k in range(len(words), 0, -1):
        cand = " ".join(words[:k]) + " "
        if all(t.startswith(cand) for t in titles):
            prefix = cand
            break
    return first + "".join(", " + t[len(prefix):] for t in titles[1:])


def build_exhibits(sheet: FactSheet) -> dict:
    return {"cycle": cycle_exhibit(sheet), "contribution": contribution_exhibit(sheet),
            "checks": check_summary(sheet), "sources": source_list(sheet)}

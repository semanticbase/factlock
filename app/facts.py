"""Turn a public-data snapshot into a FactSheet.

Every number the commentary may use is computed here, deterministically, and
carries its source, as-of date and derivation. The language model never
computes anything; it can only refer to these facts by ID.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import pathlib

from app.models import DataCheck, Fact, FactSheet

MONTHS_EN = [calendar.month_name[i] for i in range(13)]


def _month_end(period: str) -> str:
    year, mon = (int(x) for x in period.split("-")[:2])
    return dt.date(year, mon, calendar.monthrange(year, mon)[1]).isoformat()


def _date_en(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{MONTHS_EN[d.month]} {d.day}, {d.year}"


def _date_ja(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.year}年{d.month}月{d.day}日"


def _month_en(period: str) -> str:
    year, mon = (int(x) for x in period.split("-")[:2])
    return f"{MONTHS_EN[mon]} {year}"


def _month_ja(period: str) -> str:
    year, mon = (int(x) for x in period.split("-")[:2])
    return f"{year}年{mon}月"


def cli_phase(level: float, change: float) -> tuple[str, str]:
    """Business-cycle phase from the OECD reading of an amplitude-adjusted CLI:
    level versus the long-term trend (100) and the month-on-month direction."""
    rising = change > 0
    if level >= 100 and rising:
        return "in an expansion phase (above trend and rising)", "拡大局面（趨勢を上回り上昇）"
    if level >= 100:
        return "in a downturn phase (above trend and falling)", "減速局面（趨勢を上回り低下）"
    if not rising:
        return "in a slowdown phase (below trend and falling)", "後退局面（趨勢を下回り低下）"
    return "in a recovery phase (below trend and rising)", "回復局面（趨勢を下回り上昇）"


def streak(values: list[float]) -> tuple[int, int, bool]:
    """Latest run of same-direction monthly changes.

    Returns (length, sign, complete). sign is +1 rising, -1 falling, 0 flat.
    complete is False when the run reaches the start of the data window: the
    run may then be longer than the data shows, so only "in each of the last
    N months" can be said, not "for N consecutive months".
    """
    changes = [b - a for a, b in zip(values, values[1:])]
    if not changes:
        return 0, 0, True
    sign = (changes[-1] > 0) - (changes[-1] < 0)
    count = 0
    for c in reversed(changes):
        if sign != 0 and ((c > 0) - (c < 0)) == sign:
            count += 1
        else:
            break
    return count, sign, count < len(changes)


def build_fact_sheet(snapshot: dict) -> FactSheet:
    fund = snapshot["fund"]
    month, period_end = snapshot["month"], snapshot["period_end"]
    facts: dict[str, Fact] = {}
    checks: list[DataCheck] = []

    def add(**kwargs) -> Fact:
        fact = Fact(**kwargs)
        facts[fact.id] = fact
        return fact

    # --- Report period and fund --------------------------------------------
    add(id="report.month", unit="text", text_en=_month_en(month), text_ja=_month_ja(month),
        as_of=period_end, label_en="Report month", label_ja="対象月", source="report")
    add(id="report.period_end", unit="text", text_en=_date_en(period_end), text_ja=_date_ja(period_end),
        as_of=period_end, label_en="Month-end date", label_ja="月末日", source="report")
    add(id="fund.name", unit="text", text_en=fund["name_en"], text_ja=fund["name_ja"],
        as_of=period_end, label_en="Fund name", label_ja="ファンド名", source="fund_definition")

    # --- Components: weights and issuer-published total returns -------------
    weight_sum = 0.0
    contributions: list[tuple[str, float]] = []
    for comp in fund["components"]:
        ticker = comp["ticker"]
        etf = snapshot["etfs"][ticker]
        weight_pct = comp["weight"] * 100
        weight_sum += weight_pct
        add(id=f"etf.{ticker}.label", unit="text",
                    text_en=f"{comp['label_en']} ({ticker})", text_ja=f"{comp['label_ja']}（{ticker}）",
                    as_of=period_end, label_en=f"{ticker} asset class", label_ja=f"{ticker}の資産クラス",
                    source="fund_definition")
        add(id=f"etf.{ticker}.weight", unit="weight", value=weight_pct, decimals=1, as_of=period_end,
            label_en=f"Target weight of {ticker}", label_ja=f"{ticker}の目標配分", source="fund_definition")
        ret = add(id=f"etf.{ticker}.ret_1m", unit="pct", value=etf["tr_1m"], as_of=etf["returns_as_of"],
                  label_en=f"{ticker} total return, 1 month", label_ja=f"{ticker}の1か月トータルリターン",
                  source="ssga_product", derivation="Issuer-published NAV total return (distributions reinvested)")
        contrib = comp["weight"] * etf["tr_1m"]
        contributions.append((ticker, contrib))
        add(id=f"fund.contrib.{ticker}", unit="contrib", value=contrib, as_of=period_end,
            label_en=f"Contribution of {ticker}", label_ja=f"{ticker}の寄与度", source="derived",
            derivation="target weight × 1-month total return", inputs=[f"etf.{ticker}.weight", ret.id])
        # Cross-check the issuer's figure against the NAV history.
        nav0, nav1 = etf["nav_prev_month_end"]["nav"], etf["nav_month_end"]["nav"]
        nav_change = (nav1 / nav0 - 1) * 100
        gap = etf["tr_1m"] - nav_change
        if etf["distribution_frequency"].lower() == "monthly":
            checks.append(DataCheck(
                name=f"{ticker}: published return vs NAV change", status="pass",
                detail=f"Published {etf['tr_1m']:+.2f}% vs NAV change {nav_change:+.4f}%; "
                       f"the {gap:+.4f} pt gap reflects the monthly distribution."))
        else:
            status = "pass" if abs(gap) <= 0.01 else "warn"
            checks.append(DataCheck(
                name=f"{ticker}: published return vs NAV change", status=status,
                detail=f"Published {etf['tr_1m']:+.2f}% vs NAV change {nav_change:+.4f}% "
                       f"(gap {gap:+.4f} pt; tolerance 0.01 pt when no distribution is expected)."))

    checks.insert(0, DataCheck(
        name="Target weights add up to 100%", status="pass" if abs(weight_sum - 100) < 1e-9 else "fail",
        detail=f"Sum of target weights = {weight_sum:.1f}%"))

    fund_ret = sum(c for _, c in contributions)
    add(id="fund.ret_1m", unit="pct", value=fund_ret, as_of=period_end,
        label_en="Fund return, 1 month (hypothetical, gross)", label_ja="ファンドの1か月騰落率（仮想・費用控除前）",
        source="derived", derivation="Σ target weight × component 1-month total return",
        inputs=[f"fund.contrib.{t}" for t, _ in contributions])
    checks.append(DataCheck(
        name="Contributions add up to the fund return",
        status="pass" if abs(sum(c for _, c in contributions) - fund_ret) < 1e-9 else "fail",
        detail=f"Σ contributions = {sum(c for _, c in contributions):.6f} pt; fund return = {fund_ret:.6f}%"))

    ranked = sorted(contributions, key=lambda item: item[1], reverse=True)
    top_t, top_c = ranked[0]
    bottom_t, bottom_c = ranked[-1]
    add(id="fund.top_contributor", unit="text", text_en=facts[f"etf.{top_t}.label"].text_en,
        text_ja=facts[f"etf.{top_t}.label"].text_ja, as_of=period_end,
        label_en="Largest contributor", label_ja="最大の寄与資産", source="derived",
        derivation="argmax of contributions", inputs=[f"fund.contrib.{t}" for t, _ in contributions])
    add(id="fund.top_contributor_value", unit="contrib", value=top_c, as_of=period_end,
        label_en="Largest contribution", label_ja="最大の寄与度", source="derived",
        inputs=[f"fund.contrib.{top_t}"], derivation="contribution of the largest contributor")
    add(id="fund.smallest_contributor", unit="text", text_en=facts[f"etf.{bottom_t}.label"].text_en,
        text_ja=facts[f"etf.{bottom_t}.label"].text_ja, as_of=period_end,
        label_en="Smallest contributor", label_ja="寄与が最も小さい資産", source="derived",
        derivation="argmin of contributions", inputs=[f"fund.contrib.{t}" for t, _ in contributions])
    add(id="fund.smallest_contributor_value", unit="contrib", value=bottom_c, as_of=period_end,
        label_en="Smallest contribution", label_ja="最小の寄与度", source="derived",
        inputs=[f"fund.contrib.{bottom_t}"], derivation="contribution of the smallest contributor")
    negatives = [t for t, c in contributions if c < 0]
    if not negatives:
        det_en, det_ja = "no holding detracted from performance", "マイナス寄与となった資産はありませんでした"
    else:
        names_en = ", ".join(facts[f"etf.{t}.label"].text_en for t in negatives)
        names_ja = "、".join(facts[f"etf.{t}.label"].text_ja for t in negatives)
        det_en = f"{len(negatives)} holding(s) detracted from performance: {names_en}"
        det_ja = f"マイナス寄与となった資産は{len(negatives)}資産（{names_ja}）でした"
    add(id="fund.detractors", unit="text", text_en=det_en, text_ja=det_ja, as_of=period_end,
        label_en="Detractors", label_ja="マイナス寄与の資産", source="derived",
        derivation="components with a negative contribution",
        inputs=[f"fund.contrib.{t}" for t, _ in contributions])

    # --- Leading indicators (OECD) -------------------------------------------
    for code, entry in snapshot["macro"]["cli"].items():
        series = entry["series"]
        if len(series) < 2:
            checks.append(DataCheck(name=f"OECD leading indicator for {code}", status="warn",
                                    detail="Not enough observations; indicator skipped."))
            continue
        (p_prev, v_prev), (p_last, v_last) = series[-2], series[-1]
        as_of = _month_end(p_last)
        base = f"cli.{code}"
        add(id=f"{base}.label", unit="text", text_en=entry["label_en"], text_ja=entry["label_ja"],
            as_of=as_of, label_en=f"Area name ({code})", label_ja=f"地域名（{code}）", source="oecd_cli")
        add(id=f"{base}.level", unit="index", value=v_last, as_of=as_of,
            label_en=f"OECD leading indicator level, {code}", label_ja=f"OECD景気先行指数の水準（{code}）",
            source="oecd_cli", derivation="Amplitude-adjusted CLI; long-term trend = 100")
        add(id=f"{base}.chg_1m", unit="pt", value=v_last - v_prev, as_of=as_of, down_word_ja="低下",
            label_en=f"Monthly change in the leading indicator, {code}",
            label_ja=f"景気先行指数の前月差（{code}）", source="derived",
            derivation=f"level {p_last} − level {p_prev}", inputs=[f"{base}.level"])
        above = v_last >= 100
        add(id=f"{base}.trend", unit="text",
            text_en="above its long-term trend level of 100" if above else "below its long-term trend level of 100",
            text_ja="長期的な趨勢水準（100）を上回る水準" if above else "長期的な趨勢水準（100）を下回る水準",
            as_of=as_of, label_en=f"Level versus trend, {code}", label_ja=f"趨勢水準との比較（{code}）",
            source="derived", derivation="level compared with 100", inputs=[f"{base}.level"])
        phase_en, phase_ja = cli_phase(v_last, v_last - v_prev)
        add(id=f"{base}.phase", unit="text", text_en=phase_en, text_ja=phase_ja, as_of=as_of,
            label_en=f"Cycle phase (OECD reading), {code}", label_ja=f"景気局面（OECDの読み方）（{code}）",
            source="derived", derivation="level vs 100 and month-on-month direction",
            inputs=[f"{base}.level", f"{base}.chg_1m"])
        n, sign, complete = streak([v for _, v in series])
        months_en = f"{n} month{'s' if n != 1 else ''}"
        if sign == 0:
            s_en, s_ja = "was unchanged from the previous month", "前月から横ばい"
        elif complete and n == 1:
            s_en, s_ja = (("turned up in the latest month", "直近1か月で上昇に転換") if sign > 0
                          else ("turned down in the latest month", "直近1か月で低下に転換"))
        elif complete:
            word_en, word_ja = ("risen", "上昇") if sign > 0 else ("fallen", "低下")
            # Japanese as a noun phrase: a sentence-final 「〜しています」 breaks when the writer continues
            # the sentence (5 of 40 drafts in the 2026-09-24 batch, all from this phrase).
            s_en, s_ja = f"has {word_en} for {n} consecutive months", f"{n}か月連続の{word_ja}"
        else:
            word_en, word_ja = ("risen", "上昇") if sign > 0 else ("fallen", "低下")
            s_en = f"has {word_en} in each of the last {months_en}"
            s_ja = f"直近{n}か月にわたる{word_ja}"
        # State which months the run covers, so the margin note can be checked against the data.
        if sign == 0:
            run_how = "consecutive same-direction monthly changes"
        else:
            run_how = (f"{n} {'rises' if sign > 0 else 'falls'} in a row, changes {series[-n][0]} to {p_last}; "
                       f"data from {series[0][0]}")
        add(id=f"{base}.streak", unit="text", text_en=s_en, text_ja=s_ja, as_of=as_of,
            label_en=f"Run of monthly changes, {code}", label_ja=f"連続変化の月数（{code}）",
            source="derived", derivation=run_how,
            inputs=[f"{base}.level"])
        status = "pass" if p_last == month else "warn"
        checks.append(DataCheck(name=f"OECD leading indicator for {code} covers the report month",
                                status=status, detail=f"Latest observation: {p_last}"))

    # --- Long-term interest rates (OECD, monthly averages) -----------------
    for code, entry in snapshot["macro"]["lt_rate"].items():
        series = entry["series"]
        if len(series) < 2:
            checks.append(DataCheck(name=f"Long-term interest rate for {code}", status="warn",
                                    detail="Not enough observations; rate skipped."))
            continue
        (p_prev, v_prev), (p_last, v_last) = series[-2], series[-1]
        as_of = _month_end(p_last)
        base = f"rate.{code}"
        add(id=f"{base}.label", unit="text",
            text_en=f"the {entry['label_en']} long-term government bond yield (monthly average)",
            text_ja=f"{entry['label_ja']}の長期金利（月平均）", as_of=as_of,
            label_en=f"Rate name ({code})", label_ja=f"金利の名称（{code}）", source="oecd_finmark")
        add(id=f"{base}.level", unit="pct_pa", value=v_last, as_of=as_of,
            label_en=f"Long-term interest rate, {code} (monthly average)",
            label_ja=f"長期金利（{code}・月平均）", source="oecd_finmark")
        add(id=f"{base}.chg_1m", unit="pp", value=round(v_last - v_prev, 10), as_of=as_of, down_word_ja="低下",
            label_en=f"Monthly change in the long-term interest rate, {code}",
            label_ja=f"長期金利の前月差（{code}）", source="derived",
            derivation=f"average {p_last} − average {p_prev}", inputs=[f"{base}.level"])
        checks.append(DataCheck(name=f"Long-term interest rate for {code} covers the report month",
                                status="pass" if p_last == month else "warn",
                                detail=f"Latest observation: {p_last}"))

    # --- Look-through holdings (equity sleeves; latest disclosure) ----------
    combined: dict[str, dict] = {}
    holdings_dates = set()
    for comp in fund["components"]:
        holdings = snapshot["etfs"][comp["ticker"]].get("holdings")
        if not holdings:
            continue
        holdings_dates.add(holdings["as_of"])
        for h in holdings["top"]:
            key = h["identifier"] or h["name"]
            row = combined.setdefault(key, {"name": h["name"], "weight": 0.0, "via": []})
            row["weight"] += comp["weight"] * h["weight"]
            row["via"].append(comp["ticker"])
    if combined:
        h_asof = max(holdings_dates)
        add(id="holdings.as_of", unit="text", text_en=_date_en(h_asof), text_ja=_date_ja(h_asof),
            as_of=h_asof, discloses_as_of=h_asof, label_en="Holdings disclosure date",
            label_ja="組入銘柄の基準日", source="ssga_holdings")
        top = sorted(combined.values(), key=lambda r: r["weight"], reverse=True)[:5]
        for i, row in enumerate(top, start=1):
            add(id=f"lookthrough.{i}.name", unit="text", text_en=row["name"], text_ja=row["name"],
                as_of=h_asof, label_en=f"Look-through holding #{i}", label_ja=f"ルックスルー組入 第{i}位",
                source="ssga_holdings", derivation="Σ fund weight × weight within ETF, largest holdings only",
                inputs=[f"etf.{t}.weight" for t in row["via"]])
            add(id=f"lookthrough.{i}.weight", unit="weight", value=row["weight"], as_of=h_asof,
                label_en=f"Look-through weight of holding #{i}", label_ja=f"ルックスルー比率 第{i}位",
                source="ssga_holdings", derivation="Σ fund weight × weight within ETF",
                inputs=[f"etf.{t}.weight" for t in row["via"]])
        status = "pass" if h_asof == period_end else "warn"
        checks.append(DataCheck(
            name="Holdings date matches the report month end", status=status,
            detail=f"Holdings are as of {h_asof}; the commentary must state this date when citing holdings."
            if status == "warn" else f"Holdings are as of {h_asof}."))

    return FactSheet(
        fund_id=fund["id"], fund_name_en=fund["name_en"], fund_name_ja=fund["name_ja"],
        fund_note_en=fund["note_en"], fund_note_ja=fund["note_ja"], month=month,
        period_end=period_end, facts=facts, checks=checks, sources=snapshot["sources"])


def load_snapshot(data_dir: pathlib.Path, fund_id: str, month: str) -> dict:
    path = data_dir / f"{fund_id.lower()}_{month}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def available_reports(data_dir: pathlib.Path) -> list[dict]:
    reports = []
    for path in sorted(data_dir.glob("*_*.json")):
        fund_id, month = path.stem.rsplit("_", 1)
        reports.append({"fund_id": fund_id.upper(), "month": month})
    return reports

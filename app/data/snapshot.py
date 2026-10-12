"""Build a monthly snapshot of *derived* values from public sources.

The snapshot stores only the small set of values FactLock needs (month-end
returns, NAVs for cross-checks, top holdings, leading indicators, long-term
interest rates) together with provenance. Original files are cached locally
for audit but are not part of the repository.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import json
import pathlib

from app.data import sources

SCHEMA = "factlock.snapshot.v1"
TOP_HOLDINGS = 15
# 36 months back = 37 monthly observations. From 2020 to 2026 the longest run of same-direction
# monthly changes was 25 months (G7), so a run normally ends inside the window and can be stated
# as "N consecutive months". A run that reaches the window start is still worded "in each of the
# last N months" (see app.facts.streak).
CLI_MONTHS_BACK = 36
RATE_MONTHS_BACK = 3


def month_end(month: str) -> dt.date:
    year, mon = (int(x) for x in month.split("-"))
    return dt.date(year, mon, calendar.monthrange(year, mon)[1])


def shift_month(month: str, delta: int) -> str:
    year, mon = (int(x) for x in month.split("-"))
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def last_on_or_before(series: dict, day: dt.date) -> tuple[dt.date, float]:
    candidates = [d for d in series if d <= day]
    if not candidates:
        raise ValueError(f"No observation on or before {day}")
    best = max(candidates)
    return best, series[best]


def build(month: str, fund_path: pathlib.Path, cache_dir: pathlib.Path) -> dict:
    fund = json.loads(fund_path.read_text(encoding="utf-8"))
    period_end = month_end(month)
    prev_end = month_end(shift_month(month, -1))
    sources_used: list[dict] = []

    # --- ETF data published by the issuer --------------------------------
    product_fetch, products = sources.fetch_ssga_product_data(cache_dir)
    sources_used.append({
        "key": "ssga_product", "publisher": "State Street Global Advisors",
        "title": "SPDR ETF product data (public download)", "url": product_fetch.url,
        "retrieved_at": product_fetch.retrieved_at,
        "terms": "Public fund disclosure; use subject to the issuer's website terms. Returns assume reinvestment of distributions.",
    })
    etfs = {}
    nav_fetch_meta, hold_fetch_meta = [], []
    for comp in fund["components"]:
        ticker = comp["ticker"]
        product = products.get(ticker)
        if product is None:
            raise ValueError(f"{ticker} not found in the issuer product data")
        if product["returns_as_of"] != period_end.isoformat():
            raise ValueError(
                f"{ticker}: issuer returns are as of {product['returns_as_of']}, "
                f"not {period_end.isoformat()} (month not yet published?)")
        nav_fetch, navs = sources.fetch_ssga_nav_history(ticker, cache_dir)
        nav_fetch_meta.append(nav_fetch)
        d0, nav0 = last_on_or_before(navs, prev_end)
        d1, nav1 = last_on_or_before(navs, period_end)
        entry = {
            "official_name": product["official_name"],
            "asset_class": product["asset_class"],
            "distribution_frequency": product["distribution_frequency"],
            "returns_as_of": product["returns_as_of"],
            "tr_1m": product["tr_1m"],
            "tr_ytd": product["tr_ytd"],
            "nav_prev_month_end": {"date": d0.isoformat(), "nav": nav0},
            "nav_month_end": {"date": d1.isoformat(), "nav": nav1},
        }
        if comp["sleeve"] == "equity":
            hold_fetch, holdings = sources.fetch_ssga_holdings(ticker, cache_dir, TOP_HOLDINGS)
            hold_fetch_meta.append(hold_fetch)
            entry["holdings"] = holdings
        etfs[ticker] = entry
    sources_used.append({
        "key": "ssga_navhist", "publisher": "State Street Global Advisors",
        "title": "SPDR ETF daily NAV history (public download)",
        "url": nav_fetch_meta[0].url.rsplit("/", 1)[0] + "/navhist-us-en-{ticker}.xlsx",
        "retrieved_at": nav_fetch_meta[-1].retrieved_at,
        "terms": "Public fund disclosure; used only for cross-checking month-end values.",
    })
    if hold_fetch_meta:
        sources_used.append({
            "key": "ssga_holdings", "publisher": "State Street Global Advisors",
            "title": "SPDR ETF daily holdings (public download); top holdings only",
            "url": hold_fetch_meta[0].url.rsplit("/", 1)[0] + "/holdings-daily-us-en-{ticker}.xlsx",
            "retrieved_at": hold_fetch_meta[-1].retrieved_at,
            "terms": "Public fund disclosure; only the largest holdings are stored.",
        })

    # --- OECD statistics (CC BY 4.0) --------------------------------------
    cli_codes = [a["code"] for a in fund["macro"]["cli_areas"]]
    cli_fetch, cli = sources.fetch_oecd_series(
        "OECD.SDD.STES,DSD_STES@DF_CLI,", "+".join(cli_codes) + ".M.LI...AA...H",
        shift_month(month, -CLI_MONTHS_BACK), cache_dir, "oecd_cli.csv")
    rate_codes = [a["code"] for a in fund["macro"]["rate_areas"]]
    rate_fetch, rates = sources.fetch_oecd_series(
        "OECD.SDD.STES,DSD_STES@DF_FINMARK,", "+".join(rate_codes) + ".M.IRLT.PA.....",
        shift_month(month, -RATE_MONTHS_BACK), cache_dir, "oecd_irlt.csv")
    accessed = dt.date.fromisoformat(cli_fetch.retrieved_at[:10])
    citation_date = f"{accessed.day} {accessed.strftime('%B %Y')}"
    sources_used.append({
        "key": "oecd_cli", "publisher": "OECD",
        "title": "Composite Leading Indicators (amplitude adjusted)",
        "url": cli_fetch.url, "retrieved_at": cli_fetch.retrieved_at, "terms": "CC BY 4.0",
        "citation": f"OECD (2026), Composite leading indicators, OECD Data Explorer (accessed on {citation_date}).",
    })
    sources_used.append({
        "key": "oecd_finmark", "publisher": "OECD",
        "title": "Financial market indicators: long-term interest rates (monthly averages)",
        "url": rate_fetch.url, "retrieved_at": rate_fetch.retrieved_at, "terms": "CC BY 4.0",
        "citation": f"OECD (2026), Financial market indicators, OECD Data Explorer (accessed on {citation_date}).",
    })

    def trim(series: dict, code: str) -> list:
        return [[p, v] for p, v in series.get(code, []) if p <= month]

    macro = {
        "cli": {a["code"]: {"label_en": a["label_en"], "label_ja": a["label_ja"],
                            "series": trim(cli, a["code"])} for a in fund["macro"]["cli_areas"]},
        "lt_rate": {a["code"]: {"label_en": a["label_en"], "label_ja": a["label_ja"],
                                "series": trim(rates, a["code"])} for a in fund["macro"]["rate_areas"]},
    }
    return {
        "schema": SCHEMA,
        "month": month,
        "period_end": period_end.isoformat(),
        "built_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "fund": fund,
        "etfs": etfs,
        "macro": macro,
        "sources": sources_used,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a FactLock public-data snapshot")
    parser.add_argument("--month", required=True, help="Report month, e.g. 2026-08")
    parser.add_argument("--fund", default="data/funds/dgbf.json")
    parser.add_argument("--out", default="data/snapshots")
    parser.add_argument("--cache", default=".cache/raw")
    parser.add_argument("--reuse-cache", action="store_true",
                        help="Reuse raw files saved by a previous run instead of downloading again")
    args = parser.parse_args()
    sources.REUSE_CACHE = args.reuse_cache
    snapshot = build(args.month, pathlib.Path(args.fund), pathlib.Path(args.cache) / args.month)
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{snapshot['fund']['id'].lower()}_{args.month}.json"
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Snapshot written: {out_path}")


if __name__ == "__main__":
    main()

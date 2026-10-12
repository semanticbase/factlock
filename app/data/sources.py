"""Fetchers for public data, obtained directly from the original publishers.

FactLock uses only publicly disclosed data:
  * OECD statistics via the OECD SDMX API (licensed under CC BY 4.0).
  * ETF fund data published by the issuer (State Street Global Advisors,
    SPDR ETFs) on its public website.

Every fetch returns the raw bytes together with provenance (URL and
retrieval time). Raw files are saved to a local cache *before* any parsing,
so that a parsing problem never forces a second download.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import urllib.request
from dataclasses import dataclass

import openpyxl

USER_AGENT = "FactLock/0.1 (public data snapshot builder)"
OECD_BASE = "https://sdmx.oecd.org/public/rest/data"
SSGA_BASE = (
    "https://www.ssga.com/us/en/intermediary/library-content/products/"
    "fund-data/etfs/us"
)


@dataclass
class Fetched:
    url: str
    retrieved_at: str  # ISO 8601, UTC
    content: bytes


REUSE_CACHE = False  # set by the snapshot CLI (--reuse-cache)


def fetch(url: str, cache_dir: pathlib.Path, cache_name: str,
          accept: str | None = None, timeout: int = 60) -> Fetched:
    """Download a URL and store the raw bytes in the cache directory.

    With REUSE_CACHE enabled, a previously saved raw file for the same URL is
    reused instead of downloading again.
    """
    raw_path, meta_path = cache_dir / cache_name, cache_dir / (cache_name + ".meta.json")
    if REUSE_CACHE and raw_path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if meta.get("url") == url:
            return Fetched(url=url, retrieved_at=meta["retrieved_at"], content=raw_path.read_bytes())
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = resp.read()
    retrieved_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / cache_name).write_bytes(content)
    (cache_dir / (cache_name + ".meta.json")).write_text(
        json.dumps({"url": url, "retrieved_at": retrieved_at}, indent=2))
    return Fetched(url=url, retrieved_at=retrieved_at, content=content)


# ---------------------------------------------------------------- OECD ----

def fetch_oecd_series(dataflow: str, key: str, start_period: str,
                      cache_dir: pathlib.Path, cache_name: str) -> tuple[Fetched, dict]:
    """Fetch an OECD SDMX dataflow as CSV.

    Returns the raw fetch and a mapping {ref_area: [(period, value), ...]}
    sorted by period.
    """
    url = (f"{OECD_BASE}/{dataflow}/{key}?startPeriod={start_period}"
           "&dimensionAtObservation=AllDimensions")
    fetched = fetch(url, cache_dir, cache_name,
                    accept="application/vnd.sdmx.data+csv; charset=utf-8")
    text = fetched.content.decode("utf-8-sig")
    series: dict[str, list[tuple[str, float]]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        value = row.get("OBS_VALUE", "").strip()
        if not value:
            continue
        series.setdefault(row["REF_AREA"], []).append((row["TIME_PERIOD"], float(value)))
    for area in series:
        series[area].sort(key=lambda item: item[0])
    return fetched, series


# ---------------------------------------------------------------- SSGA ----

def _load_sheet_rows(content: bytes) -> list[tuple]:
    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        return list(workbook.worksheets[0].iter_rows(values_only=True))
    finally:
        workbook.close()


def _parse_pct(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    if text in ("", "-"):
        return None
    return float(text)


def _parse_date(value, fmt: str = "%b %d %Y") -> str | None:
    text = str(value).strip() if value is not None else ""
    if text in ("", "-"):
        return None
    return dt.datetime.strptime(text, fmt).date().isoformat()


def fetch_ssga_product_data(cache_dir: pathlib.Path) -> tuple[Fetched, dict]:
    """Fetch the SPDR product data file (all SPDR ETFs, one row each).

    Column meanings were verified against the file's two header rows:
    col 30 'Total Returns as of Date'; cols 31-33 cumulative 1 Month / QTD /
    YTD. The file notes that returns assume reinvestment of distributions.
    """
    url = f"{SSGA_BASE}/spdr-product-data-us-en.xlsx"
    fetched = fetch(url, cache_dir, "ssga_product_data.xlsx")
    rows = _load_sheet_rows(fetched.content)
    header_top, header_sub = rows[1], rows[2]
    expected = {1: "Ticker", 2: "Name", 30: "Total Returns as of Date"}
    for col, label in expected.items():
        if str(header_top[col]).strip() != label:
            raise ValueError(f"Unexpected product data header at column {col}: {header_top[col]!r}")
    if [str(header_sub[c]).strip() for c in (31, 32, 33)] != ["1 Month", "QTD", "YTD"]:
        raise ValueError("Unexpected product data sub-header for total returns")
    products = {}
    for row in rows[3:]:
        if not row or not row[1]:
            continue
        ticker = str(row[1]).replace("®", "").strip()
        products[ticker] = {
            "official_name": str(row[2]).strip(),
            "asset_class": str(row[8]).strip(),
            "distribution_frequency": str(row[9]).strip(),
            "returns_as_of": _parse_date(row[30]),
            "tr_1m": _parse_pct(row[31]),
            "tr_qtd": _parse_pct(row[32]),
            "tr_ytd": _parse_pct(row[33]),
        }
    return fetched, products


def fetch_ssga_nav_history(ticker: str, cache_dir: pathlib.Path) -> tuple[Fetched, dict]:
    """Fetch the daily NAV history for one SPDR ETF. Returns {date: nav}."""
    url = f"{SSGA_BASE}/navhist-us-en-{ticker.lower()}.xlsx"
    fetched = fetch(url, cache_dir, f"ssga_navhist_{ticker.lower()}.xlsx")
    navs = {}
    for row in _load_sheet_rows(fetched.content):
        if not row or not isinstance(row[0], str) or not isinstance(row[1], (int, float)):
            continue
        try:
            day = dt.datetime.strptime(row[0].strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        navs[day] = float(row[1])
    return fetched, navs


def fetch_ssga_holdings(ticker: str, cache_dir: pathlib.Path, top_n: int) -> tuple[Fetched, dict]:
    """Fetch the daily holdings file and keep only the top N holdings."""
    url = f"{SSGA_BASE}/holdings-daily-us-en-{ticker.lower()}.xlsx"
    fetched = fetch(url, cache_dir, f"ssga_holdings_{ticker.lower()}.xlsx")
    rows = _load_sheet_rows(fetched.content)
    as_of = None
    header_index = None
    for i, row in enumerate(rows):
        if row and row[0] == "Holdings:" and row[1]:
            as_of = dt.datetime.strptime(str(row[1]).replace("As of", "").strip(), "%d-%b-%Y").date()
        if row and row[0] == "Name":
            header_index = i
            break
    if header_index is None or as_of is None:
        raise ValueError(f"Holdings file for {ticker} has an unexpected layout")
    header = [str(c).strip() if c is not None else "" for c in rows[header_index]]
    col = {name: header.index(name) for name in ("Name", "Ticker", "Identifier", "Weight")}
    holdings = []
    for row in rows[header_index + 1:]:
        if not row or row[col["Name"]] is None or not isinstance(row[col["Weight"]], (int, float)):
            continue
        holdings.append({
            "name": str(row[col["Name"]]).strip(),
            "ticker": str(row[col["Ticker"]] or "").strip(),
            "identifier": str(row[col["Identifier"]] or "").strip(),
            "weight": float(row[col["Weight"]]),
        })
    holdings.sort(key=lambda h: h["weight"], reverse=True)
    return fetched, {"as_of": as_of.isoformat(), "count": len(holdings), "top": holdings[:top_n]}

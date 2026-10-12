"""M3, step 0 — the snapshot against the publishers' own files, downloaded again.

The snapshot records, for each source, the URL it was built from. This module downloads those
files again and reads them with its own parsers (written without looking at the product's fetch
code, and without importing anything from app/). It then compares every value the fact sheet
depends on: issuer-published 1-month and year-to-date total returns and their as-of date, the
two month-end NAVs used for the cross-check, every OECD leading-indicator observation in the
snapshot, and every long-term interest rate observation.

Two limits, stated in the output:
* Issuer files are overwritten when new data is published. The 1-month return in the product
  file changes at the next month end, so this comparison is only possible until then.
* Daily holdings files are replaced every day; holdings as of an earlier date cannot be
  downloaded again and are not compared.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import pathlib
import re
import urllib.error
import urllib.request

USER_AGENT = "Mozilla/5.0 (FactLock measurement; public data check)"
SDMX_CSV = "application/vnd.sdmx.data+csv; charset=utf-8"
MONTH_ABBR = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def download(url: str, dest: pathlib.Path, accept: str | None = None, timeout: int = 90) -> dict:
    """Save the file exactly as served, with its md5 and retrieval time."""
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    record = {"url": url, "file": dest.name, "retrieved_at": _now()}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            record["http_status"] = response.status
    except urllib.error.HTTPError as exc:
        record.update(http_status=exc.code, error=str(exc))
        return record
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        record.update(http_status=None, error=f"{type(exc).__name__}: {exc}")
        return record
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    record.update(bytes=len(body), md5=hashlib.md5(body).hexdigest())
    return record


# ------------------------------------------------------------------------------- parsers
def _percent(cell) -> float | None:
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    text = str(cell).strip().replace(",", "")
    m = re.fullmatch(r"([+\-\u2212]?\d+(?:\.\d+)?)\s*%", text)
    if not m:
        return None
    return float(m.group(1).replace("\u2212", "-"))


def _date(cell) -> str | None:
    """'Aug 31 2026', '31-Aug-2026', a datetime, or an ISO string → 'YYYY-MM-DD'."""
    if cell is None:
        return None
    if isinstance(cell, dt.datetime):
        return cell.date().isoformat()
    if isinstance(cell, dt.date):
        return cell.isoformat()
    text = str(cell).strip()
    m = re.fullmatch(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})", text)
    if m and m.group(1) in MONTH_ABBR:
        return dt.date(int(m.group(3)), MONTH_ABBR[m.group(1)], int(m.group(2))).isoformat()
    m = re.fullmatch(r"(\d{1,2})-([A-Z][a-z]{2})-(\d{4})", text)
    if m and m.group(2) in MONTH_ABBR:
        return dt.date(int(m.group(3)), MONTH_ABBR[m.group(2)], int(m.group(1))).isoformat()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2}).*", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _clean_ticker(cell) -> str:
    return re.sub(r"[^A-Z0-9.]", "", str(cell or "").upper())


def _rows(xlsx_bytes: bytes) -> list[tuple]:
    import openpyxl  # listed in requirements.txt
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    try:
        return [tuple(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    finally:
        wb.close()


def parse_product_data(xlsx_bytes: bytes) -> dict[str, dict]:
    """Issuer product file → {ticker: {returns_as_of, tr_1m, tr_ytd}}.

    The header row holds 'Ticker' and 'Total Returns as of Date'; the row below it names the
    return periods ('1 Month', 'YTD'). Columns are located by these labels, not by position."""
    rows = _rows(xlsx_bytes)
    head = next((i for i, r in enumerate(rows) if any(str(c).strip() == "Ticker" for c in r if c is not None)), None)
    if head is None or head + 1 >= len(rows):
        raise ValueError("product file: header row with 'Ticker' not found")
    header, sub = rows[head], rows[head + 1]
    col = {str(c).strip(): j for j, c in enumerate(header) if c is not None}
    sub_col = {str(c).strip(): j for j, c in enumerate(sub) if c is not None}
    for needed in ("Ticker", "Total Returns as of Date"):
        if needed not in col:
            raise ValueError(f"product file: column '{needed}' not found")
    for needed in ("1 Month", "YTD"):
        if needed not in sub_col:
            raise ValueError(f"product file: return period '{needed}' not found")
    out = {}
    for r in rows[head + 2:]:
        if len(r) <= max(col["Ticker"], sub_col["1 Month"], sub_col["YTD"]):
            continue
        ticker = _clean_ticker(r[col["Ticker"]])
        if not ticker:
            continue
        out[ticker] = {"returns_as_of": _date(r[col["Total Returns as of Date"]]),
                       "tr_1m": _percent(r[sub_col["1 Month"]]), "tr_ytd": _percent(r[sub_col["YTD"]])}
    if not out:
        raise ValueError("product file: no fund rows found")
    return out


def parse_nav_history(xlsx_bytes: bytes) -> dict[str, float]:
    """Issuer NAV history → {YYYY-MM-DD: NAV}. The table starts under a 'Date' / 'NAV' header."""
    rows = _rows(xlsx_bytes)
    head = next((i for i, r in enumerate(rows)
                 if r and str(r[0]).strip() == "Date" and len(r) > 1 and str(r[1]).strip() == "NAV"), None)
    if head is None:
        raise ValueError("NAV history: 'Date' / 'NAV' header not found")
    out = {}
    for r in rows[head + 1:]:
        if not r or r[0] is None:
            continue
        day = _date(r[0])
        if day and isinstance(r[1], (int, float)):
            out[day] = float(r[1])
    if not out:
        raise ValueError("NAV history: no rows found")
    return out


def parse_sdmx_csv(text: str) -> dict[tuple[str, str], float]:
    """OECD SDMX-CSV → {(REF_AREA, TIME_PERIOD): OBS_VALUE}."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    for needed in ("REF_AREA", "TIME_PERIOD", "OBS_VALUE"):
        if needed not in (reader.fieldnames or []):
            raise ValueError(f"SDMX CSV: column {needed} not found")
    out = {}
    for row in reader:
        if row["OBS_VALUE"] in ("", None):
            continue
        out[(row["REF_AREA"], row["TIME_PERIOD"])] = float(row["OBS_VALUE"])
    return out


# ------------------------------------------------------------------------------- comparison
def _same(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def compare(snapshot: dict, product: dict | None, navs: dict[str, dict], cli: dict | None,
            rates: dict | None) -> list[dict]:
    rows: list[dict] = []

    def add(what, snap_value, pub_value, equal):
        rows.append({"item": what, "snapshot": snap_value, "publisher": pub_value, "equal": bool(equal)})

    for ticker, etf in snapshot["etfs"].items():
        p = (product or {}).get(ticker)
        if product is not None:
            add(f"{ticker} 1-month total return, %", etf["tr_1m"], p and p["tr_1m"], p and _same(etf["tr_1m"], p["tr_1m"]))
            add(f"{ticker} year-to-date total return, %", etf.get("tr_ytd"), p and p["tr_ytd"],
                p and _same(etf.get("tr_ytd"), p["tr_ytd"]))
            add(f"{ticker} returns as of", etf["returns_as_of"], p and p["returns_as_of"],
                p and etf["returns_as_of"] == p["returns_as_of"])
        hist = navs.get(ticker)
        if hist is not None:
            for key in ("nav_prev_month_end", "nav_month_end"):
                day, nav = etf[key]["date"], etf[key]["nav"]
                add(f"{ticker} NAV on {day}", nav, hist.get(day), _same(nav, hist.get(day)))
    for label, table, section in (("OECD leading indicator", cli, "cli"), ("Long-term interest rate", rates, "lt_rate")):
        if table is None:
            continue
        for area, entry in snapshot["macro"][section].items():
            for period, value in entry["series"]:
                pub = table.get((area, period))
                add(f"{label} {area} {period}", value, pub, _same(value, pub))
    return rows


def run(snapshot: dict, out_dir: pathlib.Path) -> dict:
    """Download, parse and compare. Never raises for a network or parsing problem: it records it."""
    raw_dir = out_dir / "raw"
    sources = {s["key"]: s for s in snapshot["sources"]}
    downloads, notes = [], []
    product = cli = rates = None
    navs: dict[str, dict] = {}

    def fetch(url, name, accept=None):
        rec = download(url, raw_dir / name, accept)
        downloads.append(rec)
        return (raw_dir / name).read_bytes() if rec.get("md5") else None

    if "ssga_product" in sources:
        body = fetch(sources["ssga_product"]["url"], "ssga_product.xlsx")
        if body:
            try:
                product = parse_product_data(body)
            except ValueError as exc:
                notes.append(f"product file not read: {exc}")
    if "ssga_navhist" in sources:
        for ticker in snapshot["etfs"]:
            url = sources["ssga_navhist"]["url"].replace("{ticker}", ticker.lower())
            body = fetch(url, f"ssga_navhist_{ticker.lower()}.xlsx")
            if body:
                try:
                    navs[ticker] = parse_nav_history(body)
                except ValueError as exc:
                    notes.append(f"NAV history for {ticker} not read: {exc}")
    for key, name in (("oecd_cli", "oecd_cli.csv"), ("oecd_finmark", "oecd_finmark.csv")):
        if key in sources:
            body = fetch(sources[key]["url"], name, SDMX_CSV)
            if body:
                try:
                    table = parse_sdmx_csv(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    notes.append(f"{name} not read: {exc}")
                    table = None
                if key == "oecd_cli":
                    cli = table
                else:
                    rates = table
    if "ssga_holdings" in sources:
        notes.append("Holdings are not compared: the issuer replaces the daily holdings file every day, so the "
                     f"file used for the snapshot ({snapshot['etfs'][next(iter(snapshot['etfs']))].get('holdings', {}).get('as_of', 'n/a')}) "
                     "cannot be downloaded again.")
    rows = compare(snapshot, product, navs, cli, rates)
    return {
        "checked_at": _now(),
        "snapshot_built_at": snapshot.get("built_at"),
        "downloads": downloads,
        "comparisons": rows,
        "compared": len(rows),
        "equal": sum(r["equal"] for r in rows),
        "different": [r for r in rows if not r["equal"]],
        "notes": notes,
    }


def compare_saved(snapshot: dict, raw_dir: pathlib.Path) -> list[dict]:
    """Repeat the comparison from files saved by run(), without the network (for audit and tests)."""
    product = parse_product_data((raw_dir / "ssga_product.xlsx").read_bytes())
    navs = {t: parse_nav_history((raw_dir / f"ssga_navhist_{t.lower()}.xlsx").read_bytes()) for t in snapshot["etfs"]}
    cli = parse_sdmx_csv((raw_dir / "oecd_cli.csv").read_text(encoding="utf-8"))
    rates = parse_sdmx_csv((raw_dir / "oecd_finmark.csv").read_text(encoding="utf-8"))
    return compare(snapshot, product, navs, cli, rates)

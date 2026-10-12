"""M15 — the checks run on the data before any drafting, with an independent recomputation of each."""
from __future__ import annotations

import math


def check_datachecks(result: dict, snapshot: dict) -> dict:
    sheet = result["fact_sheet"]
    etfs, comps = snapshot["etfs"], snapshot["fund"]["components"]
    rows = []
    for c in sheet["checks"]:
        name, status = c["name"], c["status"]
        row = {"name": name, "status": status, "detail": c["detail"]}
        if name == "Target weights add up to 100%":
            total = math.fsum(x["weight"] for x in comps) * 100
            row.update(kind="sum", recomputed=f"{total:.10f}%", agrees=(abs(total - 100) < 1e-9) == (status == "pass"))
        elif name.endswith(": published return vs NAV change"):
            t = name.split(":")[0]
            e = etfs[t]
            nav_chg = (e["nav_month_end"]["nav"] / e["nav_prev_month_end"]["nav"] - 1) * 100
            gap = e["tr_1m"] - nav_chg
            monthly = e["distribution_frequency"].lower() == "monthly"
            row.update(kind="reconciliation", nav_change_pct=round(nav_chg, 6), gap_pp=round(gap, 6),
                       gap_bp=round(gap * 100, 2), distribution_expected=monthly,
                       implied_distribution_per_share=round(gap / 100 * e["nav_prev_month_end"]["nav"], 4) if monthly else None,
                       agrees=(f"{nav_chg:+.4f}%" in c["detail"]))
        elif name == "Contributions add up to the fund return":
            row.update(kind="identity", agrees=True,
                       note="The fund return is defined as the sum of the contributions, so this check cannot fail. "
                            "It is not counted as a verification.")
        elif name.endswith("covers the report month"):
            area = name.split(" for ")[1].split(" covers")[0]
            section = "cli" if name.startswith("OECD leading indicator") else "lt_rate"
            last = snapshot["macro"][section][area]["series"][-1][0]
            row.update(kind="coverage", latest=last, agrees=((last == snapshot["month"]) == (status == "pass")))
        elif name == "Holdings date matches the report month end":
            dates = sorted({etfs[x["ticker"]]["holdings"]["as_of"] for x in comps if etfs[x["ticker"]].get("holdings")})
            row.update(kind="disclosure", holdings_as_of=dates,
                       agrees=((dates[-1] == snapshot["period_end"]) == (status == "pass")),
                       note="A warning by design: holdings are dated after the month end, so the rules require the "
                            "date in any sentence that cites them.")
        else:
            row.update(kind="unclassified", agrees=None)
        rows.append(row)
    counted = [r for r in rows if r["kind"] != "identity"]
    return {
        "checks": len(rows),
        "by_status": {s: sum(r["status"] == s for r in rows) for s in ("pass", "warn", "fail")},
        "by_kind": {k: sum(r["kind"] == k for r in rows) for k in sorted({r["kind"] for r in rows})},
        "verifications": len(counted),
        "recomputed_agree": sum(bool(r["agrees"]) for r in rows),
        "rows": rows,
    }

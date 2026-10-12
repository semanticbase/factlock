import copy
import pathlib

import pytest

from app.facts import build_fact_sheet, cli_phase, load_snapshot, streak

DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "snapshots"


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot(DATA, "DGBF", "2026-08")


def test_fund_return_is_weighted_sum_of_published_returns(snapshot):
    sheet = build_fact_sheet(snapshot)
    expected = sum(c["weight"] * snapshot["etfs"][c["ticker"]]["tr_1m"] for c in snapshot["fund"]["components"])
    assert sheet.facts["fund.ret_1m"].value == pytest.approx(expected, abs=1e-12)
    total = sum(sheet.facts[f"fund.contrib.{c['ticker']}"].value for c in snapshot["fund"]["components"])
    assert total == pytest.approx(sheet.facts["fund.ret_1m"].value, abs=1e-12)


def test_all_integrity_checks_pass_except_holdings_date(snapshot):
    checks = {c.name: c.status for c in build_fact_sheet(snapshot).checks}
    assert checks.pop("Holdings date matches the report month end") == "warn"
    assert set(checks.values()) == {"pass"}


def test_ranking_facts(snapshot):
    sheet = build_fact_sheet(snapshot)
    contribs = {c["ticker"]: c["weight"] * snapshot["etfs"][c["ticker"]]["tr_1m"]
                for c in snapshot["fund"]["components"]}
    top = max(contribs, key=contribs.get)
    assert sheet.facts["fund.top_contributor"].text_en.endswith(f"({top})")
    assert sheet.facts["fund.top_contributor_value"].value == pytest.approx(contribs[top])


def test_detractors_listed_when_a_component_falls(snapshot):
    changed = copy.deepcopy(snapshot)
    changed["etfs"]["SPAB"]["tr_1m"] = -0.50
    sheet = build_fact_sheet(changed)
    assert "SPAB" in sheet.facts["fund.detractors"].text_en
    assert "SPAB" in sheet.facts["fund.smallest_contributor"].text_en
    assert sheet.facts["fund.smallest_contributor_value"].value < 0


def test_nav_cross_check_flags_a_gap(snapshot):
    changed = copy.deepcopy(snapshot)
    changed["etfs"]["SPY"]["tr_1m"] = changed["etfs"]["SPY"]["tr_1m"] + 0.50
    checks = {c.name: c.status for c in build_fact_sheet(changed).checks}
    assert checks["SPY: published return vs NAV change"] == "warn"


@pytest.mark.parametrize("level,change,word", [
    (101.0, 0.1, "expansion"), (101.0, -0.1, "downturn"), (99.0, -0.1, "slowdown"), (99.0, 0.1, "recovery")])
def test_cli_phase_follows_oecd_reading(level, change, word):
    assert word in cli_phase(level, change)[0]


@pytest.mark.parametrize("values,expected", [
    ([1, 2, 3, 4], (3, 1, False)), ([5, 1, 2, 3], (2, 1, True)), ([1, 2, 1, 0], (2, -1, True)),
    ([3, 2, 1], (2, -1, False)), ([1, 1, 1], (0, 0, True))])
def test_streak_marks_runs_that_reach_the_window_start(values, expected):
    assert streak(values) == expected


def test_streak_wording_never_overstates(snapshot):
    changed = copy.deepcopy(snapshot)
    series = changed["macro"]["cli"]["G7"]["series"]
    rising = [[p, 100 + i * 0.1] for i, (p, _) in enumerate(series)]
    changed["macro"]["cli"]["G7"]["series"] = rising
    text = build_fact_sheet(changed).facts["cli.G7.streak"].text_en
    assert "in each of the last" in text and "consecutive" not in text

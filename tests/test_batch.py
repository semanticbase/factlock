"""Tests for the batch measurements (M1/M2/M8, M6, M5 reviewer layer) and the real-data fixture.

The batch script is run end to end with fake models: every branch that the real run can take
(normal, resume, cost cap, repeated failures) is exercised before it is run in Cloud Shell.
tests/fixtures/gemini_run_20260924.json is a real Gemini run (gemini-3.8-flash, 2026-09-24),
kept so that the checks are also proven on text the model actually wrote.
"""
import asyncio
import copy
import json
import pathlib
import warnings

import pytest

from app.facts import build_fact_sheet, load_snapshot
from app.llm import build_models, demo_draft
from app.service import run_pipeline
from measure import baseline, cost
from measure.baseline import baseline_instruction, baseline_rules, evaluate, sentences_from_commentary
from measure.controls import run_controls
from measure.independent import check_commentary
from measure.report import summarize, wilson
from scripts import measure_batch, measure_report
from tests.test_usage import NO_ISSUES, UsageLlm, usage

warnings.filterwarnings("ignore", category=DeprecationWarning)
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "snapshots"
SNAPSHOT = json.loads((DATA / "dgbf_2026-08.json").read_text(encoding="utf-8"))
REAL = json.loads((ROOT / "tests" / "fixtures" / "gemini_run_20260924.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sheet():
    return build_fact_sheet(load_snapshot(DATA, "DGBF", "2026-08"))


@pytest.fixture(scope="module")
def demo_result():
    w, r = build_models("demo", model="x", reviewer_model=None, project=None, location="global")
    return asyncio.run(run_pipeline(writer_model=w, reviewer_model=r, data_dir=str(DATA), fund_id="DGBF", month="2026-08"))


# ---- the real Gemini run -------------------------------------------------------------------

def test_real_run_usage_and_cost_as_observed():
    s = cost.summarize(REAL)
    assert s["complete"] and s["observed_calls"] == {"writer": 1, "reviewer": 1}
    t = s["totals"]
    assert (t["prompt_tokens"], t["output_tokens"], t["thinking_tokens"], t["cached_tokens"]) == (20296, 2083, 8740, 0)
    assert s["cost_usd"]["introductory, to 2026-12-31"] == pytest.approx(0.055808, abs=1e-6)
    assert s["cost_usd"]["standard, from 2027-01-01"] == pytest.approx(0.111616, abs=1e-6)
    raw_keys = set(REAL["usage"][0]["raw"])
    assert {"prompt_token_count", "candidates_token_count", "thoughts_token_count", "total_token_count"} <= raw_keys
    assert "cached_content_token_count" not in raw_keys      # reported as absent, not as zero


def test_real_run_numbers_all_match_and_controls_fire():
    r = check_commentary(REAL["commentary"], SNAPSHOT)
    assert r["numbers"] == 68 and r["problems"] == 0 and r["digits_outside_tokens"] == 0
    c = run_controls(REAL["commentary"], SNAPSHOT)
    assert c["all_as_expected"] and c["applicable"] >= 12


# ---- M3 controls chosen by token type ----------------------------------------------------------

def test_controls_apply_to_the_demo_draft_too(demo_result):
    c = run_controls(demo_result["commentary"], SNAPSHOT)
    assert c["all_as_expected"] and c["applicable"] >= 12
    assert {x["control"] for x in c["controls"] if x["expect"] == "accepted"} == {
        "minus sign U+2212 on a negative change (correct)", "contribution in words 'percentage points' (correct)"}


# ---- M6 comparison writer and evaluator ----------------------------------------------------------

def test_baseline_differs_from_production_only_in_the_number_rules(sheet):
    text = baseline_instruction(sheet)
    assert "{{" not in text and "FactLock" not in text
    assert "type it into the text yourself" in text and '"value_en"' in text and '"formats"' not in text
    assert "No investment recommendations, no guarantees" in text           # same claims rules
    assert "The outlook section describes what the team will monitor" in text


def test_baseline_refuses_to_run_if_the_production_rules_change(monkeypatch):
    monkeypatch.setattr(baseline, "WRITER_RULES", baseline.WRITER_RULES.replace("HOW NUMBERS WORK", "NUMBERS"))
    with pytest.raises(ValueError):
        baseline_rules()


def test_evaluator_finds_nothing_in_factlock_drafts(demo_result):
    for result in (REAL, demo_result):
        e = evaluate(sentences_from_commentary(result["commentary"]), result["fact_sheet"])
        assert e["certain_errors"] == 0 and e["needs_review"] == 0, e["details"]


def _plant(ref, lang, old, new):
    s = sentences_from_commentary(copy.deepcopy(REAL["commentary"]))
    row = next(x for x in s if x["ref"] == ref)
    assert old in row[lang]
    row[lang] = row[lang].replace(old, new, 1)
    return s


@pytest.mark.parametrize("ref,lang,old,new,kind", [
    ("market.1", "en", "rose 2.71%", "rose 2.81%", "unsupported"),
    ("market.1", "en", "(SPY) rose 2.71%, developed-market equities ex-U.S. (SPDW) rose 3.14%",
     "(SPY) rose 3.14%, developed-market equities ex-U.S. (SPDW) rose 2.71%", "misattribution"),
    ("market.1", "en", "(SPAB) rose 0.39%", "(SPAB) fell 0.39%", "direction"),
    ("market.4", "en", "(rose 0.08 percentage points)", "(rose 0.08%)", "unit"),
    ("market.1", "ja", "2.71%の上昇", "2.7%の上昇", "parity"),
    ("outlook.2", "en", "framework.", "framework. Returns are guaranteed.", "prohibited"),
    ("performance.1", "en", "rose 2.17%.", "rose 2.17%, lifting the year-to-date gain above 13%.", "unsupported"),
    ("performance.1", "en", "rose 2.17%", "{{fund.ret_1m|verb}}", "placeholder"),
    ("outlook.1", "en", "rates.", "rates over the past 12 months.", "unsupported"),
    ("market.1", "en", "rose 0.39%", "rose 9%", "unsupported"),   # 9 is also a month; a unit makes it a figure
])
def test_evaluator_finds_planted_errors(ref, lang, old, new, kind):
    assert evaluate(_plant(ref, lang, old, new), REAL["fact_sheet"])["counts"][kind] >= 1


# ---- the batch script, end to end with fake models -----------------------------------------------

def _typed_draft(demo_result, wrong=True):
    rows = sentences_from_commentary(demo_result["commentary"])
    sections: dict = {}
    for r in rows:
        sections.setdefault(r["section"], []).append({"en": r["en"], "ja": r["ja"]})
    data = {"sections": [{"key": k, "sentences": v} for k, v in sections.items()]}
    text = json.dumps(data, ensure_ascii=False)
    return text.replace("rose 2.71%", "rose 2.81%", 1) if wrong else text


def _models(demo_result, sheet):
    clean = json.dumps(demo_draft(set(sheet.facts)), ensure_ascii=False)
    writer = UsageLlm(model="w", replies=[clean], usages=[usage(8620, 2078, thinking=6717)])
    reviewer = UsageLlm(model="r", replies=[NO_ISSUES], usages=[usage(11676, 5, thinking=2023)])
    ordinary = UsageLlm(model="o", replies=[_typed_draft(demo_result)], usages=[usage(9000, 2100, thinking=6000)])
    return writer, reviewer, ordinary


def test_batch_runs_every_phase_resumes_and_reports(tmp_path, demo_result, sheet, capsys):
    writer, reviewer, ordinary = _models(demo_result, sheet)
    out = tmp_path / "batch"
    code = measure_batch.main(["--n", "2", "--out", str(out)], models=(writer, reviewer), baseline_writer=ordinary)
    assert code == 0, capsys.readouterr().out
    assert len(list((out / "m1").glob("run_*.json"))) == 2 and len(list((out / "m6").glob("run_*.json"))) == 2
    assert len(list((out / "m5").glob("*.json"))) == 105 and len(list((out / "m6b").glob("run_*.json"))) == 2
    calls = (writer.calls, reviewer.calls, ordinary.calls)
    assert calls == (2, 2 + 102, 4)                    # 3 fault cases are not drafts, so the reviewer never sees them
    saved = json.loads((out / "m6" / "run_001.json").read_text())
    assert saved["usage"][0]["prompt_tokens"] == 9000 and "rose 2.81%" in saved["raw"]
    # resume: nothing is run twice
    assert measure_batch.main(["--n", "2", "--out", str(out)], models=(writer, reviewer), baseline_writer=ordinary) == 0
    assert (writer.calls, reviewer.calls, ordinary.calls) == calls
    # report
    report = summarize(out, SNAPSHOT, ROOT)
    assert report["m2"]["first_draft_passed"]["k"] == 2 and report["m3"]["controls_as_expected"]["k"] == 2
    assert report["m6"]["factlock"]["drafts_with_certain_error"]["k"] == 0
    assert report["m6"]["ordinary_use"]["drafts_with_certain_error"]["k"] == 2
    assert report["m6"]["ordinary_use"]["by_kind"]["unsupported"] == 2
    assert report["m5"]["fatal"]["cases"] == 57 and report["m5"]["legit"]["cases"] == 40
    assert report["m6"]["ordinary_use_inputs_only"]["drafts"] == 2 and report["runs"]["m6b"] == 2
    assert report["m7_usage_complete"]["k"] == 2
    assert report["m4"]["figures"] > 0 and report["m4"]["traced"] == report["m4"]["figures"]
    assert measure_report.main(["--batch", str(out), "--out", str(tmp_path / "r.json")]) == 0
    assert "M6 ordinary use: drafts with a certain error 2/2" in capsys.readouterr().out


def test_batch_stops_at_the_cost_cap(tmp_path, demo_result, sheet, capsys):
    writer, reviewer, ordinary = _models(demo_result, sheet)
    out = tmp_path / "batch"
    assert measure_batch.main(["--n", "3", "--out", str(out), "--cap-usd", "0.05"],
                              models=(writer, reviewer), baseline_writer=ordinary) == 4
    assert "STOP: the cost cap is reached" in capsys.readouterr().out
    # the first FactLock run ($0.11 at the standard price) went over the cap; nothing is called after it
    assert writer.calls == 1 and ordinary.calls == 0


def test_batch_stops_after_repeated_failures_and_can_retry(tmp_path, demo_result, sheet, capsys):
    class Broken(UsageLlm):
        async def generate_content_async(self, llm_request, stream=False):
            raise RuntimeError("service unavailable")
            yield  # noqa: unreachable - makes this an async generator
    broken = Broken(model="b", replies=["x"], usages=[None])
    out = tmp_path / "batch"
    assert measure_batch.main(["--n", "5", "--phases", "m1", "--out", str(out), "--max-failures", "3",
                               "--pause-after-failure", "0"], models=(broken, broken)) == 5
    assert len(list((out / "m1").glob("*.error.json"))) == 3
    assert "3 failures in a row" in capsys.readouterr().out
    writer, reviewer, _ = _models(demo_result, sheet)
    assert measure_batch.main(["--n", "3", "--phases", "m1", "--out", str(out)], models=(writer, reviewer)) == 0
    assert not list((out / "m1").glob("*.error.json")) and len(list((out / "m1").glob("run_*.json"))) == 3


def test_wilson_interval():
    assert wilson(40, 40) == [0.9124, 1.0]
    assert wilson(0, 0) is None


# ---- lessons from the 40-run batch (2026-09-24) ------------------------------------------------

def _with_value_format_contribution():
    com = copy.deepcopy(REAL["commentary"])
    for sec in com:
        for s in sec["sentences"]:
            for seg in s["en"]:
                if seg.get("fact_id") == "fund.contrib.SPDW":
                    seg["format"], seg["text"] = "value", "+0.63 pp"
                    return com
    raise AssertionError("fixture has no SPDW contribution")


def test_a_signed_change_in_value_format_is_not_an_accuracy_problem():
    """The product shows changes signed in the value format (+0.63 pp); 14 of 40 Gemini drafts used it."""
    assert check_commentary(_with_value_format_contribution(), SNAPSHOT)["problems"] == 0
    wrong = _with_value_format_contribution()
    next(seg for sec in wrong for s in sec["sentences"] for seg in s["en"]
         if seg.get("fact_id") == "fund.contrib.SPDW")["text"] = "-0.63 pp"
    assert check_commentary(wrong, SNAPSHOT)["problems"] == 1          # a contradicted sign still fails


def test_controls_hold_on_a_draft_that_already_has_a_problem():
    """Controls are judged by what they add: an existing problem must not make a correct rendering look
    rejected (this made 14 of 40 control sets fail in the first batch report)."""
    com = copy.deepcopy(REAL["commentary"])
    seg = next(seg for sec in com for s in sec["sentences"] for seg in s["en"] if seg.get("fact_id") == "etf.SPY.ret_1m")
    seg["text"] = seg["text"].replace("2.71", "2.81")
    assert check_commentary(com, SNAPSHOT)["problems"] == 1
    c = run_controls(com, SNAPSHOT)
    wrong = [x for x in c["controls"] if x["applicable"] and (x["expect"] == "detected") != x["detected"]]
    assert c["all_as_expected"], wrong


def test_japanese_grammar_scan_finds_a_polite_form_mid_sentence():
    from measure.report import ja_polite_mid_sentence
    com = [{"key": "market", "sentences": [{"ref": "market.2", "en": [], "ja": [
        {"type": "text", "text": "G7の景気先行指数は"},
        {"type": "fact", "fact_id": "cli.G7.streak", "format": "name", "text": "16か月連続で上昇しています"},
        {"type": "text", "text": "一方、アジアは低下しました。上昇していますが、注視します。"}]}]}]
    found = ja_polite_mid_sentence(com)
    assert [f["source"] for f in found] == ["cli.G7.streak"]     # 「ますが」 is grammatical and not counted


def test_inputs_only_instruction_gives_inputs_but_no_derived_figures(sheet):
    from measure.baseline import inputs_instruction, inputs_data
    text = inputs_instruction(sheet, SNAPSHOT)
    assert "CALCULATIONS" in text and "<DATA_JSON>" in text and "{{" not in text
    for value in ("2.71", "0.4", "NVIDIA", "2023-08"):          # published inputs are there
        assert value in text
    for value in ("2.17", "1.08", "0.63 pp", "3.23%", "16 consecutive months", "in an expansion phase"):
        assert value not in text                                 # nothing FactLock derives is there
    data = inputs_data(SNAPSHOT)
    assert set(data) == {"report_month", "period_end", "fund", "holdings", "leading_indicator", "long_term_rate_pct"}
    assert "rose 1.23%" in text and "2.17" not in text           # the example figure is not a real figure


def test_evaluator_accepts_published_values_names_and_katakana_holdings():
    """Artifacts found in the 10-run trial of 2026-09-24 (inputs-only drafts): last month's levels, bond
    maturities, holdings in katakana, and names with digits must not count against a draft."""
    from measure.baseline import published_values
    sheet, pub = REAL["fact_sheet"], published_values(SNAPSHOT)
    rows = [{"ref": "market.2", "section": "market",
             "en": "The U.S. 10-year yield rose from 4.60% in July to 4.68%, and the OECD indicators showed the G7 economies at 100.88.",
             "ja": "米国の長期金利は7月の4.60%から4.68%へ上昇し、G7の景気先行指数は100.88でした。"},
            {"ref": "portfolio.2", "section": "portfolio", "en": "NVIDIA CORP (3.23%).", "ja": "エヌビディア（3.23%）。"}]
    e = evaluate(rows, sheet, pub)
    assert e["certain_errors"] == 0 and e["counts"]["misattribution"] == 0, e["details"]
    assert evaluate(rows, sheet)["counts"]["unsupported"] == 2          # without the publishers' data: 4.60 twice
    rows[0]["en"] = rows[0]["en"].replace("4.60%", "4.61%")                # a value nobody published is still caught
    assert evaluate(rows, sheet, pub)["counts"]["unsupported"] == 1


def test_evaluator_accepts_a_correctly_ordered_respectively_list():
    """9 false positives in the 2026-09-25 batch (inputs-only drafts): a sentence that names its
    entities and then lists their values ("..., respectively" / "それぞれ...") gets comma-split into
    clauses, so a value's clause often carries a *different*, merely adjacent, entity's name. The
    fix checks such sentences against the whole sentence's mentions, not just their own clause's."""
    sheet = REAL["fact_sheet"]
    rows = [{"ref": "market.2", "section": "market",
             "en": ("The composite leading indicators for the United States, Japan, and the G7 economies "
                    "rose to 100.96, 100.30, and 100.88, respectively."),
             "ja": "米国、日本、G7の景気先行指数はそれぞれ100.96、100.30、100.88へ上昇しました。"}]
    assert evaluate(rows, sheet)["counts"]["misattribution"] == 0

    # Positive control: the relaxation must not blind the check inside such a sentence. Swapping in a
    # value for an entity (China) that the sentence never names anywhere must still be caught.
    rows[0]["en"] = rows[0]["en"].replace("100.88", "98.10")
    e = evaluate(rows, sheet)
    assert e["counts"]["misattribution"] >= 1, e["details"]
    assert any(m["number"] == "98.10" for m in e["details"]["misattribution"])


def test_evaluator_accepts_a_sentence_initial_capitalized_label():
    """Also from the 2026-09-25 batch: an ETF label is stored lower-case ("emerging-market equities
    (SPEM)") but a sentence that opens with it capitalizes the first letter ("Emerging-market
    equities added..."), so the exact-case substring check missed the mention entirely."""
    sheet = REAL["fact_sheet"]
    rows = [{"ref": "performance.3", "section": "performance",
             "en": ("Emerging-market equities added 0.34 percentage points and U.S. investment-grade "
                    "bonds added 0.12 percentage points, lifting the fund's total return this month."),
             "ja": "新興国株式が0.34パーセントポイント、米国投資適格債券が0.12パーセントポイント寄与しました。"}]
    assert evaluate(rows, sheet)["counts"]["misattribution"] == 0


def test_the_batch_default_tolerates_a_busy_endpoint(tmp_path, capsys, monkeypatch):
    """Six failures in a row before stopping, with a pause after each failed call (2026-09-24: the global
    endpoint refused about half of the calls for an hour, and three-in-a-row stopped the batch repeatedly)."""
    import asyncio
    pauses = []

    async def fake_sleep(seconds):
        pauses.append(seconds)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    class Broken(UsageLlm):
        async def generate_content_async(self, llm_request, stream=False):
            raise RuntimeError("service unavailable")
            yield  # noqa: unreachable - makes this an async generator
    broken = Broken(model="b", replies=["x"], usages=[None])
    out = tmp_path / "batch"
    assert measure_batch.main(["--n", "9", "--phases", "m1", "--out", str(out)], models=(broken, broken)) == 5
    assert "6 failures in a row" in capsys.readouterr().out and pauses == [30.0] * 5

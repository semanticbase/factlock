import copy
import pathlib

import pytest

from app.facts import build_fact_sheet, load_snapshot
from app.llm import demo_draft
from app.models import Draft, Fact
from app.render import format_fact, render_text
from app.verify import check_draft

DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "snapshots"


@pytest.fixture(scope="module")
def sheet():
    return build_fact_sheet(load_snapshot(DATA, "DGBF", "2026-08"))


@pytest.fixture()
def draft(sheet):
    return demo_draft(set(sheet.facts))


def codes(sheet, data):
    _, issues = check_draft(Draft.model_validate(data), sheet)
    return {(i.code, i.severity) for i in issues}


def errors(sheet, data):
    return {code for code, sev in codes(sheet, data) if sev == "error"}


def set_sentence(data, key, index, en=None, ja=None):
    data = copy.deepcopy(data)
    section = next(s for s in data["sections"] if s["key"] == key)
    if en is not None:
        section["sentences"][index]["en"] = en
    if ja is not None:
        section["sentences"][index]["ja"] = ja
    return data


# ---- rendering ---------------------------------------------------------------

def _fact(unit, value, down="下落"):
    return Fact(id="x", unit=unit, value=value, as_of="2026-08-31", label_en="x", label_ja="x",
                source="t", down_word_ja=down)


@pytest.mark.parametrize("unit,value,fmt,lang,expected", [
    ("pct", 2.168, "verb", "en", "rose 2.17%"), ("pct", -1.2, "verb", "en", "fell 1.20%"),
    ("pct", 0.001, "verb", "en", "was unchanged"), ("pct", 2.168, "verb", "ja", "2.17%の上昇"),
    ("pct", -1.2, "verb", "ja", "1.20%の下落"), ("pct", 0.004, "verb", "ja", "横ばい"),
    ("pct", 2.168, "signed", "en", "+2.17%"), ("pct", -0.004, "signed", "en", "0.00%"),
    ("contrib", 1.084, "signed", "en", "+1.08 pp"), ("contrib", 1.084, "signed", "ja", "+1.08%"),
    ("pp", 0.08, "verb", "en", "rose 0.08 percentage points"), ("pt", -0.05, "verb", "en", "fell 0.05 points"),
    ("weight", 40.0, "value", "en", "40.00%"), ("index", 100.8786, "value", "en", "100.88"),
])
def test_format(unit, value, fmt, lang, expected):
    assert format_fact(_fact(unit, value), fmt, lang) == expected


def test_japanese_down_word_for_rates():
    assert format_fact(_fact("pp", -0.1, down="低下"), "verb", "ja") == "0.10%ポイントの低下"


def test_format_not_allowed_for_unit():
    with pytest.raises(ValueError):
        format_fact(_fact("index", 100.0), "verb", "en")


def test_unknown_fact_and_bad_format_become_issues(sheet):
    _, issues = render_text("{{no.such.fact|value}} {{cli.G7.level|verb}}", sheet.facts, "en", "market.1")
    assert {i.code for i in issues} == {"unknown_fact", "bad_format"}


# ---- verification --------------------------------------------------------------

def test_demo_draft_passes(sheet, draft):
    assert errors(sheet, draft) == set()


@pytest.mark.parametrize("en,ja", [
    ("{{fund.name|name}} returned about 2.2% in {{report.month|name}} ({{fund.ret_1m|signed}}).", None),
    ("{{fund.name|name}} was up for three months in {{report.month|name}} ({{fund.ret_1m|signed}}).", None),
    (None, "{{fund.name|name}}の{{report.month|name}}の騰落率は{{fund.ret_1m|signed}}（約２％）となりました。"),
    (None, "{{fund.name|name}}の{{report.month|name}}の騰落率は{{fund.ret_1m|signed}}で三か月ぶりです。"),
])
def test_typed_numbers_are_rejected(sheet, draft, en, ja):
    assert "typed_number" in errors(sheet, set_sentence(draft, "performance", 0, en, ja))


def test_bilingual_mismatch(sheet, draft):
    changed = set_sentence(draft, "performance", 0,
                           ja="{{fund.name|name}}の騰落率は{{fund.ret_1m|signed}}となりました。")
    assert "bilingual_mismatch" in errors(sheet, changed)


def test_direction_conflict_english_and_japanese(sheet, draft):
    en = "{{fund.name|name}} fell in {{report.month|name}}, returning {{fund.ret_1m|signed}}."
    assert "direction_conflict" in errors(sheet, set_sentence(draft, "performance", 0, en=en))
    ja = "{{fund.name|name}}の{{report.month|name}}は{{fund.ret_1m|signed}}と下落しました。"
    assert "direction_conflict" in errors(sheet, set_sentence(draft, "performance", 0, ja=ja))


def test_direction_words_in_outlook_are_not_matched_to_data(sheet, draft):
    changed = set_sentence(draft, "outlook", 1, en="Markets could fall if conditions change.",
                           ja="市場環境によっては下落する可能性があります。")
    assert "direction_conflict" not in errors(sheet, changed)


def test_holdings_need_their_date(sheet, draft):
    en = "The largest look-through holding was {{lookthrough.1.name|name}} ({{lookthrough.1.weight|value}})."
    ja = "ルックスルーでの組入第一位は{{lookthrough.1.name|name}}（{{lookthrough.1.weight|value}}）でした。"
    assert "undisclosed_date" in errors(sheet, set_sentence(draft, "portfolio", 1, en, ja))


@pytest.mark.parametrize("en,ja,code", [
    ("Returns are guaranteed.", "運用成果を保証します。", "guarantee"),
    ("We recommend buying the fund.", "購入をお勧めします。", "recommendation"),
    ("The indicator leads stock prices by several months.", "株価に数か月先行します。", "lead_claim"),
])
def test_prohibited_claims(sheet, draft, en, ja, code):
    found = codes(sheet, set_sentence(draft, "outlook", 1, en, ja))
    assert (code, "error") in found


def test_cautions_are_warnings_not_errors(sheet, draft):
    changed = set_sentence(draft, "outlook", 1, en="Equities will rise further.",
                           ja="株式は上昇する見込みです。")
    found = codes(sheet, changed)
    assert ("unhedged_forecast", "warning") in found
    assert "unhedged_forecast" not in errors(sheet, changed)


def test_standard_disclaimers_are_not_flagged(sheet, draft):
    changed = set_sentence(draft, "outlook", 1, en="Past performance does not guarantee future results.",
                           ja="将来の運用成果を保証するものではありません。")
    assert errors(sheet, changed) == set()


def test_missing_section_and_key_fact(sheet, draft):
    no_outlook = copy.deepcopy(draft)
    no_outlook["sections"] = [s for s in no_outlook["sections"] if s["key"] != "outlook"]
    assert "missing_section" in errors(sheet, no_outlook)
    changed = set_sentence(draft, "performance", 0, en="{{fund.name|name}} is a hypothetical fund.",
                           ja="{{fund.name|name}}は仮想ファンドです。")
    assert "missing_key_fact" in errors(sheet, changed)


def test_market_section_must_cover_every_component(sheet, draft):
    trimmed = copy.deepcopy(draft)
    market = next(s for s in trimmed["sections"] if s["key"] == "market")
    market["sentences"] = market["sentences"][1:]  # drops the equity sentence
    found = errors(sheet, trimmed)
    assert "market_coverage" in found


def test_full_coverage_passes(sheet, draft):
    assert "market_coverage" not in errors(sheet, draft)

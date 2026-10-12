"""Core data structures shared by the fact builder, renderer and verifier."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# pct      : a return in percent (2.17 means 2.17%)
# contrib  : a contribution to fund return, in percentage points
# pp       : a change in an interest rate, in percentage points
# pt       : a change in an index level, in index points
# pct_pa   : a level quoted in percent per annum (an interest rate)
# weight   : a portfolio weight in percent
# index    : an index level
# text     : a bilingual text value (names, dates, phrases derived from data)
Unit = Literal["pct", "contrib", "pp", "pt", "pct_pa", "weight", "index", "text"]

FORMATS_BY_UNIT: dict[str, tuple[str, ...]] = {
    "pct": ("value", "signed", "abs", "verb"),
    "contrib": ("value", "signed", "abs", "verb"),
    "pp": ("value", "signed", "abs", "verb"),
    "pt": ("value", "signed", "abs", "verb"),
    "pct_pa": ("value",),
    "weight": ("value",),
    "index": ("value",),
    "text": ("name",),
}

# Units whose sign carries a direction (used by the direction check).
DIRECTIONAL_UNITS = {"pct", "contrib", "pp", "pt"}


class Fact(BaseModel):
    id: str
    unit: Unit
    value: Optional[float] = None
    text_en: Optional[str] = None
    text_ja: Optional[str] = None
    decimals: int = 2
    as_of: str
    label_en: str
    label_ja: str
    source: str
    derivation: Optional[str] = None
    inputs: list[str] = Field(default_factory=list)
    down_word_ja: str = "下落"
    # A text fact that discloses the as-of date of other facts (e.g. holdings).
    discloses_as_of: Optional[str] = None


class DataCheck(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail"]
    detail: str


class FactSheet(BaseModel):
    fund_id: str
    fund_name_en: str
    fund_name_ja: str
    fund_note_en: str
    fund_note_ja: str
    month: str
    period_end: str
    facts: dict[str, Fact]
    checks: list[DataCheck]
    sources: list[dict]


class Sentence(BaseModel):
    en: str
    ja: str


class Section(BaseModel):
    key: str
    sentences: list[Sentence]


class Draft(BaseModel):
    sections: list[Section]


class Issue(BaseModel):
    code: str
    severity: Literal["error", "warning"]
    message: str
    ref: Optional[str] = None
    lang: Optional[Literal["en", "ja", "both"]] = None
    origin: Literal["rules", "reviewer", "format"] = "rules"


class Report(BaseModel):
    passed: bool
    errors: list[Issue]
    warnings: list[Issue]
    rule_counts: dict[str, int]


# Sections the writer must produce, in reading order.
SECTIONS: list[dict] = [
    {"key": "market", "heading_en": "Market environment", "heading_ja": "市場環境"},
    {"key": "performance", "heading_en": "Fund performance", "heading_ja": "運用経過"},
    {"key": "portfolio", "heading_en": "Portfolio", "heading_ja": "ポートフォリオ"},
    {"key": "outlook", "heading_en": "Outlook", "heading_ja": "今後の見通し"},
]
SECTION_KEYS = [s["key"] for s in SECTIONS]

"""Deterministic checks on a draft. No language model is involved here.

Each check produces Issues. A draft passes only when there are no errors.
"""
from __future__ import annotations

import re
from collections import Counter

from app import policy
from app.models import DIRECTIONAL_UNITS, SECTIONS, SECTION_KEYS, Draft, FactSheet, Issue
from app.render import authored_text, fact_ids_in, render_text

REQUIRED_FACTS = {"performance": ["fund.ret_1m"]}
# The market section must cover every market the fund is invested in.
COMPONENT_RETURN = re.compile(r"^etf\.[A-Z0-9]+\.ret_1m$")


def _sign(value: float, decimals: int) -> int:
    rounded = float(f"{value:.{decimals}f}")
    return (rounded > 0) - (rounded < 0)


def render_draft(draft: Draft, sheet: FactSheet) -> tuple[list[dict], list[Issue]]:
    """Render every sentence in both languages. Returns sections and render issues."""
    headings = {s["key"]: s for s in SECTIONS}
    by_key = {section.key: section for section in draft.sections}
    rendered, issues = [], []
    for key in SECTION_KEYS:
        section = by_key.get(key)
        if section is None:
            continue
        out = {"key": key, "heading_en": headings[key]["heading_en"],
               "heading_ja": headings[key]["heading_ja"], "sentences": []}
        for i, sentence in enumerate(section.sentences, start=1):
            ref = f"{key}.{i}"
            en_segments, en_issues = render_text(sentence.en, sheet.facts, "en", ref)
            ja_segments, ja_issues = render_text(sentence.ja, sheet.facts, "ja", ref)
            issues += en_issues + ja_issues
            out["sentences"].append({"ref": ref, "en": en_segments, "ja": ja_segments,
                                     "source_en": sentence.en, "source_ja": sentence.ja})
        rendered.append(out)
    return rendered, issues


def check_draft(draft: Draft, sheet: FactSheet) -> tuple[list[dict], list[Issue]]:
    rendered, issues = render_draft(draft, sheet)
    facts = sheet.facts

    # 1. Structure -----------------------------------------------------------
    present = {s.key for s in draft.sections}
    for key in SECTION_KEYS:
        if key not in present:
            issues.append(Issue(code="missing_section", severity="error",
                                message=f"Section '{key}' is missing."))
    for section in draft.sections:
        if section.key not in SECTION_KEYS:
            issues.append(Issue(code="unknown_section", severity="warning",
                                message=f"Section '{section.key}' is not part of the template and will be ignored."))
        if not section.sentences:
            issues.append(Issue(code="empty_section", severity="error", ref=section.key,
                                message=f"Section '{section.key}' has no sentences."))

    for section in draft.sections:
        if section.key not in SECTION_KEYS:
            continue
        for i, sentence in enumerate(section.sentences, start=1):
            ref = f"{section.key}.{i}"
            texts = {"en": sentence.en, "ja": sentence.ja}
            for lang, text in texts.items():
                if not text.strip():
                    issues.append(Issue(code="empty_sentence", severity="error", ref=ref, lang=lang,
                                        message="The sentence is empty in this language."))

            # 2. No typed numbers ------------------------------------------------
            for lang, pattern in (("en", policy.TYPED_NUMBER_EN), ("ja", policy.TYPED_NUMBER_JA)):
                hit = pattern.search(authored_text(texts[lang]))
                if hit:
                    issues.append(Issue(
                        code="typed_number", severity="error", ref=ref, lang=lang,
                        message=f"A number was typed instead of taken from a fact: “{hit.group(0)}”. "
                                "Use a placeholder."))

            # 3. English and Japanese cite the same facts --------------------------
            en_ids, ja_ids = Counter(fact_ids_in(sentence.en)), Counter(fact_ids_in(sentence.ja))
            if set(en_ids) != set(ja_ids):
                missing_ja = sorted(set(en_ids) - set(ja_ids))
                missing_en = sorted(set(ja_ids) - set(en_ids))
                detail = []
                if missing_ja:
                    detail.append("only in English: " + ", ".join(missing_ja))
                if missing_en:
                    detail.append("only in Japanese: " + ", ".join(missing_en))
                issues.append(Issue(code="bilingual_mismatch", severity="error", ref=ref, lang="both",
                                    message="English and Japanese cite different facts (" + "; ".join(detail) + ")."))

            # 4. Typed direction words must agree with the data -------------------
            if section.key != "outlook":
                for lang, patterns in (("en", policy.DIRECTION_EN), ("ja", policy.DIRECTION_JA)):
                    authored = authored_text(texts[lang])
                    ups = patterns["up"].findall(authored)
                    downs = patterns["down"].findall(authored)
                    if not ups and not downs:
                        continue
                    signs = {
                        _sign(facts[f].value, facts[f].decimals)
                        for f in fact_ids_in(texts[lang])
                        if f in facts and facts[f].unit in DIRECTIONAL_UNITS
                    } - {0}
                    if not signs:
                        issues.append(Issue(
                            code="direction_without_data", severity="warning", ref=ref, lang=lang,
                            message="A direction word is used without a movement fact in the sentence."))
                    elif signs == {1} and downs:
                        issues.append(Issue(
                            code="direction_conflict", severity="error", ref=ref, lang=lang,
                            message="The text describes a fall, but the facts in this sentence are all positive."))
                    elif signs == {-1} and ups:
                        issues.append(Issue(
                            code="direction_conflict", severity="error", ref=ref, lang=lang,
                            message="The text describes a rise, but the facts in this sentence are all negative."))
                    elif len(signs) > 1:
                        issues.append(Issue(
                            code="direction_ambiguous", severity="warning", ref=ref, lang=lang,
                            message="Facts move in both directions here; check that each direction word "
                                    "refers to the right fact."))

            # 5. Facts dated after the month end must be disclosed -------------
            for lang in ("en", "ja"):
                ids = [f for f in fact_ids_in(texts[lang]) if f in facts]
                disclosed = {facts[f].discloses_as_of for f in ids if facts[f].discloses_as_of}
                for f in ids:
                    fact = facts[f]
                    if fact.discloses_as_of or fact.as_of == sheet.period_end:
                        continue
                    if fact.as_of not in disclosed:
                        issues.append(Issue(
                            code="undisclosed_date", severity="error", ref=ref, lang=lang,
                            message=f"{f} is as of {fact.as_of}, not the month end; "
                                    "include its date placeholder in the same sentence."))
                        break

            # 6. Claims policy -----------------------------------------------------
            for lang, rules, level in (("en", policy.PROHIBITED, "error"), ("ja", policy.PROHIBITED_JA, "error"),
                                       ("en", policy.CAUTION, "warning"), ("ja", policy.CAUTION_JA, "warning")):
                authored = authored_text(texts[lang])
                for code, message, pattern in rules:
                    hit = pattern.search(authored)
                    if hit:
                        issues.append(Issue(code=code, severity=level, ref=ref, lang=lang,
                                            message=f"{message} (“{hit.group(0)}”)"))

    # 7. The market section covers every market the fund holds --------------
    market = next((s for s in draft.sections if s.key == "market"), None)
    if market is not None:
        expected = {f for f in facts if COMPONENT_RETURN.match(f)}
        for lang in ("en", "ja"):
            cited = {f for s in market.sentences for f in fact_ids_in(getattr(s, lang))}
            for fact_id in sorted(expected - cited):
                issues.append(Issue(
                    code="market_coverage", severity="error", ref="market", lang=lang,
                    message=f"The market section must also describe {fact_id} "
                            "so that every market the fund holds is covered."))

    # 8. Required facts ------------------------------------------------------
    for key, required in REQUIRED_FACTS.items():
        section = next((s for s in draft.sections if s.key == key), None)
        if section is None:
            continue
        for lang in ("en", "ja"):
            cited = {f for s in section.sentences for f in fact_ids_in(getattr(s, lang))}
            for fact_id in required:
                if fact_id not in cited:
                    issues.append(Issue(code="missing_key_fact", severity="error", ref=key, lang=lang,
                                        message=f"The {key} section must state {fact_id}."))
    return rendered, issues


def count_by_code(issues: list[Issue]) -> dict[str, int]:
    return dict(Counter(issue.code for issue in issues))

"""Web layer: search-engine blocking, the design tokens, exhibit data, and the display helpers."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
os.environ["FACTLOCK_LLM_MODE"] = "demo"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.exhibits import build_exhibits  # noqa: E402
from app.facts import build_fact_sheet, load_snapshot  # noqa: E402
from app.main import _failed_logins, app, settings  # noqa: E402
from app.models import FORMATS_BY_UNIT  # noqa: E402
from app.render import format_fact  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


@pytest.fixture()
def gated():
    settings.passphrase = "builder cup 2026"
    _failed_logins.clear()
    with TestClient(app) as client:
        yield client
    settings.passphrase = ""
    _failed_logins.clear()


@pytest.fixture(scope="module")
def sheet():
    return build_fact_sheet(load_snapshot(ROOT / "data" / "snapshots", "DGBF", "2026-08"))


# ------------------------------------------------------------ search engines
def test_robots_txt_disallows_everything_and_needs_no_passphrase(gated):
    response = gated.get("/robots.txt")
    assert response.status_code == 200
    assert "User-agent: *" in response.text and "Disallow: /" in response.text.splitlines()


@pytest.mark.parametrize("path,status", [
    ("/", 303), ("/login", 200), ("/api/facts?month=2026-08", 401), ("/api/health", 200),
    ("/robots.txt", 200), ("/static/styles.css", 200)])
def test_every_response_carries_noindex(gated, path, status):
    response = gated.get(path, follow_redirects=False)
    assert response.status_code == status
    assert response.headers.get("x-robots-tag") == "noindex, nofollow"


def test_noindex_header_also_without_the_gate():
    settings.passphrase = ""
    with TestClient(app) as client:
        assert client.get("/").headers.get("x-robots-tag") == "noindex, nofollow"


@pytest.mark.parametrize("page", ["index.html", "login.html"])
def test_pages_carry_the_robots_meta_tag(page):
    assert '<meta name="robots" content="noindex, nofollow">' in (WEB / page).read_text(encoding="utf-8")


# ----------------------------------------------------------- design tokens
def test_styles_root_matches_design_md():
    design = (WEB / "DESIGN.md").read_text(encoding="utf-8")
    section = design[design.index("## 2. トークン"):design.index("## 3. 書体")]
    expected = {}
    for names, values in re.findall(r"^\| (`--[^|]+) \| (`#[^|]+) \|", section, flags=re.M):
        for name, value in zip(re.findall(r"`(--[\w-]+)`", names), re.findall(r"`(#[0-9A-Fa-f]{6})`", values)):
            expected[name] = value.upper()
    root = (WEB / "styles.css").read_text(encoding="utf-8")
    root = root[root.index(":root {"):root.index("}", root.index(":root {"))]
    actual = {k: v.upper() for k, v in re.findall(r"(--[\w-]+):\s*(#[0-9A-Fa-f]{6})", root)}
    assert len(expected) == 14, expected            # positive control: all 14 tokens of the table were read
    assert {k: actual.get(k) for k in expected} == expected


def test_one_sans_family_latin_first_and_no_banned_fonts():
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    sans = re.search(r"--sans:\s*([^;]+);", css).group(1)
    assert sans.index('"IBM Plex Sans"') < sans.index('"IBM Plex Sans JP"')
    everything = css + "".join((WEB / p).read_text(encoding="utf-8") for p in ("index.html", "login.html", "app.js"))
    for banned in ("Inter", "Roboto", "Arial", "system-ui", "Newsreader", "Shippori", "BIZ UDP", "Noto Serif"):
        assert not re.search(rf"\b{re.escape(banned)}\b", everything), banned
    for page in ("index.html", "login.html"):
        assert "family=IBM+Plex+Sans:wght@400;500;600;700&amp;family=IBM+Plex+Sans+JP" in (WEB / page).read_text(encoding="utf-8")


def test_design_md_is_shipped_with_the_web_folder():
    assert (WEB / "DESIGN.md").read_text(encoding="utf-8").startswith("# FactLock DESIGN.md")


# ---------------------------------------------------------------- exhibits
def test_cycle_exhibit_places_only_areas_with_both_series(sheet):
    cycle = build_exhibits(sheet)["cycle"]
    assert cycle["cells"] == {1: ["USA", "JPN"], 2: [], 3: [], 4: []}
    unplaced = {r["code"]: r["reason"] for r in cycle["rows"] if r["phase"] is None}
    assert unplaced == {"G7": "no_rate", "A5M": "no_rate", "CHN": "no_rate"}


def test_contribution_bars_stack_to_the_fund_return(sheet):
    ex = build_exhibits(sheet)["contribution"]
    assert ex["reconciles"] and ex["rows"][0]["start"] == 0
    for a, b in zip(ex["rows"], ex["rows"][1:]):
        assert b["start"] == pytest.approx(a["end"])
    assert ex["rows"][-1]["end"] == pytest.approx(sheet.facts["fund.ret_1m"].value)
    assert ex["axis"] == {"min": 0.0, "max": 2.4, "ticks": [0.0, 1.0, 2.0]}


def test_contribution_axis_handles_a_negative_contribution(sheet):
    changed = sheet.model_copy(deep=True)
    changed.facts["fund.contrib.SPAB"].value = -0.5
    changed.facts["fund.ret_1m"].value = sum(changed.facts[f"fund.contrib.{t}"].value for t in ("SPY", "SPDW", "SPEM", "SPAB"))
    ex = build_exhibits(changed)["contribution"]
    assert ex["reconciles"] and ex["axis"]["min"] <= 0 and ex["axis"]["max"] >= max(r["end"] for r in ex["rows"])


def test_check_summary_folds_families_and_keeps_every_check(sheet):
    checks = build_exhibits(sheet)["checks"]
    assert sum(len(r["members"]) for r in checks["rows"]) == len(sheet.checks) == checks["total"]
    assert checks["counts"] == {"pass": 13, "warn": 1, "fail": 0}
    assert all(r["text_ja"] for r in checks["rows"])


def test_sources_fold_the_spdr_files_and_keep_the_oecd_citations(sheet):
    src = build_exhibits(sheet)["sources"]
    assert [r["publisher"] for r in src["rows"]] == ["State Street Global Advisors", "OECD", "OECD"]
    assert src["rows"][0]["keys"] == ["ssga_product", "ssga_navhist", "ssga_holdings"]
    assert all(r["citation"] for r in src["rows"] if r["publisher"] == "OECD")
    assert src["retrieved"] == ["2026-09-20", "2026-09-23"]


def test_facts_endpoint_includes_the_exhibits():
    settings.passphrase = ""
    with TestClient(app) as client:
        body = client.get("/api/facts", params={"month": "2026-08"}).json()
    assert set(body["exhibits"]) == {"cycle", "contribution", "checks", "sources"}


# -------------------------------------------------- leading-indicator window
def test_cli_window_is_long_enough_to_state_the_run(sheet):
    snap = load_snapshot(ROOT / "data" / "snapshots", "DGBF", "2026-08")
    assert all(len(v["series"]) == 37 and v["series"][0][0] == "2023-08" for v in snap["macro"]["cli"].values())
    g7 = sheet.facts["cli.G7.streak"]
    assert g7.text_en == "has risen for 16 consecutive months" and g7.text_ja == "16か月連続の上昇"
    assert "2025-05 to 2026-08" in g7.derivation and "data from 2023-08" in g7.derivation


# ------------------------------------------------ display helpers (Node.js)
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed; the display helpers are tested with it")


def run_node(script: str, payload) -> object:
    code = f"const F = require({json.dumps(str(WEB / 'format.js'))});\nconst input = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n{script}"
    out = subprocess.run([NODE, "-e", code], input=json.dumps(payload), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@needs_node
def test_js_formatting_matches_render_py_for_every_fact_format_and_language(sheet):
    cases, expected = [], []
    for fid, fact in sheet.facts.items():
        for fmt in FORMATS_BY_UNIT[fact.unit]:
            for lang in ("en", "ja"):
                cases.append({"fact": fact.model_dump(), "fmt": fmt, "lang": lang})
                expected.append(re.sub(r"(^|[^\w.\-])-(?=\d)", "\\1\u2212", format_fact(fact, fmt, lang)))
    got = run_node("console.log(JSON.stringify(input.map((c) => F.formatFact(c.fact, c.fmt, c.lang))));", cases)
    assert len(cases) > 200                           # positive control: every fact was compared
    assert got == expected


@needs_node
def test_minus_sign_is_replaced_for_display_only():
    got = run_node("console.log(JSON.stringify(input.map(F.displayMinus)));",
                   ["-0.14 pt", "fell -0.5%", "ex-U.S.", "2026-08", "gap -0.0030 pt", "+1.08 pp"])
    assert got == ["\u22120.14 pt", "fell \u22120.5%", "ex-U.S.", "2026-08", "gap \u22120.0030 pt", "+1.08 pp"]
    # the checking logic keeps the ASCII hyphen
    assert "-" in format_fact(sheet_fact_negative(), "signed", "en")


def sheet_fact_negative():
    from app.models import Fact
    return Fact(id="x", unit="pt", value=-0.14, as_of="2026-08-31", label_en="x", label_ja="x", source="derived")


@needs_node
def test_units_follow_render_py_in_both_languages():
    facts = {"contrib": 1.084, "pp": 0.08, "pt": 0.0772, "pct": 2.71}
    payload = [{"unit": u, "value": v, "decimals": 2} for u, v in facts.items()]
    got = run_node("console.log(JSON.stringify(input.map((f) => [F.formatFact(f, 'signed', 'en'), F.formatFact(f, 'signed', 'ja')])));", payload)
    assert got == [["+1.08 pp", "+1.08%"], ["+0.08 pp", "+0.08%ポイント"], ["+0.08 pt", "+0.08ポイント"], ["+2.71%", "+2.71%"]]


@needs_node
def test_which_facts_are_locked_and_how_notes_are_shared(sheet):
    ids = ["etf.SPY.ret_1m", "cli.G7.streak", "holdings.as_of", "cli.G7.trend", "report.month", "etf.SPY.label",
           "lookthrough.1.name", "cli.G7.label", "etf.SPY.weight", "lookthrough.2.weight"]
    payload = {"facts": {i: sheet.facts[i].model_dump() for i in ids}, "ids": ids}
    got = run_node("console.log(JSON.stringify(input.ids.map((i) => [F.isLocked(i, input.facts[i]), F.noteGroup(i)])));", payload)
    locked = {i: g[0] for i, g in zip(ids, got)}
    assert [i for i, v in locked.items() if v] == ["etf.SPY.ret_1m", "cli.G7.streak", "holdings.as_of", "cli.G7.trend",
                                                   "etf.SPY.weight", "lookthrough.2.weight"]
    assert got[ids.index("etf.SPY.weight")][1] == "group:weights" and got[ids.index("lookthrough.2.weight")][1] == "group:lookthrough"


@needs_node
def test_notes_never_lowercase_a_ticker_and_state_the_run(sheet):
    payload = sheet.model_dump()
    got = run_node("""const s = input;
      const a = F.noteText('etf.SPY.ret_1m', ['etf.SPY.ret_1m'], s, 'en', () => null, null);
      const b = F.noteText('cli.G7.streak', ['cli.G7.streak'], s, 'en', (i) => (i === 'cli.G7.level' ? 1 : null), null);
      console.log(JSON.stringify([a.body, b.body, b.refs]));""", payload)
    assert got[0].startswith("SPY total return") and "NAV cross-check gap \u22120.3 bp" in got[0]
    assert "2025-05 to 2026-08" in got[1] and got[2] == [1]


@needs_node
def test_nav_gap_is_shown_in_basis_points_in_both_languages(sheet):
    # -0.0030 pp -> -0.3 bp; SPAB's +0.3589 pp distribution gap -> +35.9 bp (1 bp = 0.01 pp)
    got = run_node("""const s = input;
      const a = F.noteText('etf.SPY.ret_1m', ['etf.SPY.ret_1m'], s, 'ja', () => null, null);
      const b = F.noteText('etf.SPAB.ret_1m', ['etf.SPAB.ret_1m'], s, 'en', () => null, null);
      console.log(JSON.stringify([a.body, b.body]));""", sheet.model_dump())
    assert "騰落率との差：\u22120.3bp" in got[0]
    assert "Gap to NAV change +35.9\u00a0bp" in got[1] or "Gap to NAV change +35.9 bp" in got[1]


@needs_node
def test_nav_gap_in_every_note_matches_the_nav_data(sheet):
    """The basis points shown in each note, against a recomputation from the snapshot's NAVs.

    Written without the product's formatting: the gap is recomputed here from the published return
    and the two month-end NAVs, so a note that rounds too early (as SPAB's once did: +36.0 bp for a
    +35.89 bp gap) fails."""
    snapshot = json.loads((ROOT / "data" / "snapshots" / "dgbf_2026-08.json").read_text(encoding="utf-8"))
    tickers = list(snapshot["etfs"])
    got = run_node("""const s = input.sheet;
      console.log(JSON.stringify(input.tickers.map((t) => F.noteText(`etf.${t}.ret_1m`, [`etf.${t}.ret_1m`], s, 'en',
        () => null, null).body)));""", {"sheet": sheet.model_dump(), "tickers": tickers})
    for ticker, body in zip(tickers, got):
        etf = snapshot["etfs"][ticker]
        nav_change = (etf["nav_month_end"]["nav"] / etf["nav_prev_month_end"]["nav"] - 1) * 100
        want = round((etf["tr_1m"] - nav_change) * 100, 1)
        shown = re.search(r"([+\u2212-]?\d+\.\d)\u00a0?\s?bp", body)
        assert shown, (ticker, body)
        assert float(shown.group(1).replace("\u2212", "-")) == want, (ticker, body, want)


def test_app_js_has_no_browser_storage():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "localStorage" not in js and "sessionStorage" not in js


@pytest.mark.parametrize("ignore_file", [".dockerignore", ".gcloudignore"])
def test_design_notes_are_not_deployed(ignore_file):
    # /static/ is reachable without the passphrase, so the design notes must stay out of the container.
    assert "web/DESIGN.md" in (ROOT / ignore_file).read_text(encoding="utf-8").splitlines()


def test_login_specimen_states_no_number_count():
    # The specimen is static; a count ("19 of 19") would disagree with the live draft (approved 2026-09-23).
    assert not __import__("re").search(r"\d+ of \d+", (WEB / "login.html").read_text(encoding="utf-8"))


def test_design_md_has_no_private_canvas_url():
    # web/DESIGN.md ships in the public GitHub repo (git is not filtered by .dockerignore/.gcloudignore),
    # so a claude.ai canvas link — shared as "Only you" — must never appear in it.
    assert "claude.ai/artifact" not in (WEB / "DESIGN.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("page", ["index.html", "login.html", "app.js", "format.js"])
def test_ui_text_is_not_tied_to_one_event(page):
    # The screens and the printed paper are reused for other demonstrations, so the name of a
    # competition and wording that only makes sense during one ("judging", "submission materials")
    # stay in README.md, not in the UI.
    text = (WEB / page).read_text(encoding="utf-8")
    for phrase in ("Builder Cup", "hackathon", "judging", "submission materials"):
        assert phrase.lower() not in text.lower(), (page, phrase)


@needs_node
def test_every_derivation_has_japanese_wording(sheet):
    # Notes in Japanese must not fall back to the English derivation (found on the live service, 2026-09-23).
    derivations = sorted({f.derivation for f in sheet.facts.values() if f.derivation})
    got = run_node("console.log(JSON.stringify(input.map(F.derivationJa)));", derivations)
    assert len(derivations) >= 15                       # positive control: the sheet was read
    untranslated = [d for d, j in zip(derivations, got) if re.search(r"[a-z]{3,}", j)]
    assert untranslated == []
    assert "16か月連続の上昇（2025年5月〜2026年8月、データは2023年8月から）" in got and "当月の水準−前月の水準" in got
    assert "資産クラス別寄与度の最大値" in got and "資産クラス別寄与度の最小値" in got
    assert "寄与度が最大の資産クラス" in got and "寄与度が最小の資産クラス" in got


# ---- first-run guidance and the report selector (v1.19) -------------------------------------------

def test_before_the_first_draft_the_page_says_what_to_press():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "} else if (!result) {" in js and "To begin, press \\u201c${generateLabel()}\\u201d at the top right." in js
    guide = js[js.index("function guideBlock"):js.index("function checksBlock")]
    assert '"wide screen-only"' in guide and '"data-block": "guide"' in guide   # full width; on screen only, never printed
    assert "How to use this demo" in guide and "このデモの使い方" in guide
    assert guide.count("<li") == 0 and "steps.map" in guide
    # The guide names the button by the mode, never by its current text (which is "Drafting…" while busy).
    assert '$("#generate").textContent' not in guide and "generateLabel()" in guide
    assert 'place(guideBlock(primary));' in js


def test_report_selector_has_even_gaps():
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    rule = css[css.index(".band-field select {"):css.index(".band-field select:focus-visible")]
    assert "appearance: none" in rule and "padding: 0 30px 0 10px" in rule
    # 10px + the glyph's own side bearing on the left equals 11px on the right (measured 11.5px both, 2026-09-24)
    assert "background-position: right 11px center" in rule and "%237FD1EF" in rule


# ---- pages and their scripts stay in step (v1.20) --------------------------------------------------

def test_pages_load_scripts_by_version_and_static_ignores_the_query():
    from app.main import ASSET_VERSION, asset_version
    assert re.fullmatch(r"[0-9a-f]{10}", ASSET_VERSION) and asset_version() == ASSET_VERSION
    settings.passphrase = ""
    with TestClient(app) as client:
        for path, assets in (("/", ("styles.css", "format.js", "app.js")), ("/login", ("styles.css",))):
            html = client.get(path).text
            assert "__ASSET_VERSION__" not in html
            for asset in assets:
                assert f'"/static/{asset}?v={ASSET_VERSION}"' in html
            assert html.count("/static/") == len(assets)                 # nothing loads without a version
        assert client.get(f"/static/app.js?v={ASSET_VERSION}").status_code == 200


def test_asset_version_follows_the_files(tmp_path):
    from app.main import asset_version
    for name in ("styles.css", "format.js", "app.js", "index.html", "login.html"):
        (tmp_path / name).write_bytes((WEB / name).read_bytes())
    same = asset_version(tmp_path)
    (tmp_path / "app.js").write_bytes((WEB / "app.js").read_bytes() + b"\n// changed\n")
    assert asset_version(tmp_path) != same


def test_a_page_that_fails_to_load_asks_for_a_reload_in_plain_words():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    tail = js[js.index("init().catch("):]
    assert 'state.errorKind = "load"' in tail and "Please reload the page" in tail
    assert "error.message" not in tail and 'console.error("FactLock: the page did not load.", error)' in tail
    assert 'state.errorKind === "load" ? "The page did not load. " : "Drafting stopped. "' in js


@needs_node
def test_note_finds_inputs_cited_under_another_name_and_says_what_is_missing(sheet):
    """Page 2 of the 2026-09-24 draft: the fund return is the sum of four contributions, but only two of
    them had notes under their own names; SPY's and SPAB's were in the text as the largest and the
    smallest contribution. The note must list all four, and say when a part of the sum is not cited."""
    notes = {"fund.top_contributor_value": 12, "fund.contrib.SPDW": 13, "fund.contrib.SPEM": 14,
             "fund.smallest_contributor_value": 15}
    got = run_node("""const s = input.sheet; const notes = input.notes;
      const t = (lang, n) => F.noteText('fund.ret_1m', ['fund.ret_1m'], s, lang, (i) => n[i] || null, null).body;
      const partial = Object.fromEntries(Object.entries(notes).filter(([k]) => k !== 'fund.smallest_contributor_value'));
      console.log(JSON.stringify([t('en', notes), t('ja', notes), t('en', partial), t('ja', partial), t('en', {})]));""",
                   {"sheet": sheet.model_dump(), "notes": notes})
    assert "from notes 12, 13, 14 and 15" in got[0]
    assert "注記12・13・14・15から算出" in got[1]
    assert "from notes 12, 13 and 14 and 1 input not cited in the text" in got[2]
    assert "注記12・13・14と本文にない1件の入力から算出" in got[3]
    assert "from note" not in got[4] and "Σ target weight × component 1-month total return" in got[4]
    # A note never points at itself: the largest contribution's only input is SPY's contribution, which is
    # in the text only as this very note.
    own = run_node("""const s = input.sheet;
      console.log(JSON.stringify(F.noteText('fund.top_contributor_value', ['fund.top_contributor_value'], s, 'en',
        (i) => ({'fund.top_contributor_value': 13})[i] || null, null).body));""", {"sheet": sheet.model_dump()})
    assert "from note" not in own and "Contribution of the largest contributor" in own

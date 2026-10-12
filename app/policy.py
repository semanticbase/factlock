"""Claims policy for fund commentary (English and Japanese).

"error" rules block publication; "warning" rules ask a human to look again.
The lists are deliberately small and readable so that a compliance team can
review and extend them.
"""
from __future__ import annotations

import re

PROHIBITED: list[tuple[str, str, re.Pattern]] = [
    # (code, message, pattern)
    ("guarantee", "Guarantees or certainty about returns are not allowed.",
     # A negated guarantee ("not a guarantee", "no guarantee", "cannot guarantee", "without any guarantee") is
     # the disclaimer a fund report should carry, not a claim. Fixed-width lookbehinds only (Python).
     re.compile(r"(?<!not )(?<!no )(?<!not a )(?<!never )(?<!cannot )(?<!can't )(?<!does not )(?<!do not )"
                r"(?<!without )(?<!without a )(?<!without any )(?<!is not a )(?<!are not )(?<!is not )"
                r"\b(guarantee[sd]?|risk[- ]free|riskless|cannot lose|will (certainly|definitely|surely)|(sure|certain) to (rise|fall|gain|increase|recover))\b", re.I)),
    ("recommendation", "Investment recommendations are outside the scope of an information service.",
     re.compile(r"\b(we recommend|recommend (buying|selling|investing)|investors should|you should (buy|sell|invest)|buying opportunity|good time to (buy|sell|invest))\b", re.I)),
    ("lead_claim", "Do not claim that an indicator leads or predicts markets by a specific period.",
     re.compile(r"\bleads?\b[^.]{0,40}\bby\b[^.]{0,25}\b(weeks?|months?|quarters?)\b|\b(ahead of|in advance of)\b[^.]{0,30}\b(stock|equit\w*|market|prices?)\b|\bpredict(s|ed|ing)?\b[^.]{0,40}\b(stock|equit\w*|market|prices?|returns?)\b", re.I)),
]

PROHIBITED_JA: list[tuple[str, str, re.Pattern]] = [
    ("guarantee", "利回りや元本の保証、将来の断定は記載できません。",
     # 「保証されておらず」「保証されているものではありません」などの否定形は免責であって主張ではない。
     re.compile(r"(元本保証(?!はありません|ではありません|ではなく|されて(?:い|お)らず)"
                r"|(?<!元本)保証(?!するものでは|するものでも|されるものでは|されるものでも|されていません|されておらず|されていない|されて(?:い|お)らず"
                r"|されている(?:もの|わけ)では|しているものでは|しない|されない|されません|しておりません|はありません|はございません|の対象外|なし)"
                r"|必ず(?!しも)|確実に|間違いなく|絶対(?!値|的)|リスクはありません|損失は(生じ|発生し)ません)")),
    ("recommendation", "投資の推奨は情報提供の範囲を超えます。",
     re.compile(r"(買い推奨|売り推奨|(購入|売却|投資)を(お勧め|推奨)|投資すべき|買い時|売り時)")),
    ("lead_claim", "指標が相場や景気に一定期間先行すると断定しないでください。",
     re.compile(r"([かヶカヵケ]月先行|先行して[^。]{0,12}(株価|相場|市場)|(株価|相場)を予測)")),
]

CAUTION: list[tuple[str, str, re.Pattern]] = [
    ("unhedged_forecast", "Forward-looking statement without hedging; use cautious wording (may, could).",
     re.compile(r"\bwill (continue to )?(rise|fall|increase|decrease|rebound|recover|decline|outperform|underperform|improve|weaken|strengthen)\b", re.I)),
    ("dramatic_wording", "Dramatic wording; make sure the magnitude supports it.",
     re.compile(r"\b(soar(ed|s|ing)?|plung(e|ed|es|ing)|skyrocket(ed|s)?|collaps(e|ed|es)|crash(ed|es)?|surg(e|ed|es|ing))\b", re.I)),
    ("causal_claim", "Causal explanation; the cause must be supported by the facts provided.",
     re.compile(r"\b(because|due to|driven by|on the back of|as a result of|thanks to|owing to)\b", re.I)),
]

CAUTION_JA: list[tuple[str, str, re.Pattern]] = [
    ("unhedged_forecast", "断定的な見通しです。「〜の可能性があります」などの慎重な表現にしてください。",
     re.compile(r"(上昇|下落|回復|反発|改善|悪化|低下)する(でしょう|見込みです|と予想されます)")),
    ("dramatic_wording", "強い表現です。変化の大きさに見合うか確認してください。",
     re.compile(r"(急騰|暴騰|急落|暴落|大暴落|大幅)")),
    ("causal_claim", "要因の説明です。与えられた事実で裏付けられる要因か確認してください。",
     re.compile(r"(を受けて|を背景に|の影響で|が要因|によるもの)")),
]

# Direction words the model might type itself (placeholders render their own).
DIRECTION_EN = {
    "up": re.compile(r"\b(rose|rise[sn]?|rising|gain(s|ed)?|increas(e|ed|es)|climb(ed|s)?|advanc(e|ed|es)|rall(y|ied|ies)|rebound(ed|s)?|improv(e|ed|es)|strengthen(ed|s)?|(edged|moved|went|ticked) up)\b", re.I),
    "down": re.compile(r"\b(fell|fall(s|en)?|falling|declin(e|ed|es)|decreas(e|ed|es)|drop(ped|s)?|slip(ped|s)?|lost|loss(es)?|retreat(ed|s)?|weaken(ed|s)?|deteriorat(e|ed|es)|slid|slump(ed|s)?|(edged|moved|went|ticked) down)\b", re.I),
}
DIRECTION_JA = {
    "up": re.compile(r"(上昇|増加|反発|続伸|改善|回復|堅調|好調|押し上げ|プラス)"),
    "down": re.compile(r"(下落|減少|反落|続落|悪化|低下|軟調|不振|押し下げ|マイナス)"),
}

# Numbers the model must never type (all numbers come from placeholders).
TYPED_NUMBER_EN = re.compile(r"[0-9０-９]|\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twice|half)\b(?=\s+(\w+\s+)?(months?|years?|weeks?|days?|quarters?|times|percent|points?)\b)", re.I)
TYPED_NUMBER_JA = re.compile(r"[0-9０-９]|[〇一二三四五六七八九十百千万億兆]+\s*(か月|ヶ月|カ月|ヵ月|年|倍|割|%|％|ポイント|日|週|四半期)")

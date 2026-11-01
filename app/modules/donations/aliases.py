"""Find a CS2 weapon in free donation text.

Both the text and every alias go through the same normalisation: lower case, ё→е, Cyrillic transliterated
to Latin and everything that is not a letter or digit treated as a separator. So «АК-47», «ак 47», «ak47»
and a mixed-layout «aк47» are the same thing, and an alias can be written in either alphabet.

Matching runs over the token sequence: an alias may span up to four neighbouring tokens («m4 a1 s»,
«эмка с глушителем»). Aliases ending with ``*`` match any token that starts with them («калаш*» covers
«калашом», «калаша»); exact aliases must match whole tokens, so «ак» never fires inside «как».
The earliest match in the text wins, and at the same position the longest one.
"""
from __future__ import annotations

import re

from ... import weapons as W

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}
_TOKEN = re.compile(r"[a-z0-9]+")
# Note: «р» is transliterated phonetically (r), so the look-alike spellings «р250», «р90» are listed as
# separate aliases below instead of mapping Cyrillic letters to the Latin ones they resemble.
MAX_SPAN = 4

# weapon id -> aliases. Kept deliberately conservative: an alias that is also a common Russian word
# («новая», «хм», «текст», «магазин») would turn thank-you messages into weapon requests.
DEFAULT_ALIASES: dict[str, list[str]] = {
    "glock": ["glock", "glock18", "глок*"],
    "usp_silencer": ["usp", "usps", "юсп", "юспс", "усп", "успс", "усп с"],
    "hkp2000": ["p2000", "п2000", "р2000", "hkp2000", "p2k", "п2к"],
    "p250": ["p250", "п250", "р250"],
    "elite": ["elite", "elites", "dualies", "dual berettas", "berettas", "дуал*", "беретт*", "двойные"],
    "tec9": ["tec9", "tec", "тек", "тек9", "тэк", "тэк9"],
    "fiveseven": ["fiveseven", "five seven", "five7", "файв*", "файвсевен"],
    "cz75a": ["cz", "cz75", "cz75a", "цз", "цз75", "чешк*", "чех"],
    "deagle": ["deagle", "deag", "desert eagle", "дигл*", "дигал*", "дезерт*"],
    "revolver": ["r8", "р8", "revolver", "револьвер*", "револик*"],
    "mac10": ["mac", "mac10", "мак", "мак10", "макен*"],
    "mp9": ["mp9", "мп9"],
    "mp7": ["mp7", "мп7"],
    "mp5sd": ["mp5", "mp5sd", "мп5", "мп5сд", "морков*"],
    "ump45": ["ump", "ump45", "умп*", "юмп*"],
    "p90": ["p90", "п90", "р90", "пэ90", "пи90"],
    "bizon": ["bizon", "pp bizon", "бизон*", "пп бизон"],
    "nova": ["nova", "нова", "нову"],
    "xm1014": ["xm", "xm1014", "хм1014", "икс эм", "крокодил*", "автодроб*"],
    "sawedoff": ["sawedoff", "sawed off", "sawed", "обрез*"],
    "mag7": ["mag7", "mag", "маг7", "маг", "магсем*"],
    "m249": ["m249", "м249"],
    "negev": ["negev", "негев*"],
    "galilar": ["galil", "galilar", "галил*", "галя", "галю", "галей"],
    "famas": ["famas", "фамас*"],
    "ak47": ["ak", "ak47", "ак", "ак47", "калаш*", "акаш*", "калашников*"],
    "m4a1": ["m4a4", "м4а4", "m4", "м4", "эмк*"],
    "m4a1_silencer": ["m4a1s", "m4a1", "м4а1", "м4а1с", "m4s", "м4с", "эмка с глушителем", "эмку с глушителем",
                      "эмка с глушаком", "эмку с глушаком"],
    "sg556": ["sg", "sg553", "sg556", "сг", "сг553", "криг*", "krieg"],
    "aug": ["aug", "ауг*"],
    "ssg08": ["ssg", "ssg08", "scout", "ссг", "скаут*", "муха", "муху", "мухой", "мухе"],
    "awp": ["awp", "avp", "авп*", "слон", "слона", "слоном", "слону", "авик*"],
    "g3sg1": ["g3sg1", "g3", "г3", "г3сг1"],
    "scar20": ["scar", "scar20", "скар*"],
    "taser": ["zeus", "taser", "x27", "зевс*", "тазер*", "шокер*"],
    "knife": ["knife", "нож*", "финк*", "перо"],
}


def normalize(text: str) -> str:
    out = []
    for ch in str(text or "").lower():
        out.append(_TRANSLIT.get(ch, ch))
    return "".join(out)


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(normalize(text))


_RAW_TOKEN = re.compile(r"[0-9a-zа-яё]+")


def _spans(text: str) -> list[tuple[str, int, int]]:
    """(normalised token, start, end in the original text): lets a match be reported as the donor wrote it."""
    out = []
    for m in _RAW_TOKEN.finditer(str(text or "").lower()):
        for part in _TOKEN.findall(normalize(m.group(0))):    # «ъ»/«ь» vanish, nothing else splits
            out.append((part, m.start(), m.end()))
    return out


class AliasIndex:
    """exact: normalised alias (tokens joined) -> weapon id; prefix: list of (stem, weapon id)."""

    def __init__(self, extra: dict[str, str] | None = None) -> None:
        self.exact: dict[str, str] = {}
        self.prefix: list[tuple[str, str]] = []
        for wid, names in DEFAULT_ALIASES.items():
            for name in names:
                self._add(name, wid)
        for w in W.CATALOG:                                        # official names and ids always work
            self._add(w.name, w.id)
            self._add(w.id, w.id)
        for name, wid in (extra or {}).items():                    # user additions override defaults
            self._add(name, wid, override=True)
        self.prefix.sort(key=lambda p: -len(p[0]))                 # longest stem first

    def _add(self, alias: str, wid: str, override: bool = False) -> None:
        alias = alias.strip()
        star = alias.endswith("*")
        key = "".join(tokens(alias.rstrip("*")))
        if not key or wid not in W.BY_ID:
            return
        if star:
            self.prefix = [p for p in self.prefix if p[0] != key] if override else self.prefix
            if override or all(p[0] != key for p in self.prefix):
                self.prefix.append((key, wid))
        elif override or key not in self.exact:
            self.exact[key] = wid

    def find(self, text: str) -> tuple[str, str] | None:
        """(weapon id, matched text as written) of the earliest/longest alias in ``text``, or None."""
        spans = _spans(text)
        toks = [t for t, _, _ in spans]
        for i in range(len(toks)):
            best: tuple[int, str, int] | None = None     # (matched length, weapon, last token index)
            joined = ""
            for j in range(i, min(len(toks), i + MAX_SPAN)):
                joined += toks[j]
                wid = self.exact.get(joined)
                if wid and (best is None or len(joined) > best[0]):
                    best = (len(joined), wid, j)
            if best is None:
                for stem, wid in self.prefix:
                    if toks[i].startswith(stem):
                        best = (len(stem), wid, i)
                        break
            if best is not None:
                return best[1], str(text)[spans[i][1]:spans[best[2]][2]]
        return None


def parse_extra(text: str) -> tuple[dict[str, str], list[str]]:
    """User additions, one per line: ``алиас = оружие`` (оружие — id, название или уже известный алиас).

    Returns (alias -> weapon id, list of problems by line). Empty lines and ``#`` comments are skipped."""
    base = AliasIndex()
    out: dict[str, str] = {}
    errors: list[str] = []
    for n, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            errors.append(f"строка {n}: нужен формат «алиас = оружие»")
            continue
        alias, target = (part.strip() for part in line.split("=", 1))
        if not alias or not target:
            errors.append(f"строка {n}: пустой алиас или оружие")
            continue
        found = base.find(target)
        if not found or "".join(tokens(found[1])) != "".join(tokens(target.rstrip("*"))):
            errors.append(f"строка {n}: не знаю оружие «{target}»")
            continue
        out[alias] = found[0]
    return out, errors


# not glued to letters on the left: in «п90» or «р250» the digits belong to a weapon name
_NUMBER = re.compile(r"(?<![\w.,])(\d{1,2}[  .,]\d{3}|\d{2,5})(?!\d)")


def find_number(text: str) -> int | None:
    """First integer in the text that looks like a DPI value («1600», «1 600», «1.600»)."""
    m = _NUMBER.search(str(text or ""))
    if not m:
        return None
    return int(re.sub(r"\D", "", m.group(1)))

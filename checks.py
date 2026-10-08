"""
checks.py — facts the CODE verifies, so Runa never has to trust the AI on them.

Every function here reads only what the human literally typed. None of them
call the AI. When the AI's answer disagrees with these checks, Runa asks the
human instead of writing.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# ================================================================ injection ==
# Text that tries to talk to the system instead of reporting operations.
_INJECTION = [
    r"\b(identity_confidence|content_confidence|delivery_id|new_eta|confidence)\b",
    r"^\s*(system|assistant|developer)\s*:",
    r"\babaikan\b.{0,30}\b(aturan|instruksi|perintah|prompt|sistem)\b",
    r"\bignore\b.{0,30}\b(rules|instructions|prompt|previous)\b",
    r"\b(anggap|set|atur|ubah)\b.{0,20}\b(confidence|skor|score)\b",
]


def looks_like_injection(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low, re.M) for p in _INJECTION)


# =================================================================== hedges ==
# Words that turn a statement into an estimate. A hedged time is not a fact.
HEDGES = [
    "kayaknya", "kyknya", "kayanya", "kynya", "kira-kira", "kira2", "kirakira",
    "sepertinya", "spertinya", "mungkin", "mngkn", "sekitar", "sekitaran",
    "harusnya", "seharusnya", "semoga", "moga-moga", "moga2", "insya allah",
    "insyaallah", "insha allah", "inshallah", "insyallah", "palingan",
    "kalau lancar", "kalo lancar", "klo lancar", "probably", "maybe",
    "approximately", "approx", "should be", "hopefully", "i think", "i guess",
    "around",
]


def hedge_word(text: str) -> str | None:
    """The first hedge word in the text, or None."""
    low = text.lower()
    for h in HEDGES:
        if re.search(rf"(?<![a-z]){re.escape(h)}(?![a-z])", low):
            return h
    return None


# ======================================================= delivery numbers ==
_DO_PREFIX = r"(?:\bdo\b|\bno\.?|\bnomor\b|\bnomer\b|#)\s*[:.]?\s*(\d{3,10})\b"


def mentioned_deliveries(text: str, known: set[str]) -> list[str]:
    """Delivery numbers the human literally typed.

    - Full 10-digit numbers count, known or not.
    - Short forms ("yang 4521") count if they match the end of exactly one
      real delivery.
    - A short number right after "DO" that matches nothing ("DO 4251") is
      kept as-is, so Runa asks about it instead of the AI guessing.
    """
    found = []
    for m in re.finditer(_DO_PREFIX, text, re.I):
        token = m.group(1)
        if len(token) == 10:
            found.append(token)
            continue
        matches = [k for k in known if k.endswith(token)]
        found.append(matches[0] if len(matches) == 1 else token)
    for token in re.findall(r"\b\d{4,10}\b", text):
        if len(token) == 10:
            found.append(token)
        else:
            matches = [k for k in known if k.endswith(token)]
            if len(matches) == 1:
                found.append(matches[0])
    return list(dict.fromkeys(found))


# ================================================================ materials ==
def materials_mentioned(text: str, aliases: dict[str, list[str]]) -> set[str]:
    """Material codes whose everyday name appears in the text ("mie" -> FG-4471)."""
    low = text.lower()
    return {code for code, names in aliases.items()
            if any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", low) for n in names)}


# ==================================================================== units ==
UNIT_WORDS = {
    "CTN": {"ctn", "karton", "kartonnya", "kardus", "kardusnya", "dus", "dusnya",
            "doos", "box", "boks", "carton", "cartons", "kotak"},
    "PC": {"pc", "pcs", "biji", "buah", "lembar", "piece", "pieces"},
    "PAL": {"palet", "paletnya", "pallet", "pallets"},
    "KG": {"kg", "kilo", "kilogram"},
    "TON": {"ton"},
}
UNIT_NAME = {"CTN": "karton", "PC": "pcs", "PAL": "palet", "KG": "kg", "TON": "ton"}
_NUMBER_WORDS = r"satu|dua|tiga|empat|lima|enam|tujuh|delapan|sembilan|sepuluh|se"


def unit_of(word: str | None) -> str | None:
    if not word:
        return None
    w = word.lower().strip()
    return next((u for u, words in UNIT_WORDS.items() if w in words), None)


def unit_mismatch(text: str, delivery_unit: str,
                  ai_unit_word: str | None = None) -> tuple[str, str] | None:
    """(number+word as typed, unit) if the human counted in a different unit
    than the delivery uses — e.g. "2 palet" on a delivery counted in cartons."""
    for m in re.finditer(rf"\b(\d+(?:[.,]\d+)?|{_NUMBER_WORDS})\s*([a-zA-Z]+)\b", text, re.I):
        unit = unit_of(m.group(2))
        if unit and unit != delivery_unit:
            return m.group(0), unit
    unit = unit_of(ai_unit_word)
    if unit and unit != delivery_unit:
        return ai_unit_word, unit
    return None


# ==================================================================== times ==
_PERIODS = [
    ("dini hari", "pagi"), ("subuh", "pagi"), ("pagi", "pagi"), ("morning", "pagi"),
    ("siang", "siang"), ("afternoon", "siang"),
    ("sore", "sore"), ("petang", "sore"), ("evening", "sore"),
    ("malam", "malam"), ("malem", "malam"), ("mlm", "malam"),
    ("tonight", "malam"), ("night", "malam"),
]
_DAYS = [("lusa", 2), ("besok", 1), ("besoknya", 1), ("bsk", 1), ("tomorrow", 1)]
_NUM = {"se": 1, "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6}


@dataclass
class TimeReading:
    """What time the human literally said.
    kind: "none" (no time found), "clear", "ambiguous" (pagi or malam?),
    or "past" (already passed today, and no "besok")."""
    kind: str = "none"
    candidates: list[datetime] = field(default_factory=list)
    said: str = ""


def _period(low: str) -> str | None:
    for word, p in _PERIODS:
        if re.search(rf"(?<![a-z]){word}(?![a-z])", low):
            return p
    return None


def _to_24h(h: int, period: str) -> int:
    if period == "pagi":
        return 0 if h == 12 else h
    if period == "siang":
        return h if h >= 11 else h + 12
    if period == "sore":
        return h if h == 12 else h + 12
    # malam: 1-5 means after midnight; 12 means midnight
    if h == 12:
        return 0
    return h if h <= 5 else h + 12


def read_time(text: str, now: datetime) -> TimeReading:
    low = text.lower()

    # 1. Relative: "dua jam lagi", "sejam lagi", "30 menit lagi", "in 2 hours"
    rel = re.search(r"\b(\d+|setengah|se|satu|dua|tiga|empat|lima|enam)\s*"
                    r"(jam|menit|hours?|minutes?)\s+(?:lagi|from now)\b", low) or \
        re.search(r"\bin\s+(\d+|an?)\s+(hours?|minutes?)\b", low)
    if rel:
        n_raw, unit = rel.group(1), rel.group(2)
        if n_raw == "setengah":
            n = 0.5
        elif n_raw in ("a", "an"):
            n = 1
        else:
            n = float(n_raw) if n_raw.isdigit() else _NUM[n_raw]
        delta = timedelta(hours=n) if unit.startswith(("jam", "hour")) \
            else timedelta(minutes=n)
        return TimeReading("clear", [now + delta], rel.group(0))

    # 2. Clock times
    h = m = None
    said = ""
    explicit_24h = False
    hit = re.search(r"\b(\d{1,2})[:.](\d{2})\b", low)
    if hit:
        h, m, said = int(hit.group(1)), int(hit.group(2)), hit.group(0)
        explicit_24h = h >= 13 or h == 0 or hit.group(1).startswith("0")
    else:
        hit = re.search(r"\bsetengah\s+(\d{1,2})\b(?!\s*(?:jam|menit))", low)
        if hit:                     # "setengah 11" = half past TEN (10:30)
            n = int(hit.group(1))
            h, m, said = (n - 1) % 24 or 12, 30, hit.group(0)
        else:
            hit = re.search(r"\b(?:jam|pukul|pkl|at)\s*(\d{1,2})\b", low) or \
                re.search(r"\b(\d{1,2})\s*(?=(?:malam|malem|mlm|pagi|siang|sore|night))", low)
            if hit:
                h, m, said = int(hit.group(1)), 0, hit.group(0).strip()
            else:
                ampm = re.search(r"\b(\d{1,2})\s*(am|pm)\b", low)
                if ampm:
                    h, m = int(ampm.group(1)), 0
                    said = ampm.group(0)
                    h = (0 if h == 12 else h) if ampm.group(2) == "am" else \
                        (h if h == 12 else h + 12)
                    explicit_24h = True
    if h is None or h > 23 or m > 59:
        return TimeReading()

    period = _period(low)
    if not explicit_24h:
        if h > 12:
            explicit_24h = True
        elif period is None:
            return TimeReading("ambiguous", [], said)
        else:
            h = _to_24h(h, period)

    day_offset = next((d for w, d in _DAYS
                       if re.search(rf"(?<![a-z]){w}(?![a-z])", low)), None)
    base = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if day_offset is not None:
        return TimeReading("clear", [base + timedelta(days=day_offset)], said)
    if base >= now:
        return TimeReading("clear", [base], said)
    if period == "malam" and h <= 5:          # "jam 2 malam" = after midnight
        return TimeReading("clear", [base + timedelta(days=1)], said)
    return TimeReading("past", [base], said)


def latest_time(texts: list[str], now: datetime) -> TimeReading:
    """The most recent message that states a time wins — so corrections work."""
    for text in reversed(texts):
        reading = read_time(text, now)
        if reading.kind != "none":
            return reading
    return TimeReading()

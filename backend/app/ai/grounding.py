"""Grounding check: every number in an AI answer must come from the context it was given.

A number in the answer is grounded if it equals (to the answer's own precision) some number in the
context, or that number × 100 (fractions written as percentages), with the right sign: "-7.4%" or
"down 7.4%" needs a negative number, "up 7.4%" a positive one; a number with no sign or direction
word may match either. Units are applied before comparing ("12cr" is 12 × 10^7, "₹999" and "Rs999"
are 999, "5x" is 5). ISO dates must appear verbatim in the context.

Numbers inside outside text (news evidence and summaries, headlines, Telegram lines) don't count as
context: text the model was shown can't vouch for a number the model then repeats as fact.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

_UNITS = {"cr": 1e7, "crore": 1e7, "crores": 1e7, "lakh": 1e5, "lakhs": 1e5, "lk": 1e5, "k": 1e3,
          "x": 1.0, "bn": 1e9, "mn": 1e6}
# A number, optionally after ₹/Rs/INR and before a unit. Numbers glued to other letters (EMA20,
# RSI14, v1) are identifiers, not claims.
_NUMBER = re.compile(
    r"(?:(?<=₹)|(?<=\bRs)|(?<=\bRs\.)|(?<=\bINR)|(?<![A-Za-z0-9_.]))"
    r"(?P<num>[-+−]?\d[\d,]*(?:\.\d+)?)"
    r"(?:\s?(?P<unit>" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b|(?![A-Za-z0-9_]))", re.I)
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_UP = re.compile(r"\b(up|rose|risen|gained|gains?( of)?|higher|increase[sd]?|grew|growth of|profit of|ahead)\s+(by\s+)?$",
                 re.I)
_DOWN = re.compile(r"\b(down|fell|fallen|drop(ped)?|loss(es)? of|lower|decline[sd]?|declining|negative|behind|"
                   r"lost)\s+(by\s+)?$", re.I)
ALWAYS_ALLOWED = {0.0, 1.0, 2.0, 3.0, 100.0}
# Free text from outside the app (or written by another model): never evidence for a number. Under
# "news", "summary" is the rating model's summary too (elsewhere it is the app's own decision summary).
UNTRUSTED_TEXT_KEYS = frozenset({"evidence", "text", "title", "headline", "line", "excerpt", "snippet", "publisher"})
UNTRUSTED_NEWS_KEYS = UNTRUSTED_TEXT_KEYS | {"summary"}


class GroundingResult(BaseModel):
    grounded: bool
    ungrounded: list[str]
    checked: int


def _parse(token: str) -> tuple[float, int]:
    t = token.replace(",", "").replace("−", "-").lstrip("+")
    decimals = len(t.split(".")[1]) if "." in t else 0
    return float(t), decimals


def extract_numbers(text: str) -> list[str]:
    """The numbers as written, unit included ("12cr")."""
    return [m.group(0) for m in _NUMBER.finditer(_DATE.sub(" ", text))]


def context_values(obj: Any) -> tuple[set[float], set[str]]:
    numbers: set[float] = set()
    strings: set[str] = set()

    def walk(o: Any, trusted: bool = True, news: bool = False) -> None:
        if isinstance(o, bool) or o is None:
            return
        if isinstance(o, (int, float)):
            numbers.add(float(o))
        elif isinstance(o, str):
            if not trusted:
                return
            strings.add(o)
            strings.update(_DATE.findall(o))
            for m in _NUMBER.finditer(_DATE.sub(" ", o)):
                numbers.add(_parse(m.group("num"))[0] * _UNITS.get((m.group("unit") or "").lower(), 1.0))
        elif isinstance(o, dict):
            for k, v in o.items():
                # Field names like top5_weight, ema20, high_52w legitimately let the answer say
                # "top 5", "EMA 20", "52-week".
                if not isinstance(k, str):
                    walk(v, trusted, news)
                    continue
                numbers.update(float(n) for n in re.findall(r"\d+", k))
                walk(v, trusted and k not in (UNTRUSTED_NEWS_KEYS if news else UNTRUSTED_TEXT_KEYS),
                     news or k == "news")
        elif isinstance(o, (list, tuple, set)):
            for v in o:
                walk(v, trusted, news)

    walk(obj)
    return numbers, strings


def _direction(answer: str, start: int, token: str) -> int:
    """-1 / +1 when the sign or the words just before the number say which way it goes, else 0."""
    if token.lstrip()[:1] in "-−":
        return -1
    if token.lstrip()[:1] == "+":
        return 1
    before = answer[max(0, start - 40):start]
    before = re.sub(r"(₹|\bRs\.?|\bINR)\s*$", "", before)
    return -1 if _DOWN.search(before) else 1 if _UP.search(before) else 0


def check_grounding(answer: str, context: Any) -> GroundingResult:
    numbers, strings = context_values(context)
    candidates = numbers | {n * 100 for n in numbers}

    bad: list[str] = []
    text = _DATE.sub(lambda m: " " * len(m.group(0)), answer)  # same offsets, dates checked below
    matches = list(_NUMBER.finditer(text))
    for m in matches:
        value, decimals = _parse(m.group("num"))
        scale = _UNITS.get((m.group("unit") or "").lower(), 1.0)
        if abs(value) in ALWAYS_ALLOWED and scale == 1.0:
            continue
        magnitude, tol = abs(value) * scale, (0.5 * 10 ** (-decimals) + 1e-9) * scale
        sign = _direction(text, m.start(), m.group(0))
        if not any(abs(magnitude - abs(c)) <= tol and (sign == 0 or c == 0 or (c > 0) == (sign > 0))
                   for c in candidates):
            bad.append(m.group(0).strip())

    dates = _DATE.findall(answer)
    known_dates = {d for s in strings for d in _DATE.findall(s)}
    bad += [d for d in dates if d not in known_dates]
    return GroundingResult(grounded=not bad, ungrounded=bad, checked=len(matches) + len(dates))

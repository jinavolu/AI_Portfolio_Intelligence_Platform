"""Read-only feed of public Telegram channels, from Telegram's public web preview (/s/<channel>).

No Telegram login: only public channels, the latest ~20 posts per page, older ones page by page
(`before`). Posts are shown as they are, marked unverified; they never reach signals or alerts
(decisions.md D11 keeps signals on official disclosures and verified press). t.me is blocked on some
networks, so the host is configurable (telegram.me serves the same pages).
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime
from html.parser import HTMLParser

from app.broker.instruments import nse_equity_names
from app.db import Repository

CHANNELS_KEY = "telegram_feed_channels"
CACHE_SECONDS = 300  # be gentle with Telegram: one fetch per channel page per 5 minutes
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class FeedError(RuntimeError):
    pass


def normalize_channel(raw: str) -> str:
    """'@name', 't.me/name', 'https://t.me/s/name/123' -> 'name'."""
    s = raw.strip()
    s = re.sub(r"^(https?://)?(www\.)?(t|telegram)\.(me|dog)/(s/)?", "", s).lstrip("@").split("/")[0].split("?")[0]
    if not _NAME.match(s):
        raise FeedError(f"{raw!r} is not a public channel name or t.me link")
    return s


class _PreviewParser(HTMLParser):
    """Pulls posts out of the /s/<channel> page: id, date, text, views, media flag."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.before: int | None = None
        self.posts: list[dict] = []
        self._post: dict | None = None
        self._text_depth = 0   # >0 while inside the message text div (counts nested divs)
        self._title_depth = 0
        self._in_views = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "tme_messages_more" in cls and a.get("data-before"):
            self.before = int(a["data-before"])
        if tag == "div" and "tgme_channel_info_header_title" in cls.split():
            self._title_depth, self.title = 1, ""
            return
        if tag == "div" and a.get("data-post") and "tgme_widget_message" in cls.split():
            self._post = {"post": a["data-post"], "text": "", "date": None, "views": None, "media": False}
            self.posts.append(self._post)
            return
        p = self._post
        if p is None:
            return
        if self._text_depth:
            if tag == "div":
                self._text_depth += 1
            elif tag == "br":
                p["text"] += "\n"
            return
        if tag == "div" and "tgme_widget_message_text" in cls.split() and "js-message_reply_text" not in cls:
            self._text_depth = 1  # the post's own text, not the quoted message it replies to
        elif "tgme_widget_message_photo_wrap" in cls or "tgme_widget_message_video" in cls \
                or "tgme_widget_message_document" in cls:
            p["media"] = True
            photo = re.search(r"background-image:url\('([^']+)'\)", a.get("style") or "")
            if "tgme_widget_message_photo_wrap" in cls and photo and not p.get("photo"):
                p["photo"] = photo.group(1)  # the first photo: the one carrying a news screenshot
        elif tag == "span" and "tgme_widget_message_views" in cls:
            self._in_views = True
        elif tag == "time" and a.get("datetime") and p["date"] is None:
            p["date"] = a["datetime"]

    def handle_endtag(self, tag):
        if tag == "div" and self._title_depth:
            self._title_depth = 0
        if tag == "div" and self._text_depth:
            self._text_depth -= 1
        if tag == "span":
            self._in_views = False

    def handle_data(self, data):
        if self._title_depth:
            self.title += data
        elif self._post is not None and self._text_depth:
            self._post["text"] += data
        elif self._post is not None and self._in_views:
            self._post["views"] = data.strip()


def parse_preview(html: str, base_url: str = "https://t.me") -> dict:
    p = _PreviewParser()
    p.feed(html)
    posts = []
    for x in p.posts:
        channel, _, pid = x["post"].partition("/")
        if not pid.isdigit():
            continue
        posts.append({"channel": channel, "id": int(pid), "url": f"{base_url}/{x['post']}", "date": x["date"],
                      "text": re.sub(r"\n{3,}", "\n\n", x["text"]).strip(), "views": x["views"],
                      "media": x["media"], "photo": x.get("photo")})
    return {"title": (p.title or "").strip() or None, "before": p.before, "posts": posts}


# Capitalised words that are also NSE symbols but in posts almost always mean something else.
_NOT_A_STOCK = {
    "IPO", "NSE", "BSE", "SEBI", "RBI", "UPI", "GST", "SIP", "ETF", "FII", "DII", "PSU", "CEO", "USA", "GDP", "IPL",
    "BUY", "SELL", "HOLD", "EXIT", "ADD", "TARGET", "STOP", "LOSS", "NIFTY", "SENSEX", "BANK", "GOLD", "SILVER",
    "INDIA", "NEWS", "LIVE", "TODAY", "OPEN", "FREE", "SAFE", "GOOD", "BEST", "TOP", "HIGH", "LOW", "NEW", "ONE",
    "ALL", "YES", "STAR", "ALERT", "CALL", "LONG", "SHORT", "TRADE", "MARKET", "PROFIT", "RISK", "WATCH", "LINK",
    "JOIN", "TIME", "NOW", "BIG", "VIDEO", "PLAN", "GROWTH", "VALUE", "MONEY", "POWER", "INFRA", "ENERGY",
    "OIL", "BBL", "GAS", "RISE", "FALL", "CRUDE", "BREAKING", "UPDATE", "CABINET", "GOVT", "FOCUS", "REPORT",
    "SOURCES", "DEAL", "ORDER", "WIN", "DEFENCE", "BANKING", "AUTO", "METALS", "PHARMA", "REALTY", "FMCG",
}
# Short names everyone uses that aren't in Kite's list (only kept when the symbol is listed).
_SHORT_NAMES = {"L&T": "LT", "SBI": "SBIN", "RIL": "RELIANCE", "HUL": "HINDUNILVR", "AIRTEL": "BHARTIARTL",
                "ZOMATO": "ETERNAL", "OIL INDIA": "OIL", "BAJAJ AUTO": "BAJAJ-AUTO", "M&M": "M&M",
                "JIO FINANCIAL": "JIOFIN", "ADANI ENT": "ADANIENT", "ADANI PORTS": "ADANIPORTS", "COAL INDIA": "COALINDIA",
                "KOTAK BANK": "KOTAKBANK", "KOTAK BK": "KOTAKBANK"}
# First words of longer names that are everyday words, places or people's names: alone they don't name
# the company ("Media contact:" is not Media Matrix, "Corporate Travel" is not Travel Food, Sumit is a
# signature). The full name ("Travel Food") still counts.
_EVERYDAY_FIRST = {
    "ACCENT", "ACCORD", "ACCRETION", "ACCURACY", "ACTION", "ACTIVE", "ADDICTIVE", "ADVANCE", "ADVENT", "AFFORD",
    "ALBERT", "ALFRED", "ALKALI", "ALPINE", "AMBER", "AMIABLE", "ANDREW", "ANTHEM", "ANTONY", "ARIES", "ARMOUR",
    "ARROW", "ASPIRE", "ASSET", "ASTRA", "ATLAS", "AUSTIN", "AUTOMOBILE", "AVALON", "AVATAR", "AVENUE", "AXIOM",
    "BEACON", "BELLA", "BLISS", "BRACE", "BRAND", "BRIGHT", "BROOKS", "CALIBER", "CAMPUS", "CAPRI", "CAPTAIN",
    "CAREER", "CELEBRITY", "CHALET", "CLASSIC", "CLEAR", "COFFEE", "COMFORT", "COMMERCIAL", "COMMITTED",
    "COMPETENT", "COMPUTER", "CONTAINER", "CONTINENTAL", "CONTROL", "CORAL", "CORDS", "CORONA", "CREST", "CROWN",
    "CURRENT", "DANISH", "DELPHI", "DESTINY", "DIFFUSION", "DILIGENT", "DOLLAR", "DOLPHIN", "DREDGING", "DRONE",
    "EASTERN", "ELECTRO", "ELECTRONICS", "ELEVATE", "EMBASSY", "EMERALD", "EMPIRE", "EMPOWER", "ENCOMPASS",
    "ENDURANCE", "ENGINEERS", "ENTERTAIN", "ESSEN", "EUREKA", "EXCEL", "EXCELLENT", "FALCON", "FASCINATE", "FELIX",
    "FOODS", "FORCE", "FORGE", "FRACTAL", "FRONTIER", "FUSION", "GABRIEL", "GARDEN", "GATEWAY", "GENERIC", "GLASS",
    "GOLDEN", "GOODLUCK", "GRAPHITE", "GRILL", "HAPPIEST", "HARRISON", "HEADS", "HEALTH", "HEALTHCARE", "HEALTHY",
    "HEMISPHERE", "HERITAGE", "HEXAGON", "HIGHWAY", "HILTON", "HORIZON", "HYBRID", "IDEAL", "IDENTICAL",
    "INCREDIBLE", "INDUS", "INFLUX", "INNOVATIVE", "INSECTICIDES", "INSOLATION", "INSPIRE", "INTEGRA", "INTEGRATED",
    "INTEGRITY", "INTELLECT", "INTENSE", "INTERIORS", "INVEST", "INVESTMENT", "ITALIAN", "KARMA", "KNACK",
    "KNOWLEDGE", "LANCER", "LASER", "LATENT", "LIBERTY", "LINCOLN", "LORDS", "LOVABLE", "LOYAL", "MAGNUM",
    "MAJESTIC", "MAKERS", "MARATHON", "MARCO", "MARINE", "MARVEL", "MASON", "MATRIMONY", "MATRIX", "MEDIA",
    "MEDICO", "MERCANTILE", "MERCURY", "METRO", "MILKY", "MONARCH", "MONTE", "MOTOR", "MOVING", "MULTI", "MUSIC",
    "NATURAL", "NECTAR", "NEPTUNE", "NETWORK", "NORTHERN", "NUCLEUS", "ONWARD", "OPTIMUS", "ORACLE", "ORBIT",
    "ORCHID", "PACIFIC", "PANACEA", "PANACHE", "PANORAMA", "PARAGON", "PENINSULA", "PENTAGON", "PERMANENT",
    "PERSISTENT", "PETRO", "PHANTOM", "PLATINUM", "PLAZA", "POPULAR", "POSITRON", "PRAXIS", "PREMIUM", "PRESTIGE",
    "PRIMO", "PRINCE", "PRIORITY", "PRISM", "PRUDENT", "PRUDENTIAL", "PYRAMID", "QUADRANT", "QUEST", "QUICK",
    "QUINT", "RADIANT", "RAINBOW", "RAPID", "REFRACTORY", "REGENCY", "RELIABLE", "RENAISSANCE", "RESPONSIVE",
    "RESTAURANT", "ROBUST", "ROLEX", "ROUTE", "RUBICON", "SAFARI", "SAINT", "SAPPHIRE", "SERVICE", "SHARE", "SHARP",
    "SIGNET", "SIGNPOST", "SILKY", "SINGER", "SNOWMAN", "SOLAR", "SOLVE", "SONATA", "SPECIALITY", "STALLION",
    "STANLEY", "STERLING", "STOVE", "STUDIO", "SUMMIT", "SUNSHINE", "SUPERIOR", "SWISS", "SYNERGY", "TASTY",
    "THINKING", "TIGER", "TOTAL", "TOURISM", "TRANS", "TRANSPORT", "TRAVEL", "TRUST", "ULTRA", "UNIVERSAL",
    "UPDATER", "UPSURGE", "VERANDA", "VIBRANT", "VICEROY", "VICTORY", "VIGOR", "VINTAGE", "VINYL", "VIRTUOSO",
    "VISION", "VITAL", "VIVID", "WATERWAYS", "WHEELS", "WINDSOR", "WONDER", "WORTH",
    # places
    "ARABIAN", "ASSAM", "ATLANTA", "AUSTRALIAN", "BANARAS", "BENARES", "BENGAL", "CALIFORNIA", "CAMBRIDGE",
    "CHENNAI", "COCHIN", "HISAR", "KANPUR", "KARNATAKA", "MADHYA", "MADRAS", "MYSORE", "NOIDA", "ORISSA", "PANAMA",
    "SAURASHTRA", "TAMILNAD", "TOKYO", "TUTICORIN",
    # people's names, common in quotes and signatures
    "AAKASH", "ABHISHEK", "ADVAIT", "ADVANI", "AHLUWALIA", "AKASH", "AMBANI", "ANANT", "ANKIT", "ANUPAM", "ASHOK",
    "ASHUTOSH", "ASHWINI", "BANSAL", "BHANDARI", "DHRUV", "DILIP", "GANDHI", "GOPAL", "JALAN", "JAYANT", "JAYESH",
    "KIRAN", "KRITIKA", "LOKESH", "MAHENDRA", "MAMATA", "MANAV", "MANOJ", "MISHRA", "MITTAL", "MOHIT", "MUKESH",
    "NAMAN", "NAVIN", "NEELAM", "NIKITA", "NITIN", "PARAG", "PARTH", "PATIL", "POOJA", "PRADEEP", "PRANAV", "PRITI",
    "RADHIKA", "RAGHAV", "RAJESH", "RAVINDER", "RAVINDRA", "RUDRA", "SAGAR", "SAMAY", "SHANKAR", "SHILPA", "SHIVAM",
    "SIDDHI", "SONAL", "SUDEEP", "SUMIT", "SUNIL", "SUPRIYA", "SURYA", "TANEJA", "VARUN", "VIBHOR", "VIDYA", "VIJAY",
    "VIKRAM", "VINEET", "VINOD", "VIRAT",
}
# An exchange letter (read from an image) ends with its signature ("Yours faithfully, ... Company Secretary"): names there are people.
_SIGN_OFF = re.compile(r"\b(yours (faithfully|sincerely|truly)|thanking you|digitally signed by)\b", re.I)
# Endings of Kite's abbreviated names that no post repeats ("TATA POWER CO" is written "Tata Power").
_NAME_TAIL = {"LTD", "LIMITED", "CO", "COMPANY", "CORP", "CORPORATION", "L", "INC", "&", "AND", "THE", "VN", "IND"}
_COMMON_FIRST = {"INDIAN", "INDIA", "NATIONAL", "GLOBAL", "UNITED", "GENERAL", "BHARAT", "HINDUSTAN", "SHREE", "SHRI",
                 "SRI", "NEW", "FIRST", "SUPER", "STAR", "PRIME", "GOLD", "GREEN", "POWER", "ENERGY", "CAPITAL",
                 "FINANCE", "HOUSING", "STEEL", "METAL", "PAPER", "SUGAR", "TEXTILE", "CEMENT", "CHEMICAL",
                 "PHARMA", "SPORTS", "PREMIER", "STANDARD", "ADVANCED", "ASIAN", "ORIENTAL", "SOUTH", "NORTH", "EAST",
                 "WEST", "CENTRAL", "MODERN", "CITY", "ROYAL", "DIAMOND", "SILVER", "TECH", "DIGITAL", "SMART"}

# The post's view of a stock, read from words near the mention (same line), else the whole post.
_BUY = re.compile(r"\b(buy|buying|accumulate|add on dips|add more|invest in|long[- ]?term pick|bullish|breakout|"
                  r"target|entry|upside|multibagger|recommend(ed)?)\b|కొనండి|కొనుగోలు|పెట్టుబడి", re.I)
_SELL = re.compile(r"\b(sell|selling|exit|book (profit|loss)|avoid|bearish|breakdown|downside|short sell|"
                   r"(don'?t|do not|never) buy)\b|అమ్మండి|అమ్మకం|అమ్మేయ", re.I)
# Text read from an image is mostly news wires ("Manufacture & Sell Radios", "tranche of investment"):
# there only tip wording counts.
_TIP_BUY = re.compile(r"^\W*(buy|accumulate)\b|\b(buy|accumulate)\s*[:@\-]|\b(buy|accumulate)\s+(on|at|above|around)\b|"
                      r"\bbullish\b|\bbreakout\b|\b(tgt|target)\s*[:\-]?\s*(₹|rs\.?)?\s*\d", re.I)
_TIP_SELL = re.compile(r"^\W*(sell|exit|avoid)\b|\b(sell|exit|avoid|short)\s*[:@\-]|"
                       r"\b(sell|exit|short)\s+(on|at|below|around)\b|\bbearish\b|\bbreakdown\b", re.I)


# One-word names ("Current", "Elevate") are everyday words too: they count only in a post about markets.
_MARKET_CONTEXT = re.compile(r"\b(stocks?|shares?|ipo|listings?|target|buy|sell|nse|bse|nifty|sensex|results?|"
                             r"invest\w*|portfolio|dividend|rs\.?|profit|breakout|breakdown|multibagger|bullish|"
                             r"bearish|rally|falls?|gains?|order|board|quarter|q[1-4])\b|₹|స్టాక్|షేర్", re.I)
_URL = re.compile(r"https?://\S+|www\.\S+|\S+\.(com|in|be|me|ly)/\S*", re.I)


def _youtube(post: dict) -> bool:
    """Any post linking to YouTube (videos, shorts, their thumbnails): the owner doesn't follow them."""
    return bool(re.search(r"youtu\.?be", post["text"], re.I))


def _stands_alone(line: str, m: re.Match, before: str = "", after: str = "") -> bool:
    """A one-word name ("Infosys") names that company only when capitalised and not part of another
    capitalised name: "Mach Travel Solutions" is not Travel Food, "share capital" is not Share India.
    `before` / `after`: the neighbouring lines when a line break cut the sentence (text read from an
    image wraps mid-name: "Corporate" / "Travel vertical")."""
    if not m.group(1)[0].isupper():
        return False
    prev = re.search(r"([A-Za-z][\w&.]*)\W*$", f"{before} {line[:m.start()]}")
    nxt = re.match(r"\W*([A-Za-z][\w&]*)", f"{line[m.end():]} {after}")
    return not any(w and w.group(1)[0].isupper() and not (_BUY.fullmatch(w.group(1)) or _SELL.fullmatch(w.group(1))
                                                         or w.group(1).upper() in _NOT_A_STOCK)
                   for w in (prev, nxt))


_SENTENCE_END = re.compile(r"[.!?;:]\s*$")
WRAP_MIN_WORDS = 4  # a line of prose that wrapped; shorter lines are list items ("Ircon", "Texmaco +6%")


def _wraps(line: str) -> bool:
    """Whether the break after `line` cut a sentence (prose wrapping), not ended a list item."""
    return len(line.split()) >= WRAP_MIN_WORDS and not _SENTENCE_END.search(line)


def _sentences(text: str) -> list[tuple[str, str, str]]:
    """(sentence, line before, line after): sentences split at . ! ? ; and line breaks, as before. A
    sentence at the start or end of a line also gets the neighbouring line when the break between them
    only wrapped the prose, so a name cut in two by the wrap ("Corporate" / "Travel") is seen whole."""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        before = lines[i - 1] if i and _wraps(lines[i - 1]) else ""
        after = lines[i + 1] if i + 1 < len(lines) and _wraps(line) else ""
        parts = re.split(r"[.!?;]+(?:\s|$)", line)
        out += [(p, before if j == 0 else "", after if j == len(parts) - 1 else "") for j, p in enumerate(parts)]
    return out


def _all_caps(line: str) -> bool:
    """News-wire screenshots are written in capitals, so capitals say nothing about names there."""
    letters = [c for c in line if c.isascii() and c.isalpha()]
    return len(letters) >= 8 and sum(c.isupper() for c in letters) >= 0.8 * len(letters)


def _headline_name(line: str, m: re.Match, whole_line: bool = True) -> bool:
    """In an all-caps line, a word names a company only in the wire format "VEDANTA: CO RAISES…"
    (followed by a colon), in a tip ("BUY: IRCTC TGT 780", "SELL @ 410 TATA POWER"), or when it is
    the whole line (a thumbnail label such as "HFCL"; not for the first word of a longer name: in a TV
    caption "PATANJALI" over "KESWANI" is a person)."""
    before, after = line[:m.start()], line[m.end():]
    return (bool(re.match(r"\s*:|\s+(tgt|target|cmp|sl|stop ?loss)\b|\s*@", after, re.I))
            or bool(re.search(r"\b(buy|sell|accumulate|exit|avoid|short)\s*[:@\-]?\s*(₹?\s*[\d.]+\s*)?$", before, re.I))
            or (whole_line and line.strip(" #$:|-") == m.group(0).strip(" #$")))


def _distinctive(word: str) -> bool:
    """A name word that points at a company rather than being an everyday or sector word."""
    return len(word) >= 4 and not word.isdigit() and word not in _COMMON_FIRST | _NOT_A_STOCK | _NAME_TAIL


def _view(text: str, tips_only: bool = False) -> str | None:
    buy_re, sell_re = (_TIP_BUY, _TIP_SELL) if tips_only else (_BUY, _SELL)
    buy, sell = bool(buy_re.search(text)), bool(sell_re.search(text))
    return "BUY" if buy and not sell else "SELL" if sell and not buy else None


class StockMatcher:
    """Finds NSE stocks named in a post: by symbol (IRCTC, #irctc, $IRCTC) or by company name
    ("Tata Power", "Infosys"), built from Kite's instrument list. A single-word name counts only when
    it is distinctive and belongs to one company."""

    def __init__(self, names: dict[str, str]):
        self.names = names
        self.base = {s.rsplit("-", 1)[0] if s.rsplit("-", 1)[-1] in ("BE", "SM", "ST", "BZ") else s: s for s in names}
        aliases: dict[str, set[str]] = {}
        exact: dict[str, str] = {}  # a company whose whole name is the alias owns it ("VEDANTA" is VEDL)
        for sym, name in names.items():
            words = [w for w in re.split(r"[^A-Z0-9&]+", name.upper()) if w]
            while words and words[-1] in _NAME_TAIL:
                words.pop()
            while words and words[0] in ("THE", "&"):
                words.pop(0)
            if words:
                exact.setdefault(" ".join(words), sym)
            for n in {len(words), 2}:
                # "SUN TV" counts although both words are short: neither is an everyday word.
                if len(words) >= n >= 2 and (any(_distinctive(w) for w in words[:n]) or (
                        len("".join(words[:n])) >= 5 and not set(words[:n]) & (_COMMON_FIRST | _NOT_A_STOCK))):
                    aliases.setdefault(" ".join(words[:n]), set()).add(sym)
            if words and len(words[0]) >= 5 and _distinctive(words[0]) and (
                    len(words) == 1 or words[0] not in _EVERYDAY_FIRST):
                aliases.setdefault(words[0], set()).add(sym)
        self.alias = {a: next(iter(s)) if len(s) == 1 else exact[a] for a, s in aliases.items()
                      if (len(s) == 1 or a in exact) and a not in _NOT_A_STOCK}
        # One word standing for a longer name ("PATANJALI" for Patanjali Foods): weaker evidence.
        self._partial = {a for a in self.alias if " " not in a and a not in exact}
        self._short = {a: s for a, s in _SHORT_NAMES.items() if s in names}
        self.alias |= self._short
        self._alias_re = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(
            re.escape(a).replace(r"\ ", r"\s+") for a in sorted(self.alias, key=len, reverse=True)) + r")(?![A-Za-z0-9])",
            re.I) if self.alias else None

    def find(self, text: str) -> dict[str, str]:
        """{symbol: the sentence it was named in}."""
        found: dict[str, str] = {}
        text = _URL.sub(" ", text)  # "?feature=share" in a link is not Share India
        market = bool(_MARKET_CONTEXT.search(text))
        for line, before, after in _sentences(text):
            caps = _all_caps(line)
            for m in re.finditer(r"([#$])?\b([A-Za-z][A-Za-z0-9&]{2,19})\b", line):
                tag, word = m.group(1), m.group(2).upper()
                sym = self.base.get(word) or (self._tag_prefix(word) if tag else None)
                # A symbol that is also the first word of its name (PATANJALI) is held to the same bar.
                if sym and word not in _NOT_A_STOCK and (tag or (m.group(2).isupper() and (
                        not caps or _headline_name(line, m, word not in self._partial)))):
                    found.setdefault(sym, line)
            if self._alias_re:
                for m in self._alias_re.finditer(line):
                    alias = re.sub(r"\s+", " ", m.group(1).upper())
                    if (" " in alias or alias in self._short
                            or (_headline_name(line, m, alias not in self._partial) if caps
                                else market and _stands_alone(line, m, before, after))):
                        found.setdefault(self.alias[alias], line)
        return found

    def _tag_prefix(self, word: str) -> str | None:
        """News hashtags shorten names: #AmberEnt is AMBER, #DixonTech is DIXON (longest symbol of
        at least 4 letters the tag starts with)."""
        w = word.upper()
        for n in range(len(w) - 1, 3, -1):
            if w[:n] in self.base:
                return self.base[w[:n]]
        return None

    def stocks(self, text: str, held: set[str], image_text: str | None = None) -> list[dict]:
        held_base = {s.split("-")[0] for s in held}
        found = self.find(text)
        in_image = {s: line for s, line in self.find(_SIGN_OFF.split(image_text, 1)[0]).items()
                    if s not in found} if image_text else {}
        # A sentence without buy/sell words takes the post's view only in a post about one or two
        # stocks; in a news digest one "buy" would otherwise colour every company listed.
        post_view = _view(_URL.sub(" ", text)) if len(found) <= 2 else None
        views = {s: _view(line) or post_view for s, line in found.items()}
        views |= {s: _view(line, tips_only=True) for s, line in in_image.items()}
        lines = found | in_image
        out = [{"symbol": s, "name": self.names.get(s), "held": s in held or s.split("-")[0] in held_base,
                "view": v, "from_image": s in in_image, "line": lines[s].strip()[:200]} for s, v in views.items()]
        return sorted(out, key=lambda s: (not s["held"], s["symbol"]))


IMAGE_FEATURE, IMAGE_READER = "feed_image", "rapidocr-en-v1"
IMAGE_RETRY_AFTER = 3600  # seconds before an image that failed to read is tried again
# Screen furniture in news screenshots: handles, "· 9m" time-ago stamps, bare numbers from price charts,
# and channel watermarks ("X/@CNBCTV18Live", "cnbctv18.com", "ETNOW-IN", "#ONCNBCTV18", "Follow us on",
# ET Now's "RISE WITH NOW INDIA" one word a line).
_OCR_NOISE = re.compile(r"^(@\S+|[\d.,:%\[\]()+\-▲▼ ]+|today|follow( us on)?|news|tv18|cnbc|x\s*/\s*@\S+|"
                        r"[\w\-]+\.(com|in|net)|etnow-in|#on\w+|rise|with|now|india)$", re.I)
_STRAY_LETTER = re.compile(r"(?<![\w'.&])[B-HJ-Zb-hj-z](?![\w'.&])")  # any letter but a / I standing alone


def clean_image_text(text: str) -> str:
    """Drops screen furniture and garbled lines (OCR of a stamp or a logo reads as "ESVS O P ES V ET":
    three or more stray letters). Run on display too, so images read before a rule was added get it."""
    return "\n".join(t for t in text.splitlines()
                     if not _OCR_NOISE.match(t.strip()) and len(_STRAY_LETTER.findall(t)) < 3)


class ImageReader:
    """Reads the text in feed photos offline (RapidOCR's English model, about 2-3 s an image), in a
    background thread: each photo once, cached in the AI cache table. English only; Telugu in an
    image comes out as noise and is not used."""

    def __init__(self, repo: Repository, settings, clock, http=None):
        self.repo, self.settings, self.clock = repo, settings, clock
        self._http = http
        self._ocr = None
        self._queue: list[dict] = []
        self._pending: set[str] = set()
        self._failed: dict[str, float] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        if not self.settings.feed_read_images:
            return False
        try:
            import rapidocr  # noqa: F401 - optional extra: uv pip install -e .[ocr]
        except ImportError:
            return False
        return True

    @staticmethod
    def key(post: dict) -> str:
        return f"tgimg:{post['channel'].lower()}/{post['id']}:{IMAGE_READER}"

    def read(self, post: dict) -> tuple[str | None, str | None]:
        """(text, status) for a post's photo, queueing it if unread. status: read | pending | failed |
        off; None when the post has no photo."""
        if not post.get("photo"):
            return None, None
        hit = self.repo.cache_get(self.key(post))
        if hit is not None:
            return clean_image_text(hit.get("text") or ""), "read"
        if not self.enabled:
            return None, "off"
        k = self.key(post)
        with self._lock:
            if time.monotonic() - self._failed.get(k, -IMAGE_RETRY_AFTER) < IMAGE_RETRY_AFTER:
                return None, "failed"
            if k not in self._pending:
                self._pending.add(k)
                self._queue.append(post)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, daemon=True, name="feed-images")
                self._worker.start()
        return None, "pending"

    def _read_image(self, data: bytes) -> str:
        if self._ocr is None:
            from rapidocr import LangRec, RapidOCR

            self._ocr = RapidOCR(params={"Rec.lang_type": LangRec.EN, "Global.log_level": "error"})
        result = self._ocr(data)
        lines = [t.strip() for t, score in zip(result.txts or (), result.scores or ()) if score >= 0.6]
        # Keep lines that read as words (drops handles, chart axes and garbled Telugu).
        return "\n".join(t for t in lines if not _OCR_NOISE.match(t)
                         and sum(c.isascii() and c.isalpha() for c in t) >= max(3, len(t) // 2))

    def _run(self) -> None:
        while True:
            with self._lock:
                post = self._queue.pop() if self._queue else None  # newest first, as the page lists them
                if post is None:
                    self._pending.clear()
                    self._worker = None
                    return
            k = self.key(post)
            try:
                if self._http is None:
                    import httpx
                    self._http = httpx.Client(timeout=20)
                r = self._http.get(post["photo"])
                r.raise_for_status()
                self.repo.cache_put(k, IMAGE_FEATURE, IMAGE_READER, IMAGE_READER,
                                    {"text": self._read_image(r.content)[:4000]}, self.clock.now())
            except Exception:  # noqa: BLE001 - a bad image or network error: try that one again later
                with self._lock:
                    self._failed[k] = time.monotonic()
            finally:
                with self._lock:
                    self._pending.discard(k)


STORY_HOURS = 6          # posts this close in time can be the same story
STORY_SIMILARITY = 0.6   # share of distinct words in common (text + image text) for "same words"
STORY_MAX_STOCKS = 3     # "same stocks" only for focused posts, not market round-ups naming ten companies


def _words(p: dict) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", f"{p['text']} {p.get('image_text') or ''}".lower()))


def group_same_story(posts: list[dict]) -> None:
    """Newest first. A post about the same story as a newer one shown within STORY_HOURS (nearly the same
    words, or the same 1-3 stocks named) gets `same_story_as`: the newer post's key, which the page folds
    it under. Channels repost one news item several times (MTNL's land sale: the filing, then a wire)."""
    heads: list[tuple[dict, datetime | None, set[str], frozenset[str]]] = []
    for p in posts:
        p["key"] = f"{p['channel']}/{p['id']}"
        p["same_story_as"] = None
        when = datetime.fromisoformat(p["date"]) if p.get("date") else None
        words, stocks = _words(p), frozenset(s["symbol"] for s in p["stocks"])
        for h, h_when, h_words, h_stocks in heads:
            if when is None or h_when is None or (h_when - when).total_seconds() > STORY_HOURS * 3600:
                continue
            same_words = words and h_words and len(words & h_words) / len(words | h_words) >= STORY_SIMILARITY
            same_stocks = stocks and stocks == h_stocks and len(stocks) <= STORY_MAX_STOCKS
            if same_words or same_stocks:
                p["same_story_as"] = h["key"]
                break
        else:
            heads.append((p, when, words, stocks))


class TelegramFeed:
    def __init__(self, repo: Repository, base_url: str, today=None, http=None, names: dict[str, str] | None = None,
                 images: ImageReader | None = None):
        self.repo = repo
        self.images = images
        self.base_url = base_url.rstrip("/")
        self._http = http
        self._today = today or date.today
        self._names = names  # tests; otherwise Kite's public instrument list, refreshed daily
        self._matcher: tuple[date, StockMatcher] | None = None
        self._cache: dict[tuple[str, int | None], tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def matcher(self) -> StockMatcher:
        today = self._today()
        if self._matcher is None or self._matcher[0] != today:
            self._matcher = (today, StockMatcher(self._names if self._names is not None else nse_equity_names(today)))
        return self._matcher[1]

    def _client(self):
        if self._http is None:
            import httpx  # optional extra, like the Telegram notifier

            # No redirects: Telegram redirects a channel without a public preview to its t.me page.
            self._http = httpx.Client(timeout=20, follow_redirects=False,
                                      headers={"User-Agent": "Mozilla/5.0 (portfolio-intelligence feed)"})
        return self._http

    # --- channels (shared: they are the person's reading list, not an account's) ---
    def channels(self) -> list[str]:
        return (self.repo.get_setting(CHANNELS_KEY, shared=True) or {}).get("channels", [])

    def add_channel(self, raw: str, now) -> dict:
        name = normalize_channel(raw)
        page = self.page(name)  # refuses private or unknown channels before saving
        chans = self.channels()
        if name.lower() not in (c.lower() for c in chans):
            self.repo.put_setting(CHANNELS_KEY, {"channels": [*chans, name]}, now, shared=True)
        return {"channel": name, "title": page["title"], "posts": len(page["posts"])}

    def remove_channel(self, name: str, now) -> None:
        self.repo.put_setting(CHANNELS_KEY, {"channels": [c for c in self.channels() if c.lower() != name.lower()]},
                              now, shared=True)

    # --- posts ---
    def page(self, channel: str, before: int | None = None) -> dict:
        key = (channel.lower(), before)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
                return hit[1]
        url = f"{self.base_url}/s/{channel}" + (f"?before={before}" if before else "")
        try:
            r = self._client().get(url)
        except Exception as e:  # noqa: BLE001 - network errors are shown per channel
            raise FeedError(f"Couldn't reach Telegram ({self.base_url}): {e}") from e
        page = parse_preview(r.text, self.base_url) if r.status_code == 200 else None
        if r.is_redirect or (page and page["title"] is None):
            raise FeedError(f"{channel} isn't a public channel: check the name, or it may be private or deleted")
        if page is None:
            raise FeedError(f"Telegram answered {r.status_code} for {channel}; try again later")
        with self._lock:
            self._cache[key] = (time.monotonic(), page)
        return page

    def feed(self, symbols: set[str], channel: str | None = None, before: int | None = None) -> dict:
        """Newest first across the saved channels (or one channel, paging back with `before`)."""
        names = [normalize_channel(channel)] if channel else self.channels()
        matcher = self.matcher()
        posts, errors, more, titles = [], {}, {}, {}
        for name in names:
            try:
                page = self.page(name, before if channel else None)
            except FeedError as e:
                errors[name] = str(e)
                continue
            titles[name] = page["title"]
            more[name] = page["before"]
            posts += self._posts(page, matcher, symbols)
        posts.sort(key=lambda p: p["date"] or "", reverse=True)
        group_same_story(posts)
        return {"channels": self.channels(), "titles": titles, "older": more, "errors": errors, "posts": posts,
                "images_enabled": bool(self.images and self.images.enabled)}

    def _posts(self, page: dict, matcher: StockMatcher, symbols: set[str]) -> list[dict]:
        out = []
        for p in page["posts"]:
            if _youtube(p):
                continue  # the owner doesn't follow the videos; their thumbnails aren't read either
            image_text, image_status = self.images.read(p) if self.images else (None, None)
            stocks = matcher.stocks(p["text"], symbols, image_text)
            out.append({**p, "channel_title": page["title"], "stocks": stocks,
                        "image_text": image_text, "image_status": image_status,
                        "holdings": [s["symbol"] for s in stocks if s["held"]]})
        return out

    def mentions(self, symbols: set[str], pages: int = 3) -> dict:
        """Stocks named in the last `pages` pages (about 20 posts each) of every saved channel:
        {"by_symbol": {SYM: [mention, newest first]}, "posts": n, "reading": images still being read}."""
        matcher = self.matcher()
        by_symbol: dict[str, list[dict]] = {}
        n = reading = 0
        for name in self.channels():
            before = None
            for _ in range(pages):
                try:
                    page = self.page(name, before)
                except FeedError:
                    break
                for p in self._posts(page, matcher, symbols):
                    n += 1
                    reading += p["image_status"] == "pending"
                    for s in p["stocks"]:
                        by_symbol.setdefault(s["symbol"], []).append({
                            "channel": p["channel_title"] or p["channel"], "date": p["date"], "url": p["url"],
                            "view": s["view"], "line": s["line"], "from_image": s["from_image"], "name": s["name"]})
                before = page["before"]
                if not before:
                    break
        for items in by_symbol.values():
            items.sort(key=lambda m: m["date"] or "", reverse=True)
        return {"by_symbol": by_symbol, "posts": n, "reading": reading, "channels": self.channels()}

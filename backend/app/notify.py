"""Alert notifications to Telegram (a bot the owner creates; messages go to the owner's own chat).

An outbox, not inline sending: every minute the scheduler sends high/medium alerts that haven't been
sent yet. Alerts raised in quiet hours (22:00-07:00 IST) are held until morning; a failed send is
retried on the next tick; nothing is sent twice. Info alerts are never sent. At 16:10 IST a short
daily summary. Only the alert's own text leaves the machine (stock, headline, key numbers).
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from app.clock import IST, Clock
from app.config import Settings
from app.db import DEFAULT_ACCOUNT, LEGACY_ACCOUNT, Repository

log = logging.getLogger("pi.notify")

CHAT_KEY, BASELINE_KEY, SUMMARY_KEY = "telegram_chat", "notify_baseline", "notify_summary"
SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}
MAX_PER_TICK = 5  # more than this at once → one combined message


class TelegramClient:
    API = "https://api.telegram.org"

    def __init__(self, token: str):
        import httpx  # optional extra: pip install -e ".[fundamentals]"
        self.token = token
        self.http = httpx.Client(timeout=20)

    def _call(self, verb: str, method: str, **kwargs):
        """A network failure becomes RuntimeError (the API answers 502, the outbox retries). Only the
        error's type is kept: httpx messages can include the URL, and the URL holds the bot token."""
        try:
            return self.http.request(verb, f"{self.API}/bot{self.token}/{method}", **kwargs)
        except Exception as e:  # noqa: BLE001 - httpx.HTTPError and friends
            raise RuntimeError(f"Couldn't reach Telegram ({type(e).__name__})") from None

    def send(self, chat_id: str, text: str) -> str | None:
        """Send; returns the new chat id if Telegram says the group was upgraded to a supergroup."""
        payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        r = self._call("POST", "sendMessage", json=payload)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        new_id = (body.get("parameters") or {}).get("migrate_to_chat_id")
        if new_id:  # group became a supergroup: its id changed; resend there
            r = self._call("POST", "sendMessage", json={**payload, "chat_id": new_id})
            body = r.json()
        if r.status_code != 200 or not body.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: HTTP {r.status_code} {r.text[:200]}")
        return str(new_id) if new_id else None

    def chats(self) -> list[dict]:
        """Private chats and groups that reached the bot recently (Telegram keeps ~24 h of updates).
        Groups show up when the bot is added (my_chat_member) or when someone writes /start there."""
        r = self._call("GET", "getUpdates", params={"timeout": 0})
        if r.status_code != 200 or not r.json().get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: HTTP {r.status_code} {r.text[:200]}")
        chats: dict[str, dict] = {}
        for u in r.json().get("result", []):
            m = u.get("message") or u.get("my_chat_member") or {}
            chat = m.get("chat") or {}
            kind = chat.get("type")
            if kind not in ("private", "group", "supergroup") or "id" not in chat:
                continue
            member = (u.get("my_chat_member") or {}).get("new_chat_member") or {}
            if member.get("status") in ("left", "kicked"):
                chats.pop(str(chat["id"]), None)  # the bot was removed from that chat
                continue
            name = (chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
                    or chat.get("username") or "")
            chats[str(chat["id"])] = {"chat_id": str(chat["id"]), "name": name,
                                      "type": "group" if kind in ("group", "supergroup") else "private"}
        return list(chats.values())


def _e(s) -> str:
    return html.escape(str(s or ""))


def format_alert(a: dict, app_url: str) -> str:
    """One alert: severity, stock, headline, key numbers, what to do, link. Plain and short."""
    e = (a.get("details") or {}).get("explain") or {}
    sym = "Portfolio" if a["symbol"] == "*" else a["symbol"]
    facts = " · ".join(f"{_e(k)} {_e(v)}" for k, v in (e.get("facts") or [])[:5])
    lines = [f"<b>{a['severity'].upper()} · {_e(sym)}</b>: {_e(e.get('headline') or a['type'].replace('_', ' ').lower())}"]
    if facts:
        lines.append(facts)
    elif e.get("what") or a.get("message"):
        lines.append(_e((e.get("what") or a["message"])[:300]))
    if e.get("action"):
        lines.append(f"<i>What to do:</i> {_e(e['action'][:300])}")
    link = (a.get("details") or {}).get("url")
    if link:
        lines.append(f'<a href="{_e(link)}">Source</a>')
    if a["symbol"] != "*":
        lines.append(f'<a href="{_e(app_url)}/#/stock/{_e(a["symbol"])}">Open in the app</a>')
    return "\n".join(lines)


def format_batch(alerts: list[dict], app_url: str) -> str:
    rows = [f"• <b>{_e('Portfolio' if a['symbol'] == '*' else a['symbol'])}</b> ({a['severity']}): "
            f"{_e(((a.get('details') or {}).get('explain') or {}).get('headline') or a['type'].replace('_', ' ').lower())}"
            for a in alerts[:25]]
    more = f"\n…and {len(alerts) - 25} more" if len(alerts) > 25 else ""
    return f"<b>{len(alerts)} new alerts</b>\n" + "\n".join(rows) + more + f'\n<a href="{_e(app_url)}/#/alerts">Open alerts</a>'


def in_quiet_hours(now: datetime, start: time, end: time) -> bool:
    t = now.astimezone(IST).time()
    return (t >= start or t < end) if start > end else (start <= t < end)


class Notifier:
    def __init__(self, settings: Settings, repo: Repository, clock: Clock, client: TelegramClient | None = None):
        self.settings, self.repo, self.clock = settings, repo, clock
        token = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else None
        self.client = client or (TelegramClient(token) if token else None)
        self.last_error: str | None = None
        self.quiet = (time(*map(int, settings.notify_quiet_start.split(":"))),
                      time(*map(int, settings.notify_quiet_end.split(":"))))

    # ------------------------------------------------------------------ setup
    @property
    def chat_id(self) -> str | None:
        if self.settings.telegram_chat_id:
            return self.settings.telegram_chat_id
        return (self.repo.get_setting(CHAT_KEY, shared=True) or {}).get("chat_id")

    @property
    def enabled(self) -> bool:
        return self.client is not None and self.chat_id is not None

    def link(self) -> dict:
        """Find the private chat that messaged the bot and remember it. Refuses if several did."""
        if self.client is None:
            raise ValueError("Set PI_TELEGRAM_BOT_TOKEN in backend/.env first (from @BotFather), then restart.")
        found = self.client.chats()
        groups = [c for c in found if c["type"] == "group"]
        chats = groups or found  # a group, if you added the bot to one, is preferred over a private chat
        if not chats:
            raise ValueError("No message found. Open your bot in Telegram and press Start, or add the bot to your "
                             "group and send /start there, then try again.")
        if len(chats) > 1:
            kind = "groups" if groups else "people"
            raise ValueError(f"{len(chats)} {kind} reached the bot; set PI_TELEGRAM_CHAT_ID to the one you want: "
                             + ", ".join(f"{c['chat_id']} ({c['name']})" for c in chats))
        chat = chats[0]
        self.repo.put_setting(CHAT_KEY, chat, self.clock.now(), shared=True)
        self._baseline()
        self._send("✅ Portfolio Intelligence is linked. High and medium alerts will be sent here (none 22:00-07:00 "
                   "IST) with a summary at 16:10 IST on weekdays.")
        return chat

    def test(self) -> None:
        if not self.enabled:
            raise ValueError("Telegram isn't linked yet.")
        self._send("🔔 Test from Portfolio Intelligence: notifications work.")

    def _send(self, text: str) -> None:
        if self.repo.account not in (DEFAULT_ACCOUNT, LEGACY_ACCOUNT):
            text = f"<b>Kite {_e(self.repo.account)}</b> · {text}"  # one chat serves every account
        new_id = self.client.send(self.chat_id, text)
        if new_id:  # the group was upgraded to a supergroup: remember its new id
            chat = {**(self.repo.get_setting(CHAT_KEY, shared=True) or {}), "chat_id": new_id}
            self.repo.put_setting(CHAT_KEY, chat, self.clock.now(), shared=True)

    def _baseline(self) -> int:
        """First switch-on: alerts that already exist count as sent, so nothing old floods the chat."""
        b = self.repo.get_setting(BASELINE_KEY)
        if b is None:
            latest = self.repo.list_alerts(limit=1)
            b = {"max_id": latest[0]["id"] if latest else 0, "sent": []}
            self.repo.put_setting(BASELINE_KEY, b, self.clock.now())
        return b["max_id"]

    # ------------------------------------------------------------------ outbox
    def tick(self) -> int:
        """Send pending alerts (outside quiet hours). Returns how many were sent."""
        if not self.enabled:
            return 0
        now = self.clock.now()
        sent = self._summary_if_due(now)
        if in_quiet_hours(now, *self.quiet):
            return sent
        base = self.repo.get_setting(BASELINE_KEY) or {"max_id": self._baseline(), "sent": []}
        done = set(base.get("sent", []))
        cutoff = now - timedelta(hours=24)
        pending = [a for a in reversed(self.repo.list_alerts(open_only=True, limit=200))
                   if a["id"] > base["max_id"] and a["id"] not in done
                   and SEVERITY_ORDER.get(a["severity"], 2) <= SEVERITY_ORDER[self.settings.notify_min_severity]
                   and datetime.fromisoformat(a["created_at"]) >= cutoff]
        if not pending:
            return sent
        # One message per alert, or one batch message; each is recorded as soon as it is sent, so a
        # failure part-way retries only the alerts not yet delivered.
        messages = ([(pending, format_batch(pending, self.settings.app_url))] if len(pending) > MAX_PER_TICK
                    else [([a], format_alert(a, self.settings.app_url)) for a in pending])
        delivered = 0
        for alerts, text in messages:
            try:
                self._send(text)
            except Exception as e:  # noqa: BLE001 - retried next tick
                self.last_error = str(e)[:300]
                log.warning("Telegram send failed: %s", self.last_error)
                return sent + delivered
            done |= {a["id"] for a in alerts}
            delivered += len(alerts)
            self.repo.put_setting(BASELINE_KEY, {"max_id": base["max_id"], "sent": sorted(done)[-1000:]}, now)
        self.last_error = None
        return sent + delivered

    def _summary_if_due(self, now: datetime) -> int:
        at = time(*map(int, self.settings.notify_summary_time.split(":")))
        ist = now.astimezone(IST)
        if ist.weekday() >= 5 or ist.time() < at or in_quiet_hours(now, *self.quiet):
            return 0
        if (self.repo.get_setting(SUMMARY_KEY) or {}).get("day") == ist.date().isoformat():
            return 0
        latest = self.repo.latest_snapshots(1)
        if not latest:
            return 0
        from app.services import attention_digest
        d = attention_digest(latest[0])
        counts = self.repo.open_alert_counts()
        lines = [f"<b>Daily summary · {ist.strftime('%d %b')}</b>",
                 f"Open alerts: {counts.get('total', 0)} (high {counts.get('high', 0)}, medium {counts.get('medium', 0)})"]
        for g in d["groups"]:
            top = ", ".join(x["symbol"] for x in g["largest"][:3])
            lines.append(f"• {_e(g['title'])}: {g['count']} ({g['total_weight']:.1%}){' e.g. ' + _e(top) if top else ''}")
        lines += self._telegram_lines(set(latest[0].holdings), ist.date())
        lines.append(f'<a href="{_e(self.settings.app_url)}/#/alerts">Open the app</a>')
        try:
            self._send("\n".join(lines))
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)[:300]
            return 0
        self.repo.put_setting(SUMMARY_KEY, {"day": ist.date().isoformat()}, now)
        return 1

    # Set by main.py to TelegramFeed.mentions: {symbol: [mention]} from the owner's saved channels.
    telegram_mentions: Callable[[set[str]], dict] | None = None
    TELEGRAM_TOP = 5

    def _telegram_lines(self, held: set[str], day: date) -> list[str]:
        """Holdings the saved Telegram channels named today, most-named first: information only (D13: the
        feed never feeds signals or alerts), so it says that, and says what to do with it."""
        if not self.telegram_mentions or not held:
            return []
        try:
            by_symbol = self.telegram_mentions(held)["by_symbol"]
        except Exception as e:  # noqa: BLE001 - Telegram unreachable: the summary goes out without this line
            log.info("telegram mentions skipped in summary: %s", e)
            return []
        held_base = {s.split("-")[0] for s in held}
        counts = []
        for sym, items in by_symbol.items():
            if sym not in held and sym.split("-")[0] not in held_base:
                continue
            today = [m for m in items if m.get("date") and datetime.fromisoformat(m["date"]).astimezone(IST).date() == day]
            if today:
                buy = sum(m["view"] == "BUY" for m in today)
                sell = sum(m["view"] == "SELL" for m in today)
                counts.append((len(today), sym, buy, sell))
        if not counts:
            return []
        counts.sort(key=lambda c: (-c[0], c[1]))

        def one(n: int, sym: str, buy: int, sell: int) -> str:
            tone = ", ".join(x for x in (f"{buy} read as buy" if buy else "", f"{sell} as sell" if sell else "") if x)
            return f"{_e(sym)} {n} post{'s' if n != 1 else ''}{f' ({tone})' if tone else ''}"

        more = f" and {len(counts) - self.TELEGRAM_TOP} more" if len(counts) > self.TELEGRAM_TOP else ""
        return [f"• Telegram channels named {len(counts)} of your holdings today: "
                + "; ".join(one(*c) for c in counts[:self.TELEGRAM_TOP]) + more
                + ". Unverified opinions, not used in signals: check the stock page before acting."]

    def status(self) -> dict:
        now = self.clock.now()
        chat = self.repo.get_setting(CHAT_KEY, shared=True) or {}
        return {"token_set": self.client is not None, "linked": self.chat_id is not None, "enabled": self.enabled,
                "chat_name": chat.get("name"), "chat_type": chat.get("type", "private"),
                "quiet_hours_ist": f"{self.settings.notify_quiet_start}-{self.settings.notify_quiet_end}",
                "in_quiet_hours": in_quiet_hours(now, *self.quiet), "min_severity": self.settings.notify_min_severity,
                "summary_ist": self.settings.notify_summary_time, "last_error": self.last_error}

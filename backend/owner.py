"""The dashboard's owner (Raed) and logging in through his Telegram.

- The owner is one Telegram chat, kept in bot_state["owner_chat"]. It is set once, either with
  "/owner <code>" (a one-time code the cloud run places from the OWNER_CLAIM_CODE secret, stored
  hashed and erased once used), or from an already logged-in dashboard ("link my Telegram").
- Logging in: the page asks for a login ticket and shows a 4-digit code plus a t.me link. The bot
  sends the owner the same code with "yes, it's me" / "no" buttons. The page is let in only after
  the owner taps yes. Anyone else who opens the link gets nothing. Tickets live 5 minutes.
- Every successful login is reported to the owner in Telegram.
"""

import hashlib
import hmac
import secrets
import time

from . import storage

TICKET_SECONDS = 300
LINK_SECONDS = 900


def _h(s):
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()


def owner_chat():
    return storage.read("bot_state").get("owner_chat")


def _prune(d, ttl):
    now = time.time()
    return {k: v for k, v in (d or {}).items() if now - v.get("at", 0) < ttl}


# ---------------------------------------------------------------- claiming the owner seat

def set_claim(code):
    """Cloud run: arm a one-time claim code (only while no owner is set)."""
    if not code:
        return False
    with storage.edit("bot_state") as st:
        if st.get("owner_chat") or st.get("owner_claim_used") == _h(code):
            return False
        st["owner_claim"] = _h(code)
    return True


def claim(chat_id, code):
    """'/owner <code>' in the bot. True if this chat became the owner."""
    code = (code or "").strip()
    if not code:
        return False
    with storage.edit("bot_state") as st:
        want = st.get("owner_claim") or ""
        if st.get("owner_chat") or not want or not hmac.compare_digest(want, _h(code)):
            return False
        st["owner_chat"] = chat_id
        st["owner_claim_used"] = want
        st.pop("owner_claim", None)
    return True


def new_owner_link():
    """Logged-in dashboard: a token for t.me/<bot>?start=owner_<token> (links or changes the owner)."""
    token = secrets.token_urlsafe(18)
    with storage.edit("bot_state") as st:
        links = _prune(st.get("owner_links"), LINK_SECONDS)
        links[_h(token)] = {"at": time.time()}
        st["owner_links"] = links
    return token


def link_owner(chat_id, token):
    with storage.edit("bot_state") as st:
        links = _prune(st.get("owner_links"), LINK_SECONDS)
        ok = _h(token) in links
        if ok:
            del links[_h(token)]
            st["owner_chat"] = chat_id
        st["owner_links"] = links
    return ok


# ---------------------------------------------------------------- login tickets

def new_ticket():
    """(ticket, code, bot_token): the page keeps the ticket (its secret, never shown), shows the code,
    and opens t.me/<bot>?start=login_<bot_token>. Someone who sees that link can't use it to log in."""
    ticket = secrets.token_urlsafe(18)
    bot_token = secrets.token_urlsafe(18)
    code = f"{secrets.randbelow(9000) + 1000}"
    with storage.edit("bot_state") as st:
        tickets = _prune(st.get("dash_login"), TICKET_SECONDS)
        tickets[_h(ticket)] = {"at": time.time(), "code": code, "state": "pending", "bot": _h(bot_token)}
        st["dash_login"] = dict(list(tickets.items())[-20:])
    return ticket, code, bot_token


def ticket_for_bot(token):
    """The pending ticket a /start login_<token> refers to: (key, code) or (None, None)."""
    want = _h(token or "")
    tickets = _prune(storage.read("bot_state").get("dash_login"), TICKET_SECONDS)
    key = next((k for k, t in tickets.items() if hmac.compare_digest(t.get("bot", ""), want)), None)
    if not key or tickets[key].get("state") != "pending":
        return None, None
    return key[:16], tickets[key]["code"]


def answer(chat_id, short_key, yes):
    """The owner tapped yes / no in the bot. True if a pending ticket changed."""
    if chat_id != owner_chat():
        return False
    with storage.edit("bot_state") as st:
        tickets = _prune(st.get("dash_login"), TICKET_SECONDS)
        hit = next((k for k in tickets if k[:16] == short_key and tickets[k].get("state") == "pending"), None)
        if hit:
            tickets[hit]["state"] = "approved" if yes else "denied"
        st["dash_login"] = tickets
    return bool(hit)


def check_ticket(ticket):
    """'pending' | 'approved' (consumed: the session is created once) | 'denied' | 'expired'."""
    with storage.edit("bot_state") as st:
        tickets = _prune(st.get("dash_login"), TICKET_SECONDS)
        t = tickets.get(_h(ticket or ""))
        state = t.get("state") if t else "expired"
        if state in ("approved", "denied"):
            del tickets[_h(ticket)]
        st["dash_login"] = tickets
    return state

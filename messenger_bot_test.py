"""Ace Bot — Hybrid AI Messenger Bot Final Deployment.

Architecture (strict processing order per incoming message):
  1. HMAC-SHA256 signature check         — rejects unsigned/forged payloads
  2. Echo filter                         — breaks admin-reply infinite loop
  3. Message deduplication (Redis SETNX) — drops Facebook retry spam
  4. Bot paused guard                    — silent while admin has thread
  5. Admin handover keywords             — SMTP alert + per-user pause (first)
  6. First-time greeting + carousel      — rule-based, fires exactly once per user
  7. Product keyword / price search      — pure rule-based, JSON = source of truth
  8. Gemini conversational fallback      — only fires when rules don't match

Session Storage:
  - Primary : Redis (Upstash) — persists across Render restarts/rebuilds
  - Fallback : In-memory dict — used if REDIS_URL is not set (local dev)

Deduplication:
  - Primary : Redis SETNX with 5-min TTL — atomic, cross-worker safe
  - Fallback : In-memory deque(100) — used if REDIS_URL is not set

Data contract (products.json):
  - keywords    : list[str]   — all lowercase, no currency symbols
  - price       : int | float — numeric only (e.g. 450, not "₱450")
  - category    : str         — "oversized_tee" | "mesh_short" | "hoodie" | etc.
  - availability: str         — "In Stock" | "Limited Edition" | "Low Stock" | "Out of Stock"

Deployment : Render (gunicorn messenger_bot_test:app)
Python     : 3.11+
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from queue import Full, Queue
from threading import Lock, Thread
from typing import Any, Optional

import google.generativeai as genai
import redis as redis_lib
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, jsonify, request

__all__: list[str] = []

# ============================================================================
# SECTION 1 — Setup & Security
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _read_secret(filename: str, env_key: str) -> Optional[str]:
    """Read a secret from a Render Secret File, falling back to an env var."""
    try:
        with open(f"/etc/secrets/{filename}") as f:
            value = f.read().strip()
            if value:
                return value
    except FileNotFoundError:
        pass
    return os.environ.get(env_key)


# ── Credentials ─────────────────────────────────────────────────────────────
FB_APP_SECRET     = _read_secret("FB_APP_SECRET", "FB_APP_SECRET")
PAGE_ACCESS_TOKEN = _read_secret("PAGE_ACCESS_TOKEN", "PAGE_ACCESS_TOKEN")

VERIFY_TOKEN        = os.environ.get("VERIFY_TOKEN")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY")
SENDER_EMAIL        = os.environ.get("SENDER_EMAIL")
SENDER_PASSWORD     = os.environ.get("SENDER_PASSWORD")
RECEIVER_EMAIL      = os.environ.get("RECEIVER_EMAIL")
GITHUB_PRODUCTS_URL = os.environ.get("GITHUB_PRODUCTS_URL")
HEALTH_TOKEN        = os.environ.get("HEALTH_TOKEN")
REDIS_URL           = os.environ.get("REDIS_URL")  # e.g. redis://default:password@host:port

# ── Service config ───────────────────────────────────────────────────────────
GRAPH_API_VERSION  = "v22.0"
GEMINI_MODEL       = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
SMTP_HOST          = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT          = int(os.environ.get("SMTP_PORT", "587"))
CACHE_REFRESH_MINS = int(os.environ.get("CACHE_REFRESH_MINS", "60"))

# ── Input guards ─────────────────────────────────────────────────────────────
MAX_INPUT_CHARS   = 500
MAX_PAYLOAD_CHARS = 1_000

# ── Rate limits ──────────────────────────────────────────────────────────────
EMAIL_RATE_LIMIT  = 2    # max admin emails per user
EMAIL_WINDOW_SECS = 300  # within this window (seconds)

# ── Dedup TTL ────────────────────────────────────────────────────────────────
DEDUP_TTL_SECS   = 300                # how long to remember a message ID in Redis
SESSION_TTL_SECS = 60 * 60 * 24 * 90  # expire inactive sessions after 90 days

# GITHUB_PRODUCTS_URL validated at startup — prevents SSRF.
_GITHUB_RAW_PATTERN = re.compile(
    r"^https://raw\.githubusercontent\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
)

# Price-filter regex — e.g. "below 450", "under 500", "above 300"
_PRICE_RE = re.compile(
    r"\b(below|under|less than|above|over|more than)\s+(\d+(?:\.\d+)?)\b"
)

_GEMINI_SYSTEM_INSTRUCTION = """\
You are Sofia, an AI customer assistant for Ace Apparel — a Filipino streetwear brand.

Rules:
- Reply in natural Taglish (Tagalog-English mix). Use "po" and "opo" naturally.
- Address the customer by their first name when available.
- You may answer questions about shipping, sizing, store locations, and brand info.
- If the customer wants to browse products, tell them to type a keyword such as
  "mesh shorts", "oversized tee", or "products" to see the catalogue.
- NEVER state specific product prices, SKUs, or stock levels yourself.
  All product data is served by the rule engine, not by you.
- Keep replies concise — under 200 characters where possible.
"""

HANDOVER_KEYWORDS = frozenset({
    "refund", "complaint", "complain", "admin", "manager", "supervisor",
    "problema", "issue", "reklamo", "balik", "return", "problem", "cancel",
})

_GREETING_KEYWORDS = frozenset({
    "hi", "hello", "hey", "kumusta", "kamusta", "musta", "good morning",
    "good afternoon", "good evening", "magandang umaga", "magandang hapon",
    "magandang gabi", "sup", "yo",
})

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB hard cap


def _verify_signature(raw_body: bytes, header: str) -> bool:
    """Validate X-Hub-Signature-256 using constant-time comparison."""
    if not FB_APP_SECRET:
        logger.error("FB_APP_SECRET not set — rejecting all webhook traffic.")
        return False
    if not header or not header.startswith("sha256="):
        return False
    computed = hmac.new(FB_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, header[7:])


# ============================================================================
# SECTION 2 — Redis Client + Session & Deduplication
# ============================================================================

# Redis client — set in _startup(). None means Redis unavailable → use fallback.
_redis: Optional[Any] = None  # redis_lib.Redis instance when connected

# ── In-memory fallbacks (used when REDIS_URL is not set e.g. local dev) ──────
_seen_message_ids: deque[str] = deque(maxlen=100)
_user_sessions:   dict[str, dict] = {}

# One lock for all in-memory state mutations (threads share memory).
# Also used to serialise Redis session reads+writes within this process.
_state_lock = Lock()

# Webhook processing queue
_event_queue: "Queue[dict]" = Queue(maxsize=int(os.environ.get("WEBHOOK_QUEUE_MAX", "200")))
_event_pool:  Optional[ThreadPoolExecutor] = None


# ── Deduplication ─────────────────────────────────────────────────────────────

def _is_duplicate(mid: str) -> bool:
    """Return True if this message/postback ID was already processed.

    Redis path  : SETNX with TTL — atomic, survives restarts, cross-worker safe.
    Fallback    : in-memory deque — used when REDIS_URL is not configured.
    """
    if _redis is not None:
        try:
            # SET NX EX: set the key only if it does NOT exist, expire after TTL.
            # Returns True if key was set (new), None if key already existed (dup).
            added = _redis.set(f"seen:{mid}", "1", nx=True, ex=DEDUP_TTL_SECS)
            return added is None
        except Exception as exc:
            logger.error("Redis dedup error (falling back to in-memory): %s", exc)

    # In-memory fallback
    with _state_lock:
        if mid in _seen_message_ids:
            return True
        _seen_message_ids.append(mid)
        return False


# ── Session helpers ───────────────────────────────────────────────────────────

def _session_key(psid: str) -> str:
    return f"session:{psid}"


def _get_session(psid: str) -> dict:
    """Fetch session dict from Redis or in-memory fallback.

    Always returns a complete dict with all expected keys.
    Caller must hold _state_lock if modifying the returned dict
    and then calling _save_session().
    """
    if _redis is not None:
        try:
            raw = _redis.get(_session_key(psid))
            if raw:
                data = json.loads(raw)
                # Ensure all keys exist (safe for sessions created by older versions)
                data.setdefault("greeted",  False)
                data.setdefault("paused",   False)
                data.setdefault("email_ts", [])
                return data
        except Exception as exc:
            logger.error("Redis get_session error: %s", exc)

    # In-memory fallback
    return _user_sessions.setdefault(psid, {
        "greeted":  False,
        "paused":   False,
        "email_ts": [],
    })


def _save_session(psid: str, session: dict) -> None:
    """Persist a session dict to Redis or in-memory fallback."""
    if _redis is not None:
        try:
            # ex=SESSION_TTL_SECS resets the 90-day clock on every interaction.
            # Inactive users expire automatically; active users never lose their session.
            _redis.set(_session_key(psid), json.dumps(session), ex=SESSION_TTL_SECS)
            return
        except Exception as exc:
            logger.error("Redis save_session error: %s", exc)

    # In-memory fallback
    _user_sessions[psid] = session


def _is_first_time(psid: str) -> bool:
    """Atomically check-and-mark whether this is a user's first interaction.

    Returns True exactly once per user, then False on every subsequent call.
    Thread-safe via _state_lock; Redis provides cross-restart persistence.
    """
    with _state_lock:
        session = _get_session(psid)
        if session["greeted"]:
            return False
        session["greeted"] = True
        _save_session(psid, session)
        return True


def _mark_greeted(psid: str) -> None:
    """Mark a user as greeted without sending the greeting.

    Used when a first-ever message triggers a non-greeting path
    (e.g., admin handover) so the user doesn't get a welcome message later.
    """
    with _state_lock:
        session = _get_session(psid)
        session["greeted"] = True
        _save_session(psid, session)


def _is_paused(psid: str) -> bool:
    """Return True if the bot is paused for this user (admin has the thread)."""
    with _state_lock:
        return _get_session(psid).get("paused", False)


def _set_paused(psid: str, paused: bool) -> None:
    """Set the pause state for a user's thread."""
    with _state_lock:
        session = _get_session(psid)
        session["paused"] = paused
        _save_session(psid, session)


def _allow_email(psid: str) -> bool:
    """Sliding-window rate check for admin email alerts.

    Timestamps are stored inside the session dict so they persist in Redis.
    """
    with _state_lock:
        session = _get_session(psid)
        now     = time.time()
        recent  = [t for t in session.get("email_ts", []) if t > now - EMAIL_WINDOW_SECS]
        if len(recent) >= EMAIL_RATE_LIMIT:
            return False
        recent.append(now)
        session["email_ts"] = recent
        _save_session(psid, session)
        return True


# ============================================================================
# SECTION 3 — Data Layer
# ============================================================================

_products_cache:   list[dict]        = []
_cache_updated_at: Optional[datetime] = None
_cache_lock        = Lock()

_DEFAULT_CAROUSEL_SPEC: list[tuple[str, int]] = [
    ("oversized_tee", 4),
    ("mesh_short",    3),
    ("hoodie",        3),
]

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_product(p: dict) -> dict:
    return {
        k: _CONTROL_RE.sub("", str(v))[:500] if isinstance(v, str) else v
        for k, v in p.items()
    }


def _refresh_cache() -> None:
    global _products_cache, _cache_updated_at
    try:
        resp = requests.get(GITHUB_PRODUCTS_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            logger.error("products.json is not a JSON array — skipping refresh.")
            return
        cleaned = [_sanitize_product(p) for p in data]
        with _cache_lock:
            _products_cache   = cleaned
            _cache_updated_at = datetime.now()
        logger.info("Cache refreshed — %d products loaded.", len(cleaned))
    except requests.RequestException as exc:
        logger.error("Cache refresh failed: %s", exc)
    except Exception:
        logger.exception("Unexpected error refreshing cache.")


def _get_products() -> list[dict]:
    with _cache_lock:
        return _products_cache.copy()


def _parse_price_condition(text: str) -> Optional[tuple[str, float]]:
    m = _PRICE_RE.search(text)
    if not m:
        return None
    return m.group(1), float(m.group(2))


def _apply_price_filter(products: list[dict], operator: str, amount: float) -> list[dict]:
    lower  = operator in ("below", "under", "less than")
    result = []
    for p in products:
        try:
            price = float(p.get("price", 0))
        except (TypeError, ValueError):
            continue
        if lower and price < amount:
            result.append(p)
        elif not lower and price > amount:
            result.append(p)
    return result


def _stock_label(availability: str) -> str:
    labels = {
        "In Stock":        "Available",
        "Limited Edition": "Limited Ed.",
        "Low Stock":       "Low Stock",
        "Out of Stock":    "Out of Stock",
    }
    return labels.get(str(availability).strip(), "Out of Stock")


def _search_products(text: str) -> list[dict]:
    products   = _get_products()
    price_cond = _parse_price_condition(text)

    for p in products:
        pid   = str(p.get("id", "")).lower()
        pname = str(p.get("name", "")).lower()
        if (pid and pid in text) or (pname and pname in text):
            logger.info("Direct match: %s", p.get("id"))
            return [p]

    seen: set[str] = set()
    hits: list[dict] = []
    for p in products:
        kws = [str(k).lower() for k in p.get("keywords", [])]
        if any(k in text for k in kws) and p["id"] not in seen:
            seen.add(p["id"])
            hits.append(p)

    if price_cond and hits:
        hits = _apply_price_filter(hits, price_cond[0], price_cond[1])

    logger.info("Keyword scan: %d hit(s) for '%s'", len(hits), text[:80])
    return hits[:10]


def _build_default_carousel() -> list[dict]:
    products = _get_products()
    result: list[dict] = []
    for category, count in _DEFAULT_CAROUSEL_SPEC:
        matches = [p for p in products if p.get("category") == category]
        result.extend(matches[:count])
    return result[:10]


# ============================================================================
# SECTION 4 — Meta API Handlers
# ============================================================================

def _get_user_profile(psid: str) -> dict:
    try:
        resp = requests.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{psid}",
            params={"fields": "first_name,last_name", "access_token": PAGE_ACCESS_TOKEN},
            timeout=5,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("get_user_profile(%s): %s", psid[:20], exc)
        return {"first_name": "Customer"}


def _post_to_messenger(psid: str, message_data: dict) -> bool:
    raw = None
    try:
        raw = requests.post(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages",
            params={"access_token": PAGE_ACCESS_TOKEN},
            headers={"Content-Type": "application/json"},
            json={
                "recipient":      {"id": psid},
                "messaging_type": "RESPONSE",
                "message":        message_data,
            },
            timeout=10,
        )
        body = raw.json()
        if "error" in body:
            logger.error(
                "Graph API error [%s]: %s",
                body["error"].get("code"),
                body["error"].get("message"),
            )
            return False
        raw.raise_for_status()
        return True
    except Exception as exc:
        tail = raw.text[:300] if raw is not None else "no response"
        logger.error("_post_to_messenger failed: %s | %s", exc, tail)
        return False


def send_text(psid: str, text: str) -> bool:
    return _post_to_messenger(psid, {"text": text})


def send_carousel(psid: str, products: list[dict]) -> bool:
    if not products:
        return send_text(psid, "Naku, pasensya na po... hindi ko po mahanap yan sa catalog namin.")

    elements = []
    for p in products[:10]:
        try:
            raw_price     = str(p.get("price", 0)).replace("₱", "").replace(",", "").strip()
            price_display = f"₱{int(float(raw_price)):,}"
        except (ValueError, TypeError):
            price_display = "Contact us for price"
        stock_label = _stock_label(p.get("availability", ""))
        elements.append({
            "title":     str(p.get("name", "Ace Product"))[:80],
            "image_url": p.get("image_url", "https://via.placeholder.com/500x500.png"),
            "subtitle":  f"{price_display}  •  {stock_label}"[:80],
            "buttons": [{
                "type":    "postback",
                "title":   "View Details",
                "payload": json.dumps({
                    "action":     "view_price",
                    "product_id": p.get("id", ""),
                }),
            }],
        })

    return _post_to_messenger(psid, {
        "attachment": {
            "type":    "template",
            "payload": {"template_type": "generic", "elements": elements},
        }
    })


def _send_typing(psid: str, on: bool = True) -> None:
    try:
        requests.post(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages",
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={
                "recipient":     {"id": psid},
                "sender_action": "typing_on" if on else "typing_off",
            },
            timeout=5,
        )
    except Exception as exc:
        logger.debug("Typing indicator error (non-critical): %s", exc)


def _send_product_detail(psid: str, product: dict, first_name: str) -> None:
    send_carousel(psid, [product])
    try:
        raw_price     = str(product.get("price", 0)).replace("₱", "").replace(",", "").strip()
        price_display = f"₱{int(float(raw_price)):,}"
    except (ValueError, TypeError):
        price_display = "Contact us for price"
    stock_label = _stock_label(product.get("availability", ""))
    send_text(psid, (
        f"{product.get('name')}\n"
        f"Price:        {price_display}\n"
        f"Availability: {stock_label}\n"
        f"SKU:          {product.get('id', 'N/A')}\n\n"
        f"{product.get('description', '')}\n\n"
        f"Interesado po, {first_name}? Mag-message lang!"
    ))


def _notify_admin(psid: str, message: str, profile: dict) -> bool:
    if not (SENDER_EMAIL and SENDER_PASSWORD and RECEIVER_EMAIL):
        logger.warning("Admin email not configured — handover email skipped.")
        return False
    if not _allow_email(psid):
        logger.warning("Email rate-limited for %s — suppressed.", psid[:20])
        return False

    name = f"{profile.get('first_name','')} {profile.get('last_name','')}".strip()
    body = (
        f"Handover triggered.\n\n"
        f"Customer : {name}\n"
        f"PSID     : {psid}\n"
        f"Message  : {message}\n"
        f"Time     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Reply in Facebook Page Inbox.\n"
        f"Type 'bot' or 'sofia' in the inbox thread to resume the bot."
    )
    msg            = MIMEMultipart()
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECEIVER_EMAIL
    msg["Subject"] = f"Ace Bot — Handover: {profile.get('first_name','Customer')}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        logger.info("Handover email sent for %s.", psid[:20])
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False


# ============================================================================
# SECTION 5 — Hybrid Routing Engine
# ============================================================================

def _send_first_time_greeting(psid: str) -> None:
    """Send the welcome message + default carousel. Call _is_first_time() before this."""
    profile    = _get_user_profile(psid)
    first_name = profile.get("first_name", "Customer")
    send_text(psid, (
        f"Hi {first_name}! I'm Sofia, your AI assistant for Ace Apparel.\n\n"
        f"Feel free to ask me anything — Oversized Tees, Mesh Shorts, "
        f"Hoodies, Jerseys, Socks, and Gym Sandos. "
        f"I'm here to help you find the right fit!"
    ))
    default_products = _build_default_carousel()
    if default_products:
        send_text(psid, "Here are some of our popular items po:")
        send_carousel(psid, default_products)


def _handle_message(psid: str, raw_text: str) -> None:
    """Route one incoming message through the hybrid engine.

    Processing order (strict — do not reorder):
      1. Admin handover    — fires even on first-ever message
      2. First-time greeting + carousel
      3. Catalog browse trigger
      4. Greeting intercept
      5. Product keyword / SKU / price search
      6. Gemini conversational fallback
    """
    _send_typing(psid, True)

    try:
        profile    = _get_user_profile(psid)
        first_name = profile.get("first_name", "Customer")
        text       = raw_text[:MAX_INPUT_CHARS].strip().lower()

        # ── Step 1: Admin handover ────────────────────────────────────────────
        # Bot does NOT auto-pause here. It alerts admin via email and keeps
        # responding. Admin pauses the bot by typing any message in Page Inbox.
        if any(kw in text for kw in HANDOVER_KEYWORDS):
            send_text(psid, (
                "We are really sorry for the inconvenience po.\n"
                "I-a-alert ko na po si admin para matulungan po kayo agad."
            ))
            _mark_greeted(psid)
            _notify_admin(psid, raw_text, profile)
            return

        # ── Step 2: First-time greeting ───────────────────────────────────────
        if _is_first_time(psid):
            _send_first_time_greeting(psid)
            return

        # ── Step 3: Catalog browse trigger ────────────────────────────────────
        if any(kw in text for kw in ("products", "product", "catalog", "catalogue", "browse", "listahan")):
            default_products = _build_default_carousel()
            if default_products:
                send_text(psid, f"Here are some of our items po, {first_name}:")
                send_carousel(psid, default_products)
            else:
                send_text(psid, "Naku, pasensya na po... wala pang products sa catalog.")
            return

        # ── Step 4: Greeting intercept ────────────────────────────────────────
        if text in _GREETING_KEYWORDS or any(text.startswith(kw) for kw in _GREETING_KEYWORDS):
            send_text(psid, f"Nandito pa po ako, {first_name}! Paano kita matutulungan?")
            default_products = _build_default_carousel()
            if default_products:
                send_text(psid, "Here are some of our popular items po:")
                send_carousel(psid, default_products)
            return

        # ── Step 5: Product search ────────────────────────────────────────────
        matches = _search_products(text)
        if matches:
            if len(matches) == 1:
                _send_product_detail(psid, matches[0], first_name)
            else:
                send_text(psid, (
                    f"Nahanap ko po ang {len(matches)} product(s) para sa inyo, {first_name}!"
                ))
                send_carousel(psid, matches)
            return

        # ── Step 6: Gemini fallback ───────────────────────────────────────────
        send_text(psid, _get_gemini_reply(raw_text, first_name))

    except Exception:
        logger.exception("Unhandled error in _handle_message for %s.", psid[:20])
        send_text(psid, "Sorry po, may technical issue kami. Please try again.")
    finally:
        _send_typing(psid, False)


def _get_gemini_reply(user_message: str, first_name: str) -> str:
    try:
        model  = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=_GEMINI_SYSTEM_INSTRUCTION,
        )
        prompt = f"Customer ({first_name}): {user_message}\nSofia:"
        resp   = model.generate_content(
            prompt,
            request_options={"timeout": int(os.environ.get("GEMINI_TIMEOUT_SECS", "12"))},
        )
        return (resp.text or "").strip()
    except Exception as exc:
        logger.error("Gemini error: %s", exc)
        return (
            f"Pasensya na po, {first_name}, may konting issue kami. "
            f"Subukan po ulit mamaya o mag-type ng 'products' para makita ang aming catalog."
        )


def _handle_admin_echo(event: dict) -> None:
    """Handle echo events from admin messages in the Page inbox."""
    psid = event.get("recipient", {}).get("id")
    text = event.get("message", {}).get("text", "")

    if not psid:
        logger.warning("Echo event missing recipient.id — skipping.")
        return

    if text.strip().lower() in ("bot", "sofia"):
        _set_paused(psid, False)
        send_text(psid, "Bumalik na po ako! Paano ko pa po kayo matutulungan?")
        logger.info("Bot resumed for user %s.", psid[:20])
    else:
        _set_paused(psid, True)
        logger.info("Admin replied to %s — bot remains paused.", psid[:20])


def _handle_postback(psid: str, raw_payload: str) -> None:
    """Route postback events from carousel buttons and the Get Started button."""
    raw_payload = raw_payload[:MAX_PAYLOAD_CHARS]

    try:
        data       = json.loads(raw_payload)
        profile    = _get_user_profile(psid)
        first_name = profile.get("first_name", "Customer")

        if data.get("action") == "view_price":
            product_id = data.get("product_id", "")
            product    = next(
                (p for p in _get_products() if str(p.get("id")) == str(product_id)),
                None,
            )
            if product:
                _send_product_detail(psid, product, first_name)
            else:
                send_text(psid, f"Sorry po {first_name}, hindi ko mahanap ang product na 'yan.")
        else:
            logger.warning("Unknown postback action: %s", data.get("action"))

    except json.JSONDecodeError:
        if raw_payload.strip().upper() == "GET_STARTED":
            if _is_first_time(psid):
                _send_first_time_greeting(psid)
            else:
                profile    = _get_user_profile(psid)
                first_name = profile.get("first_name", "Customer")
                send_text(psid, f"Nandito pa po ako, {first_name}! Paano kita matutulungan?")
                default_products = _build_default_carousel()
                if default_products:
                    send_text(psid, "Here are some of our popular items po:")
                    send_carousel(psid, default_products)
            return
        logger.info("Plain-string postback: %s", raw_payload[:60])
    except Exception as exc:
        logger.error("_handle_postback error: %s", exc)


# ============================================================================
# SECTION 6 — Flask Routes
# ============================================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        logger.info("Webhook verified.")
        return request.args.get("hub.challenge", ""), 200
    logger.warning("Webhook verification failed — token mismatch.")
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    """Receive Messenger events. Returns 200 immediately; processing is async."""
    raw_body = request.get_data()
    if not _verify_signature(raw_body, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("Rejected POST — bad signature from %s.", request.remote_addr)
        abort(403)

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return "Bad Request", 400

    if data.get("object") != "page":
        return "OK", 200

    try:
        _event_queue.put_nowait(data)
        logger.info("Event queued. Queue size: %d", _event_queue.qsize())
    except Full:
        logger.error("Webhook queue full — dropping payload to protect uptime.")
    return "OK", 200


@app.route("/health", methods=["GET"])
def health_check():
    if not HEALTH_TOKEN or request.headers.get("Authorization") != f"Bearer {HEALTH_TOKEN}":
        abort(404)

    age = None
    if _cache_updated_at:
        age = round((datetime.now() - _cache_updated_at).total_seconds())

    redis_ok = False
    if _redis is not None:
        try:
            _redis.ping()
            redis_ok = True
        except Exception:
            pass

    return jsonify({
        "status":          "healthy",
        "redis_connected": redis_ok,
        "products_cached": len(_get_products()),
        "cache_age_secs":  age,
        "timestamp":       datetime.now().isoformat(),
    }), 200


@app.route("/", methods=["GET"])
def index():
    return jsonify({"bot": "Ace Bot", "version": "Final Deployment — Redis"}), 200


# ============================================================================
# SECTION 7 — Startup
# ============================================================================

def _validate_env() -> None:
    required = {
        "PAGE_ACCESS_TOKEN":   PAGE_ACCESS_TOKEN,
        "FB_APP_SECRET":       FB_APP_SECRET,
        "VERIFY_TOKEN":        VERIFY_TOKEN,
        "GEMINI_API_KEY":      GEMINI_API_KEY,
        "GITHUB_PRODUCTS_URL": GITHUB_PRODUCTS_URL,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise EnvironmentError(f"Critical secrets missing: {missing}")

    if not _GITHUB_RAW_PATTERN.match(GITHUB_PRODUCTS_URL):
        raise EnvironmentError(
            f"GITHUB_PRODUCTS_URL must be a raw.githubusercontent.com URL. "
            f"Got: {GITHUB_PRODUCTS_URL!r}"
        )

    optional = {
        "HEALTH_TOKEN":    HEALTH_TOKEN,
        "SENDER_EMAIL":    SENDER_EMAIL,
        "SENDER_PASSWORD": SENDER_PASSWORD,
        "RECEIVER_EMAIL":  RECEIVER_EMAIL,
        "REDIS_URL":       REDIS_URL,
    }
    missing_opt = [k for k, v in optional.items() if not v]
    if missing_opt:
        logger.warning(
            "Optional config missing: %s", ", ".join(missing_opt),
        )
    logger.info("Environment validated.")


def _init_redis() -> None:
    """Connect to Redis. Sets module-level _redis on success, leaves it None on failure."""
    global _redis
    if not REDIS_URL:
        logger.warning("REDIS_URL not set — using in-memory session storage (not persistent).")
        return
    try:
        client = redis_lib.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
        client.ping()
        _redis = client
        logger.info("Redis connected: %s", REDIS_URL.split("@")[-1])  # log host only, not password
    except Exception as exc:
        logger.error("Redis connection failed — falling back to in-memory: %s", exc)


def _startup() -> None:
    """Initialise Redis, Gemini, product cache, scheduler, and webhook worker."""
    _init_redis()

    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini configured (model: %s).", GEMINI_MODEL)

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _refresh_cache, "interval", minutes=CACHE_REFRESH_MINS,
        id="refresh_cache", replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — cache refresh every %d min.", CACHE_REFRESH_MINS)

    _refresh_cache()

    global _event_pool
    _event_pool = ThreadPoolExecutor(
        max_workers=int(os.environ.get("WEBHOOK_WORKERS", "4")),
        thread_name_prefix="webhook",
    )

    def _process_one(ev: dict) -> None:
        logger.info("Event keys: %s", list(ev.keys()))
        psid = ev.get("sender", {}).get("id")
        if not psid:
            logger.info("No PSID found in event — skipping.")
            return

        if "message" in ev:
            msg = ev["message"]
            if msg.get("is_echo"):
                _handle_admin_echo(ev)
                return
            text = msg.get("text", "").strip()
            mid  = msg.get("mid", "")
            if not text or not mid:
                return
            if _is_duplicate(mid):
                logger.info("Duplicate mid dropped: %s", mid)
                return
            if _is_paused(psid):
                logger.info("Bot paused for %s — message ignored.", psid[:20])
                return
            _handle_message(psid, text)
            return

        if "postback" in ev:
            postback    = ev.get("postback", {})
            payload_str = postback.get("payload", "")
            if not payload_str:
                return
            pb_mid = postback.get("mid")
            if pb_mid:
                if _is_duplicate(pb_mid):
                    logger.info("Duplicate postback mid dropped: %s", pb_mid)
                    return
            else:
                pb_key = "pb:" + hashlib.sha256(
                    f"{psid}:{payload_str}".encode()
                ).hexdigest()
                if _is_duplicate(pb_key):
                    logger.info("Duplicate postback dropped for %s.", psid[:20])
                    return
            _handle_postback(psid, payload_str)

    def _webhook_worker() -> None:
        logger.info("Webhook worker thread is alive.")
        while True:
            payload = _event_queue.get()
            try:
                futures = []
                logger.info("Payload entry count: %d", len(payload.get("entry", [])))  # ADD
                for entry in payload.get("entry", []):
                    logger.info("Entry keys: %s | messaging count: %d", list(entry.keys()), len(entry.get("messaging", [])))  # ADD
                    for event in entry.get("messaging", []):
                        try:
                            if _event_pool is None:
                                _process_one(event)
                            else:
                                futures.append(_event_pool.submit(_process_one, event))
                        except Exception:
                            logger.exception("Failed to dispatch webhook event.")
                if futures:
                    timeout = int(os.environ.get("WEBHOOK_PAYLOAD_TIMEOUT_SECS", "25"))
                    done, not_done = wait(futures, timeout=timeout)
                    for fut in done:
                        try:
                            fut.result()
                        except Exception:
                            logger.exception("Webhook task failed.")
                    if not_done:
                        logger.warning(
                            "%d webhook task(s) still running after %ds.",
                            len(not_done), timeout,
                        )
            except Exception:
                logger.exception("Unhandled error processing webhook payload.")
            finally:
                _event_queue.task_done()

    Thread(target=_webhook_worker, daemon=True).start()
    logger.info("Webhook worker started (queue max: %d).", _event_queue.maxsize)


# Module-level startup — runs when Gunicorn imports this module.
_validate_env()
# _startup() is called by gunicorn post_fork hook in gunicorn.conf.py
if __name__ == "__main__":
    _startup()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)





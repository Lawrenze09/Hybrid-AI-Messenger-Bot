"""Ace Bot — Hybrid AI Messenger Bot Final Deployment.

Architecture (strict processing order per incoming message):
  1. HMAC-SHA256 signature check         — rejects unsigned/forged payloads
  2. Echo filter                         — breaks admin-reply infinite loop
  3. Message deduplication (deque/100)   — drops Facebook retry spam
  4. Bot paused guard                    — silent while admin has thread
  5. First-time greeting + carousel      — rule-based, fires exactly once per user
  6. Admin handover keywords             — SMTP alert + per-user pause
  7. Product keyword / price search      — pure rule-based, JSON = source of truth
  8. Gemini conversational fallback      — only fires when rules don't match

Data contract (products.json):
  - keywords    : list[str]   — all lowercase, no currency symbols
  - price       : int | float — numeric only (e.g. 450, not "₱450")
  - category    : str         — "oversized_tee" | "mesh_short" | "hoodie" | etc.
  - availability: str         — "Available" | "Out of Stock"

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
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from threading import Lock
from typing import Optional

import google.generativeai as genai
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
    """Read a secret from a Render Secret File, falling back to an env var.

    Render mounts Secret Files at ``/etc/secrets/<filename>``. These never
    appear in process listings or env dumps — the right place for the most
    sensitive tokens (App Secret, Page Access Token).

    Args:
        filename: Basename of the file under ``/etc/secrets/``.
        env_key:  Env var name used as a local-dev fallback.

    Returns:
        The secret value, or ``None`` if neither source has it.
    """
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

# GITHUB_PRODUCTS_URL validated at startup — prevents SSRF if the env var
# is accidentally pointed at an internal service.
_GITHUB_RAW_PATTERN = re.compile(
    r"^https://raw\.githubusercontent\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
)

# Price-filter regex — e.g. "below 450", "under 500", "above 300"
_PRICE_RE = re.compile(
    r"\b(below|under|less than|above|over|more than)\s+(\d+(?:\.\d+)?)\b"
)

# Sofia's Gemini system instruction — tells the LLM what it can and cannot do.
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

# Flask app
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB hard cap


def _verify_signature(raw_body: bytes, header: str) -> bool:
    """Validate the X-Hub-Signature-256 header on every incoming webhook POST.

    Facebook signs every delivery with HMAC-SHA256(app_secret, raw_body).
    Using ``hmac.compare_digest`` prevents timing-oracle attacks — an attacker
    cannot brute-force the App Secret one byte at a time by measuring how long
    a rejected comparison takes.

    Args:
        raw_body: Raw request bytes, read before Flask parses JSON.
        header:   Value of the ``X-Hub-Signature-256`` request header.

    Returns:
        ``True`` if the digest matches, ``False`` otherwise.
    """
    if not FB_APP_SECRET:
        logger.error("FB_APP_SECRET not set — rejecting all webhook traffic.")
        return False
    if not header or not header.startswith("sha256="):
        return False
    computed = hmac.new(FB_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, header[7:])


# ============================================================================
# SECTION 2 — Memory & Deduplication
# ============================================================================

# deque(maxlen=100) — O(1) append, O(n) lookup. Fine at this message volume.
# At production scale, swap for a Redis SET with TTL.
_seen_message_ids: deque[str] = deque(maxlen=100)

# Per-user session state keyed by PSID.
# Schema: {"greeted": bool, "paused": bool, "email_ts": list[float]}
_user_sessions: dict[str, dict] = {}

# One lock protects both shared structures above.
_state_lock = Lock()


def _is_duplicate(mid: str) -> bool:
    """Return True if this message ID has already been processed.

    Facebook retries webhook delivery when it doesn't receive a timely 200.
    Without dedup a slow downstream call causes the same message to fire twice.

    Args:
        mid: The ``mid`` field from the Messenger event.

    Returns:
        ``True`` if already seen, ``False`` (and registers the ID) otherwise.
    """
    with _state_lock:
        if mid in _seen_message_ids:
            return True
        _seen_message_ids.append(mid)
        return False


def _get_session(psid: str) -> dict:
    """Return the mutable session dict for a user, creating it on first access.

    Caller must hold ``_state_lock`` if mutating the returned dict outside
    of the dedicated helper methods below.

    Args:
        psid: Facebook Page-Scoped User ID.
    """
    return _user_sessions.setdefault(psid, {
        "greeted":  False,
        "paused":   False,
        "email_ts": [],
    })


def _is_first_time(psid: str) -> bool:
    """Atomically check-and-mark whether this is a user's first message.

    The check and the write happen inside a single lock acquisition — no race
    window even if two events for the same PSID arrive concurrently.

    Args:
        psid: Facebook Page-Scoped User ID.

    Returns:
        ``True`` exactly once per user; ``False`` on every subsequent call.
    """
    with _state_lock:
        session = _get_session(psid)
        if session["greeted"]:
            return False
        session["greeted"] = True
        return True


def _is_paused(psid: str) -> bool:
    """Return True if the bot is paused for this user (admin has the thread).

    Args:
        psid: Facebook Page-Scoped User ID.
    """
    with _state_lock:
        return _get_session(psid).get("paused", False)


def _set_paused(psid: str, paused: bool) -> None:
    """Set the pause state for a user's thread.

    Args:
        psid:   Facebook Page-Scoped User ID.
        paused: ``True`` to pause (admin active), ``False`` to resume (bot active).
    """
    with _state_lock:
        _get_session(psid)["paused"] = paused


def _allow_email(psid: str) -> bool:
    """Sliding-window rate check for admin email alerts.

    A spoofed webhook flood would otherwise spam the admin inbox and trigger
    Google's abuse filters, permanently disabling the sender account.

    Args:
        psid: Facebook Page-Scoped User ID.

    Returns:
        ``True`` if within quota, ``False`` if throttled.
    """
    with _state_lock:
        session   = _get_session(psid)
        now       = time.time()
        recent    = [t for t in session["email_ts"] if t > now - EMAIL_WINDOW_SECS]
        if len(recent) >= EMAIL_RATE_LIMIT:
            return False
        recent.append(now)
        session["email_ts"] = recent
        return True


# ============================================================================
# SECTION 3 — Data Layer
# ============================================================================

_products_cache:   list[dict]        = []
_cache_updated_at: Optional[datetime] = None
_cache_lock        = Lock()

# First-time carousel composition: category → count (must sum to ≤ 10).
_DEFAULT_CAROUSEL_SPEC: list[tuple[str, int]] = [
    ("oversized_tee", 4),
    ("mesh_short",    3),
    ("hoodie",        3),
]

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_product(p: dict) -> dict:
    """Strip control characters from string fields to protect downstream payloads.

    Args:
        p: Raw product dict from the JSON feed.

    Returns:
        Cleaned copy with string values capped at 500 chars.
    """
    return {
        k: _CONTROL_RE.sub("", str(v))[:500] if isinstance(v, str) else v
        for k, v in p.items()
    }


def _refresh_cache() -> None:
    """Fetch products.json from GitHub and replace the in-memory cache.

    Called once at startup and then every CACHE_REFRESH_MINS by APScheduler.
    Logs the error and returns silently on failure — the scheduler must keep
    running even when GitHub is unreachable.
    """
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
    """Return a safe snapshot of the product cache for iteration.

    Returns:
        Shallow copy — safe to use outside the lock.
    """
    with _cache_lock:
        return _products_cache.copy()


def _parse_price_condition(text: str) -> Optional[tuple[str, float]]:
    """Extract a price-filter condition from free text.

    Examples:
        "mesh short below 500"    → ("below", 500.0)
        "hoodies above 300"       → ("above", 300.0)
        "oversized tee less than 450" → ("less than", 450.0)

    Args:
        text: Normalised (lowercased) customer message.

    Returns:
        ``(operator, amount)`` tuple, or ``None`` if no price condition found.
    """
    m = _PRICE_RE.search(text)
    if not m:
        return None
    return m.group(1), float(m.group(2))


def _apply_price_filter(
    products: list[dict], operator: str, amount: float
) -> list[dict]:
    """Filter products by a numeric price threshold.

    Prices in products.json are stored as integers/floats (no ₱ symbol) —
    this makes comparison trivial with no string-parsing hacks.

    Args:
        products: Product list to filter.
        operator: One of "below" / "under" / "less than" / "above" / "over"
                  / "more than".
        amount:   Numeric price threshold.

    Returns:
        Filtered list.
    """
    lower = operator in ("below", "under", "less than")
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
    """Map products.json availability to display label."""
    labels = {
        "In Stock": "Available",
        "Limited Edition": "Limited Ed.",
        "Low Stock": "Low Stock",
        "Out of Stock": "Out of Stock",
    }
    return labels.get(str(availability).strip(), "Out of Stock")


def _search_products(text: str) -> list[dict]:
    """Match a customer message against the product catalogue.

    Three-pass strategy:

    Pass 1 — Direct SKU / name containment.
        Checks if a product ID or name appears *within* the message (not
        equality). "Magkano ang ACE-OVT-001" → matches ACE-OVT-001.

    Pass 2 — Keyword overlap.
        Checks lowercase keywords array. All keywords in products.json must
        be lowercase (data contract). Collects up to 10 hits.

    Pass 3 — Optional price filter.
        Applied on top of keyword results if the message contains a price
        condition (e.g. "mesh shorts below 500").

    Args:
        text: Normalised (lowercased, stripped) customer message.

    Returns:
        List of matching product dicts, capped at 10 (FB carousel limit).
    """
    products   = _get_products()
    price_cond = _parse_price_condition(text)

    # Pass 1 — SKU or name contained anywhere in the message.
    for p in products:
        pid   = str(p.get("id", "")).lower()
        pname = str(p.get("name", "")).lower()
        if (pid and pid in text) or (pname and pname in text):
            logger.info("Direct match: %s", p.get("id"))
            return [p]

    # Pass 2 — keyword overlap.
    seen: set[str] = set()
    hits: list[dict] = []
    for p in products:
        kws = [str(k).lower() for k in p.get("keywords", [])]
        if any(k in text for k in kws) and p["id"] not in seen:
            seen.add(p["id"])
            hits.append(p)

    # Pass 3 — optional price filter on top of keyword results.
    if price_cond and hits:
        hits = _apply_price_filter(hits, price_cond[0], price_cond[1])

    logger.info("Keyword scan: %d hit(s) for '%s'", len(hits), text[:80])
    return hits[:10]


def _build_default_carousel() -> list[dict]:
    """Select products for the first-time welcome carousel.

    Pulls 4 oversized tees, 3 mesh shorts, and 3 hoodies by the ``category``
    field. Falls back gracefully if a category has fewer items than requested.

    Returns:
        Ordered list of up to 10 product dicts.
    """
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
    """Fetch first and last name from the Graph API.

    Args:
        psid: Facebook Page-Scoped User ID.

    Returns:
        Profile dict. Falls back to ``{"first_name": "Customer"}`` on any error.
    """
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
    """Internal POST to the Messenger Send API.

    ``messaging_type="RESPONSE"`` is required for Generic Templates on Graph
    API v12+. Plain text has a legacy fallback; templates silently fail without it.

    Args:
        psid:         Recipient's PSID.
        message_data: Messenger message object (text or attachment).

    Returns:
        ``True`` on success, ``False`` on any API or network error.
    """
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
    """Send a plain-text message to a Messenger user.

    Args:
        psid: Recipient's Facebook Page-Scoped User ID.
        text: Message body.

    Returns:
        ``True`` on success.
    """
    return _post_to_messenger(psid, {"text": text})


def send_carousel(psid: str, products: list[dict]) -> bool:
    """Send a Facebook Generic Template carousel.

    Prices are formatted from the integer in products.json back into a
    ₱-prefixed display string. All fields come directly from the JSON.

    Args:
        psid:     Recipient's Facebook Page-Scoped User ID.
        products: Product dicts (max 10; extras silently dropped).

    Returns:
        ``True`` on success.
    """
    if not products:
        return send_text(psid, "Naku, pasensya na po... hindi ko po mahanap yan sa catalog namin")

    elements = []
    for p in products[:10]:
        try:
            raw_price     = str(p.get("price", 0)).replace("₱", "").replace(",", "").strip()
            price_display = f"₱{int(float(raw_price)):,}"
        except (ValueError, TypeError):
            price_display = "Contact us for price"
        _avail      = p.get("availability", "")
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
    """Toggle the typing bubble for a conversation.

    Args:
        psid: Recipient's Facebook Page-Scoped User ID.
        on:   ``True`` to show, ``False`` to hide.
    """
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
    """Send a single-product image card followed by a plain-text detail block.

    All data — name, price, availability, description — comes from the JSON.
    Nothing is inferred or generated by the LLM.

    Args:
        psid:       Recipient's Facebook Page-Scoped User ID.
        product:    The matched product dict.
        first_name: Customer's first name.
    """
    send_carousel(psid, [product])
    try:
        raw_price     = str(product.get("price", 0)).replace("₱", "").replace(",", "").strip()
        price_display = f"₱{int(float(raw_price)):,}"
    except (ValueError, TypeError):
        price_display = "Contact us for price"
    _avail      = product.get("availability", "")
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
    """Send a Gmail handover alert via smtplib.

    Rate-limited to prevent inbox spam from spoofed webhook floods.

    Args:
        psid:    Sender's PSID.
        message: Message that triggered the handover.
        profile: User profile dict (first_name / last_name).

    Returns:
        ``True`` if the email was sent.
    """
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


def _handle_message(psid: str, raw_text: str) -> None:
    """Route one incoming customer message through the hybrid engine.

    Processing order (strict — do not reorder):
      1. First-time greeting + default carousel  (rule-based, fires once per user)
      2. Admin handover keywords                 (rule-based, pause this thread)
      3. Product SKU / keyword / price search    (rule-based, JSON = source of truth)
      4. Gemini conversational fallback          (LLM, only when 1-3 don't apply)

    Args:
        psid:     Sender's Facebook Page-Scoped User ID.
        raw_text: Unmodified message text from the Messenger event.
    """
    _send_typing(psid, True)

    try:
        profile    = _get_user_profile(psid)
        first_name = profile.get("first_name", "Customer")

        # Normalise once here — all rule checks below use this.
        text = raw_text[:MAX_INPUT_CHARS].strip().lower()

        # ── Step 1: First-time greeting + default carousel ───────────────────
        if _is_first_time(psid):
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
            # Return here so the triggering message (often "hi") isn't also
            # processed through the product engine on the very first interaction.
            return

        # ── Step 2: Admin handover ────────────────────────────────────────────
        if any(kw in text for kw in HANDOVER_KEYWORDS):
            send_text(psid, (
                "We are really sorry for the inconvenience po.\n"
                "I-a-alert ko na po si admin para matulungan po kayo agad."
            ))
            _notify_admin(psid, raw_text, profile)
            _set_paused(psid, True)
            return

        # Explicit catalog browse trigger
        if any(kw in text for kw in ("products", "product", "catalog", "catalogue", "browse", "listahan")):
            default_products = _build_default_carousel()
            if default_products:
                send_text(psid, f"Here are some of our items po, {first_name}:")
                send_carousel(psid, default_products)
            else:
                send_text(psid, "Naku, pasensya na po... wala pang products sa catalog.")
            return

        # ── Step 3: Product keyword / SKU / price search ─────────────────────
        matches = _search_products(text)
        if matches:
            if len(matches) == 1:
                # Direct hit — show image card + exact price from JSON, no LLM.
                _send_product_detail(psid, matches[0], first_name)
            else:
                send_text(psid, (
                    f"Nahanap ko po ang {len(matches)} product(s) para sa inyo, "
                    f"{first_name}!"
                ))
                send_carousel(psid, matches)
            return

        # ── Step 4: Gemini conversational fallback ────────────────────────────
        # Only reaches here if no rule matched — greetings, sizing questions,
        # shipping queries, general brand conversation.
        send_text(psid, _get_gemini_reply(raw_text, first_name))

    except Exception:
        logger.exception("Unhandled error in _handle_message for %s.", psid[:20])
        send_text(psid, "Sorry po, may technical issue kami. Please try again.")
    finally:
        _send_typing(psid, False)


def _get_gemini_reply(user_message: str, first_name: str) -> str:
    """Generate a Gemini response for messages that didn't match any rule.

    Gemini handles conversational queries — greetings, sizing, shipping, etc.
    The system instruction explicitly forbids it from stating prices or inventing
    product details; all product data must come from the rule engine.

    Args:
        user_message: Raw customer message text.
        first_name:   Customer's first name for the prompt.

    Returns:
        AI-generated reply string, or a safe Taglish fallback on error.
    """
    try:
        model  = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=_GEMINI_SYSTEM_INSTRUCTION,
        )
        prompt = f"Customer ({first_name}): {user_message}\nSofia:"
        return model.generate_content(prompt).text.strip()
    except Exception as exc:
        logger.error("Gemini error: %s", exc)
        return (
            f"Pasensya na po, {first_name}, may konting issue kami."
            f"Subukan po ulit mamaya o mag-type ng 'products' para makita ang default carousel."
        )


def _handle_admin_echo(event: dict) -> None:
    """Process echo events fired when the Page sends a message from Inbox.

    In an echo event ``sender.id`` is the Page ID, so state is tracked by
    ``recipient.id`` (the customer PSID). This handler must be called before
    ``_handle_message`` — without this intercept, the bot's own outgoing
    messages loop back as echo events and re-enter the pipeline infinitely.

    'bot' or 'sofia' typed by the admin resumes Sofia and confirms to the customer.

    Args:
        event: Raw messaging event dict from the webhook payload.
    """
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
        # Any other admin message keeps the thread paused.
        _set_paused(psid, True)
        logger.info("Admin replied to %s — bot remains paused.", psid[:20])


def _handle_postback(psid: str, raw_payload: str) -> None:
    """Route a postback event from a carousel 'View Details' button.

    Args:
        psid:        Sender's Facebook Page-Scoped User ID.
        raw_payload: Raw payload string from the postback event.
    """
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
                send_text(
                    psid,
                    f"Sorry po {first_name}, hindi ko mahanap ang product na 'yan.",
                )
        else:
            logger.warning("Unknown postback action: %s", data.get("action"))

    except json.JSONDecodeError:
        # Persistent-menu buttons send plain strings, not JSON — expected.
        logger.info("Plain-string postback: %s", raw_payload[:60])
    except Exception as exc:
        logger.error("_handle_postback error: %s", exc)


# ============================================================================
# SECTION 6 — Flask Routes
# ============================================================================


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Handle the one-time Meta webhook verification handshake.

    Returns:
        The ``hub.challenge`` value with HTTP 200 on success, 403 on mismatch.
    """
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
    """Receive and route all incoming Messenger events.

    Security gates (in order):
      1. HMAC-SHA256 — rejects anything not signed by Facebook.
      2. object == 'page' — ignores non-page subscriptions.
      3. is_echo intercept — admin outgoing messages go to _handle_admin_echo(),
         never to _handle_message(). This breaks the infinite-loop.
      4. Deduplication — drops Facebook retry deliveries.
      5. Paused guard — drops customer messages while admin has the thread.

    Always returns 200 to Facebook for valid signed payloads. Non-200 causes
    Facebook to retry delivery, which amplifies any loop problem.

    Returns:
        ``"OK"`` with HTTP 200 on success.
    """
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

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            psid = event.get("sender", {}).get("id")
            if not psid:
                continue

            if "message" in event:
                msg = event["message"]

                # Echo = the Page sent this from Inbox. Must not re-enter the pipeline.
                if msg.get("is_echo"):
                    _handle_admin_echo(event)
                    continue

                text = msg.get("text", "").strip()
                mid  = msg.get("mid", "")

                if not text or not mid:
                    continue
                if _is_duplicate(mid):
                    logger.info("Duplicate mid dropped: %s", mid)
                    continue
                if _is_paused(psid):
                    logger.info("Bot paused for %s — message ignored.", psid[:20])
                    continue

                _handle_message(psid, text)

            elif "postback" in event:
                payload = event["postback"].get("payload", "")
                if payload:
                    _handle_postback(psid, payload)

    return "OK", 200


@app.route("/health", methods=["GET"])
def health_check():
    """Return bot health metrics to authorised callers.

    Requires ``Authorization: Bearer <HEALTH_TOKEN>``. Returns HTTP 404
    (not 401) on bad credentials — avoids confirming the endpoint exists
    to unauthenticated scanners.

    Returns:
        JSON health payload with HTTP 200, or HTTP 404 on auth failure.
    """
    if not HEALTH_TOKEN or request.headers.get("Authorization") != f"Bearer {HEALTH_TOKEN}":
        abort(404)

    age = None
    if _cache_updated_at:
        age = round((datetime.now() - _cache_updated_at).total_seconds())

    return jsonify({
        "status":          "healthy",
        "products_cached": len(_get_products()),
        "cache_age_secs":  age,
        "active_sessions": len(_user_sessions),
        "timestamp":       datetime.now().isoformat(),
    }), 200


@app.route("/", methods=["GET"])
def index():
    """Liveness probe for Render's default health check."""
    return jsonify({"bot": "Ace Bot", "version": "Final Deployment"}), 200


# ============================================================================
# SECTION 7 — Startup
# ============================================================================


def _validate_env() -> None:
    """Assert all required secrets are present and config is valid.

    Raises:
        EnvironmentError: On any missing critical secret or invalid URL pattern.
    """
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
    }
    missing_opt = [k for k, v in optional.items() if not v]
    if missing_opt:
        logger.warning(
            "Optional config missing (admin email / health check disabled): %s",
            ", ".join(missing_opt),
        )
    logger.info("Environment validated.")


def _startup() -> None:
    """Initialise Gemini, the product cache, and the background refresh scheduler.

    Separated from ``_validate_env`` so unit tests can call the latter without
    spinning up a real scheduler or making network calls.
    """
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini configured (model: %s).", GEMINI_MODEL)

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _refresh_cache, "interval", minutes=CACHE_REFRESH_MINS,
        id="refresh_cache", replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — cache refresh every %d min.", CACHE_REFRESH_MINS)

    _refresh_cache()  # populate immediately on boot, don't wait 60 min


if __name__ == "__main__":
    _validate_env()
    _startup()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)

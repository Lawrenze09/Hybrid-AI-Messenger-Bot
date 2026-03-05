"""Ace Bot Test — Rule-Based Messenger Bot (v4.0).

Receives Facebook Messenger webhooks and routes every message through a
strict rule engine backed by a GitHub-hosted JSON catalogue. No LLM is
used — all prices, SKUs, and availability come directly from the JSON
to prevent hallucination.

Pipeline order:
  1. HMAC-SHA256 signature check  (preserved from v3)
  2. Echo filter                  (new — kills infinite-loop spam)
  3. Admin session guard          (preserved from v3)
  4. Handover keyword scan        (preserved from v3)
  5. 'products' list trigger      (new)
  6. SKU / keyword rule engine    (fixed — containment match, not equality)
  7. Fixed fallback               (new — replaces Gemini)

Deployment target: Render (https://ace-apparel-bot-test.onrender.com)
Python: 3.11+  |  See requirements.txt for dependencies.
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
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from threading import Lock
from typing import Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, jsonify, request

__all__: list[str] = []

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Secret loading
# ---------------------------------------------------------------------------


def _read_secret(filename: str, env_key: str) -> Optional[str]:
    """Read a secret from a Render Secret File, falling back to an env var.

    Render mounts Secret Files at ``/etc/secrets/<filename>``. Prefer those
    over env vars for tokens that must not appear in process listings or logs.

    Args:
        filename: Basename of the file under ``/etc/secrets/``.
        env_key:  Environment variable name used as a local-dev fallback.

    Returns:
        The secret value, or ``None`` if neither source is available.
    """
    try:
        path = f"/etc/secrets/{filename}"
        with open(path) as f:
            value = f.read().strip()
            if value:
                return value
    except FileNotFoundError:
        pass
    return os.environ.get(env_key)


# Critical tokens loaded from Render Secret Files; everything else from env vars.
FB_APP_SECRET     = _read_secret("FB_APP_SECRET", "FB_APP_SECRET")
PAGE_ACCESS_TOKEN = _read_secret("PAGE_ACCESS_TOKEN", "PAGE_ACCESS_TOKEN")

VERIFY_TOKEN        = os.environ.get("VERIFY_TOKEN")
SENDER_EMAIL        = os.environ.get("SENDER_EMAIL")
SENDER_PASSWORD     = os.environ.get("SENDER_PASSWORD")
RECEIVER_EMAIL      = os.environ.get("RECEIVER_EMAIL")
GITHUB_PRODUCTS_URL = os.environ.get("GITHUB_PRODUCTS_URL")
HEALTH_TOKEN        = os.environ.get("HEALTH_TOKEN")

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

GRAPH_API_VERSION  = "v22.0"
WEBHOOK_URL        = "https://ace-apparel-bot-test.onrender.com/webhook"

# SMTP — override via env vars if you ever move off Gmail.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

# How often to re-fetch products.json from GitHub.
CACHE_REFRESH_MINS = int(os.environ.get("CACHE_REFRESH_MINS", "60"))

# Hard input caps applied before any processing.
MAX_INPUT_CHARS   = 500
MAX_PAYLOAD_CHARS = 1_000

# Admin-email sliding-window rate limit per sender.
EMAIL_RATE_LIMIT  = 2
EMAIL_WINDOW_SECS = 300

# GITHUB_PRODUCTS_URL must point to a raw GitHub URL to prevent a misconfigured
# env var from inadvertently fetching arbitrary remote content.
_GITHUB_RAW_PATTERN = re.compile(
    r"^https://raw\.githubusercontent\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
)

# Shown when no product or keyword matches. Deterministic — no LLM involved.
_NO_MATCH_REPLY = (
    'Hindi ko mahanap ang item na iyan. '
    'I-type po ang "products" para makita ang listahan. 😊'
)

# Trigger word that dumps the full catalogue as plain text.
_LIST_TRIGGER = "products"

HANDOVER_KEYWORDS = frozenset({
    "refund", "complaint", "complain", "admin", "manager", "supervisor",
    "problema", "issue", "reklamo", "balik", "return", "problem", "cancel",
})

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB — rejected before our code runs

# ---------------------------------------------------------------------------
# Sanitization helpers
# ---------------------------------------------------------------------------

_NEWLINE_RE      = re.compile(r"[\r\n\t]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _safe_log(text: str, max_len: int = 200) -> str:
    """Sanitize user input before it reaches the logger.

    Newlines in log messages let an attacker inject fake log entries, making
    incident response significantly harder on shared log platforms.

    Args:
        text:    Raw user-supplied string.
        max_len: Maximum characters to retain after sanitization.

    Returns:
        Sanitized string safe to pass to any logging call.
    """
    return _NEWLINE_RE.sub(" ", str(text))[:max_len]


def _safe_email_body(text: str) -> str:
    """Strip control characters from text destined for an email body.

    Args:
        text: Raw user-supplied string.

    Returns:
        String with control characters removed.
    """
    return _CONTROL_CHAR_RE.sub("", _NEWLINE_RE.sub(" ", str(text)))


def _sanitize_product(product: dict) -> dict:
    """Clamp and strip control chars from every string field in a product record.

    Args:
        product: Raw product dict from the GitHub JSON feed.

    Returns:
        Cleaned copy of the dict with string values capped at 500 chars.
    """
    _clean = lambda v: _CONTROL_CHAR_RE.sub("", str(v))[:500]  # noqa: E731
    return {k: (_clean(v) if isinstance(v, str) else v) for k, v in product.items()}

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------


class BotState:
    """Thread-safe store for the product cache, dedup set, and user sessions.

    All mutable state lives here behind a single ``threading.Lock``. This is
    appropriate for a single-process Render deployment. If you ever scale to
    multiple workers, swap the in-memory dicts for a Redis-backed store and
    replace ``_within_limit`` with a distributed sliding-window counter.

    Attributes:
        products_cache:     Sanitized list of product dicts from GitHub.
        cache_last_updated: UTC datetime of the most recent successful fetch.
    """

    def __init__(self) -> None:
        """Initialise all state buckets with empty defaults."""
        self._lock                = Lock()
        self.products_cache:       list[dict] = []
        self.cache_last_updated:   Optional[datetime] = None
        self._processed_messages:  dict[str, float] = {}
        self._user_context:        dict[str, dict] = {}
        self._email_calls:         dict[str, list[float]] = {}

    # -- Product cache -------------------------------------------------------

    def update_cache(self, products: list[dict]) -> None:
        """Replace the product cache with a freshly fetched, sanitized list.

        Args:
            products: Raw product list from the GitHub JSON feed.
        """
        cleaned = [_sanitize_product(p) for p in products]
        with self._lock:
            self.products_cache     = cleaned
            self.cache_last_updated = datetime.now()
        logger.info("Cache refreshed — %d products loaded.", len(cleaned))

    def get_products(self) -> list[dict]:
        """Return a snapshot of the current product cache.

        Returns:
            Shallow copy of the cached product list.
        """
        with self._lock:
            return self.products_cache.copy()

    # -- Message deduplication -----------------------------------------------

    def is_duplicate(self, message_id: str) -> bool:
        """Return True if this message ID was already processed within the last hour.

        Facebook retries failed webhook deliveries. Without dedup, a transient
        downstream error causes the same message to be processed twice.

        Args:
            message_id: The ``mid`` field from the Messenger event.

        Returns:
            ``True`` if already processed, ``False`` and registered otherwise.
        """
        with self._lock:
            cutoff = time.time() - 3600
            self._processed_messages = {
                k: v for k, v in self._processed_messages.items() if v > cutoff
            }
            if message_id in self._processed_messages:
                return True
            self._processed_messages[message_id] = time.time()
            return False

    # -- User session --------------------------------------------------------

    def get_context(self, psid: str) -> dict:
        """Retrieve the current session context for a user.

        Args:
            psid: Facebook Page-Scoped User ID.

        Returns:
            Context dict (empty dict if no context exists yet).
        """
        with self._lock:
            return self._user_context.get(psid, {})

    def set_context(self, psid: str, ctx: dict) -> None:
        """Overwrite the session context for a user.

        Args:
            psid: Facebook Page-Scoped User ID.
            ctx:  New context dict to store.
        """
        with self._lock:
            self._user_context[psid] = ctx

    # -- Rate limiting -------------------------------------------------------

    def _within_limit(
        self, bucket: dict[str, list[float]], psid: str, limit: int, window: int
    ) -> bool:
        """Sliding-window rate check. Caller must hold ``self._lock``.

        Args:
            bucket: The per-psid timestamp list dict to operate on.
            psid:   Facebook Page-Scoped User ID.
            limit:  Max calls allowed within ``window`` seconds.
            window: Sliding window duration in seconds.

        Returns:
            ``True`` if the call is within quota, ``False`` if throttled.
        """
        now    = time.time()
        recent = [t for t in bucket.get(psid, []) if t > now - window]
        if len(recent) >= limit:
            return False
        recent.append(now)
        bucket[psid] = recent
        return True

    def allow_email(self, psid: str) -> bool:
        """Check and record an admin-email attempt for the given user.

        Args:
            psid: Facebook Page-Scoped User ID.

        Returns:
            ``True`` if under quota, ``False`` if rate-limited.
        """
        with self._lock:
            return self._within_limit(
                self._email_calls, psid, EMAIL_RATE_LIMIT, EMAIL_WINDOW_SECS
            )


bot_state = BotState()

# ---------------------------------------------------------------------------
# Webhook signature verification  (PRESERVED — do not modify)
# ---------------------------------------------------------------------------


def _verify_signature(raw_body: bytes, header: str) -> bool:
    """Validate the X-Hub-Signature-256 header on an incoming webhook POST.

    Facebook signs every delivery with ``HMAC-SHA256(app_secret, body)`` and
    sends the digest as ``sha256=<hex>``. Using ``hmac.compare_digest`` for the
    comparison prevents timing-oracle attacks that could otherwise allow an
    attacker to brute-force the App Secret one byte at a time.

    Args:
        raw_body: Raw request bytes (must be read before Flask parses JSON).
        header:   Value of the ``X-Hub-Signature-256`` request header.

    Returns:
        ``True`` if the signature is valid, ``False`` otherwise.
    """
    if not FB_APP_SECRET:
        logger.error("FB_APP_SECRET is not set — rejecting all incoming webhook traffic.")
        return False
    if not header or not header.startswith("sha256="):
        return False

    computed = hmac.new(
        FB_APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, header[7:])

# ---------------------------------------------------------------------------
# Product cache — GitHub fetch + APScheduler  (PRESERVED — do not modify)
# ---------------------------------------------------------------------------


def _fetch_products() -> None:
    """Pull ``products.json`` from GitHub and refresh the in-memory cache.

    Logs errors and returns silently on failure so the scheduler keeps running
    on the next interval rather than crashing the worker thread.
    """
    try:
        resp = requests.get(GITHUB_PRODUCTS_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            logger.error("products.json root is not a JSON array — skipping update.")
            return
        bot_state.update_cache(data)
    except requests.RequestException as exc:
        logger.error("Product fetch failed: %s", exc)
    except Exception:
        logger.exception("Unexpected error while fetching products.")

# ---------------------------------------------------------------------------
# Meta Graph API helpers
# ---------------------------------------------------------------------------


def _get_user_profile(psid: str) -> dict:
    """Fetch first and last name from the Graph API for a given PSID.

    Args:
        psid: Facebook Page-Scoped User ID.

    Returns:
        Profile dict with at least ``{"first_name": str}``. Falls back to
        ``{"first_name": "Customer"}`` on any network or API error.
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
        logger.error("get_user_profile(%s): %s", _safe_log(psid), exc)
        return {"first_name": "Customer"}


def _send_message(psid: str, message_data: dict) -> bool:
    """POST a message payload to the Messenger Send API.

    ``messaging_type="RESPONSE"`` is required for Generic Template carousels on
    Graph API v12+. Plain text has a legacy fallback; templates do not — omitting
    it causes carousels to silently fail while plain text still goes through.

    Args:
        psid:         Recipient's Facebook Page-Scoped User ID.
        message_data: Messenger message object (text or attachment payload).

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
            # Facebook sometimes returns HTTP 200 with an error payload inside.
            logger.error(
                "Graph API error [code %s]: %s",
                body["error"].get("code"),
                body["error"].get("message"),
            )
            return False
        raw.raise_for_status()
        return True
    except Exception as exc:
        tail = raw.text[:300] if raw is not None else "no response"
        logger.error("_send_message failed: %s | %s", exc, tail)
        return False


def _send_typing(psid: str, on: bool = True) -> None:
    """Toggle the typing indicator for a conversation.

    Args:
        psid: Recipient's Facebook Page-Scoped User ID.
        on:   ``True`` to show the indicator, ``False`` to hide it.
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

# ---------------------------------------------------------------------------
# Rule engine — product matching  (FIXED)
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Lowercase and strip whitespace for consistent comparisons."""
    return text.lower().strip()


def _find_products(user_message: str) -> list[dict]:
    """Match a customer message against the product catalogue.

    Two-pass strategy:

    Pass 1 — SKU / name containment check.
        Checks if a product ID or name appears *within* the message, not
        whether the message *equals* the ID. The old equality check
        (``msg == product_id``) missed every sentence containing an SKU,
        e.g. "magkano po ang ACE-OVT-001", and fell through to Gemini which
        then hallucinated the price. This is the fix for that bug.

    Pass 2 — keyword / category scan.
        Collects up to 10 products whose keyword list overlaps with the
        message (10 is the Facebook Generic Template carousel hard limit).

    Args:
        user_message: Normalised (lowercased, stripped) text from the customer.

    Returns:
        List of matching product dicts. Empty list if nothing matches.
    """
    msg      = _normalise(user_message)
    products = bot_state.get_products()

    # Pass 1 — if the message contains a product ID or name, return that product.
    for p in products:
        pid   = _normalise(p.get("id", ""))
        pname = _normalise(p.get("name", ""))
        if (pid and pid in msg) or (pname and pname in msg):
            logger.info("SKU/name match: '%s' in '%s'", p.get("id"), _safe_log(msg))
            return [p]

    # Pass 2 — keyword scan across the whole catalogue.
    seen:    set[str]   = set()
    results: list[dict] = []
    for p in products:
        keywords = [_normalise(k) for k in p.get("keywords", [])]
        if any(k in msg for k in keywords) and p["id"] not in seen:
            seen.add(p["id"])
            results.append(p)
            if len(results) == 10:
                break

    logger.info("Keyword scan: %d hit(s) for '%s'", len(results), _safe_log(msg))
    return results


def _build_carousel(products: list[dict]) -> dict:
    """Build a Facebook Generic Template payload from a list of products.

    Every field — title, price, image, availability — is read directly from
    ``products.json``. Nothing is inferred or generated.

    ``image_url`` must be a stable, publicly reachable HTTPS URL. Facebook
    fetches it server-side; ``fbcdn.net`` URLs are user-scoped and expire.

    Args:
        products: List of matched product dicts (max 10 used).

    Returns:
        Messenger attachment payload for a Generic Template carousel.
    """
    elements = [
        {
            "title":     p.get("name", "Ace Product")[:80],
            "image_url": p.get("image_url", "https://via.placeholder.com/500x500.png"),
            "subtitle":  (
                f"{p.get('price', '₱0')}  •  "
                f"{'In Stock' if p.get('availability') == 'In Stock' else 'Out of Stock'}"
            )[:80],
            "buttons": [{
                "type":    "postback",
                "title":   "View Details",
                "payload": json.dumps({
                    "action":     "view_price",
                    "product_id": p.get("id", ""),
                }),
            }],
        }
        for p in products[:10]
    ]
    return {
        "attachment": {
            "type":    "template",
            "payload": {"template_type": "generic", "elements": elements},
        }
    }


def _send_product_detail(product: dict, psid: str, first_name: str) -> None:
    """Send a single-product detail card as a Generic Template.

    Used when there is exactly one match (direct SKU hit). All data — name,
    price, image, availability — comes from the JSON record, not the LLM.

    Args:
        product:    The matched product dict from the cache.
        psid:       Recipient's Facebook Page-Scoped User ID.
        first_name: Customer's first name for the personalised message.
    """
    availability = product.get("availability", "Unknown")
    stock_label  = "In Stock" if availability == "In Stock" else "Out of Stock"

    # Send as a Generic Template card so it includes the image.
    carousel = _build_carousel([product])
    _send_message(psid, carousel)

    # Follow up with a plain-text detail message for price and availability.
    _send_message(psid, {
        "text": (
            f"{product.get('name')}\n"
            f"Price:        {product.get('price', 'Contact us')}\n"
            f"Availability: {stock_label}\n"
            f"SKU:          {product.get('id', 'N/A')}\n\n"
            f"Interested po, {first_name}? Mag-message lang!"
        )
    })


def _build_product_list_text() -> str:
    """Build a plain-text product listing from the entire cache.

    Triggered when the customer types 'products'. Returns a compact catalogue
    so the customer can browse and pick a SKU to query.

    Returns:
        Formatted string listing all cached products with price and stock status.
    """
    products = bot_state.get_products()
    if not products:
        return "Wala pa po kaming products sa ngayon. Subukan po ulit mamaya."

    lines = ["📦 Ace Apparel Products:\n"]
    for p in products:
        stock = "Available" if p.get("availability") == "In Stock" else "Unavailable"
        lines.append(
            f"{stock} {p.get('name')} — {p.get('price', '₱0')}  [{p.get('id')}]"
        )
    lines.append('\nI-type po ang SKU (hal. "ACE-OVT-001") para sa full details.')
    return "\n".join(lines)


def _handle_view_price(product_id: str, psid: str, first_name: str) -> None:
    """Send detail for a product when the carousel 'View Details' button is tapped.

    All fields read directly from the JSON cache. Price is never inferred.

    Args:
        product_id: SKU from the postback payload.
        psid:       Recipient's Facebook Page-Scoped User ID.
        first_name: Customer's first name for the personalised reply.
    """
    product = next(
        (p for p in bot_state.get_products() if str(p.get("id")) == str(product_id)),
        None,
    )
    if not product:
        _send_message(psid, {
            "text": f"Sorry po {first_name}, hindi ko po mahanap ang product na 'yan."
        })
        return

    _send_product_detail(product, psid, first_name)

# ---------------------------------------------------------------------------
# Admin handover
# ---------------------------------------------------------------------------


def _notify_admin(psid: str, user_message: str, profile: dict) -> bool:
    """Send a Gmail alert when a handover keyword is detected.

    Rate-limited to 2 alerts per PSID per 5 minutes. Without this, a spoofed
    webhook flood would spam the admin inbox and trigger Google's abuse filters,
    disabling the sender account and killing all future legitimate alerts.

    Args:
        psid:         Sender's Facebook Page-Scoped User ID.
        user_message: Message that triggered the handover.
        profile:      User profile dict with first_name / last_name.

    Returns:
        ``True`` if the email was sent, ``False`` on rate-limit or error.
    """
    if not bot_state.allow_email(psid):
        logger.warning("Email rate limit hit for %s — alert suppressed.", _safe_log(psid))
        return False

    name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    body = (
        f"Handover triggered.\n\n"
        f"Customer: {name}\n"
        f"PSID:     {psid}\n"
        f"Message:  {_safe_email_body(user_message)}\n"
        f"Time:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Reply in Facebook Page Inbox.\n"
        f"To return control to Sofia, type 'bot' or 'sofia' in the inbox thread."
    )
    msg            = MIMEMultipart()
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECEIVER_EMAIL
    msg["Subject"] = f"Ace Bot — Handover: {profile.get('first_name', 'Customer')}"
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        logger.info("Handover email sent for %s.", _safe_log(psid))
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False


def _trigger_handover(psid: str, message: str, first_name: str, profile: dict) -> None:
    """Pause Sofia, alert the admin, and confirm to the customer.

    Args:
        psid:       Sender's Facebook Page-Scoped User ID.
        message:    Message that triggered the handover.
        first_name: Customer's first name for the reply.
        profile:    User profile dict passed to the email alert.
    """
    _notify_admin(psid, message, profile)
    _send_message(psid, {
        "text": (
            f"Wait lang po {first_name}, ia-alert ko na po ang aming admin para "
            f"ma-assist kayo agad sa concern niyo.\n\n"
            f"Pasensya na po sa abala! Ace Team will be with you shortly."
        )
    })
    ctx = bot_state.get_context(psid)
    bot_state.set_context(psid, {
        **ctx,
        "status":    "awaiting_admin",
        "timestamp": datetime.now().isoformat(),
    })


def _handle_admin_echo(event: dict) -> None:
    """Process echo events fired when the Page sends a message from Inbox.

    In an echo event ``sender.id`` is the Page ID, not the customer, so state
    is tracked by ``recipient.id`` (the customer PSID). This handler must be
    called before ``_process_message``; otherwise echo events would call the
    Graph API with the Page's own PSID as the sender — the root cause of the
    infinite-loop bug this version fixes.

    Any admin message pauses Sofia. Typing 'bot' or 'sofia' resumes her and
    sends the customer a return greeting.

    Args:
        event: Raw messaging event dict from the webhook payload.
    """
    psid = event.get("recipient", {}).get("id")
    text = event.get("message", {}).get("text", "")

    if not psid:
        logger.warning("Echo event missing recipient.id — skipping.")
        return

    ctx = bot_state.get_context(psid)

    if text.strip().lower() in ("bot", "sofia"):
        bot_state.set_context(psid, {**ctx, "status": "chatting"})
        _send_message(psid, {"text": "Bumalik na po ako! 🙋‍♀️ Paano ko pa po kayo matutulungan?"})
        logger.info("Sofia resumed for user %s.", _safe_log(psid))
    else:
        bot_state.set_context(psid, {
            **ctx,
            "status":    "awaiting_admin",
            "timestamp": datetime.now().isoformat(),
        })
        logger.info("Admin replied to %s → Sofia paused.", _safe_log(psid))

# ---------------------------------------------------------------------------
# Main message pipeline  (FIXED)
# ---------------------------------------------------------------------------


def _process_message(psid: str, message_text: str, message_id: str) -> None:
    """Route an incoming customer message through the rule engine.

    Changes from v3:
    - Greeting removed. Bot only responds when it has something useful to say.
    - Gemini removed. No LLM is called at any point in this pipeline.
    - SKU match fixed: containment check (id IN message) replaces broken
      equality check (message == id) that caused price hallucinations.
    - 'products' trigger added for full catalogue browse.
    - Fixed Taglish fallback replaces the Gemini fallback.

    Pipeline order:
      1. Dedup — drop Facebook retry deliveries silently.
      2. Admin guard — stay silent if admin has the thread.
      3. Handover scan — alert admin and pause on sensitive keywords.
      4. 'products' trigger — send full product list as plain text.
      5. Rule engine — SKU/name hit → detail card; keyword hit → carousel.
      6. Fixed fallback — no match → deterministic reply, zero hallucination risk.

    Args:
        psid:         Sender's Facebook Page-Scoped User ID.
        message_text: Raw text from the Messenger event (already stripped by caller).
        message_id:   Unique ``mid`` for deduplication.
    """
    if bot_state.is_duplicate(message_id):
        return

    # Normalise once here; all downstream checks use this cleaned version.
    message_text = message_text[:MAX_INPUT_CHARS]
    msg_lower    = message_text.strip().lower()

    _send_typing(psid, True)

    try:
        profile    = _get_user_profile(psid)
        first_name = profile.get("first_name", "Customer")

        # Admin session active — Sofia stays completely silent.
        if bot_state.get_context(psid).get("status") == "awaiting_admin":
            logger.info("Admin session active for %s — Sofia silent.", _safe_log(psid))
            return

        # Handover keywords — pause this thread and notify the admin.
        if any(kw in msg_lower for kw in HANDOVER_KEYWORDS):
            _trigger_handover(psid, message_text, first_name, profile)
            return

        # 'products' trigger — dump the full catalogue so the customer can browse.
        if _LIST_TRIGGER in msg_lower:
            _send_message(psid, {"text": _build_product_list_text()})
            return

        # Rule engine — SKU / name / keyword match against the JSON cache.
        matches = _find_products(message_text)
        if matches:
            if len(matches) == 1:
                # Single hit: show image card + detail text with exact price from JSON.
                _send_product_detail(matches[0], psid, first_name)
            else:
                # Multiple hits: carousel so the customer can browse the category.
                _send_message(psid, {
                    "text": (
                        f"Nahanap ko po ang {len(matches)} products para sa inyo, "
                        f"{first_name}!"
                    )
                })
                _send_message(psid, _build_carousel(matches))
            return

        # No match — fixed reply. No LLM, no hallucination risk.
        _send_message(psid, {"text": _NO_MATCH_REPLY})

    except Exception:
        logger.exception("Unhandled error in _process_message for %s.", _safe_log(psid))
        _send_message(psid, {"text": "Sorry po, may technical issue kami. Please try again. 🙏"})
    finally:
        _send_typing(psid, False)


def _process_postback(psid: str, raw_payload: str) -> None:
    """Route a postback event from a carousel button or persistent menu.

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
            _handle_view_price(data.get("product_id", ""), psid, first_name)
        else:
            logger.warning("Unknown postback action: %s", _safe_log(str(data.get("action"))))
    except json.JSONDecodeError:
        # Persistent-menu buttons send plain strings, not JSON — expected.
        logger.info("Plain-string postback received: %s", _safe_log(raw_payload[:60]))
    except Exception as exc:
        logger.error("_process_postback error: %s", exc)

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Handle the one-time Meta webhook verification handshake.

    Returns:
        The ``hub.challenge`` value with HTTP 200 on success, or
        ``"Forbidden"`` with HTTP 403 on token mismatch.
    """
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        logger.info("Webhook verified successfully.")
        return request.args.get("hub.challenge", ""), 200
    logger.warning("Webhook verification failed — token mismatch.")
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    """Receive and route all incoming Messenger events.

    Security gates (in order):
      1. HMAC-SHA256 — rejects anything not signed by Facebook.
      2. object == 'page' — ignores non-page subscriptions silently.
      3. is_echo intercept — routes admin messages to _handle_admin_echo()
         and skips _process_message() entirely. This is the fix for the
         infinite-loop bug: without this gate, the bot's own outgoing messages
         loop back as echo events and re-enter the pipeline.

    Always returns 200 to Facebook for valid signed payloads. Returning any
    non-200 causes Facebook to retry delivery, which amplifies the loop.

    Returns:
        ``"OK"`` with HTTP 200 on success, error status otherwise.
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

                # Echo = the Page sent this from Inbox. Route to the admin handler,
                # never to _process_message(). This breaks the infinite-loop.
                if msg.get("is_echo"):
                    _handle_admin_echo(event)
                    continue

                text = msg.get("text", "").strip()
                mid  = msg.get("mid", "")
                if text and mid:
                    _process_message(psid, text, mid)

            elif "postback" in event:
                payload = event["postback"].get("payload", "")
                if payload:
                    _process_postback(psid, payload)

    return "OK", 200


@app.route("/health", methods=["GET"])
def health_check():
    """Return bot health metrics to authorised callers.

    Requires ``Authorization: Bearer <HEALTH_TOKEN>``. Returns HTTP 404
    (not 401) on bad/missing credentials — avoids confirming the endpoint
    exists to an unauthenticated scanner.

    Returns:
        JSON health payload with HTTP 200, or HTTP 404 on auth failure.
    """
    if not HEALTH_TOKEN or request.headers.get("Authorization") != f"Bearer {HEALTH_TOKEN}":
        abort(404)

    age = None
    if bot_state.cache_last_updated:
        age = round((datetime.now() - bot_state.cache_last_updated).total_seconds())

    return jsonify({
        "status":          "healthy",
        "products_cached": len(bot_state.get_products()),
        "cache_age_secs":  age,
        "timestamp":       datetime.now().isoformat(),
    }), 200


@app.route("/", methods=["GET"])
def index():
    """Liveness probe for Render's default health check.

    Returns:
        JSON with bot name and version, HTTP 200.
    """
    return jsonify({"bot": "Ace Bot Test", "version": "4.0"}), 200

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _validate_env() -> None:
    """Assert all required secrets are present and config values are valid.

    Fails fast at startup rather than surfacing a missing-key error mid-request.
    GEMINI_API_KEY removed from required list — Gemini is no longer used.

    Raises:
        EnvironmentError: If any critical secret is missing, or if
            ``GITHUB_PRODUCTS_URL`` does not match the raw GitHub URL pattern.
    """
    required = {
        "PAGE_ACCESS_TOKEN":   PAGE_ACCESS_TOKEN,
        "FB_APP_SECRET":       FB_APP_SECRET,
        "VERIFY_TOKEN":        VERIFY_TOKEN,
        "GITHUB_PRODUCTS_URL": GITHUB_PRODUCTS_URL,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise EnvironmentError(f"Critical secrets missing: {missing}")

    if not _GITHUB_RAW_PATTERN.match(GITHUB_PRODUCTS_URL):
        raise EnvironmentError(
            f"GITHUB_PRODUCTS_URL does not look like a raw GitHub URL: "
            f"{GITHUB_PRODUCTS_URL!r}"
        )

    optional = {
        "HEALTH_TOKEN":    HEALTH_TOKEN,
        "SENDER_EMAIL":    SENDER_EMAIL,
        "SENDER_PASSWORD": SENDER_PASSWORD,
        "RECEIVER_EMAIL":  RECEIVER_EMAIL,
    }
    missing_optional = [k for k, v in optional.items() if not v]
    if missing_optional:
        logger.warning(
            "Optional config missing (admin email / health check disabled): %s",
            ", ".join(missing_optional),
        )

    logger.info("Environment validated.")


def _startup() -> None:
    """Initialise the product cache and start the background refresh scheduler.

    Separated from ``_validate_env`` so tests can validate config without
    spinning up a real scheduler or making network calls.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _fetch_products, "interval", minutes=CACHE_REFRESH_MINS,
        id="fetch_products", replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — product cache refresh every %d min.", CACHE_REFRESH_MINS)

    _fetch_products()


if __name__ == "__main__":
    _validate_env()
    _startup()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)

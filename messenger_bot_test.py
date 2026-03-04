"""Ace Bot Test — Hybrid AI Messenger Bot.

Receives Facebook Messenger webhooks, routes product queries through a
rule-based engine backed by a GitHub-hosted JSON catalogue, and falls back
to Gemini for open-domain conversation. Sensitive admin threads are handed
off via Gmail and a per-conversation pause/resume flag.

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

import google.generativeai as genai
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


# Tokens that must never appear in logs or child-process environments are
# loaded from Render Secret Files; everything else comes from env vars.
FB_APP_SECRET = _read_secret("FB_APP_SECRET", "FB_APP_SECRET")
PAGE_ACCESS_TOKEN = _read_secret("PAGE_ACCESS_TOKEN", "PAGE_ACCESS_TOKEN")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL")
GITHUB_PRODUCTS_URL = os.environ.get("GITHUB_PRODUCTS_URL")
HEALTH_TOKEN = os.environ.get("HEALTH_TOKEN")

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

GRAPH_API_VERSION = "v22.0"
WEBHOOK_URL = "https://ace-apparel-bot-test.onrender.com/webhook"

# Gemini model — bump here when upgrading, not scattered through the code.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

# SMTP — override via env vars if you ever move off Gmail.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

# How often to re-fetch products.json from GitHub.
CACHE_REFRESH_MINS = int(os.environ.get("CACHE_REFRESH_MINS", "60"))

# Hard input caps applied before any processing.
MAX_INPUT_CHARS = 500
MAX_PAYLOAD_CHARS = 1_000

# Gemini sliding-window rate limit per sender.
GEMINI_RATE_LIMIT = 10
GEMINI_WINDOW_SECS = 60

# Admin-email sliding-window rate limit per sender.
EMAIL_RATE_LIMIT = 2
EMAIL_WINDOW_SECS = 300

# GITHUB_PRODUCTS_URL must point to a raw GitHub URL to prevent a misconfigured
# env var from inadvertently fetching arbitrary remote content.
_GITHUB_RAW_PATTERN = re.compile(
    r"^https://raw\.githubusercontent\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
)

# Gemini system prompt. Kept at module level so it's easy to find and edit
# without digging through function bodies.
_SOFIA_SYSTEM_PROMPT = """\
You are Sofia, the friendly AI assistant for 'Ace', a premium streetwear brand.
Brand vibe: Streetwear · Minimalist · Sporty.
Materials: Heavyweight cotton, Breathable mesh, French Terry Fleece.

Rules:
- Reply in natural Taglish (Tagalog-English). Use "po" and "opo".
- Address the customer by their first name.
- Products: Oversized Tees, Mesh Shorts, Hoodies, Jerseys, Socks, Gym Sandos.
- Order / refund queries: tell the customer the admin will assist shortly.
- Keep replies under 150 characters (Messenger renders long text poorly).
- Default recommendation when asked: Mesh Shorts or Heavyweight Tees.

Customer ({first_name}): {message}
Sofia:"""

_SOFIA_INTRO = (
    "I'm Sofia, your Ace Apparel assistant. 👋\n\n"
    "Feel free to ask me anything — Oversized Tees, Mesh Shorts, "
    "Hoodies, Jerseys, Socks, and Gym Sandos. "
    "I'm here to help you find the right fit! 😊"
)

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

_NEWLINE_RE = re.compile(r"[\r\n\t]")
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
        self._lock = Lock()
        self.products_cache: list[dict] = []
        self.cache_last_updated: Optional[datetime] = None
        self._processed_messages: dict[str, float] = {}
        self._user_context: dict[str, dict] = {}
        self._gemini_calls: dict[str, list[float]] = {}
        self._email_calls: dict[str, list[float]] = {}

    # -- Product cache -------------------------------------------------------

    def update_cache(self, products: list[dict]) -> None:
        """Replace the product cache with a freshly fetched, sanitized list.

        Args:
            products: Raw product list from the GitHub JSON feed.
        """
        cleaned = [_sanitize_product(p) for p in products]
        with self._lock:
            self.products_cache = cleaned
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
        """Check whether a message ID has already been processed.

        Facebook retries failed webhook deliveries. Without dedup, a transient
        downstream error would cause the same message to be processed twice.

        Args:
            message_id: The ``mid`` field from the Messenger event.

        Returns:
            ``True`` if the ID was seen within the last hour, ``False`` and
            registered otherwise.
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

    def is_first_contact(self, psid: str) -> bool:
        """Atomically check-and-mark whether this is a user's first message.

        The check and the mark happen inside a single lock acquisition, so
        there is no window between reading and writing the greeted flag.

        Args:
            psid: Facebook Page-Scoped User ID.

        Returns:
            ``True`` exactly once per user lifetime; ``False`` on every
            subsequent call.
        """
        with self._lock:
            ctx = self._user_context.get(psid, {})
            if ctx.get("greeted"):
                return False
            self._user_context[psid] = {**ctx, "greeted": True}
            return True

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
        now = time.time()
        recent = [t for t in bucket.get(psid, []) if t > now - window]
        if len(recent) >= limit:
            return False
        recent.append(now)
        bucket[psid] = recent
        return True

    def allow_gemini(self, psid: str) -> bool:
        """Check and record a Gemini API call attempt for the given user.

        Args:
            psid: Facebook Page-Scoped User ID.

        Returns:
            ``True`` if under quota, ``False`` if rate-limited.
        """
        with self._lock:
            return self._within_limit(
                self._gemini_calls, psid, GEMINI_RATE_LIMIT, GEMINI_WINDOW_SECS
            )

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
# Webhook signature verification
# ---------------------------------------------------------------------------


def _verify_signature(raw_body: bytes, header: str) -> bool:
    """Validate the X-Hub-Signature-256 header on an incoming webhook POST.

    Facebook signs every delivery with ``HMAC-SHA256(app_secret, body)`` and
    sends the digest as ``sha256=<hex>``. Verifying this is the only reliable
    way to prove a POST originated from Facebook, since the webhook URL is
    intentionally public.

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
# Product cache — GitHub fetch + APScheduler
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

    ``messaging_type="RESPONSE"`` is required for template messages on Graph
    API v12+. Omitting it causes Generic Template carousels to silently fail
    while plain text still goes through via a legacy fallback — this was the
    root bug in v1/v2 of this project.

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
                "recipient": {"id": psid},
                "messaging_type": "RESPONSE",
                "message": message_data,
            },
            timeout=10,
        )
        body = raw.json()
        if "error" in body:
            # Facebook sometimes returns HTTP 200 with an error object inside.
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
                "recipient": {"id": psid},
                "sender_action": "typing_on" if on else "typing_off",
            },
            timeout=5,
        )
    except Exception as exc:
        logger.debug("Typing indicator error (non-critical): %s", exc)

# ---------------------------------------------------------------------------
# Product matching
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Lowercase and strip whitespace for consistent keyword comparisons."""
    return text.lower().strip()


def _find_products(user_message: str) -> list[dict]:
    """Search the product cache for matches against a user's message.

    Two-pass strategy: exact ID/name match returns immediately as a single
    result. If nothing matches exactly, a keyword/substring scan collects up
    to ten unique hits (the Facebook Generic Template carousel hard limit).

    Args:
        user_message: Raw text from the customer.

    Returns:
        List of matching product dicts, ordered by match quality.
    """
    msg = _normalise(user_message)
    products = bot_state.get_products()

    for p in products:
        if msg in (_normalise(p.get("id", "")), _normalise(p.get("name", ""))):
            return [p]

    seen: set[str] = set()
    results: list[dict] = []
    for p in products:
        keywords = [_normalise(k) for k in p.get("keywords", [])]
        hit = any(k in msg for k in keywords) or _normalise(p.get("name", "")) in msg
        if hit and p["id"] not in seen:
            seen.add(p["id"])
            results.append(p)
            if len(results) == 10:
                break

    logger.info("Product scan: %d hit(s) for '%s'", len(results), _safe_log(msg))
    return results


def _build_carousel(products: list[dict]) -> dict:
    """Build a Facebook Generic Template payload from a product list.

    ``image_url`` must be a stable, publicly reachable HTTPS URL. Facebook
    fetches it server-side during rendering; user-scoped ``fbcdn.net`` URLs
    expire and will silently fail at the card level.

    Args:
        products: List of product dicts (max 10 will be used).

    Returns:
        Messenger attachment payload for a Generic Template carousel.
    """
    elements = [
        {
            "title": p.get("name", "Ace Product")[:80],
            "image_url": p.get("image_url", "https://via.placeholder.com/500x500.png"),
            "subtitle": f"{p.get('price', '₱0')}  •  {p.get('description', '')}"[:80],
            "buttons": [{
                "type": "postback",
                "title": "View Price",
                "payload": json.dumps({"action": "view_price", "product_id": p.get("id", "")}),
            }],
        }
        for p in products[:10]
    ]
    return {
        "attachment": {
            "type": "template",
            "payload": {"template_type": "generic", "elements": elements},
        }
    }


def _handle_view_price(product_id: str, psid: str, first_name: str) -> None:
    """Respond to a 'View Price' carousel button click.

    Args:
        product_id: Product ID from the postback payload.
        psid:       Sender's Facebook Page-Scoped User ID.
        first_name: Customer's first name for the personalised reply.
    """
    product = next(
        (p for p in bot_state.get_products() if str(p.get("id")) == str(product_id)),
        None,
    )
    if not product:
        _send_message(psid, {"text": f"Sorry po {first_name}, hindi ko po mahanap ang product na 'yan."})
        return

    _send_message(psid, {
        "text": (
            f"{product.get('name')}\n\n"
            f"Price: {product.get('price', 'Contact us')}\n"
            f"Availability: {product.get('availability', 'In Stock')}\n\n"
            f"Interested po kayo, {first_name}? Just send us a message!"
        )
    })

# ---------------------------------------------------------------------------
# Gemini AI fallback
# ---------------------------------------------------------------------------


def _get_ai_response(user_message: str, first_name: str) -> str:
    """Generate a Gemini response for messages that don't match any product.

    Args:
        user_message: Customer's message text (already length-capped).
        first_name:   Customer's first name for prompt personalisation.

    Returns:
        AI-generated reply string, or a localised fallback on error.
    """
    try:
        prompt = _SOFIA_SYSTEM_PROMPT.format(first_name=first_name, message=user_message)
        model = genai.GenerativeModel(GEMINI_MODEL)
        return model.generate_content(prompt).text.strip()
    except Exception as exc:
        logger.error("Gemini error: %s", exc)
        return (
            f"Pasensya na po, {first_name}, may konting issue kami ngayon. 😅 "
            f"Message po kayo ulit mamaya o wait niyo po ang aming admin. Salamat! 🙏"
        )

# ---------------------------------------------------------------------------
# Sofia welcome
# ---------------------------------------------------------------------------


def _send_welcome_if_new(psid: str, first_name: str) -> None:
    """Send Sofia's introduction message to a first-time user.

    The ``is_first_contact`` call is atomic (single lock acquisition), so
    there is no race condition if two events for the same PSID arrive in
    quick succession.

    Args:
        psid:       Sender's Facebook Page-Scoped User ID.
        first_name: Customer's first name for personalisation.
    """
    if bot_state.is_first_contact(psid):
        _send_message(psid, {"text": f"Hi {first_name}! {_SOFIA_INTRO}"})
        logger.info("Welcome message sent to new user %s.", _safe_log(psid))

# ---------------------------------------------------------------------------
# Admin handover
# ---------------------------------------------------------------------------


def _notify_admin(psid: str, user_message: str, profile: dict) -> bool:
    """Send a Gmail alert when a handover keyword is detected.

    Rate-limited to prevent a flood of spoofed webhook events from spamming
    the admin inbox and triggering Google's abuse filters (which would disable
    the sender account and kill all future legitimate alerts).

    Args:
        psid:         Sender's Facebook Page-Scoped User ID.
        user_message: Raw customer message that triggered the handover.
        profile:      User profile dict with ``first_name`` / ``last_name``.

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
    msg = MIMEMultipart()
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
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
    """Pause Sofia, notify the admin, and confirm to the customer.

    Args:
        psid:       Sender's Facebook Page-Scoped User ID.
        message:    Raw customer message that triggered the handover.
        first_name: Customer's first name for the reply.
        profile:    User profile dict passed through to the email alert.
    """
    _notify_admin(psid, message, profile)
    _send_message(psid, {
        "text": (
            f"Wait lang po {first_name}, ia-alert ko na po ang aming admin para "
            f"ma-assist kayo agad sa concern niyo.\n\n"
            f"Pasensya na po sa abala! Ace Team will be with you shortly. 🙏"
        )
    })
    ctx = bot_state.get_context(psid)
    bot_state.set_context(psid, {
        **ctx,
        "status": "awaiting_admin",
        "timestamp": datetime.now().isoformat(),
    })


def _handle_admin_echo(event: dict) -> None:
    """Process echo events — fired when the Page sends a message from Inbox.

    In an echo event ``sender.id`` is the Page ID, not the customer, so we
    track state by ``recipient.id`` (the customer PSID). This handler must be
    called before ``_process_message``; otherwise echo events would trigger
    Graph API calls against the Page's own PSID.

    Any message from the admin pauses Sofia. The resume keyword ('bot' or
    'sofia') hands control back and sends a return greeting to the customer.

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
            "status": "awaiting_admin",
            "timestamp": datetime.now().isoformat(),
        })
        logger.info("Admin replied to %s → Sofia paused.", _safe_log(psid))

# ---------------------------------------------------------------------------
# Main message pipeline
# ---------------------------------------------------------------------------


def _process_message(psid: str, message_text: str, message_id: str) -> None:
    """Route an incoming customer message through the full response pipeline.

    Args:
        psid:         Sender's Facebook Page-Scoped User ID.
        message_text: Raw text from the Messenger event.
        message_id:   Unique ``mid`` used for deduplication.
    """
    if bot_state.is_duplicate(message_id):
        return

    message_text = message_text[:MAX_INPUT_CHARS]
    _send_typing(psid, True)

    try:
        profile = _get_user_profile(psid)
        first_name = profile.get("first_name", "Customer")

        _send_welcome_if_new(psid, first_name)

        if bot_state.get_context(psid).get("status") == "awaiting_admin":
            logger.info("Admin session active for %s — Sofia silent.", _safe_log(psid))
            return

        if any(kw in message_text.lower() for kw in HANDOVER_KEYWORDS):
            _trigger_handover(psid, message_text, first_name, profile)
            return

        matches = _find_products(message_text)
        if matches:
            _send_message(psid, {
                "text": f"Nahanap ko po ang {len(matches)} product(s) para sa inyo, {first_name}!"
            })
            _send_message(psid, _build_carousel(matches))
            return

        if not bot_state.allow_gemini(psid):
            _send_message(psid, {
                "text": (
                    f"Sandali lang po, {first_name}! 🙏 "
                    f"Medyo mabilis ang mga mensahe — try again in a minute."
                )
            })
            return

        _send_message(psid, {"text": _get_ai_response(message_text, first_name)})

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
        data = json.loads(raw_payload)
        profile = _get_user_profile(psid)
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

    Rejects unsigned payloads immediately. Non-page subscriptions and unknown
    event types are silently ignored so the endpoint always returns 200 to
    Facebook (required to prevent automatic webhook disablement).

    Returns:
        ``"OK"`` with HTTP 200 on success, or an error status otherwise.
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
                if msg.get("is_echo"):
                    _handle_admin_echo(event)
                    continue
                text = msg.get("text", "").strip()
                mid = msg.get("mid", "")
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
    (not 401) on bad/missing credentials to avoid confirming the endpoint
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
        "status": "healthy",
        "products_cached": len(bot_state.get_products()),
        "cache_age_secs": age,
        "timestamp": datetime.now().isoformat(),
    }), 200


@app.route("/", methods=["GET"])
def index():
    """Liveness probe for Render's default health check.

    Returns:
        JSON with bot name and version, HTTP 200.
    """
    return jsonify({"bot": "Ace Bot Test", "version": "3.0"}), 200

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _validate_env() -> None:
    """Assert that all required secrets and config values are present.

    Fails fast at startup rather than surfacing a KeyError mid-request.

    Raises:
        EnvironmentError: If any critical secret is missing, or if
            ``GITHUB_PRODUCTS_URL`` does not match the expected raw GitHub URL
            pattern.
    """
    required = {
        "PAGE_ACCESS_TOKEN": PAGE_ACCESS_TOKEN,
        "FB_APP_SECRET": FB_APP_SECRET,
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "VERIFY_TOKEN": VERIFY_TOKEN,
        "GITHUB_PRODUCTS_URL": GITHUB_PRODUCTS_URL,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise EnvironmentError(f"Critical secrets missing: {missing}")

    if not _GITHUB_RAW_PATTERN.match(GITHUB_PRODUCTS_URL):
        raise EnvironmentError( 
            f"GITHUB_PRODUCTS_URL does not look like a raw GitHub URL: {GITHUB_PRODUCTS_URL!r}"
        )

    optional = {
        "HEALTH_TOKEN": HEALTH_TOKEN,
        "SENDER_EMAIL": SENDER_EMAIL,
        "SENDER_PASSWORD": SENDER_PASSWORD,
        "RECEIVER_EMAIL": RECEIVER_EMAIL,
    }
    missing_optional = [k for k, v in optional.items() if not v]
    if missing_optional:
        logger.warning("Optional config missing (admin email/health check disabled): %s",
                       ", ".join(missing_optional))

    logger.info("Environment validated.")


def _startup() -> None:
    """Initialise all external integrations and background jobs.

    Call once at process start. Separated from ``_validate_env`` so tests can
    validate config without spinning up real schedulers or API connections.
    """
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Gemini configured (model: %s).", GEMINI_MODEL)

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


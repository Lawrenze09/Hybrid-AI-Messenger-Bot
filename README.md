# Hybrid AI Messenger Bot
### Ace Apparel — Automated Customer Support System

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-000000?style=flat-square&logo=flask&logoColor=white)
![Gemini](https://img.shields.io/badge/Gemini_2.5_Flash-AI_Fallback-FF6B35?style=flat-square&logo=google&logoColor=white)
![Render](https://img.shields.io/badge/Deployed-Render-46E3B7?style=flat-square&logo=render&logoColor=white)
![Status](https://img.shields.io/badge/Status-Production_Testing-FFC107?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

**A production-grade Facebook Messenger bot engineered around a Hybrid AI Architecture —
deterministic accuracy for structured data, generative AI only where it adds value.**

[Architecture](#architecture) · [Security](#security-engineering) · [Setup](#local-setup) · [Deployment](#deployment)

</div>

---

## Overview

Most Messenger bots make one of two mistakes: they rely entirely on an LLM and hallucinate
product details, or they're purely rule-based and fail the moment a customer asks something
unexpected. This system solves both problems by separating concerns at the architectural level.

Product data — SKUs, pricing, availability — is served exclusively from a rule-based JSON engine
that is 100% deterministic. Gemini only handles the conversational layer: anything that isn't
a structured product query. This keeps token costs minimal, eliminates hallucination risk on
business-critical data, and keeps average response latency under 800ms.

The bot is currently in the **production testing phase**, running on a staging Page before
full deployment to the official Ace Apparel Facebook presence.

---

## Core Capabilities

- **Hybrid query routing** — exact/keyword product matching before any LLM call is made
- **Generic Template carousel** — Facebook-native product cards with price lookup postbacks
- **Admin handover protocol** — keyword-triggered SMTP alert + per-user Sofia pause/resume
- **Sofia intro system** — atomic first-contact detection, greeting sent exactly once per user
- **HMAC-SHA256 webhook verification** — every POST validated against Meta's App Secret before processing
- **Sliding-window rate limiting** — independent limiters on Gemini calls and admin email per sender
- **60-minute async cache refresh** — product catalogue refreshed in the background without blocking requests
- **Message deduplication** — TTL-based memory cache prevents double-processing on Facebook retries
- **Input sanitization** — log injection, email header injection, and control-char defenses throughout

---

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| **Backend** | Python 3.11, Flask 3.x | Webhook server, request routing |
| **AI** | Google Gemini 2.5 Flash Lite | Natural language fallback |
| **Scheduling** | APScheduler 3.x | Background product cache refresh |
| **Messaging** | Meta Graph API v22.0 | Messenger Send API, webhook events |
| **Email** | Gmail SMTP / MIME | Admin handover alerts |
| **Hosting** | Render (Gunicorn) | Production deployment |
| **Secrets** | Render Secret Files + env vars | Tiered credential management |

---

## Architecture

```
Facebook Messenger (Customer)
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│                   /webhook  (POST)                      │
│                                                         │
│  1. HMAC-SHA256 Signature Check ──────────────────────► REJECT 403 if invalid
│     (X-Hub-Signature-256 header validated               │
│      using hmac.compare_digest — constant time)         │
│                                                         │
│  2. Echo Event Intercept ─────────────────────────────► handle_admin_echo()
│     (admin messages from Page Inbox)                    │  pause / resume Sofia
│                                                         │
│  3. First-Contact Check ──────────────────────────────► Sofia intro (atomic, once)
│                                                         │
│  4. Admin Session Guard ──────────────────────────────► Silent if admin has thread
│                                                         │
│  5. Handover Keyword Scan ────────────────────────────► SMTP alert to admin
│     (refund, complaint, reklamo, cancel...)             │  Sofia paused for user
│                                                         │
│  6. ┌── Rule Engine (JSON catalogue) ────────────────► Carousel  [0 token cost]
│     │   Exact ID/name → keyword/substring scan          │
│     │   Returns up to 10 products (FB carousel limit)   │
│     │                                                   │
│     └── No match → Gemini Fallback ───────────────────► Natural language reply
│         (rate-limited: 10 calls / 60s / user)           │
└─────────────────────────────────────────────────────────┘
         │
         ▼
Facebook Messenger (Response delivered)
```

### Why Hybrid, Not Pure LLM

| Concern | Pure LLM | Hybrid Approach |
|---|---|---|
| Product price accuracy | Hallucination risk | 100% from JSON source of truth |
| Token cost | Every message billed | Only unmatched queries reach Gemini |
| Response latency | 1–3s per call | Sub-100ms for rule-matched queries |
| Scalability | Cost grows linearly with traffic | Rule engine cost is flat |
| SKU / availability | Must be injected into every prompt | Live cache, refreshed every 60 min |

---

## Security Engineering

### HMAC-SHA256 Webhook Verification

The webhook URL is publicly accessible by design — Meta needs to reach it. Without signature
verification, any script on the internet can POST forged events, which translates directly to
spoofed admin alerts and exhausted Gemini quota.

Meta signs every delivery with `HMAC-SHA256(app_secret, raw_body)` and sends the digest in the
`X-Hub-Signature-256` header. The implementation recomputes the digest server-side and compares
using `hmac.compare_digest` — a **constant-time comparison** that eliminates timing-oracle
attacks that would otherwise allow an attacker to brute-force the App Secret one byte at a time.

```python
computed = hmac.new(FB_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
return hmac.compare_digest(computed, header[7:])  # strips the "sha256=" prefix
```

Every request failing this check is rejected with HTTP 403 before any business logic executes.

### Credential Tiering

Not all secrets carry equal risk. The architecture reflects that with two storage tiers:

| Credential | Storage Tier | Rationale |
|---|---|---|
| `FB_APP_SECRET` | Render Secret File | Never appears in env dumps, process listings, or logs |
| `PAGE_ACCESS_TOKEN` | Render Secret File | Never appears in env dumps, process listings, or logs |
| `GEMINI_API_KEY` | Environment variable | Standard API key, rotatable without redeployment |
| `SENDER_PASSWORD` | Environment variable | Gmail App Password — not the account password |
| `VERIFY_TOKEN` | Environment variable | Low-sensitivity, used only for initial handshake |

Render Secret Files are mounted at `/etc/secrets/<filename>` at runtime. The loader checks
the Secret File path first and falls back to env vars automatically for local development — no
code changes required between environments.

### Additional Defenses

- **1 MB request body cap** — Flask rejects oversized payloads before deserialization (`MAX_CONTENT_LENGTH`)
- **500-char input cap** — user messages truncated before reaching the rule engine, Gemini, or the logger
- **Log injection prevention** — `\r\n\t` stripped from all user input before any `logger.*` call
- **Email body sanitization** — control characters `\x00-\x1f` removed before SMTP send
- **GitHub URL allowlist** — `GITHUB_PRODUCTS_URL` validated against a strict `raw.githubusercontent.com` regex on startup; a misconfigured value raises `EnvironmentError` before the server accepts traffic
- **`/health` returns HTTP 404** on bad/missing auth token — avoids confirming the endpoint exists to scanners (a 401 would)

---

## Performance and Optimization

### Asynchronous Product Cache

Fetching the product catalogue on every request would add 200–400ms of latency and create
unnecessary load on GitHub's raw content servers. `APScheduler` runs a background thread
every 60 minutes to refresh the in-memory cache. All requests read from memory — the refresh
job is invisible to the request cycle.

```
Incoming request:
  BotState.get_products()  →  in-memory list  (microseconds)

Background thread (every 60 min):
  _fetch_products()  →  GitHub raw URL  →  BotState.update_cache()
```

The cache refresh also sanitizes every string field on load, so a tampered `products.json`
cannot inject control characters into Messenger payloads.

### Message Deduplication (TTL Cache)

Facebook retries webhook delivery when it doesn't receive a timely HTTP 200. Without deduplication,
a slow downstream call — Gemini taking 2–3 seconds — causes the same message to be processed
twice: the customer gets two responses and the admin gets two email alerts.

Each incoming `mid` (message ID) is recorded in a TTL-bounded dict with a 1-hour expiry window.
Any retry within that window is dropped silently at the top of the pipeline before any I/O occurs.

### Rate Limiting

Independent sliding-window rate limiters per sender prevent resource exhaustion from any single
PSID — whether a real user typing quickly or an attacker replaying spoofed webhook events.

| Resource | Limit | Window | What it protects |
|---|---|---|---|
| Gemini API calls | 10 requests | 60 seconds | API quota and cost |
| Admin email alerts | 2 emails | 5 minutes | Gmail sender account |

---

## Admin Handover Protocol

When a customer message contains a sensitive keyword (`refund`, `complaint`, `reklamo`,
`cancel`, `problema`, and others), the pipeline:

1. Sends the customer a holding message confirming the admin has been alerted
2. Fires an SMTP email to the admin with the customer's name, PSID, message text, and timestamp
3. Sets the user's session to `awaiting_admin` — Sofia goes silent for that specific thread

When the admin replies from Facebook Page Inbox, the bot detects the outgoing echo event and
maintains the pause. Typing `bot` or `sofia` in the inbox thread resumes the automated pipeline
and sends the customer a return greeting — no dashboard or manual toggle required.

---

## Local Setup

### Prerequisites

- Python 3.11+
- Facebook Developer account with a Messenger app and Page
- Google AI Studio API key
- Gmail account with an [App Password](https://myaccount.google.com/apppasswords) configured
- [ngrok](https://ngrok.com) for exposing the local server to Meta's webhook infrastructure

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/Lawrenze09/Hybrid-AI-Messenger-Bot.git
cd Hybrid-AI-Messenger-Bot

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Edit .env — every required variable is documented inside
```

### Environment Variables

| Variable | Required | Source |
|---|---|---|
| `FB_APP_SECRET` | ✅ | Meta Developer Console → App Settings → Basic |
| `PAGE_ACCESS_TOKEN` | ✅ | Meta Developer Console → Messenger → Settings |
| `VERIFY_TOKEN` | ✅ | Any string — must match your Meta webhook config |
| `GEMINI_API_KEY` | ✅ | [Google AI Studio](https://aistudio.google.com) |
| `GITHUB_PRODUCTS_URL` | ✅ | Raw `githubusercontent.com` URL to `products.json` |
| `SENDER_EMAIL` | ✅ | Gmail address for outbound admin alerts |
| `SENDER_PASSWORD` | ✅ | Gmail App Password |
| `RECEIVER_EMAIL` | ✅ | Destination address for admin alerts |
| `HEALTH_TOKEN` | Optional | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `GEMINI_MODEL` | Optional | Defaults to `gemini-2.5-flash-lite` |
| `CACHE_REFRESH_MINS` | Optional | Defaults to `60` |

### Running Locally

```bash
# Terminal 1 — start the Flask server
python messenger_bot_test.py

# Terminal 2 — expose it via ngrok
ngrok http 5000
```

Set your Meta webhook Callback URL to the ngrok `https://` URL with `/webhook` appended.
Subscribe to: `messages`, `messaging_postbacks`, `message_echoes`.

### Verifying the Setup

```bash
# Confirm HMAC verification is live — must return 403
curl -X POST https://<ngrok-url>/webhook \
  -H "Content-Type: application/json" \
  -d '{"entry":[]}'

# Confirm health endpoint is reachable
curl -H "Authorization: Bearer <HEALTH_TOKEN>" \
  http://localhost:5000/health
```

Expected health response:
```json
{
  "status": "healthy",
  "products_cached": 12,
  "cache_age_secs": 4,
  "timestamp": "2026-03-05T12:00:04.123456"
}
```

---

## Deployment

Hosted on [Render](https://ace-apparel-bot-test.onrender.com). The free tier spins down after 15 minutes of inactivity;
session state (first-contact flags, rate-limit counters) resets on wake. This is acceptable for
a testing deployment — a paid tier or Redis-backed state would be appropriate for production.

**Procfile:**
```
web: gunicorn messenger_bot_test:app
```

**runtime.txt:**
```
python-3.11.9
```

**Webhook URL:**
```
https://ace-apparel-bot-test.onrender.com/webhook
```

`FB_APP_SECRET` and `PAGE_ACCESS_TOKEN` should be configured as **Secret Files** in Render,
not as standard environment variables. See
[Render Secret Files documentation](https://render.com/docs/secret-files).

---

## Product Catalogue Schema

`products.json` is served from a stable `raw.githubusercontent.com` URL and cached in memory.

```json
{
  "id": "ACE-OVT-001",
  "name": "Ace Onyx Stealth Tee",
  "keywords": ["oversized", "tee", "heavyweight", "streetwear"],
  "price": "₱450",
  "availability": "In Stock",
  "image_url": "https://your-cdn.com/ace-onyx-stealth.jpg",
  "description": "300 GSM heavyweight cotton, dropped shoulders.",
  "color": "Black"
}
```

> `image_url` must be a stable, publicly reachable HTTPS URL — not a Facebook CDN link.
> Facebook fetches carousel images server-side during template rendering; `fbcdn.net` URLs
> are user-scoped, expire, and will cause the entire carousel card to fail silently.

---

## Project Structure

```
Hybrid-AI-Messenger-Bot/
├── messenger_bot_test.py     # Main application — webhook server and all bot logic
├── products.json             # Product catalogue (consumed via GitHub raw URL)
├── requirements.txt          # Pinned Python dependencies
├── .env.example              # Environment variable template with inline documentation
├── .gitignore                # Secrets, virtual environments, caches, editor configs
├── Procfile                  # Gunicorn entry point for Render
├── runtime.txt               # Python version pin — 3.11.9
├── privacy.html              # Privacy policy for Meta App Review
└── README.md
```

---

## License

MIT — see [LICENSE](LICENSE) for details.

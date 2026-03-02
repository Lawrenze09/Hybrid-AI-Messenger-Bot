"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HYBRID AI MESSENGER BOT - PRODUCTION GRADE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Flask-based webhook for Facebook Messenger with:
- Product keyword matching from GitHub JSON
- Gemini Pro AI fallback for unmatched queries
- Admin handover alert via Gmail
- 60-minute cache refresh cycle
- Message deduplication
- Personalized responses using user's first name

Author: Nazh Lawrenze Romero
Tech Stack: Flask, Meta Graph API, Gemini Pro, APScheduler
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
#Standard Libraries
import os
import json
import time
import logging
import requests
import smtplib 

#Third Party Libraries
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from threading import Lock
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import google.generativeai as genai

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION & ENVIRONMENT VARIABLES
# ═══════════════════════════════════════════════════════════════════════════

PAGE_ACCESS_TOKEN = os.environ.get('PAGE_ACCESS_TOKEN')
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
SENDER_PASSWORD = os.environ.get('SENDER_PASSWORD') 
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL')
GITHUB_PRODUCTS_URL = os.environ.get('GITHUB_PRODUCTS_URL')

# Flask App Initialization
app = Flask(__name__)

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

required_vars = [PAGE_ACCESS_TOKEN, VERIFY_TOKEN, GEMINI_API_KEY, SENDER_EMAIL, SENDER_PASSWORD, RECEIVER_EMAIL, GITHUB_PRODUCTS_URL]
if not all(required_vars):
    logger.warning("Warning: Some environment variables are missing! Check your Render Dashboard.")
else:
    logger.info("All core environment variables are loaded.")


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

class BotState:
    """Thread-safe global state manager"""
    def __init__(self):
        self.products_cache = []
        self.cache_last_updated = None
        self.processed_messages = {}
        self.lock = Lock()
        self.user_context = {}
    
    def update_cache(self, products):
        """Thread-safe cache update"""
        with self.lock:
            self.products_cache = products
            self.cache_last_updated = datetime.now()
            logger.info(f"Cache updated with {len(products)} products")
    
    def get_products(self):
        """Thread-safe cache retrieval"""
        with self.lock:
            return self.products_cache.copy()
    
    def is_message_processed(self, message_id):
        """Check if message was already processed"""
        with self.lock:
            # Clean up old entries (older than 1 hour)
            cutoff_time = time.time() - 3600
            self.processed_messages = {
                mid: timestamp for mid, timestamp in self.processed_messages.items()
                if timestamp > cutoff_time
            }
            
            if message_id in self.processed_messages:
                return True
            
            self.processed_messages[message_id] = time.time()
            return False
    
    def get_user_context(self, sender_id):
        """Get conversation context for user"""
        with self.lock:
            return self.user_context.get(sender_id, {})
    
    def set_user_context(self, sender_id, context):
        """Set conversation context for user"""
        with self.lock:
            self.user_context[sender_id] = context

# Initialize global state
bot_state = BotState()

# ═══════════════════════════════════════════════════════════════════════════
# DATA LAYER: GITHUB JSON CACHING SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

def fetch_products_from_github():
    """
    Fetch products.json from GitHub and update in-memory cache.
    Runs every 60 minutes via APScheduler.
    
    Expected JSON structure:
    [
        {
            "id": "ACE-OVT-001",
            "name": "Ace Onyx Stealth",
            "keywords": ["oversized", "tee", "streetwear", "cotton", "heavyweight"],
            "price": "₱450",
            "availability": "In Stock",
            "image_url": "https://via.placeholder.com/500x500.png?text=Ace+OVT+001",
            "description": "Premium heavyweight cotton tee with 300 GSM fabric for ultimate comfort and durability.",
            "color": "Black"
        }
    ]
    """
    try:
        logger.info(f"Fetching products from: {GITHUB_PRODUCTS_URL}")
        response = requests.get(GITHUB_PRODUCTS_URL, timeout=10)
        response.raise_for_status()
        
        products = response.json()
        
        # Validate JSON structure
        if not isinstance(products, list):
            logger.error("Products JSON is not a list")
            return
        
        # Update cache
        bot_state.update_cache(products)
        logger.info(f"Successfully cached {len(products)} products")
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching products from GitHub: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing products JSON: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in fetch_products_from_github: {e}")

def initialize_scheduler():
    """Initialize APScheduler for background tasks"""
    scheduler = BackgroundScheduler()
    
    # Schedule products refresh every 60 minutes
    scheduler.add_job(
        func=fetch_products_from_github,
        trigger="interval",
        minutes=60,
        id='fetch_products',
        name='Fetch products from GitHub',
        replace_existing=True
    )
    
    scheduler.start()
    logger.info("Scheduler initialized - Products will refresh every 60 minutes")
    
    # Fetch products immediately on startup
    fetch_products_from_github()

# ═══════════════════════════════════════════════════════════════════════════
# META GRAPH API INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════

def get_user_profile(sender_id):
    """
    Fetch user profile from Meta Graph API.
    Returns user's first_name for personalization.
    
    Args:
        sender_id (str): Facebook User ID (PSID)
    
    Returns:
        dict: {'first_name': 'John', 'last_name': 'Doe', ...}
    """
    try:
        url = f"https://graph.facebook.com/v18.0/{sender_id}"
        params = {
            'fields': 'first_name,last_name,profile_pic',
            'access_token': PAGE_ACCESS_TOKEN
        }
        
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        
        profile = response.json()
        logger.info(f"Fetched profile for {sender_id}: {profile.get('first_name')}")
        return profile
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching user profile: {e}")
        return {'first_name': 'Customer'}  # Fallback
    except Exception as e:
        logger.error(f"Unexpected error in get_user_profile: {e}")
        return {'first_name': 'Customer'}

def send_facebook_message(sender_id, message_data):
    """
    Send message to Facebook Messenger using Send API.
    
    Args:
        sender_id (str): Recipient's PSID
        message_data (dict): Message payload (text, template, etc.)
    """
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages"
        params = {'access_token': PAGE_ACCESS_TOKEN}
        headers = {'Content-Type': 'application/json'}
        
        payload = {
            'recipient': {'id': sender_id},
            'message': message_data
        }
        
        response = requests.post(url, params=params, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        
        logger.info(f"Message sent to {sender_id}")
        return True
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending message to {sender_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error in send_facebook_message: {e}")
        return False

def send_typing_indicator(sender_id, typing_on=True):
    """Send typing indicator (on/off)"""
    try:
        url = f"https://graph.facebook.com/v18.0/me/messages"
        params = {'access_token': PAGE_ACCESS_TOKEN}
        headers = {'Content-Type': 'application/json'}
        
        payload = {
            'recipient': {'id': sender_id},
            'sender_action': 'typing_on' if typing_on else 'typing_off'
        }
        
        requests.post(url, params=params, headers=headers, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Error sending typing indicator: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# HYBRID LOGIC GATEKEEPER: PRODUCT MATCHING
# ═══════════════════════════════════════════════════════════════════════════

def normalize_text(text):
    """
    Normalize user input for keyword matching.
    - Lowercase
    - Strip whitespace
    - Remove special characters (optional)
    """
    return text.lower().strip()

def find_matching_products(user_message):
    """
    Search cached products for keyword matches.
    
    Args:
        user_message (str): User's input message
    
    Returns:
        list: A list containing one product (if exact ID match)
              or Multiple products (if keyword match,up to 10)
    """
    normalized_msg = normalize_text(user_message)
    products = bot_state.get_products()
    matches = []

    for product in products:
        p_id = normalize_text(product.get('id', ''))
        p_name = normalize_text(product.get('name', ''))
        
        # Kung saktong-sakto ang ID o Name sa message
        if normalized_msg == p_id or normalized_msg == p_name:
            logger.info(f"Exact match found: {product.get('id')}")
            return [product]
    
    for product in products:
        keywords = [normalize_text(kw) for kw in product.get('keywords', [])]
        
        # Check if any keyword matches
        if any(kw in normalized_msg for kw in keywords):
            matches.append(product)
        
        # Also check if product name is mentioned
        if normalize_text(product.get('name', '')) in normalized_msg:
            matches.append(product)
    
    # Remove duplicates and limit to 10 (Facebook carousel limit)
    seen_ids = set()
    unique_matches = []
    for product in matches:
        if product['id'] not in seen_ids:
            seen_ids.add(product['id'])
            unique_matches.append(product)
            if len(unique_matches) >= 10:
                break
    
    logger.info(f"Found {len(unique_matches)} matching products for: '{user_message}'")
    return unique_matches

def create_product_carousel(products, sender_id):
    """
    Create Facebook Generic Template carousel.
    
    Args:
        products (list): List of product dicts
        sender_id (str): User's PSID (for postback context)
    
    Returns:
        dict: Facebook template message payload
    """
    elements = []
    
    for product in products[:10]:  # Max 10 cards
        product_id = product.get('id', 'no-id')
        title = product.get('name', 'Ace Product')
        img = product.get('image_url', 'https://via.placeholder.com/500')
        price = product.get('price', '₱0')
        desc = product.get('description', '')
        subtitle = f"{price} - {desc}"[:80]
        
        element = {
            "title": title,
            "image_url": img,
            "subtitle": subtitle,
            "buttons": [
                {
                    "type": "postback",
                    "title": "View Price",
                    "payload": json.dumps({
                        "action": "view_price",
                        "product_id": product_id
                    })
                }
            ]
        }
        elements.append(element)
    
    return {
        "attachment": {
            "type": "template",
            "payload": {
                "template_type": "generic",
                "elements": elements
            }
        }
    }

def handle_view_price_postback(product_id, sender_id, user_first_name):
    """
    Handle 'View Price' button click.
    Send price and availability details.
    """
    products = bot_state.get_products()
    product = next((p for p in products if str(p.get('id', '')) == str(product_id)), None)
    
    if not product:
        send_facebook_message(sender_id, {
            'text': f"Sorry {user_first_name}, hindi ko po mahanap ang product na 'yan sa listahan namin."
        })
        return
    
    # Format response
    price_message = (
        f"{product.get('name')}\n\n"
        f"Price: {product.get('price', 'Contact us')}\n"
        f"Availability: {product.get('availability', 'In Stock')}\n\n"
        f"Interested po kayo, {user_first_name}? Just send us a message!"
    )
    
    send_facebook_message(sender_id, {'text': price_message})

# ═══════════════════════════════════════════════════════════════════════════
# GEMINI AI INTEGRATION (FALLBACK)
# ═══════════════════════════════════════════════════════════════════════════

def initialize_gemini():
    """Initialize Gemini 2.5 Flash Lite API"""
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        logger.info("Gemini 2.5 Flash Lite API initialized")
    except Exception as e:
        logger.error(f"Error initializing Gemini: {e}")

def get_gemini_response(user_message, user_first_name):
    """
    Get AI response from Gemini 2.5 Flash Lite when no product match is found.
    
    Args:
        user_message (str): User's query
        user_first_name (str): User's name for personalization
    
    Returns:
        str: AI-generated response in Taglish
    """
    try:
        # Initialize model
        model = genai.GenerativeModel('gemini-2.5-flash-lite')
        
        # System prompt for Taglish customer service personality
        system_prompt = f"""
You are Sofia, the trendy and polite AI assistant for 'Ace', a premium streetwear brand.
Brand Vibe: Streetwear, Minimalist, Sporty.
Materials: We use Heavyweight cotton, Breathable mesh, and French Terry Fleece.

Guidelines:
- Speak in natural, friendly Taglish (Tagalog-English).
- Always address the customer as {user_first_name}.
- Use "po" and "opo" to maintain a respectful Filipino culture.
- Since we sell Oversized Tees, Mesh Shorts, Hoodies, Jerseys, Socks, and Gym Sandos, encourage them to check our catalog.
- If a query is about an order or a refund, tell them: "Wait lang po {user_first_name}, ia-alert ko na po ang aming admin para ma-assist kayo agad."
- Keep responses concise (under 150 characters) for Messenger.
- If the customer asks for a recommendation, suggest our Best Selling Mesh Shorts or Heavyweight Tees.

User query: {user_message}

Respond as Sofia from Ace:
"""
        
        # Generate response
        response = model.generate_content(system_prompt)
        ai_reply = response.text.strip()
        
        logger.info(f"Gemini response generated for: {user_message}")
        return ai_reply
        
    except Exception as e:
        logger.error(f"Error getting Gemini response: {e}")
        return (
            f"Pasensya na po, {user_first_name}, naka-day off po muna si Sofia ngayon. 😅"
            f"Message po kayo ulit mamaya o bukas, or wait niyo po ang aming human admin na mag-reply. Thank you! 🙏"
        )

# ═══════════════════════════════════════════════════════════════════════════
# ADMIN HANDOVER PROTOCOL (Gmail Webhook)
# ═══════════════════════════════════════════════════════════════════════════

HANDOVER_KEYWORDS = ['refund', 'complaint', 'complain', 'admin', 'manager', 'supervisor', 
                     'problema', 'issue', 'reklamo', 'balik', 'return', 'problem', 'cancel']

def check_handover_trigger(message):
    """Check if message contains handover keywords"""
    normalized = normalize_text(message)
    return any(keyword in normalized for keyword in HANDOVER_KEYWORDS)

def notify_admin_via_email(sender_id, user_message, user_profile):
    """Sends an email alert using Gmail SMTP when handover is triggered."""
    try:
        if not SENDER_EMAIL or not SENDER_PASSWORD:
            logger.warning("Email configuration missing!")
            return False

        first_name = user_profile.get('first_name', 'Customer')
        last_name = user_profile.get('last_name', '')
        
        # Setup Email Content
        subject = f"Admin Handover Requested: {first_name}"
        body = f"""
        Isang customer ang nangangailangan ng tulong (Handover Triggered).

        DETAILS:
        - Name: {first_name} {last_name}
        - Facebook PSID: {sender_id}
        - Message: "{user_message}"
        - Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

        Instruction: Pumunta sa Facebook Page Inbox para maka-reply sa kanila.
        """

        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        # SMTP Server Connection (Gmail)
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls() # Secure the connection
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
            
        logger.info(f"Admin notified via Gmail for user {sender_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email alert: {e}")
        return False
        
def handle_admin_handover(sender_id, user_message, user_first_name, user_profile):
    """
    Execute admin handover protocol.
    1. Notify admin via Gmail SMTP
    2. Send confirmation message to user
    """
    notify_admin_via_email(sender_id, user_message, user_profile)

    handover_message = (
        f"Wait lang po {user_first_name}, ia-alert ko na po ang aming admin para ma-assist kayo agad sa concern niyo.\n\n"
        f"Pasensya na po sa abala, stay tuned po! Ace Team will be with you shortly."
    )
    
    send_facebook_message(sender_id, {'text': handover_message})
    
    # 3. Mark user as waiting for admin in context
    bot_state.set_user_context(sender_id, {
        'status': 'awaiting_admin',
        'timestamp': datetime.now().isoformat()
    })

# ═══════════════════════════════════════════════════════════════════════════
# MESSAGE PROCESSING LOGIC (MAIN HANDLER)
# ═══════════════════════════════════════════════════════════════════════════

def process_message(sender_id, message_text, message_id):
    """
    Main message processing pipeline.
    
    Flow:
    1. Check for duplicates
    2. Fetch user profile
    3. Check for admin handover keywords
    4. Search for product matches
    5. If matches: Send carousel
    6. If no matches: Use Gemini AI
    
    Args:
        sender_id (str): User's PSID
        message_text (str): User's message
        message_id (str): Unique message ID for deduplication
    """
    # ─── STEP 1: DEDUPLICATION ───
    if bot_state.is_message_processed(message_id):
        logger.info(f"Skipping duplicate message: {message_id}")
        return
    
    try:
        # Show typing indicator
        send_typing_indicator(sender_id, True)
        
        # ─── STEP 2: FETCH USER PROFILE ───
        user_profile = get_user_profile(sender_id)
        user_first_name = user_profile.get('first_name', 'Customer')

        # ─── STEP 3: Context Check ───
        user_context = bot_state.get_user_context(sender_id)
        if user_context.get('status') == 'awaiting_admin':
            if 'bot' in message_text.lower() or 'sofia' in message_text.lower():
                bot_state.set_user_context(sender_id, {'status': 'chatting'})
                send_facebook_message(sender_id, {'text': "Bumalik na po ako! 🙋‍♀️ Paano ko po kayo matutulungan ulit?"})
                logger.info(f"AI Resumed for user {sender_id}")
                return
                
            logger.info(f"User {sender_id} is waiting for admin. Sofia is staying quiet.")
            send_typing_indicator(sender_id, False)
            return
        
        # ─── STEP 4: KEYWORD DETECTION ───
        if check_handover_trigger(message_text):
            handle_admin_handover(sender_id, message_text, user_first_name, user_profile)
            send_typing_indicator(sender_id, False)
            return
        
        # ─── STEP 4: SEARCH FOR PRODUCT MATCHES ───
        matching_products = find_matching_products(message_text)
        
        if matching_products:
            # ─── STEP 5A: SEND PRODUCT CAROUSEL ───
            carousel = create_product_carousel(matching_products, sender_id)
            
            # Send personalized intro message first
            intro_text = (
                f"Hi {user_first_name}!\n"
                f"Found {len(matching_products)} product(s) for you po:"
            )
            send_facebook_message(sender_id, {'text': intro_text})
            
            # Send carousel
            send_facebook_message(sender_id, carousel)
            
        else:
            # ─── STEP 5B: USE GEMINI AI FALLBACK ───
            ai_response = get_gemini_response(message_text, user_first_name)
            send_facebook_message(sender_id, {'text': ai_response})
        
        # Turn off typing indicator
        send_typing_indicator(sender_id, False)
        
    except Exception as e:
        logger.error(f"Error processing message from {sender_id}: {e}")
        
        # Send error message to user
        error_msg = (
            f"Sorry {user_first_name}, may technical issue po kami. "
            f"Please try again in a moment."
        )
        send_facebook_message(sender_id, {'text': error_msg})
        send_typing_indicator(sender_id, False)

def process_postback(sender_id, postback_payload):
    """
    Handle postback events (button clicks).
    
    Args:
        sender_id (str): User's PSID
        postback_payload (str): JSON string with action data
    """
    try:
        payload = json.loads(postback_payload)
        action = payload.get('action')
        
        # Fetch user profile
        user_profile = get_user_profile(sender_id)
        user_first_name = user_profile.get('first_name', 'Customer')
        
        if action == 'view_price':
            product_id = payload.get('product_id')
            handle_view_price_postback(product_id, sender_id, user_first_name)
        else:
            logger.warning(f"Unknown postback action: {action}")
            
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing postback payload: {e}")
    except Exception as e:
        logger.error(f"Error processing postback: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# FLASK WEBHOOK ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """
    Webhook verification endpoint (Facebook requirement).
    
    Facebook will call this during setup with:
    - hub.mode=subscribe
    - hub.verify_token=<my_verify_token>
    - hub.challenge=<random_string>
    """
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        logger.info('Webhook verified successfully')
        return challenge, 200
    else:
        logger.warning('Webhook verification failed')
        return 'Forbidden', 403

@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Main webhook endpoint for receiving messages and events.
    
    Handles:
    - messages (text, attachments)
    - messaging_postbacks (button clicks)
    - messaging_reads, messaging_deliveries (ignored)
    """
    try:
        data = request.get_json()
        
        # Validate webhook signature (recommended for production)
        # signature = request.headers.get('X-Hub-Signature-256')
        # if not validate_signature(request.data, signature):
        #     return 'Invalid signature', 403
        
        # Process each entry
        for entry in data.get('entry', []):
            for messaging_event in entry.get('messaging', []):
                sender_id = messaging_event['sender']['id']
                
                # ─── HANDLE TEXT MESSAGES ───
                if 'message' in messaging_event:
                    message = messaging_event['message']
                    message_id = message.get('mid')
                    message_text = message.get('text')
                    
                    if message_text:
                        process_message(sender_id, message_text, message_id)
                
                # ─── HANDLE POSTBACKS (BUTTON CLICKS) ───
                elif 'postback' in messaging_event:
                    postback = messaging_event['postback']
                    payload = postback.get('payload')
                    
                    if payload:
                        process_postback(sender_id, payload)
        
        return 'OK', 200
        
    except Exception as e:
        logger.error(f"Error in webhook handler: {e}")
        return 'Internal Server Error', 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for monitoring"""
    cache_age = None
    if bot_state.cache_last_updated:
        cache_age = (datetime.now() - bot_state.cache_last_updated).total_seconds()
    
    return jsonify({
        'status': 'healthy',
        'products_cached': len(bot_state.get_products()),
        'cache_age_seconds': cache_age,
        'cache_last_updated': bot_state.cache_last_updated.isoformat() if bot_state.cache_last_updated else None,
        'timestamp': datetime.now().isoformat()
    }), 200

@app.route('/', methods=['GET'])
def home():
    """Root endpoint"""
    return jsonify({
        'bot': 'Hybrid AI Messenger Bot',
        'status': 'running',
        'version': '1.0.0',
        'endpoints': {
            'webhook': '/webhook',
            'health': '/health'
        }
    }), 200

# ═══════════════════════════════════════════════════════════════════════════
# APPLICATION STARTUP
# ═══════════════════════════════════════════════════════════════════════════

def validate_environment():
    """Validate required environment variables"""
    required_vars = {
        'PAGE_ACCESS_TOKEN': PAGE_ACCESS_TOKEN,
        'GEMINI_API_KEY': GEMINI_API_KEY
    }
    
    missing = [key for key, value in required_vars.items() if not value]
    
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        raise ValueError(f"Missing environment variables: {missing}")
    
    logger.info("All required environment variables present")

if __name__ == '__main__':
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("HYBRID AI MESSENGER BOT STARTING...")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    
    try:
        # Validate configuration
        validate_environment()
        
        # Initialize Gemini API
        initialize_gemini()
        
        # Initialize scheduler for background tasks
        initialize_scheduler()
        
        # Start Flask app
        port = int(os.getenv('PORT', 5000))
        logger.info(f"🚀 Starting Flask app on port {port}")
        app.run(host='0.0.0.0', port=port, debug=False)
        
    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        raise


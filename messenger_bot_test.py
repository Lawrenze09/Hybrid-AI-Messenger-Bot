import requests
import google.generativeai as genai
import os
from flask import Flask, request

app = Flask(__name__)

# Credentials retrieved from environment variables for security
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN')
PAGE_ACCESS_TOKEN = os.environ.get('PAGE_ACCESS_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# Gemini AI model initialization
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3-flash-preview')

# In-memory storage for paused conversations (Handover logic)
paused_users = set()

@app.route("/", methods=['GET'])
def verify():
    """Endpoint for Meta Webhook verification"""
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed", 403

@app.route("/", methods=['POST'])
def webhook():
    """Main entry point for incoming Messenger events"""
    data = request.get_json()
    if data.get("object") == "page":
        for entry in data.get("entry"):
            for messaging_event in entry.get("messaging"):
                sender_id = messaging_event["sender"]["id"]

                # 1. PROCESS POSTBACK AND QUICK REPLY PAYLOADS
                payload = None
                if messaging_event.get("postback"):
                    payload = messaging_event["postback"].get("payload")
                elif messaging_event.get("message") and messaging_event["message"].get("quick_reply"):
                    payload = messaging_event["message"]["quick_reply"].get("payload")

                if payload:
                    if payload == "PRICE_INFO":
                        send_message(sender_id, "ACE APPAREL PRICES:\n- Graphic Tees: P500\n- Hoodies: P1,200\n- Caps: P350")
                    elif payload == "HOW_TO_ORDER":
                        send_message(sender_id, "HOW TO ORDER:\n1. Screenshot item.\n2. Send details.\n3. Wait for confirmation.")
                    elif payload == "SHIPPING_INFO":
                        send_message(sender_id, "SHIPPING:\n- Metro Manila: 2-3 days\n- Provinces: 5-7 days via J&T")
                    elif payload == "ADMIN_REQUEST": # Triggered from Persistent Menu
                        paused_users.add(sender_id)
                        send_message(sender_id, "We are redirecting you to our admin. Stand by.")
                    continue 

                # 2. PROCESS STANDARD TEXT MESSAGES
                if messaging_event.get("message") and messaging_event["message"].get("text"):
                    message_text = messaging_event['message'].get('text', '').lower()

                    # --- HANDOVER LOGIC (ON/OFF SWITCH) ---
                    if "#admin" in message_text:
                        paused_users.add(sender_id)
                        send_message(sender_id, "Bot disabled. An admin will assist you shortly.")
                        continue
                    elif "#bot" in message_text:
                        paused_users.discard(sender_id)
                        send_message(sender_id, "Bot reactivated. How can I help you, ya?")
                        continue

                    # Skip further processing if bot is paused for this user
                    if sender_id in paused_users:
                        continue

                    # --- KEYWORD-BASED RESPONSES ---
                    if any(word in message_text for word in ["hm", "price", "presyo"]):
                        send_message(sender_id, "ACE APPAREL PRICES:\n- Graphic Tees: P500\n- Hoodies: P1,200\n- Caps: P350")
                    
                    elif any(word in message_text for word in ["available", "meron"]):
                        send_message(sender_id, "OUR CURRENT DROP:\n- Classic Ace Tee\n- Signature Hoodie\n- Trucker Caps\n\nLimited stocks only! Would you like to see the size chart or place an order?")        
                    # --- FALLBACK TO AI GENERATION ---
                    else:
                        ai_reply = get_gemini_response(message_text)
                        send_message(sender_id, ai_reply)

    return "ok", 200

def get_gemini_response(prompt):
    """Generates a response using Gemini based on brand context"""
    try:
        context = """
        You are the official AI assistant of 'Ace Apparel'. 
        Tone: Friendly, street-smart, and Taglish.
        Only answer questions related to Ace Apparel products and shipping.
        If the question is unrelated, politely state brand-only assistance.
        Limit responses to 20 words.
        
        Products:
        1. Graphic Tees - P500 (Black/White)
        2. Oversized Hoodies - P1,200
        3. Ace Signature Cap - P350
        
        Shipping: Nationwide via J&T (2-3 days MM, 5-7 days Provinces).
        """
        response = model.generate_content(context + "\nUser: " + prompt)
        return response.text
    except Exception as e:
        print(f"AI Error: {e}")
        return "We are handing you over to our admin to finalize your details."

def send_message(recipient_id, text):
    """Sends a message with Quick Replies via Send API"""
    url = f"https://graph.facebook.com/v21.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    
    quick_replies = [
        {"content_type": "text", "title": "Prices/Products", "payload": "PRICE_INFO"},
        {"content_type": "text", "title": "How to order?", "payload": "HOW_TO_ORDER"},
        {"content_type": "text", "title": "Shipping Info", "payload": "SHIPPING_INFO"}
    ]

    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "text": text,
            "quick_replies": quick_replies
        }
    }
    requests.post(url, json=payload)

if __name__ == "__main__":
    app.run(port=5000)

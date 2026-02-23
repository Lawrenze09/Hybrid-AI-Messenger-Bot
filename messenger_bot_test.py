import requests
import google.generativeai as genai
import os
from flask import Flask, request, make_response

app = Flask(__name__)

# Credentials retrieved from environment variables
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN')
PAGE_ACCESS_TOKEN = os.environ.get('PAGE_ACCESS_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# Gemini AI model initialization
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3-flash-preview')

# In-memory storage to track processed message IDs (Prevents duplicate replies)
processed_messages = set()
paused_users = set()

@app.route("/", methods=['GET'])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed", 403

@app.route("/", methods=['POST'])
def webhook():
    data = request.get_json()
    
    if data.get("object") == "page":
        for entry in data.get("entry"):
            for messaging_event in entry.get("messaging"):
                # Get Message ID or Watermark to identify duplicates
                mid = messaging_event.get("message", {}).get("mid")
                if mid and mid in processed_messages:
                    continue # Skip if already processed
                
                if mid:
                    processed_messages.add(mid)
                    # Optional: Keep the set small
                    if len(processed_messages) > 1000:
                        processed_messages.pop()

                sender_id = messaging_event["sender"]["id"]

                # 1. PROCESS POSTBACK AND QUICK REPLY
                payload = None
                if messaging_event.get("postback"):
                    payload = messaging_event["postback"].get("payload")
                elif messaging_event.get("message") and messaging_event["message"].get("quick_reply"):
                    payload = messaging_event["message"]["quick_reply"].get("payload")

                if payload:
                    handle_payload(sender_id, payload)
                    continue

                # 2. PROCESS STANDARD TEXT MESSAGES
                if messaging_event.get("message") and messaging_event["message"].get("text"):
                    message_text = messaging_event['message'].get('text', '').lower()

                    # HANDOVER LOGIC
                    if "#admin" in message_text:
                        paused_users.add(sender_id)
                        send_message(sender_id, "Bot disabled. An admin will assist you shortly.")
                    elif "#bot" in message_text:
                        paused_users.discard(sender_id)
                        send_message(sender_id, "Bot reactivated. How can I help you, ya?")
                    elif sender_id not in paused_users:
                        # KEYWORD-BASED
                        if any(word in message_text for word in ["hm", "price", "presyo"]):
                            send_message(sender_id, "ACE APPAREL PRICES:\n- Graphic Tees: P500\n- Hoodies: P1,200\n- Caps: P350")
                        elif any(word in message_text for word in ["available", "meron"]):
                            send_message(sender_id, "OUR CURRENT DROP:\n- Classic Ace Tee\n- Signature Hoodie\n- Trucker Caps\n\nLimited stocks only!")
                        else:
                            # AI FALLBACK
                            ai_reply = get_gemini_response(message_text)
                            send_message(sender_id, ai_reply)

    return make_response("EVENT_RECEIVED", 200)

def handle_payload(sender_id, payload):
    if payload == "PRICE_INFO":
        send_message(sender_id, "ACE APPAREL PRICES:\n- Graphic Tees: P500\n- Hoodies: P1,200\n- Caps: P350")
    elif payload == "HOW_TO_ORDER":
        send_message(sender_id, "HOW TO ORDER:\n1. Screenshot item.\n2. Send details.\n3. Wait for confirmation.")
    elif payload == "SHIPPING_INFO":
        send_message(sender_id, "SHIPPING:\n- Metro Manila: 2-3 days\n- Provinces: 5-7 days via J&T")
    elif payload == "ADMIN_REQUEST":
        paused_users.add(sender_id)
        send_message(sender_id, "We are redirecting you to our admin. Stand by.")

def get_gemini_response(prompt):
    try:
        context = "Official AI assistant of 'Ace Apparel'. Friendly, street-smart, Taglish. Brand-only info. Limit 20 words."
        response = model.generate_content(context + "\nUser: " + prompt)
        return response.text
    except Exception as e:
        return "We are handing you over to our admin to finalize your details."

def send_message(recipient_id, text):
    url = f"https://graph.facebook.com/v21.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    quick_replies = [
        {"content_type": "text", "title": "Prices/Products", "payload": "PRICE_INFO"},
        {"content_type": "text", "title": "How to order?", "payload": "HOW_TO_ORDER"},
        {"content_type": "text", "title": "Shipping Info", "payload": "SHIPPING_INFO"}
    ]
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text, "quick_replies": quick_replies}}
    requests.post(url, json=payload)

if __name__ == "__main__":
    app.run(port=5000)

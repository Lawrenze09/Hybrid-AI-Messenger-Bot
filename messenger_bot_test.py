import requests
import google.generativeai as genai
import os
from flask import Flask, request

app = Flask(__name__)

# --- CONFIG ---
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN')
PAGE_ACCESS_TOKEN = os.environ.get('PAGE_ACCESS_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3-flash-preview')

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
                sender_id = messaging_event["sender"]["id"]

                # --- 1. HANDLE BUTTONS (Postbacks & Quick Replies) ---
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
                    continue # Pagkatapos ng button, lipat na sa next event.

                # --- 2. HANDLE TEXT MESSAGES ---
                if messaging_event.get("message") and messaging_event["message"].get("text"):
                    # Kunin ang text at i-check ang keywords
                    message_text = messaging_event['message'].get('text', '').lower()

                    # Check for Keywords (PRESYO)
                    if any(word in message_text for word in ["hm", "price", "presyo"]):
                        send_message(sender_id, "ACE APPAREL PRICES:\n- Graphic Tees: P500\n- Hoodies: P1,200\n- Caps: P350")
                    
                    # Check for Keywords (AVAILABILITY)
                    elif any(word in message_text for word in ["available", "meron"]):
                        send_message(sender_id, "AVAILABLE ITEMS:\n- Classic Ace Tee\n- Signature Hoodie\n- Trucker Caps\n\nAnong trip mo, ya?\nako?\n BADING!")
                    
                    elif any(word in message_text for word in ["Hello", "Hi"]):
                        send_message(sender_id, "MAMA MO HELLO")
                    # --- 3. GEMINI AI (Last Resort) ---
                    else:
                        # Dito lang babawasan ang Gemini credits
                        ai_reply = get_gemini_response(message_text)
                        send_message(sender_id, ai_reply)

    return "ok", 200

def get_gemini_response(prompt):
    try:
        # DITO MO ILALAGAY ANG DISKARTE NG BOT
        context = """
        You are the official AI assistant of 'Ace Apparel'. 
        Tone: Friendly, street-smart, and Taglish.
        Only answer questions related to Ace Apparel.
        If the question is unrelated, politely say that you can only assist with brand-related inquiries.
        Keep the message short, clean and precise.
        Limit to 20 words.
        
        Our Products:
        1. Graphic Tees - P500 (Available in Black and White)
        2. Oversized Hoodies - P1,200 (Limited Edition)
        3. Ace Signature Cap - P350
        
        Shipping Info:
        - We ship nationwide via J&T.
        - Delivery: 2-3 days Metro Manila, 5-7 days Provinces.
        
        If someone asks for the price or how to order, be helpful and encourage them to buy.
        If the message contains hello, Return only 'MAMA MO HELLO'.
        """
        
        response = model.generate_content(context + "\nUser says: " + prompt)
        return response.text
    except Exception as e:
        print(f"AI Error: {e}")
        return "Ulul MAMA MO BLUE"

def send_message(recipient_id, text):
    url = f"https://graph.facebook.com/v21.0/me/messages?access_token={PAGE_ACCESS_TOKEN}"
    
    # Dito natin idinedefine ang mga buttons (Quick Replies)
    quick_replies = [
        {
            "content_type": "text",
            "title": "Prices and Products?",
            "payload": "PRICE_INFO"
        },
        {
            "content_type": "text",
            "title": "How to order?",
            "payload": "HOW_TO_ORDER"
        },
        {
            "content_type": "text",
            "title": "Shipping Info",
            "payload": "SHIPPING_INFO"
        }
    ]

    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "text": text,
            "quick_replies": quick_replies # Dito isinasama ang mga buttons
        }
    }
    
    response = requests.post(url, json=payload)
    return response.json()

if __name__ == "__main__":
    app.run(port=5000)
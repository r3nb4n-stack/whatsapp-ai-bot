import os
import json
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# =========================
# CONFIGURATION
# =========================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "my_verify_token")
WEBHOOK_VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN", "my_verify_token")
OPENAI_URL = "https://api.openai.com/v1/responses"
MEMORY_FILE = "memory.json"

# =========================
# PERSONAL AI INSTRUCTIONS
# =========================

SYSTEM_PROMPT = """
You are a personal AI assistant.

Be friendly, natural and conversational.
Give clear answers.
When explaining technical things, prefer simple step-by-step instructions.
Use the user's saved memory when relevant.
Never claim to remember something that isn't in the saved memory.
Do not reveal API keys, access tokens, passwords or other secrets.
"""

# =========================
# MEMORY
# =========================

def load_memory():
    try:
        with open(MEMORY_FILE, "r") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "name": "",
            "facts": [],
            "preferences": []
        }


def save_memory(memory):
    with open(MEMORY_FILE, "w") as file:
        json.dump(memory, file, indent=4)


memory = load_memory()

# =========================
# OPENAI
# =========================

def ask_ai(message):
    memory_text = json.dumps(memory, indent=2)

    prompt = f"""
{SYSTEM_PROMPT}

Saved user memory:
{memory_text}

User message:
{message}
"""

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "gpt-5.6-luna",
        "input": prompt
    }

    response = requests.post(
        OPENAI_URL,
        headers=headers,
        json=data,
        timeout=60
    )

    if response.status_code != 200:
        print("OpenAI Error:", response.text)
        return "Sorry, I couldn't process that right now."

    result = response.json()

    reply = ""

    for item in result.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                reply += content.get("text", "")

    return reply.strip() or "I couldn't generate a reply."

# =========================
# MEMORY COMMAND
# =========================

def process_message(message):

    global memory

    if message.lower().startswith("remember:"):
        fact = message[9:].strip()

        if fact:
            memory["facts"].append(fact)
            save_memory(memory)
            return "🧠 I'll remember that."

        return "Tell me what you want me to remember."

    reply = ask_ai(message)

    # Automatically remember simple personal facts
    lower = message.lower()

    if "my name is " in lower:
        name = message[lower.index("my name is ") + 11:].strip()

        if name:
            memory["name"] = name
            save_memory(memory)

    elif "i like " in lower:
        fact = message.strip()

        if fact not in memory["preferences"]:
            memory["preferences"].append(fact)
            save_memory(memory)

    return reply

# =========================
# WHATSAPP WEBHOOK
# =========================

@app.route("/webhook", methods=["GET"])
def verify_webhook():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    print("Webhook verification request received")
    print("Mode:", mode)
    print("Token received:", token is not None)
    print("Challenge received:", challenge is not None)

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({"status": "ignored"}), 200

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]

        messages = value.get("messages", [])

        if not messages:
            return jsonify({"status": "no message"}), 200

        message = messages[0]

        if message.get("type") != "text":
            return jsonify({"status": "ignored"}), 200

        sender = message["from"]
        text = message["text"]["body"]

        print(f"Message from {sender}: {text}")

        reply = process_message(text)

        send_whatsapp_message(sender, reply)

    except Exception as error:
        print("Webhook error:", error)

    return jsonify({"status": "ok"}), 200

# =========================
# SEND WHATSAPP MESSAGE
# =========================

def send_whatsapp_message(to, message):

    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("WhatsApp credentials are not configured yet.")
        print("Bot reply:", message)
        return

    url = (
        f"https://graph.facebook.com/v23.0/"
        f"{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {
            "body": message
        }
    }

    response = requests.post(
        url,
        headers=headers,
        json=data,
        timeout=30
    )

    if response.status_code != 200:
        print("WhatsApp Error:", response.text)

# =========================
# START SERVER
# =========================

if __name__ == "__main__":
    print("🤖 Personal WhatsApp AI Bot")
    print("🚀 Server starting...")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )

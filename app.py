import os
import json
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# =========================================================
# CONFIGURATION
# =========================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "my_verify_token")

OPENAI_URL = "https://api.openai.com/v1/responses"

MEMORY_FILE = "memory.json"

# =========================================================
# PERSONAL AI SETTINGS
# =========================================================

SYSTEM_PROMPT = """
You are a personal AI assistant running inside WhatsApp.

Your personality:
- Friendly
- Natural
- Conversational
- Helpful
- Slightly casual
- Do not sound like a robotic customer-support bot

You are talking to the user through WhatsApp.

Use the user's saved memory and conversation history when relevant.

Important:
- Do not invent memories.
- Do not claim to remember something unless it exists in saved memory.
- Do not reveal API keys, access tokens, passwords or other secrets.
- Keep normal WhatsApp replies reasonably concise.
- For technical questions, explain things simply and step-by-step.
- If the user asks something casual, respond naturally.
- You may use emojis occasionally, but don't overuse them.
"""

# =========================================================
# MEMORY
# =========================================================

def default_memory():
    return {
        "name": "",
        "facts": [],
        "preferences": [],
        "conversation_history": {}
    }


def load_memory():

    try:
        with open(MEMORY_FILE, "r") as file:
            memory = json.load(file)

        # Make sure older memory files still work
        if "name" not in memory:
            memory["name"] = ""

        if "facts" not in memory:
            memory["facts"] = []

        if "preferences" not in memory:
            memory["preferences"] = []

        if "conversation_history" not in memory:
            memory["conversation_history"] = {}

        return memory

    except (FileNotFoundError, json.JSONDecodeError):

        memory = default_memory()
        save_memory(memory)
        return memory


def save_memory(memory):

    with open(MEMORY_FILE, "w") as file:
        json.dump(memory, file, indent=4)


memory = load_memory()

# =========================================================
# CONVERSATION HISTORY
# =========================================================

MAX_HISTORY = 20


def get_history(user_id):

    history = memory["conversation_history"].get(user_id, [])

    return history


def save_history(user_id, user_message, assistant_message):

    if user_id not in memory["conversation_history"]:
        memory["conversation_history"][user_id] = []

    history = memory["conversation_history"][user_id]

    history.append({
        "user": user_message,
        "assistant": assistant_message
    })

    # Keep only the latest conversations
    memory["conversation_history"][user_id] = history[-MAX_HISTORY:]

    save_memory(memory)


# =========================================================
# BUILD AI PROMPT
# =========================================================

def build_prompt(user_id, message):

    memory_text = json.dumps(
        {
            "name": memory.get("name", ""),
            "facts": memory.get("facts", []),
            "preferences": memory.get("preferences", [])
        },
        indent=2
    )

    history = get_history(user_id)

    history_text = ""

    for item in history:

        history_text += (
            f"User: {item['user']}\n"
            f"Assistant: {item['assistant']}\n"
        )

    prompt = f"""
{SYSTEM_PROMPT}

=========================
SAVED PERSONAL MEMORY
=========================

{memory_text}

=========================
RECENT CONVERSATION
=========================

{history_text}

=========================
CURRENT USER MESSAGE
=========================

User: {message}

Respond naturally to the user.
"""

    return prompt


# =========================================================
# OPENAI
# =========================================================

def ask_ai(user_id, message):

    prompt = build_prompt(user_id, message)

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "gpt-5.6-luna",
        "input": prompt
    }

    try:

        response = requests.post(
            OPENAI_URL,
            headers=headers,
            json=data,
            timeout=60
        )

        if response.status_code != 200:

            print("OpenAI Error:")
            print(response.text)

            return "Sorry, I couldn't process that right now."

        result = response.json()

        reply = ""

        for item in result.get("output", []):

            for content in item.get("content", []):

                if content.get("type") == "output_text":

                    reply += content.get("text", "")

        reply = reply.strip()

        if not reply:
            return "I couldn't generate a reply."

        # Save conversation
        save_history(
            user_id,
            message,
            reply
        )

        return reply

    except Exception as error:

        print("OpenAI request error:", error)

        return "Sorry, something went wrong while processing your message."


# =========================================================
# AUTOMATIC MEMORY
# =========================================================

def process_message(user_id, message):

    global memory

    lower = message.lower().strip()

    # -----------------------------------------------------
    # MANUAL MEMORY
    # -----------------------------------------------------

    if lower.startswith("remember:"):

        fact = message[len("remember:"):].strip()

        if not fact:

            return "Tell me what you want me to remember."

        if fact not in memory["facts"]:

            memory["facts"].append(fact)
            save_memory(memory)

        return "🧠 I'll remember that."

    # -----------------------------------------------------
    # FORGET COMMAND
    # -----------------------------------------------------

    if lower.startswith("forget:"):

        fact = message[len("forget:"):].strip()

        if not fact:

            return "Tell me what you want me to forget."

        removed = False

        for item in memory["facts"][:]:

            if item.lower() == fact.lower():

                memory["facts"].remove(item)
                removed = True

        for item in memory["preferences"][:]:

            if item.lower() == fact.lower():

                memory["preferences"].remove(item)
                removed = True

        save_memory(memory)

        if removed:
            return "🧹 I've removed that from my saved memory."

        return "I couldn't find that in my saved memory."

    # -----------------------------------------------------
    # NAME
    # -----------------------------------------------------

    if "my name is " in lower:

        position = lower.index("my name is ")

        name = message[position + len("my name is "):].strip()

        if name:

            memory["name"] = name
            save_memory(memory)

    # -----------------------------------------------------
    # LIKE
    # -----------------------------------------------------

    elif lower.startswith("i like "):

        preference = message.strip()

        if preference not in memory["preferences"]:

            memory["preferences"].append(preference)
            save_memory(memory)

    # -----------------------------------------------------
    # DISLIKE
    # -----------------------------------------------------

    elif lower.startswith("i don't like "):

        preference = message.strip()

        if preference not in memory["preferences"]:

            memory["preferences"].append(preference)
            save_memory(memory)

    # -----------------------------------------------------
    # LOVE
    # -----------------------------------------------------

    elif lower.startswith("i love "):

        preference = message.strip()

        if preference not in memory["preferences"]:

            memory["preferences"].append(preference)
            save_memory(memory)

    # -----------------------------------------------------
    # GENERAL MESSAGE
    # -----------------------------------------------------

    return ask_ai(user_id, message)


# =========================================================
# WHATSAPP WEBHOOK VERIFICATION
# =========================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    print("Webhook verification request received")

    if mode == "subscribe" and token == VERIFY_TOKEN:

        return challenge, 200

    return "Verification failed", 403


# =========================================================
# WHATSAPP INCOMING MESSAGE
# =========================================================

@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():

    data = request.get_json(silent=True)

    if not data:

        return jsonify({
            "status": "ignored"
        }), 200

    try:

        entry = data["entry"][0]

        changes = entry["changes"][0]

        value = changes["value"]

        messages = value.get("messages", [])

        if not messages:

            return jsonify({
                "status": "no message"
            }), 200

        message = messages[0]

        # Only handle text messages
        if message.get("type") != "text":

            return jsonify({
                "status": "ignored"
            }), 200

        sender = message["from"]

        text = message["text"]["body"]

        print(
            f"Message from {sender}: {text}"
        )

        # Generate response
        reply = process_message(
            sender,
            text
        )

        # Send response
        send_whatsapp_message(
            sender,
            reply
        )

    except Exception as error:

        print("Webhook error:")
        print(error)

    return jsonify({
        "status": "ok"
    }), 200


# =========================================================
# SEND WHATSAPP MESSAGE
# =========================================================

def send_whatsapp_message(to, message):

    if not WHATSAPP_TOKEN:

        print("WhatsApp token is missing.")

        return

    if not PHONE_NUMBER_ID:

        print("Phone Number ID is missing.")

        return

    url = (
        f"https://graph.facebook.com/v25.0/"
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

    try:

        response = requests.post(
            url,
            headers=headers,
            json=data,
            timeout=30
        )

        if response.status_code != 200:

            print("WhatsApp Error:")
            print(response.text)

    except Exception as error:

        print("WhatsApp request error:")
        print(error)


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "online",
        "bot": "Personal WhatsApp AI Bot"
    })


# =========================================================
# START SERVER
# =========================================================

if __name__ == "__main__":

    print("🤖 Personal WhatsApp AI Bot")
    print("🚀 Server starting...")

    port = int(
        os.getenv("PORT", 5000)
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )"""

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

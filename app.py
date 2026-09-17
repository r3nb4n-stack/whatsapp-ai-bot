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
MAX_HISTORY = 20


# =========================================================
# AI PERSONALITY
# =========================================================

SYSTEM_PROMPT = """
You are a personal AI assistant running inside WhatsApp.

Be friendly, natural, conversational and helpful.

Your personality:
- Friendly
- Natural
- Casual
- Helpful
- Not robotic

Use the user's saved memory and recent conversation when relevant.

Important:
- Do not invent memories.
- Do not claim to remember something unless it exists in saved memory.
- Never reveal API keys, access tokens, passwords or other secrets.
- Keep normal WhatsApp replies reasonably concise.
- For technical questions, explain things simply and step-by-step.
- Use emojis occasionally, but do not overuse them.
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

        memory.setdefault("name", "")
        memory.setdefault("facts", [])
        memory.setdefault("preferences", [])
        memory.setdefault("conversation_history", {})

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

def get_history(user_id):

    return memory["conversation_history"].get(user_id, [])


def save_history(user_id, user_message, assistant_message):

    if user_id not in memory["conversation_history"]:

        memory["conversation_history"][user_id] = []

    memory["conversation_history"][user_id].append({
        "user": user_message,
        "assistant": assistant_message
    })

    memory["conversation_history"][user_id] = (
        memory["conversation_history"][user_id][-MAX_HISTORY:]
    )

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

    history_text = ""

    for item in get_history(user_id):

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

    if not OPENAI_API_KEY:

        return "OpenAI API key is not configured."

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "gpt-5.6-luna",
        "input": build_prompt(user_id, message)
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

        save_history(
            user_id,
            message,
            reply
        )

        return reply

    except Exception as error:

        print("OpenAI request error:")
        print(error)

        return "Sorry, something went wrong while processing your message."


# =========================================================
# PROCESS MESSAGE
# =========================================================

def process_message(user_id, message):

    global memory

    lower = message.lower().strip()


    # -----------------------------------------------------
    # REMEMBER
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
    # FORGET
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

        name = message[
            position + len("my name is "):
        ].strip()

        if name:

            memory["name"] = name
            save_memory(memory)


    # -----------------------------------------------------
    # LIKE
    # -----------------------------------------------------

    elif lower.startswith("i like "):

        if message not in memory["preferences"]:

            memory["preferences"].append(message)
            save_memory(memory)


    # -----------------------------------------------------
    # DON'T LIKE
    # -----------------------------------------------------

    elif lower.startswith("i don't like "):

        if message not in memory["preferences"]:

            memory["preferences"].append(message)
            save_memory(memory)


    # -----------------------------------------------------
    # LOVE
    # -----------------------------------------------------

    elif lower.startswith("i love "):

        if message not in memory["preferences"]:

            memory["preferences"].append(message)
            save_memory(memory)


    # -----------------------------------------------------
    # SEND TO AI
    # -----------------------------------------------------

    return ask_ai(
        user_id,
        message
    )


# =========================================================
# HOME / HEALTH CHECK
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "online",
        "bot": "Personal WhatsApp AI Bot"
    })


# =========================================================
# WHATSAPP WEBHOOK VERIFICATION
# =========================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")


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

        messages = value.get(
            "messages",
            []
        )


        if not messages:

            return jsonify({
                "status": "no message"
            }), 200


        message = messages[0]


        if message.get("type") != "text":

            return jsonify({
                "status": "ignored"
            }), 200


        sender = message["from"]

        text = message["text"]["body"]


        print(
            f"Message from {sender}: {text}"
        )


        reply = process_message(
            sender,
            text
        )


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

        "Authorization":
            f"Bearer {WHATSAPP_TOKEN}",

        "Content-Type":
            "application/json"
    }


    data = {

        "messaging_product":
            "whatsapp",

        "to":
            to,

        "type":
            "text",

        "text": {

            "body":
                message
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
# START SERVER
# =========================================================

if __name__ == "__main__":

    print("🤖 Personal WhatsApp AI Bot")

    print("🚀 Server starting...")


    port = int(
        os.getenv(
            "PORT",
            5000
        )
    )


    app.run(

        host="0.0.0.0",

        port=port,

        debug=False
    )

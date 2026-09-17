import os
import json
import requests
import psycopg

from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

@app.route("/privacy-policy")
def privacy_policy():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Privacy Policy - Ren Wp Ai Bot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>
    <body>
        <h1>Privacy Policy</h1>

        <p>This Privacy Policy explains how Ren Wp Ai Bot handles information.</p>

        <h2>Information We Collect</h2>
        <p>
            The bot may store information that users voluntarily provide,
            such as their name, preferences, and messages, in order to provide
            personalized responses.
        </p>

        <h2>How We Use Information</h2>
        <p>
            Information is used only to provide and improve the bot's
            conversational features and personalized responses.
        </p>

        <h2>Third-Party Services</h2>
        <p>
            The bot uses WhatsApp Cloud API and OpenAI services to process
            messages and generate responses.
        </p>

        <h2>Data Deletion</h2>
        <p>
            Users may request deletion of information stored by the bot
            by contacting the app owner.
        </p>

        <h2>Contact</h2>
        <p>
            For privacy-related questions or deletion requests, please
            contact the app owner through the associated WhatsApp service.
        </p>

        <p>Last updated: September 2026</p>
    </body>
    </html>
    """

# =========================================================
# CONFIG
# =========================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "my_verify_token")
DATABASE_URL = os.getenv("DATABASE_URL", "")

OPENAI_URL = "https://api.openai.com/v1/responses"

MAX_HISTORY = 20


# =========================================================
# AI PERSONALITY
# =========================================================

SYSTEM_PROMPT = """
You are a personal AI assistant running inside WhatsApp.

Be friendly, natural, casual and helpful.

Use the user's saved memory and recent conversation when relevant.

Important:
- Do not invent memories.
- Do not claim to remember something unless it exists in saved memory.
- Never reveal API keys, access tokens, passwords or database credentials.
- Keep normal WhatsApp replies reasonably concise.
- Explain technical things simply and step-by-step.
- Use emojis occasionally.
"""


# =========================================================
# DATABASE
# =========================================================

def get_connection():
    return psycopg.connect(DATABASE_URL)


def setup_database():

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT DEFAULT '',
                    facts JSONB DEFAULT '[]'::jsonb,
                    preferences JSONB DEFAULT '[]'::jsonb
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    assistant_message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

        conn.commit()


def get_user(user_id):

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT name, facts, preferences
                FROM users
                WHERE user_id = %s
            """, (user_id,))

            row = cur.fetchone()

            if row:
                return {
                    "name": row[0],
                    "facts": row[1] or [],
                    "preferences": row[2] or []
                }

            cur.execute("""
                INSERT INTO users
                (user_id, name, facts, preferences)
                VALUES (%s, '', '[]'::jsonb, '[]'::jsonb)
            """, (user_id,))

        conn.commit()

    return {
        "name": "",
        "facts": [],
        "preferences": []
    }


def update_user(user_id, name, facts, preferences):

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE users
                SET name = %s,
                    facts = %s::jsonb,
                    preferences = %s::jsonb
                WHERE user_id = %s
            """, (
                name,
                json.dumps(facts),
                json.dumps(preferences),
                user_id
            ))

        conn.commit()


# =========================================================
# CONVERSATION HISTORY
# =========================================================

def get_history(user_id):

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT user_message, assistant_message
                FROM conversations
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (user_id, MAX_HISTORY))

            rows = cur.fetchall()

    rows.reverse()

    return [
        {
            "user": row[0],
            "assistant": row[1]
        }
        for row in rows
    ]


def save_history(user_id, user_message, assistant_message):

    with get_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO conversations
                (user_id, user_message, assistant_message)
                VALUES (%s, %s, %s)
            """, (
                user_id,
                user_message,
                assistant_message
            ))

        conn.commit()


# =========================================================
# BUILD PROMPT
# =========================================================

def build_prompt(user_id, message):

    user = get_user(user_id)

    history = get_history(user_id)

    memory_text = json.dumps(user, indent=2)

    history_text = ""

    for item in history:

        history_text += (
            f"User: {item['user']}\n"
            f"Assistant: {item['assistant']}\n"
        )

    return f"""
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

        print("OpenAI Error:")
        print(error)

        return "Sorry, something went wrong."


# =========================================================
# PROCESS MESSAGE
# =========================================================

def process_message(user_id, message):

    user = get_user(user_id)

    lower = message.lower().strip()


    # REMEMBER
    if lower.startswith("remember:"):

        fact = message[len("remember:"):].strip()

        if not fact:
            return "Tell me what you want me to remember."

        if fact not in user["facts"]:
            user["facts"].append(fact)

        update_user(
            user_id,
            user["name"],
            user["facts"],
            user["preferences"]
        )

        return "🧠 I'll remember that."


    # FORGET
    if lower.startswith("forget:"):

        fact = message[len("forget:"):].strip()

        if not fact:
            return "Tell me what you want me to forget."

        removed = False

        for item in user["facts"][:]:

            if item.lower() == fact.lower():

                user["facts"].remove(item)
                removed = True

        for item in user["preferences"][:]:

            if item.lower() == fact.lower():

                user["preferences"].remove(item)
                removed = True

        update_user(
            user_id,
            user["name"],
            user["facts"],
            user["preferences"]
        )

        if removed:
            return "🧹 I've removed that from my saved memory."

        return "I couldn't find that in my saved memory."


    # NAME
    if "my name is " in lower:

        position = lower.index("my name is ")

        name = message[
            position + len("my name is "):
        ].strip()

        if name:

            user["name"] = name

            update_user(
                user_id,
                user["name"],
                user["facts"],
                user["preferences"]
            )


    # LIKE
    elif lower.startswith("i like "):

        if message not in user["preferences"]:

            user["preferences"].append(message)

            update_user(
                user_id,
                user["name"],
                user["facts"],
                user["preferences"]
            )


    # LOVE
    elif lower.startswith("i love "):

        if message not in user["preferences"]:

            user["preferences"].append(message)

            update_user(
                user_id,
                user["name"],
                user["facts"],
                user["preferences"]
            )


    # DON'T LIKE
    elif lower.startswith("i don't like "):

        if message not in user["preferences"]:

            user["preferences"].append(message)

            update_user(
                user_id,
                user["name"],
                user["facts"],
                user["preferences"]
            )


    return ask_ai(user_id, message)


# =========================================================
# HOME
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "online",
        "bot": "Personal WhatsApp AI Bot"
    })


# =========================================================
# WEBHOOK VERIFICATION
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
# WHATSAPP WEBHOOK
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
# START
# =========================================================

if __name__ == "__main__":

    print("🤖 Personal WhatsApp AI Bot")

    print("🚀 Starting server...")

    if DATABASE_URL:

        try:

            setup_database()

            print("🐘 PostgreSQL connected.")

        except Exception as error:

            print("Database connection error:")
            print(error)

    else:

        print("DATABASE_URL is missing.")


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

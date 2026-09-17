import os
import json
import logging

import requests
import psycopg
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

MODEL = "gpt-5.6-luna"

WA_URL = f"https://graph.facebook.com/v25.0/{PHONE_NUMBER_ID}/messages"
OPENAI_URL = "https://api.openai.com/v1/responses"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(__name__)


# =========================
# DATABASE
# =========================

def db():
    return psycopg.connect(DATABASE_URL)


def setup_db():
    try:
        with db() as conn:
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

        log.info("Database ready")

    except Exception:
        log.exception("Database setup failed")


def get_user(uid):

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT name,facts,preferences FROM users WHERE user_id=%s",
                (uid,)
            )

            row = cur.fetchone()

            if row:
                return {
                    "name": row[0] or "",
                    "facts": row[1] or [],
                    "preferences": row[2] or []
                }

            cur.execute(
                "INSERT INTO users(user_id) VALUES(%s)",
                (uid,)
            )

        conn.commit()

    return {
        "name": "",
        "facts": [],
        "preferences": []
    }


def save_user(uid, user):

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO users(
                    user_id,
                    name,
                    facts,
                    preferences
                )
                VALUES(%s,%s,%s::jsonb,%s::jsonb)

                ON CONFLICT(user_id)
                DO UPDATE SET
                    name=EXCLUDED.name,
                    facts=EXCLUDED.facts,
                    preferences=EXCLUDED.preferences
            """, (
                uid,
                user["name"],
                json.dumps(user["facts"]),
                json.dumps(user["preferences"])
            ))

        conn.commit()


def get_history(uid):

    try:

        with db() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    SELECT user_message,assistant_message
                    FROM conversations
                    WHERE user_id=%s
                    ORDER BY id DESC
                    LIMIT 20
                """, (uid,))

                rows = cur.fetchall()

        return list(reversed(rows))

    except Exception:

        log.exception("History read failed")

        return []


def save_chat(uid, user_msg, bot_msg):

    try:

        with db() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    INSERT INTO conversations(
                        user_id,
                        user_message,
                        assistant_message
                    )
                    VALUES(%s,%s,%s)
                """, (
                    uid,
                    user_msg,
                    bot_msg
                ))

            conn.commit()

    except Exception:

        log.exception("Conversation save failed")


# =========================
# MEMORY
# =========================

def memory_command(uid, msg):

    user = get_user(uid)
    low = msg.lower().strip()

    # remember:
    if low.startswith("remember:"):

        fact = msg.split(":", 1)[1].strip()

        if not fact:
            return "Tell me what you want me to remember."

        if fact not in user["facts"]:
            user["facts"].append(fact)

        save_user(uid, user)

        return "Got it — I'll remember that."

    # forget:
    if low.startswith("forget:"):

        target = msg.split(":", 1)[1].strip().lower()

        if not target:
            return "Tell me what you want me to forget."

        old_count = (
            len(user["facts"]) +
            len(user["preferences"])
        )

        user["facts"] = [
            x for x in user["facts"]
            if target not in str(x).lower()
        ]

        user["preferences"] = [
            x for x in user["preferences"]
            if target not in str(x).lower()
        ]

        save_user(uid, user)

        new_count = (
            len(user["facts"]) +
            len(user["preferences"])
        )

        if old_count != new_count:
            return "Okay, I've removed that from your stored memory."

        return "I couldn't find that in your stored memory."

    # name
    if low.startswith("my name is "):

        name = msg[len("my name is "):].strip()

        if name:

            user["name"] = name

            save_user(uid, user)

            return (
                f"Nice to meet you, {name}! "
                "I'll remember your name."
            )

    # preferences
    if low.startswith("i like ") or \
       low.startswith("i love ") or \
       low.startswith("i don't like "):

        if low.startswith("i don't like "):

            preference = msg[len("i don't like "):].strip()
            item = f"doesn't like {preference}"

        elif low.startswith("i love "):

            preference = msg[len("i love "):].strip()
            item = f"loves {preference}"

        else:

            preference = msg[len("i like "):].strip()
            item = f"likes {preference}"

        if preference:

            if item not in user["preferences"]:
                user["preferences"].append(item)

            save_user(uid, user)

            return "Got it — I'll remember that."

    # memory question
    memory_questions = {
        "what do you remember",
        "what do you remember about me",
        "what do you know",
        "what do you know about me"
    }

    if low in memory_questions:

        parts = []

        if user["name"]:
            parts.append(
                "Name: " + user["name"]
            )

        if user["facts"]:

            parts.append(
                "Facts:\n" +
                "\n".join(
                    "- " + str(x)
                    for x in user["facts"]
                )
            )

        if user["preferences"]:

            parts.append(
                "Preferences:\n" +
                "\n".join(
                    "- " + str(x)
                    for x in user["preferences"]
                )
            )

        if parts:
            return "\n\n".join(parts)

        return "I don't have any saved information about you yet."

    return None


# =========================
# OPENAI
# =========================

def extract_text(data):

    if (
        isinstance(data.get("output_text"), str)
        and data["output_text"].strip()
    ):
        return data["output_text"].strip()

    texts = []

    for item in data.get("output", []):

        if not isinstance(item, dict):
            continue

        for part in item.get("content", []):

            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and part.get("text")
            ):
                texts.append(part["text"])

    return "\n".join(texts).strip()


def ai_reply(uid, msg):

    user = get_user(uid)
    recent = get_history(uid)

    conversation = "\n".join(
        f"User: {u}\nAssistant: {a}"
        for u, a in recent
    )

    prompt = f"""
Stored memory:

{json.dumps(user, ensure_ascii=False)}

Recent conversation:

{conversation}

Current user message:

{msg}
"""

    payload = {
        "model": MODEL,
        "instructions": (
            "You are a friendly personal AI assistant "
            "communicating through WhatsApp. "
            "Use only the supplied memory and do not invent "
            "memories. Keep normal replies concise."
        ),
        "input": prompt
    }

    try:

        log.info("Calling OpenAI")

        response = requests.post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=90
        )

        log.info(
            "OpenAI status: %s",
            response.status_code
        )

        if response.status_code >= 400:

            log.error(
                "OpenAI error: %s",
                response.text[:2000]
            )

            return (
                "Sorry, I'm having trouble connecting "
                "to my AI service right now."
            )

        text = extract_text(response.json())

        if text:
            return text

        log.error("OpenAI returned no text")

        return "I received an empty AI response. Please try again."

    except Exception:

        log.exception("OpenAI request failed")

        return (
            "Something went wrong while generating "
            "my reply. Please try again."
        )


# =========================
# WHATSAPP
# =========================

def send_whatsapp(uid, msg):

    try:

        response = requests.post(
            WA_URL,
            headers={
                "Authorization": f"Bearer {WHATSAPP_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "messaging_product": "whatsapp",
                "to": uid,
                "type": "text",
                "text": {
                    "preview_url": False,
                    "body": msg
                }
            },
            timeout=30
        )

        log.info(
            "WhatsApp send status: %s",
            response.status_code
        )

        if response.status_code >= 400:

            log.error(
                "WhatsApp send error: %s",
                response.text[:2000]
            )

            return False

        return True

    except Exception:

        log.exception("WhatsApp send failed")

        return False


# =========================
# HOME
# =========================

@app.route("/")
def home():

    return jsonify({
        "status": "online",
        "bot": "Personal WhatsApp AI Bot"
    })


# =========================
# PRIVACY POLICY
# =========================

@app.route("/privacy-policy")
def privacy():

    return """
    <html>
    <head>
        <title>Privacy Policy - Ren Wp Ai Bot</title>
    </head>
    <body>

        <h1>Privacy Policy</h1>

        <p>
        Ren Wp Ai Bot may store messages, names and preferences
        to provide personalized responses.
        </p>

        <p>
        The bot uses WhatsApp Cloud API and OpenAI services.
        </p>

        <p>
        Users may request deletion of stored information by
        contacting the app owner.
        </p>

        <p>
        Last updated: September 2026
        </p>

    </body>
    </html>
    """


# =========================
# WEBHOOK VERIFICATION
# =========================

@app.route("/webhook", methods=["GET"])
def verify():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if (
        mode == "subscribe"
        and token == VERIFY_TOKEN
    ):

        log.info("Webhook verification successful")

        return challenge, 200

    log.warning("Webhook verification failed")

    return "Verification failed", 403


# =========================
# WEBHOOK RECEIVER
# =========================

@app.route("/webhook", methods=["POST"])
def webhook():

    log.info("========== WEBHOOK POST ==========")

    try:

        data = request.get_json(
            silent=True
        ) or {}

        log.info(
            "Webhook object: %s",
            data.get("object")
        )

        for entry in data.get("entry", []):

            for change in entry.get("changes", []):

                value = change.get("value", {})

                messages = value.get(
                    "messages",
                    []
                )

                for message in messages:

                    message_type = message.get("type")

                    log.info(
                        "Message type: %s",
                        message_type
                    )

                    if message_type != "text":

                        log.info(
                            "Ignoring non-text message"
                        )

                        continue

                    uid = message.get("from")

                    text = message.get(
                        "text",
                        {}
                    ).get(
                        "body",
                        ""
                    ).strip()

                    if not uid or not text:
                        continue

                    log.info(
                        "Received WhatsApp message: %d characters",
                        len(text)
                    )

                    # Memory command
                    try:

                        reply = memory_command(
                            uid,
                            text
                        )

                    except Exception:

                        log.exception(
                            "Memory processing failed"
                        )

                        reply = None

                    # AI reply
                    if reply is None:

                        reply = ai_reply(
                            uid,
                            text
                        )

                    # Save conversation
                    save_chat(
                        uid,
                        text,
                        reply
                    )

                    # Send WhatsApp reply
                    success = send_whatsapp(
                        uid,
                        reply
                    )

                    if success:

                        log.info(
                            "WhatsApp reply sent successfully"
                        )

                    else:

                        log.error(
                            "WhatsApp reply failed"
                        )

        log.info("========== WEBHOOK DONE ==========")

        return "EVENT_RECEIVED", 200

    except Exception:

        log.exception(
            "CRITICAL WEBHOOK ERROR"
        )

        return "EVENT_RECEIVED", 200


# =========================
# START SERVER
# =========================

if __name__ == "__main__":

    log.info(
        "Starting Personal WhatsApp AI Bot"
    )

    required = [
        VERIFY_TOKEN,
        WHATSAPP_TOKEN,
        PHONE_NUMBER_ID,
        OPENAI_API_KEY,
        DATABASE_URL
    ]

    if not all(required):

        log.error(
            "One or more environment variables are missing"
        )

    setup_db()

    port = int(
        os.getenv(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
                )

import os
import json
import logging
from datetime import datetime

import requests
import psycopg
from flask import Flask, request, jsonify
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

OPENAI_MODEL = "gpt-5.6-luna"

WHATSAPP_API_URL = (
    f"https://graph.facebook.com/v25.0/"
    f"{PHONE_NUMBER_ID}/messages"
)

OPENAI_API_URL = "https://api.openai.com/v1/responses"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# ENVIRONMENT CHECK
# ============================================================

def check_environment():
    required = {
        "VERIFY_TOKEN": VERIFY_TOKEN,
        "WHATSAPP_TOKEN": WHATSAPP_TOKEN,
        "PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "DATABASE_URL": DATABASE_URL,
    }

    missing = [name for name, value in required.items() if not value]

    if missing:
        logger.error(
            "Missing environment variables: %s",
            ", ".join(missing)
        )
        return False

    logger.info("Environment variables OK")
    return True


# ============================================================
# DATABASE
# ============================================================

def get_db():
    return psycopg.connect(DATABASE_URL)


def setup_database():
    try:
        with get_db() as conn:
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

        logger.info("Database setup complete")

    except Exception:
        logger.exception("Database setup failed")


def get_user(user_id):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT name, facts, preferences
                    FROM users
                    WHERE user_id = %s
                    """,
                    (user_id,)
                )

                row = cur.fetchone()

                if row:
                    name, facts, preferences = row

                    return {
                        "name": name or "",
                        "facts": facts or [],
                        "preferences": preferences or []
                    }

                cur.execute(
                    """
                    INSERT INTO users
                    (user_id, name, facts, preferences)
                    VALUES (%s, %s, %s::jsonb, %s::jsonb)
                    """,
                    (
                        user_id,
                        "",
                        json.dumps([]),
                        json.dumps([])
                    )
                )

                conn.commit()

                return {
                    "name": "",
                    "facts": [],
                    "preferences": []
                }

    except Exception:
        logger.exception("Failed to get/create user")
        raise


def save_user(user_id, name, facts, preferences):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    INSERT INTO users
                    (user_id, name, facts, preferences)
                    VALUES (%s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        name = EXCLUDED.name,
                        facts = EXCLUDED.facts,
                        preferences = EXCLUDED.preferences
                    """,
                    (
                        user_id,
                        name,
                        json.dumps(facts),
                        json.dumps(preferences)
                    )
                )

            conn.commit()

    except Exception:
        logger.exception("Failed to save user memory")
        raise


def get_conversation_history(user_id, limit=20):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT user_message, assistant_message
                    FROM conversations
                    WHERE user_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (user_id, limit)
                )

                rows = cur.fetchall()

        rows.reverse()

        return rows

    except Exception:
        logger.exception("Failed to get conversation history")
        return []


def save_conversation(user_id, user_message, assistant_message):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    INSERT INTO conversations
                    (user_id, user_message, assistant_message)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        user_id,
                        user_message,
                        assistant_message
                    )
                )

            conn.commit()

    except Exception:
        logger.exception("Failed to save conversation")


# ============================================================
# MEMORY COMMANDS
# ============================================================

def remember_fact(user_id, fact):
    user = get_user(user_id)

    facts = user["facts"]

    if fact not in facts:
        facts.append(fact)

    save_user(
        user_id,
        user["name"],
        facts,
        user["preferences"]
    )


def forget_fact(user_id, text):
    user = get_user(user_id)

    original_facts = user["facts"]
    original_preferences = user["preferences"]

    text_lower = text.lower().strip()

    facts = [
        fact for fact in original_facts
        if text_lower not in str(fact).lower()
    ]

    preferences = [
        pref for pref in original_preferences
        if text_lower not in str(pref).lower()
    ]

    save_user(
        user_id,
        user["name"],
        facts,
        preferences
    )

    removed = (
        len(original_facts) != len(facts)
        or len(original_preferences) != len(preferences)
    )

    return removed


def process_memory_command(user_id, message):
    """
    Returns a reply if the message is a memory command.
    Otherwise returns None.
    """

    text = message.strip()
    lower = text.lower()

    user = get_user(user_id)

    # --------------------------------------------------------
    # remember:
    # --------------------------------------------------------

    if lower.startswith("remember:"):

        fact = text[len("remember:"):].strip()

        if not fact:
            return "Tell me what you want me to remember."

        remember_fact(user_id, fact)

        return f"Got it — I'll remember that."

    # --------------------------------------------------------
    # forget:
    # --------------------------------------------------------

    if lower.startswith("forget:"):

        fact = text[len("forget:"):].strip()

        if not fact:
            return "Tell me what you want me to forget."

        removed = forget_fact(user_id, fact)

        if removed:
            return "Okay, I've removed that from your stored memory."

        return "I couldn't find that in your stored memory."

    # --------------------------------------------------------
    # my name is ...
    # --------------------------------------------------------

    if lower.startswith("my name is "):

        name = text[len("my name is "):].strip()

        if name:
            save_user(
                user_id,
                name,
                user["facts"],
                user["preferences"]
            )

            return f"Nice to meet you, {name}! I'll remember your name."

    # --------------------------------------------------------
    # I like ...
    # --------------------------------------------------------

    if lower.startswith("i like "):

        preference = text[len("i like "):].strip()

        if preference:
            preferences = user["preferences"]

            if preference not in preferences:
                preferences.append(preference)

            save_user(
                user_id,
                user["name"],
                user["facts"],
                preferences
            )

            return f"Got it — I'll remember that you like {preference}."

    # --------------------------------------------------------
    # I love ...
    # --------------------------------------------------------

    if lower.startswith("i love "):

        preference = text[len("i love "):].strip()

        if preference:
            preferences = user["preferences"]

            if preference not in preferences:
                preferences.append(preference)

            save_user(
                user_id,
                user["name"],
                user["facts"],
                preferences
            )

            return f"Got it — I'll remember that you love {preference}."

    # --------------------------------------------------------
    # I don't like ...
    # --------------------------------------------------------

    if lower.startswith("i don't like "):

        preference = text[len("i don't like "):].strip()

        if preference:
            preferences = user["preferences"]

            item = f"doesn't like {preference}"

            if item not in preferences:
                preferences.append(item)

            save_user(
                user_id,
                user["name"],
                user["facts"],
                preferences
            )

            return f"Got it — I'll remember that you don't like {preference}."

    # --------------------------------------------------------
    # What do you remember?
    # --------------------------------------------------------

    memory_questions = [
        "what do you remember about me",
        "what do you remember",
        "what do you know about me",
        "what do you know"
    ]

    if lower in memory_questions:

        user = get_user(user_id)

        result = []

        if user["name"]:
            result.append(f"Name: {user['name']}")

        if user["facts"]:
            result.append(
                "Facts:\n" +
                "\n".join(
                    f"- {fact}"
                    for fact in user["facts"]
                )
            )

        if user["preferences"]:
            result.append(
                "Preferences:\n" +
                "\n".join(
                    f"- {pref}"
                    for pref in user["preferences"]
                )
            )

        if not result:
            return "I don't have any saved information about you yet."

        return "\n\n".join(result)

    return None


# ============================================================
# OPENAI
# ============================================================

def extract_openai_text(data):
    """
    Extract generated text from the Responses API JSON.
    """

    # Some API responses may provide output_text directly.
    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()

    output = data.get("output", [])

    texts = []

    for item in output:

        if not isinstance(item, dict):
            continue

        content = item.get("content", [])

        if not isinstance(content, list):
            continue

        for part in content:

            if not isinstance(part, dict):
                continue

            if part.get("type") == "output_text":

                text = part.get("text", "")

                if text:
                    texts.append(text)

    return "\n".join(texts).strip()


def generate_ai_reply(user_id, user_message):
    user = get_user(user_id)

    history = get_conversation_history(user_id, limit=20)

    system_prompt = """
You are a personal AI assistant communicating through WhatsApp.

Be helpful, friendly, natural, and conversational.

You have access to stored memory about the user.
Use it when relevant, but do not mention internal databases,
PostgreSQL, API keys, webhooks, or implementation details unless
the user specifically asks about the bot's technical implementation.

Do not claim to remember something unless it appears in the
provided memory.

Keep normal WhatsApp replies reasonably concise.

If the user asks something technical, explain it clearly and
step-by-step when appropriate.
"""

    memory_text = f"""
Stored user information:

Name:
{user["name"] or "Not stored"}

Facts:
{json.dumps(user["facts"], ensure_ascii=False)}

Preferences:
{json.dumps(user["preferences"], ensure_ascii=False)}
"""

    conversation_text = ""

    if history:
        conversation_text = "\nRecent conversation:\n"

        for old_user, old_assistant in history:
            conversation_text += (
                f"User: {old_user}\n"
                f"Assistant: {old_assistant}\n"
            )

    full_input = f"""
{memory_text}

{conversation_text}

Current user message:
{user_message}
"""

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": OPENAI_MODEL,
        "instructions": system_prompt,
        "input": full_input
    }

    logger.info("Calling OpenAI Responses API")

    try:
        response = requests.post(
            OPENAI_API_URL,
            headers=headers,
            json=payload,
            timeout=90
        )

        logger.info(
            "OpenAI HTTP status: %s",
            response.status_code
        )

        if response.status_code != 200:

            logger.error(
                "OpenAI API error: %s",
                response.text[:2000]
            )

            return (
                "Sorry, I'm having trouble connecting to my AI service "
                "right now. Please try again in a moment."
            )

        data = response.json()

        reply = extract_openai_text(data)

        if not reply:

            logger.error(
                "OpenAI returned no text. Response keys: %s",
                list(data.keys())
            )

            return (
                "I received an empty response from the AI. "
                "Please try again."
            )

        logger.info("OpenAI response received successfully")

        return reply

    except requests.Timeout:

        logger.exception("OpenAI request timed out")

        return (
            "The AI took too long to respond. "
            "Please try again."
        )

    except Exception:

        logger.exception("Unexpected OpenAI error")

        return (
            "Something went wrong while generating my reply. "
            "Please try again."
        )


# ============================================================
# WHATSAPP
# ============================================================

def send_whatsapp_message(recipient, message):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": message
        }
    }

    logger.info("Sending WhatsApp reply")

    try:
        response = requests.post(
            WHATSAPP_API_URL,
            headers=headers,
            json=payload,
            timeout=30
        )

        logger.info(
            "WhatsApp API status: %s",
            response.status_code
        )

        if response.status_code >= 400:

            logger.error(
                "WhatsApp API error: %s",
                response.text[:2000]
            )

            return False

        return True

    except requests.Timeout:

        logger.exception("WhatsApp API request timed out")

        return False

    except Exception:

        logger.exception("Unexpected WhatsApp sending error")

        return False


# ============================================================
# WHATSAPP WEBHOOK
# ============================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    logger.info("Webhook verification request received")

    if mode == "subscribe" and token == VERIFY_TOKEN:

        logger.info("Webhook verification successful")

        return challenge, 200

    logger.warning("Webhook verification failed")

    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def webhook():

    logger.info("========================================")
    logger.info("Webhook POST received")
    logger.info("========================================")

    try:

        data = request.get_json(silent=True)

        if not data:

            logger.warning("Webhook contained no JSON")

            return "EVENT_RECEIVED", 200

        logger.info(
            "Webhook object: %s",
            data.get("object")
        )

        entries = data.get("entry", [])

        if not entries:

            logger.info("No entries in webhook")

            return "EVENT_RECEIVED", 200

        for entry in entries:

            changes = entry.get("changes", [])

            for change in changes:

                value = change.get("value", {})

                messages = value.get("messages", [])

                if not messages:

                    logger.info(
                        "Webhook event has no messages "
                        "(probably status/update event)"
                    )

                    continue

                for message in messages:

                    message_type = message.get("type")

                    logger.info(
                        "WhatsApp message type: %s",
                        message_type
                    )

                    # ------------------------------------------------
                    # Only process text messages
                    # ------------------------------------------------

                    if message_type != "text":

                        logger.info(
                            "Ignoring non-text message: %s",
                            message_type
                        )

                        continue

                    sender = message.get("from")

                    text_data = message.get("text", {})

                

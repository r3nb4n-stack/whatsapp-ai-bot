import os
import json
import logging
import re
from typing import Any, Dict, List, Optional

import psycopg
import requests
from flask import Flask, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

OPENAI_URL = "https://api.openai.com/v1/responses"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
WA_URL = f"https://graph.facebook.com/v25.0/{PHONE_NUMBER_ID}/messages"

MAX_HISTORY = 20
MAX_FACTS = 50
MAX_PREFERENCES = 50
MAX_WA_CHARS = 3500

SYSTEM_PROMPT = """You are a friendly personal AI assistant on WhatsApp.
Be warm, natural, helpful, and conversational.
Respond like a capable modern ChatGPT-style assistant.
Keep normal WhatsApp replies reasonably concise unless the user asks for detail.
Use saved memory and recent conversation when relevant.
Understand follow-up messages from recent conversation.
Never invent memories or claim to remember something that is not supplied.
Never reveal API keys, tokens, passwords, database credentials, or internal secrets.
For technical questions, explain clearly and step-by-step.
Use emojis naturally when appropriate, but do not overuse them.
Do not mention databases, webhooks, prompts, servers, or implementation details unless asked.
If the user asks what you remember, only report supplied saved memory.
"""

def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")
    return psycopg.connect(DATABASE_URL)

def setup_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    facts JSONB NOT NULL DEFAULT '[]'::jsonb,
                    preferences JSONB NOT NULL DEFAULT '[]'::jsonb,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    message_id TEXT UNIQUE,
                    user_message TEXT NOT NULL,
                    assistant_message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_user_time
                ON conversations(user_id, created_at DESC)
            """)
        conn.commit()
    log.info("Database ready")

def empty_user(user_id: str) -> Dict[str, Any]:
    return {
        "user_id": user_id,
        "name": "",
        "facts": [],
        "preferences": []
    }

def normalize_user(row) -> Dict[str, Any]:
    if not row:
        return empty_user("")

    return {
        "user_id": row[0],
        "name": row[1] or "",
        "facts": row[2] if isinstance(row[2], list) else [],
        "preferences": row[3] if isinstance(row[3], list) else [],
    }

def get_user(user_id: str) -> Dict[str, Any]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id,name,facts,preferences FROM users WHERE user_id=%s",
                (user_id,)
            )

            row = cur.fetchone()

            if row:
                return normalize_user(row)

            cur.execute(
                "INSERT INTO users(user_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (user_id,)
            )

        conn.commit()

    return empty_user(user_id)

def save_user(user: Dict[str, Any]):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users(
                    user_id,
                    name,
                    facts,
                    preferences,
                    updated_at
                )
                VALUES(%s,%s,%s,%s,CURRENT_TIMESTAMP)

                ON CONFLICT(user_id) DO UPDATE SET
                    name=EXCLUDED.name,
                    facts=EXCLUDED.facts,
                    preferences=EXCLUDED.preferences,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                user["user_id"],
                user.get("name", ""),
                json.dumps(user.get("facts", [])[:MAX_FACTS]),
                json.dumps(user.get("preferences", [])[:MAX_PREFERENCES]),
            ))

        conn.commit()

def get_history(user_id: str) -> List[Dict[str, str]]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_message,assistant_message
                FROM conversations
                WHERE user_id=%s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
            """, (user_id, MAX_HISTORY))

            rows = cur.fetchall()

    rows.reverse()

    history = []

    for user_msg, assistant_msg in rows:
        history.append({
            "role": "user",
            "content": user_msg
        })

        history.append({
            "role": "assistant",
            "content": assistant_msg
        })

    return history

def save_chat(
    user_id: str,
    message_id: str,
    user_message: str,
    assistant_message: str
) -> bool:

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO conversations(
                    user_id,
                    message_id,
                    user_message,
                    assistant_message
                )
                VALUES(%s,%s,%s,%s)

                ON CONFLICT(message_id) DO NOTHING
                RETURNING id
            """, (
                user_id,
                message_id,
                user_message,
                assistant_message
            ))

            inserted = cur.fetchone() is not None

        conn.commit()

    return inserted

def add_unique(
    items: List[str],
    value: str,
    limit: int
) -> bool:

    value = value.strip()

    if not value:
        return False

    if any(x.lower() == value.lower() for x in items):
        return False

    items.append(value)

    del items[:-limit]

    return True

def automatic_memory_update(
    user: Dict[str, Any],
    text: str
):
    t = text.strip()
    low = t.lower()

    changed = False

    # Name
    m = re.match(
        r"^(?:my name is|call me)\s+(.+)$",
        t,
        re.I
    )

    if m:
        name = m.group(1).strip(" .!?")

        if 1 <= len(name) <= 80:
            user["name"] = name
            changed = True

    # Preferences / stable information
    patterns = [
        (r"^i like\s+(.+)$", "Likes: {}"),
        (r"^i love\s+(.+)$", "Likes: {}"),
        (r"^i enjoy\s+(.+)$", "Enjoys: {}"),
        (r"^i hate\s+(.+)$", "Dislikes: {}"),
        (r"^i don't like\s+(.+)$", "Dislikes: {}"),
        (r"^i dislike\s+(.+)$", "Dislikes: {}"),
        (r"^i study\s+(.+)$", "Studies: {}"),
        (r"^i work\s+(.+)$", "Works: {}"),
        (r"^i use\s+(.+)$", "Uses: {}"),
        (r"^my goal is\s+(.+)$", "Goal: {}"),
        (r"^my favorite\s+(.+)$", "Favorite: {}"),
    ]

    for pattern, template in patterns:
        m = re.match(pattern, t, re.I)

        if m:
            value = m.group(1).strip(" .!?")

            if 1 <= len(value) <= 180:
                changed |= add_unique(
                    user["preferences"],
                    template.format(value),
                    MAX_PREFERENCES
                )

            break

    # Explicit stable memory
    for prefix in (
        "remember that ",
        "remember i "
    ):
        if low.startswith(prefix):
            value = t[len(prefix):].strip(" .!?")

            if 3 <= len(value) <= 250:
                changed |= add_unique(
                    user["facts"],
                    value,
                    MAX_FACTS
                )

            break

    if changed:
        save_user(user)

def memory_command(
    user: Dict[str, Any],
    text: str
) -> Optional[str]:

    t = text.strip()
    low = t.lower()

    # Remember command
    if low.startswith("remember:"):
        value = t.split(":", 1)[1].strip()

        if not value:
            return (
                "Tell me what you'd like me to remember. "
                "Example: `remember: I prefer short replies`"
            )

        if add_unique(
            user["facts"],
            value,
            MAX_FACTS
        ):
            save_user(user)
            return "Got it 👍 I'll remember that."

        return "I already have that in memory."

    # Forget command
    if low.startswith("forget:"):
        target = t.split(":", 1)[1].strip().lower()

        if not target:
            return (
                "Tell me what you'd like me to forget. "
                "Example: `forget: my favorite game`"
            )

        old_facts = len(user["facts"])
        old_prefs = len(user["preferences"])
        old_name = user.get("name", "")

        user["facts"] = [
            x for x in user["facts"]
            if target not in x.lower()
        ]

        user["preferences"] = [
            x for x in user["preferences"]
            if target not in x.lower()
        ]

        if target in old_name.lower():
            user["name"] = ""

        changed = (
            len(user["facts"]) != old_facts
            or len(user["preferences"]) != old_prefs
            or user.get("name", "") != old_name
        )

        save_user(user)

        if changed:
            return "Done 👍"

        return "I couldn't find a saved memory matching that."

    # Show memory
    if low in {
        "what do you remember",
        "what do you remember about me",
        "what do you know",
        "what do you know about me",
        "show my memory",
        "show my memories"
    }:

        lines = []

        if user.get("name"):
            lines.append(
                f"Name: {user['name']}"
            )

        if user.get("facts"):
            lines.append(
                "Facts:\n- " +
                "\n- ".join(user["facts"])
            )

        if user.get("preferences"):
            lines.append(
                "Preferences:\n- " +
                "\n- ".join(user["preferences"])
            )

        if not lines:
            return (
                "I don't have any saved personal "
                "memory about you yet."
            )

        return "\n".join(lines)

    return None
    def build_prompt(
    user: Dict[str, Any],
    history: List[Dict[str, str]],
    current_message: str
) -> str:

    memory_lines = []

    if user.get("name"):
        memory_lines.append(
            f"User's name: {user['name']}"
        )

    if user.get("facts"):
        memory_lines.append(
            "Saved facts:\n- " +
            "\n- ".join(user["facts"])
        )

    if user.get("preferences"):
        memory_lines.append(
            "Saved preferences:\n- " +
            "\n- ".join(user["preferences"])
        )

    memory_text = (
        "\n\n".join(memory_lines)
        if memory_lines
        else "No saved personal memory."
    )

    prompt = SYSTEM_PROMPT
    prompt += "\n\nSAVED PERSONAL MEMORY:\n"
    prompt += memory_text

    if history:
        prompt += "\n\nRECENT CONVERSATION:\n"

        for item in history:
            role = item["role"].upper()
            content = item["content"]

            prompt += f"{role}: {content}\n"

    prompt += "\nCURRENT USER MESSAGE:\n"
    prompt += current_message

    return prompt


def extract_text(data: Dict[str, Any]) -> str:
    """
    Extract text from OpenAI Responses API output.
    Supports output_text and nested output/content formats.
    """

    if isinstance(data.get("output_text"), str):
        return data["output_text"].strip()

    parts = []

    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue

        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue

            text = content.get("text")

            if isinstance(text, str):
                parts.append(text)

    return "\n".join(parts).strip()


def ask_ai(
    user: Dict[str, Any],
    history: List[Dict[str, str]],
    message: str
) -> str:

    if not OPENAI_API_KEY:
        log.error("OPENAI_API_KEY is missing")
        return "Sorry, the AI service is not configured right now."

    prompt = build_prompt(
        user,
        history,
        message
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt
    }

    try:
        log.info("Calling OpenAI")

        response = requests.post(
            OPENAI_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        log.info(
            "OpenAI status: %s",
            response.status_code
        )

        if response.status_code != 200:
            log.error(
                "OpenAI error: %s",
                response.text[:2000]
            )

            return (
                "Sorry 😭 I couldn't get a response "
                "from the AI right now."
            )

        data = response.json()

        answer = extract_text(data)

        if not answer:
            log.error(
                "OpenAI returned no usable text: %s",
                data
            )

            return (
                "I received an empty response from "
                "the AI. Try again."
            )

        return answer

    except requests.Timeout:
        log.exception("OpenAI request timed out")

        return (
            "The AI took too long to respond 😭 "
            "Please try again."
        )

    except requests.RequestException:
        log.exception("OpenAI request failed")

        return (
            "Sorry 😭 I couldn't connect to the AI "
            "right now."
        )

    except Exception:
        log.exception("Unexpected OpenAI error")

        return (
            "Something went wrong while generating "
            "the reply."
        )


def split_message(
    text: str,
    max_chars: int = MAX_WA_CHARS
) -> List[str]:

    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text.strip()

    while len(remaining) > max_chars:
        cut = remaining.rfind(
            "\n",
            0,
            max_chars
        )

        if cut < int(max_chars * 0.5):
            cut = remaining.rfind(
                " ",
                0,
                max_chars
            )

        if cut <= 0:
            cut = max_chars

        chunk = remaining[:cut].strip()

        if chunk:
            chunks.append(chunk)

        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def send_whatsapp(
    recipient: str,
    message: str
) -> bool:

    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log.error(
            "WhatsApp environment variables are missing"
        )
        return False

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    success = True

    for chunk in split_message(message):
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {
                "body": chunk
            }
        }

        try:
            response = requests.post(
                WA_URL,
                headers=headers,
                json=payload,
                timeout=30
            )

            log.info(
                "WhatsApp send status: %s",
                response.status_code
            )

            if response.status_code not in {
                200,
                201
            }:
                success = False

                log.error(
                    "WhatsApp send error: %s",
                    response.text[:2000]
                )

        except requests.RequestException:
            success = False
            log.exception(
                "WhatsApp request failed"
            )

    return success


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "service": "WhatsApp AI Bot"
    })


@app.route("/privacy-policy", methods=["GET"])
def privacy_policy():

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Privacy Policy</title>
        <meta name="viewport"
              content="width=device-width, initial-scale=1">
        <style>
            body {
                font-family: Arial, sans-serif;
                max-width: 800px;
                margin: 40px auto;
                padding: 20px;
                line-height: 1.6;
            }
        </style>
    </head>

    <body>
        <h1>Privacy Policy</h1>

        <p>
            This WhatsApp AI bot processes messages sent to it
            in order to provide AI-generated responses.
        </p>

        <h2>Information stored</h2>

        <p>
            The bot may store conversation messages and
            user-provided personal memory such as preferences,
            name, goals, or other information explicitly
            provided for remembering.
        </p>

        <h2>AI processing</h2>

        <p>
            Messages may be sent to an AI service to generate
            responses.
        </p>

        <h2>Memory controls</h2>

        <p>
            Users can ask the bot what it remembers and can
            request that saved information be forgotten using
            the bot's memory commands.
        </p>

        <h2>Contact</h2>

        <p>
            If you have questions about this bot or its data,
            contact the bot owner.
        </p>
    </body>
    </html>
    """

    return html


@app.route("/webhook", methods=["GET"])
def verify_webhook():

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

    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():

    log.info("Webhook POST received")

    try:
        data = request.get_json(
            silent=True
        )

        if not data:
            log.warning("Empty webhook body")
            return "OK", 200

        log.info(
            "Webhook object: %s",
            data.get("object")
        )

        if data.get("object") != "whatsapp_business_account":
            return "OK", 200

        for entry in data.get("entry", []):

            for change in entry.get(
                "changes",
                []
            ):

                value = change.get(
                    "value",
                    {}
                )

                messages = value.get(
                    "messages",
                    []
                )

                # Status updates and other events
                # do not contain messages.
                if not messages:
                    continue

                for message in messages:

                    message_type = message.get(
                        "type"
                    )

                    log.info(
                        "Message type: %s",
                        message_type
                    )

                    # Only process text messages.
                    if message_type != "text":
                        log.info(
                            "Ignoring non-text message"
                        )
                        continue

                    message_id = message.get(
                        "id"
                    )

                    sender = message.get(
                        "from"
                    )

                    text_data = message.get(
                        "text",
                        {}
                    )

                    user_message = text_data.get(
                        "body",
                        ""
                    ).strip()

                    if not sender or not user_message:
                        log.warning(
                            "Missing sender or message text"
                        )
                        continue

                    if not message_id:
                        log.warning(
                            "Message has no ID"
                        )
                        continue

                    log.info(
                        "Received WhatsApp message: %s characters",
                        len(user_message)
                    )

                    # Check for duplicate webhook delivery
                    # before doing any AI work.
                    with db() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                SELECT 1
                                FROM conversations
                                WHERE message_id=%s
                                LIMIT 1
                                """,
                                (message_id,)
                            )

                            duplicate = (
                                cur.fetchone()
                                is not None
                            )

                    if duplicate:
                        log.info(
                            "Duplicate message ignored: %s",
                            message_id
                        )
                        continue

                    user = get_user(sender)

                    command_reply = memory_command(
                        user,
                        user_message
                    )

                    if command_reply is not None:

                        reply = command_reply

                    else:

                        # Extract stable memories before
                        # generating the response so the AI
                        # can use newly learned information.
                        automatic_memory_update(
                            user,
                            user_message
                        )

                        # Reload memory in case it changed.
                        user = get_user(sender)

                        history = get_history(sender)

                        reply = ask_ai(
                            user,
                            history,
                            user_message
                        )

                    inserted = save_chat(
                        sender,
                        message_id,
                        user_message,
                        reply
                    )

                    if not inserted:
                        log.info(
                            "Message became duplicate; "
                            "skipping reply"
                        )
                        continue

                    if send_whatsapp(
                        sender,
                        reply
                    ):
                        log.info(
                            "WhatsApp reply sent successfully"
                        )
                    else:
                        log.error(
                            "WhatsApp reply failed"
                        )

        log.info("Webhook DONE")

        return "OK", 200

    except Exception:
        log.exception(
            "Webhook processing error"
        )

        # Always return 200 to Meta so that a malformed
        # individual event does not cause endless retries.
        return "OK", 200


def startup_checks():

    missing = []

    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")

    if not WHATSAPP_TOKEN:
        missing.append("WHATSAPP_TOKEN")

    if not PHONE_NUMBER_ID:
        missing.append("PHONE_NUMBER_ID")

    if not VERIFY_TOKEN:
        missing.append("VERIFY_TOKEN")

    if not DATABASE_URL:
        missing.append("DATABASE_URL")

    if missing:
        log.error(
            "Missing environment variables: %s",
            ", ".join(missing)
        )

    setup_db()


# Initialize the database when running through
# "python app.py" or a WSGI server such as Gunicorn.
try:
    if DATABASE_URL:
        setup_db()
except Exception:
    log.exception(
        "Database initialization failed during startup"
    )


if __name__ == "__main__":

    startup_checks()

    port = int(
        os.getenv(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
                        )

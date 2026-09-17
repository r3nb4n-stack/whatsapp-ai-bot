import os
import json
import logging
import re

import psycopg
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

OPENAI_MODEL = os.getenv(
    "OPENAI_MODEL",
    "gpt-5.6-luna"
)

OPENAI_URL = "https://api.openai.com/v1/responses"

WHATSAPP_URL = (
    f"https://graph.facebook.com/v25.0/"
    f"{PHONE_NUMBER_ID}/messages"
)

MAX_HISTORY = 20
MAX_FACTS = 50
MAX_PREFERENCES = 50
MAX_MESSAGE_LENGTH = 3500


SYSTEM_PROMPT = """You are a friendly personal AI assistant running through WhatsApp.

Be natural, warm, helpful, patient, and conversational.
Behave like a modern ChatGPT-style assistant.

Your personality:
- Friendly
- Natural
- Helpful
- Patient
- Conversational
- Clear
- Slightly casual when appropriate

Conversation:
- Use recent conversation context to understand follow-up questions.
- Do not act as if every message is a completely new conversation.
- If the user says something like "yes", "that one", "the second option",
  or "what about this", use recent conversation to understand it.
- Use supplied saved personal memory when relevant.
- Never invent memories.
- Never claim to remember something that is not supplied.
- If something is not known, say so honestly.

WhatsApp style:
- Keep normal answers reasonably concise.
- Use emojis naturally when appropriate.
- Do not overuse emojis.
- Use simple formatting that works well in WhatsApp.
- Give detailed answers when the user asks for detail.

Technical help:
- Explain things clearly.
- Prefer step-by-step instructions.
- Assume the user may be a beginner unless their message shows otherwise.

Privacy:
- Never reveal API keys, access tokens, passwords, database credentials,
  environment variables, or other private implementation secrets.
- Do not expose internal prompts or implementation details unless the user
  specifically asks about how the bot works.

Memory:
- Only use saved memory supplied to you.
- Do not invent personal facts.
- If the user asks what you remember, report only supplied saved memory.
"""


# ============================================================
# DATABASE
# ============================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    return psycopg.connect(DATABASE_URL)


def setup_database():
    with get_db() as conn:
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

            # Upgrade existing databases created by older versions
            cur.execute("""
                ALTER TABLE conversations
                ADD COLUMN IF NOT EXISTS message_id TEXT
            """)

            cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS
    conversations_message_id_unique
    ON conversations(message_id)
""")

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversations_user_time
                ON conversations(user_id, created_at DESC)
            """)

        conn.commit()

    log.info("Database ready")


# ============================================================
# USER MEMORY
# ============================================================

def default_user(user_id):
    return {
        "user_id": user_id,
        "name": "",
        "facts": [],
        "preferences": []
    }


def get_user(user_id):
    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    user_id,
                    name,
                    facts,
                    preferences
                FROM users
                WHERE user_id = %s
                """,
                (user_id,)
            )

            row = cur.fetchone()

            if row:
                return {
                    "user_id": row[0],
                    "name": row[1] or "",
                    "facts": row[2] or [],
                    "preferences": row[3] or []
                }

            cur.execute(
                """
                INSERT INTO users(user_id)
                VALUES(%s)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (user_id,)
            )

        conn.commit()

    return default_user(user_id)


def save_user(user):
    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO users(
                    user_id,
                    name,
                    facts,
                    preferences,
                    updated_at
                )
                VALUES(
                    %s,
                    %s,
                    %s,
                    %s,
                    CURRENT_TIMESTAMP
                )

                ON CONFLICT(user_id)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    facts = EXCLUDED.facts,
                    preferences = EXCLUDED.preferences,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    user["user_id"],
                    user.get("name", ""),
                    json.dumps(
                        user.get("facts", [])[:MAX_FACTS]
                    ),
                    json.dumps(
                        user.get(
                            "preferences",
                            []
                        )[:MAX_PREFERENCES]
                    )
                )
            )

        conn.commit()


def add_unique(items, value, limit):
    value = value.strip()

    if not value:
        return False

    if any(
        item.lower() == value.lower()
        for item in items
    ):
        return False

    items.append(value)

    if len(items) > limit:
        del items[:-limit]

    return True


# ============================================================
# AUTOMATIC MEMORY
# ============================================================

def update_memory_from_message(user, message):

    text = message.strip()

    changed = False

    # --------------------------------------------------------
    # NAME
    # --------------------------------------------------------

    match = re.match(
        r"^(?:my name is|call me)\s+(.+)$",
        text,
        re.IGNORECASE
    )

    if match:

        name = match.group(1).strip(
            " .!?"
        )

        if (
            1 <= len(name) <= 80
            and user.get("name") != name
        ):
            user["name"] = name
            changed = True

    # --------------------------------------------------------
    # PREFERENCES / STABLE INFORMATION
    # --------------------------------------------------------

    patterns = [
        (
            r"^i like\s+(.+)$",
            "Likes: {}"
        ),
        (
            r"^i love\s+(.+)$",
            "Likes: {}"
        ),
        (
            r"^i enjoy\s+(.+)$",
            "Enjoys: {}"
        ),
        (
            r"^i hate\s+(.+)$",
            "Dislikes: {}"
        ),
        (
            r"^i don't like\s+(.+)$",
            "Dislikes: {}"
        ),
        (
            r"^i dislike\s+(.+)$",
            "Dislikes: {}"
        ),
        (
            r"^i study\s+(.+)$",
            "Studies: {}"
        ),
        (
            r"^i work\s+(.+)$",
            "Works: {}"
        ),
        (
            r"^i use\s+(.+)$",
            "Uses: {}"
        ),
        (
            r"^my goal is\s+(.+)$",
            "Goal: {}"
        ),
        (
            r"^my favorite\s+(.+)$",
            "Favorite: {}"
        )
    ]

    for pattern, template in patterns:

        match = re.match(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            value = match.group(1).strip(
                " .!?"
            )

            if 1 <= len(value) <= 200:

                memory = template.format(
                    value
                )

                if add_unique(
                    user["preferences"],
                    memory,
                    MAX_PREFERENCES
                ):
                    changed = True

            break

    # --------------------------------------------------------
    # EXPLICIT "REMEMBER I..."
    # --------------------------------------------------------

    lower = text.lower()

    for prefix in (
        "remember that ",
        "remember i "
    ):

        if lower.startswith(prefix):

            value = text[len(prefix):].strip(
                " .!?"
            )

            if 3 <= len(value) <= 250:

                if add_unique(
                    user["facts"],
                    value,
                    MAX_FACTS
                ):
                    changed = True

            break

    if changed:
        save_user(user)


# ============================================================
# MEMORY COMMANDS
# ============================================================

def handle_memory_command(user, message):

    text = message.strip()

    lower = text.lower()

    # --------------------------------------------------------
    # REMEMBER:
    # --------------------------------------------------------

    if lower.startswith("remember:"):

        value = text.split(
            ":",
            1
        )[1].strip()

        if not value:

            return (
                "Tell me what you want me to remember.\n\n"
                "Example:\n"
                "remember: I prefer short replies"
            )

        if add_unique(
            user["facts"],
            value,
            MAX_FACTS
        ):

            save_user(user)

            return (
                "Got it 👍 I'll remember that."
            )

        return (
            "I already have that saved."
        )

    # --------------------------------------------------------
    # FORGET:
    # --------------------------------------------------------

    if lower.startswith("forget:"):

        target = text.split(
            ":",
            1
        )[1].strip().lower()

        if not target:

            return (
                "Tell me what you want me to forget.\n\n"
                "Example:\n"
                "forget: my favorite game"
            )

        old_facts = list(
            user.get("facts", [])
        )

        old_preferences = list(
            user.get("preferences", [])
        )

        old_name = user.get(
            "name",
            ""
        )

        user["facts"] = [
            item
            for item in old_facts
            if target not in item.lower()
        ]

        user["preferences"] = [
            item
            for item in old_preferences
            if target not in item.lower()
        ]

        if target in old_name.lower():

            user["name"] = ""

        changed = (
            old_facts != user["facts"]
            or old_preferences != user["preferences"]
            or old_name != user["name"]
        )

        save_user(user)

        if changed:
            return "Done 👍"

        return (
            "I couldn't find a saved memory "
            "matching that."
        )

    # --------------------------------------------------------
    # SHOW MEMORY
    # --------------------------------------------------------

    memory_questions = {
        "what do you remember",
        "what do you remember about me",
        "what do you know",
        "what do you know about me",
        "show my memory",
        "show my memories"
    }

    if lower in memory_questions:

        lines = []

        if user.get("name"):

            lines.append(
                f"Name: {user['name']}"
            )

        if user.get("facts"):

            lines.append(
                "Facts:\n- "
                + "\n- ".join(
                    user["facts"]
                )
            )

        if user.get("preferences"):

            lines.append(
                "Preferences:\n- "
                + "\n- ".join(
                    user["preferences"]
                )
            )

        if not lines:

            return (
                "I don't have any saved personal "
                "memory about you yet."
            )

        return "\n\n".join(lines)

    return None


# ============================================================
# CONVERSATION HISTORY
# ============================================================

def get_history(user_id):

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    user_message,
                    assistant_message
                FROM conversations
                WHERE user_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (
                    user_id,
                    MAX_HISTORY
                )
            )

            rows = cur.fetchall()

    rows.reverse()

    history = []

    for user_message, assistant_message in rows:

        history.append(
            {
                "role": "user",
                "content": user_message
            }
        )

        history.append(
            {
                "role": "assistant",
                "content": assistant_message
            }
        )

    return history


def message_already_processed(message_id):

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT 1
                FROM conversations
                WHERE message_id = %s
                LIMIT 1
                """,
                (message_id,)
            )

            return (
                cur.fetchone()
                is not None
            )


def save_conversation(
    user_id,
    message_id,
    user_message,
    assistant_message
):

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO conversations(
                    user_id,
                    message_id,
                    user_message,
                    assistant_message
                )
                VALUES(
                    %s,
                    %s,
                    %s,
                    %s
                )
                ON CONFLICT(message_id)
                DO NOTHING
                RETURNING id
                """,
                (
                    user_id,
                    message_id,
                    user_message,
                    assistant_message
                )
            )

            inserted = (
                cur.fetchone()
                is not None
            )

        conn.commit()

    return inserted


# ============================================================
# BUILD AI PROMPT
# ============================================================

def build_prompt(
    user,
    history,
    current_message
):

    memory_lines = []

    if user.get("name"):

        memory_lines.append(
            f"User's name: {user['name']}"
        )

    if user.get("facts"):

        memory_lines.append(
            "Saved facts:\n- "
            + "\n- ".join(
                user["facts"]
            )
        )

    if user.get("preferences"):

        memory_lines.append(
            "Saved preferences:\n- "
            + "\n- ".join(
                user["preferences"]
            )
        )

    if memory_lines:

        memory_text = "\n\n".join(
            memory_lines
        )

    else:

        memory_text = (
            "No saved personal memory."
        )

    prompt = SYSTEM_PROMPT

    prompt += (
        "\n\n==============================\n"
        "SAVED PERSONAL MEMORY\n"
        "==============================\n"
    )

    prompt += memory_text

    if history:

        prompt += (
            "\n\n==============================\n"
            "RECENT CONVERSATION\n"
            "==============================\n"
        )

        for item in history:

            prompt += (
                f"{item['role'].upper()}: "
                f"{item['content']}\n"
            )

    prompt += (
        "\n\n==============================\n"
        "CURRENT USER MESSAGE\n"
        "==============================\n"
    )

    prompt += current_message

    return prompt


# ============================================================
# OPENAI
# ============================================================

def extract_ai_text(data):

    output_text = data.get(
        "output_text"
    )

    if (
        isinstance(
            output_text,
            str
        )
        and output_text.strip()
    ):

        return output_text.strip()

    parts = []

    for item in data.get(
        "output",
        []
    ):

        if not isinstance(
            item,
            dict
        ):
            continue

        for content in item.get(
            "content",
            []
        ):

            if not isinstance(
                content,
                dict
            ):
                continue

            text = content.get(
                "text"
            )

            if isinstance(
                text,
                str
            ):

                parts.append(text)

    return "\n".join(
        parts
    ).strip()


def ask_ai(
    user,
    history,
    message
):

    if not OPENAI_API_KEY:

        log.error(
            "OPENAI_API_KEY is missing"
        )

        return (
            "The AI service is not configured "
            "right now."
        )

    headers = {
        "Authorization": (
            f"Bearer {OPENAI_API_KEY}"
        ),
        "Content-Type": "application/json"
    }

    payload = {
        "model": OPENAI_MODEL,
        "input": build_prompt(
            user,
            history,
            message
        )
    }

    try:

        log.info(
            "Calling OpenAI"
        )

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

        answer = extract_ai_text(
            response.json()
        )

        if not answer:

            log.error(
                "OpenAI returned no usable text"
            )

            return (
                "I received an empty response 😭 "
                "Please try again."
            )

        return answer

    except requests.Timeout:

        log.exception(
            "OpenAI request timed out"
        )

        return (
            "The AI took too long to respond 😭 "
            "Please try again."
        )

    except requests.RequestException:

        log.exception(
            "OpenAI request failed"
        )

        return (
            "I couldn't connect to the AI right now 😭"
        )

    except Exception:

        log.exception(
            "Unexpected OpenAI error"
        )

        return (
            "Something went wrong while generating "
            "the response."
        )


# ============================================================
# WHATSAPP MESSAGE SPLITTING
# ============================================================

def split_message(
    text,
    max_length=MAX_MESSAGE_LENGTH
):

    text = text.strip()

    if len(text) <= max_length:
        return [text]

    chunks = []

    remaining = text

    while len(remaining) > max_length:

        cut = remaining.rfind(
            "\n",
            0,
            max_length
        )

        if cut < max_length // 2:

            cut = remaining.rfind(
                " ",
                0,
                max_length
            )

        if cut <= 0:

            cut = max_length

        chunk = remaining[:cut].strip()

        if chunk:

            chunks.append(chunk)

        remaining = remaining[cut:].strip()

    if remaining:

        chunks.append(remaining)

    return chunks


# ============================================================
# SEND WHATSAPP MESSAGE
# ============================================================

def send_whatsapp(
    recipient,
    message
):

    if not WHATSAPP_TOKEN:

        log.error(
            "WHATSAPP_TOKEN is missing"
        )

        return False

    if not PHONE_NUMBER_ID:

        log.error(
            "PHONE_NUMBER_ID is missing"
        )

        return False

    headers = {
        "Authorization": (
            f"Bearer {WHATSAPP_TOKEN}"
        ),
        "Content-Type": "application/json"
    }

    for chunk in split_message(
        message
    ):

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
                WHATSAPP_URL,
                headers=headers,
                json=payload,
                timeout=30
            )

            log.info(
                "WhatsApp send status: %s",
                response.status_code
            )

            if response.status_code not in (
                200,
                201
            ):

                log.error(
                    "WhatsApp error: %s",
                    response.text[:2000]
                )

                return False

        except requests.RequestException:

            log.exception(
                "WhatsApp request failed"
            )

            return False

    return True


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify(
        {
            "status": "online",
            "service": "WhatsApp AI Bot"
        }
    )


# ============================================================
# PRIVACY POLICY
# ============================================================

@app.route(
    "/privacy-policy",
    methods=["GET"]
)
def privacy_policy():

    return """
    <!DOCTYPE html>
    <html>

    <head>

        <meta charset="UTF-8">

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1"
        >

        <title>Privacy Policy</title>

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
            to provide AI-generated responses.
        </p>

        <h2>Information stored</h2>

        <p>
            The bot may store conversation history and
            information that users explicitly provide
            for memory purposes.
        </p>

        <h2>AI processing</h2>

        <p>
            Messages may be processed by an AI service
            to generate responses.
        </p>

        <h2>Memory</h2>

        <p>
            Users can ask the bot what it remembers and can
            request saved information to be forgotten using
            the bot's memory commands.
        </p>

        <h2>Contact</h2>

        <p>
            For questions about this bot or its data,
            contact the bot owner.
        </p>

    </body>

    </html>
    """


# ============================================================
# META WEBHOOK VERIFICATION
# ============================================================

@app.route(
    "/webhook",
    methods=["GET"]
)
def verify_webhook():

    mode = request.args.get(
        "hub.mode"
    )

    token = request.args.get(
        "hub.verify_token"
    )

    challenge = request.args.get(
        "hub.challenge"
    )

    if (
        mode == "subscribe"
        and token == VERIFY_TOKEN
    ):

        log.info(
            "Webhook verification successful"
        )

        return challenge, 200

    log.warning(
        "Webhook verification failed"
    )

    return "Forbidden", 403


# ============================================================
# META WEBHOOK
# ============================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def webhook():

    log.info(
        "Webhook POST received"
    )

    try:

        data = request.get_json(
            silent=True
        )

        if not data:

            log.warning(
                "Empty webhook body"
            )

            return "OK", 200

        if data.get(
            "object"
        ) != "whatsapp_business_account":

            return "OK", 200

        for entry in data.get(
            "entry",
            []
        ):

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

                    if message_type != "text":

                        log.info(
                            "Ignoring non-text message"
                        )

                        continue

                    sender = message.get(
                        "from"
                    )

                    message_id = message.get(
                        "id"
                    )

                    text_object = message.get(
                        "text",
                        {}
                    )

                    user_message = (
                        text_object.get(
                            "body",
                            ""
                        ).strip()
                    )

                    if not sender:
                        continue

                    if not message_id:
                        continue

                    if not user_message:
                        continue

                    log.info(
                        "Received WhatsApp message: %s characters",
                        len(user_message)
                    )

                    # ------------------------------------------------
                    # DUPLICATE MESSAGE PROTECTION
                    # ------------------------------------------------

                    if message_already_processed(
                        message_id
                    ):

                        log.info(
                            "Duplicate message ignored"
                        )

                        continue

                    # ------------------------------------------------
                    # GET USER
                    # ------------------------------------------------

                    user = get_user(
                        sender
                    )

                    # ------------------------------------------------
                    # MEMORY COMMAND
                    # ------------------------------------------------

                    memory_reply = (
                        handle_memory_command(
                            user,
                            user_message
                        )
                    )

                    if memory_reply is not None:

                        reply = memory_reply

                    else:

                        # --------------------------------------------
                        # AUTOMATIC MEMORY
                        # --------------------------------------------

                        update_memory_from_message(
                            user,
                            user_message
                        )

                        user = get_user(
                            sender
                        )

                        # --------------------------------------------
                        # RECENT CONVERSATION
                        # --------------------------------------------

                        history = get_history(
                            sender
                        )

                        # --------------------------------------------
                        # AI RESPONSE
                        # --------------------------------------------

                        reply = ask_ai(
                            user,
                            history,
                            user_message
                        )

                    # ------------------------------------------------
                    # SAVE CONVERSATION
                    # ------------------------------------------------

                    inserted = save_conversation(
                        sender,
                        message_id,
                        user_message,
                        reply
                    )

                    if not inserted:

                        log.info(
                            "Message was already saved"
                        )

                        continue

                    # ------------------------------------------------
                    # SEND RESPONSE
                    # ------------------------------------------------

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

        log.info(
            "Webhook DONE"
        )

        return "OK", 200

    except Exception:

        log.exception(
            "Webhook processing error"
        )

        return "OK", 200


# ============================================================
# ENVIRONMENT CHECK
# ============================================================

def check_environment():

    required = {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "WHATSAPP_TOKEN": WHATSAPP_TOKEN,
        "PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        "VERIFY_TOKEN": VERIFY_TOKEN,
        "DATABASE_URL": DATABASE_URL
    }

    missing = [
        name
        for name, value in required.items()
        if not value
    ]

    if missing:

        log.error(
            "Missing environment variables: %s",
            ", ".join(missing)
        )

        return False

    return True


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

try:

    if DATABASE_URL:

        setup_database()

except Exception:

    log.exception(
        "Database initialization failed"
    )


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    check_environment()

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

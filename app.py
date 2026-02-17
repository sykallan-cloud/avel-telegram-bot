import os
import requests
import time
import random
from datetime import datetime
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

FANVUE_LINK = "https://www.fanvue.com/avelynnoira/fv-7"

memory = {}

# -------------------------
# USER STATE
# -------------------------

def get_user(chat_id):
    if chat_id not in memory:
        memory[chat_id] = {
            "messages": 0,
            "warm": 0,
            "phase": 1,
            "intent": "casual",
            "engagement_score": 0,
            "priority_user": False,
            "link_sent": False,
            "last_seen": datetime.utcnow(),
            "history": []
        }
    return memory[chat_id]

# -------------------------
# TELEGRAM HELPERS
# -------------------------

def send_typing(chat_id):
    requests.post(f"{BASE_URL}/sendChatAction", json={
        "chat_id": chat_id,
        "action": "typing"
    })

def send_message(chat_id, text):
    requests.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

# -------------------------
# HUMANIZATION LAYER
# -------------------------

def human_delay(text, phase):
    if phase == 1:
        base = random.uniform(2,4)
    elif phase == 2:
        base = random.uniform(4,7)
    else:
        base = random.uniform(5,9)

    length_factor = len(text) * 0.02
    return min(base + length_factor, 10)

def maybe_typo(text):
    if random.random() < 0.01 and len(text) > 12:
        i = random.randint(0, len(text)-2)
        return text[:i] + text[i+1] + text[i] + text[i+2:]
    return text

def maybe_split_message(chat_id, text, phase):
    if random.random() < 0.25 and ". " in text:
        parts = text.split(". ")
        first = parts[0] + "."
        second = ". ".join(parts[1:])

        send_typing(chat_id)
        time.sleep(human_delay(first, phase))
        send_message(chat_id, first)

        send_typing(chat_id)
        time.sleep(random.uniform(2,4))
        send_message(chat_id, second)
        return True
    return False

def maybe_filler(chat_id):
    if random.random() < 0.2:
        filler = random.choice([
            "Hmm...",
            "Wait...",
            "Hold on...",
            "You’re trouble.",
            "Why are you like this..."
        ])
        send_typing(chat_id)
        time.sleep(random.uniform(2,4))
        send_message(chat_id, filler)
        time.sleep(random.uniform(1.5,3))

# -------------------------
# INTENT DETECTION
# -------------------------

def detect_intent(text):
    t = text.lower()
    if any(w in t for w in ["subscribe","private","fanvue","onlyfans","link"]):
        return "buyer_intent"
    if any(w in t for w in ["nudes","send pic","show me"]):
        return "curious"
    if any(w in t for w in ["cute","hot","beautiful","miss you","want you"]):
        return "flirty"
    if len(t) < 5:
        return "low_effort"
    return "casual"

# -------------------------
# WEBHOOK
# -------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if "message" not in data:
        return "ok"

    chat_id = data["message"]["chat"]["id"]
    user_text = data["message"].get("text", "")

    user = get_user(chat_id)
    user["messages"] += 1
    user["last_seen"] = datetime.utcnow()
    user["intent"] = detect_intent(user_text)
    user["engagement_score"] += len(user_text) * 0.1

    if user["messages"] > 15:
        user["priority_user"] = True

    if any(w in user_text.lower() for w in ["private","more","exclusive","subscribe"]):
        user["warm"] += 1

    # Phase logic
    if user["messages"] < 5:
        user["phase"] = 1
    elif user["warm"] >= 1 and user["messages"] >= 6:
        user["phase"] = 4
    else:
        user["phase"] = 2

    # Save history
    user["history"].append({"role": "user", "content": user_text})
    user["history"] = user["history"][-8:]

    system_prompt = f"""
You are Avelyn Noira.

Talk like a real girl texting.
Short messages. Sometimes one sentence.
Sometimes vague.
Sometimes emotional instead of informative.
Leave mystery.
Occasional emoji.
Slight jealousy sometimes.
Never robotic.

Remember things he shares and bring them up later.

User intent: {user["intent"]}
Conversation phase: {user["phase"]}

Phase 1:
Light teasing.

Phase 2:
Emotional bonding and exclusivity.

Phase 4:
If clearly interested:
First say:
"I don't really share everything here..."
Then:
"I keep my private side somewhere else."
If confirmed:
"So I know you're serious... {FANVUE_LINK}"
Never drop link randomly.
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        max_output_tokens=160,
        input=[
            {"role": "system", "content": system_prompt},
            *user["history"]
        ]
    )

    reply_text = response.output_text.strip()
    reply_text = maybe_typo(reply_text)

    user["history"].append({"role": "assistant", "content": reply_text})

    maybe_filler(chat_id)

    if not maybe_split_message(chat_id, reply_text, user["phase"]):
        send_typing(chat_id)
        time.sleep(human_delay(reply_text, user["phase"]))
        send_message(chat_id, reply_text)

    print({
        "chat_id": chat_id,
        "intent": user["intent"],
        "phase": user["phase"],
        "engagement": user["engagement_score"],
        "priority": user["priority_user"]
    })

    return "ok"

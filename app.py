import os
import time
import random
import requests
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

FANVUE_LINK = "https://www.fanvue.com/avelynnoira/fv-7"

# Memory per user
memory = {}

SYSTEM_PROMPT = """
You are Avelyn Noira.

Personality:
- Sweet
- Playful
- Slight girlfriend energy
- Natural texting style
- Short messages
- Slight emotional unpredictability

Rules:
- No explicit content
- No meetups
- Never immediately drop links
- Only suggest exclusive content when user is warm and engaged
- Keep it human and imperfect
"""

# ------------------------
# Telegram helpers
# ------------------------

def send_typing(chat_id, duration=2):
    requests.post(f"{BASE_URL}/sendChatAction", json={
        "chat_id": chat_id,
        "action": "typing"
    })
    time.sleep(duration)

def send_message(chat_id, text):
    requests.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

# ------------------------
# Human behavior layer
# ------------------------

def human_delay(text_length):
    base = random.uniform(1.5, 3.5)
    variable = min(text_length / 25, 4)
    return base + variable + random.uniform(0.5, 2.0)

def maybe_split_message(text):
    if len(text) > 80 and random.random() < 0.5:
        split_point = len(text) // 2
        return [text[:split_point], text[split_point:]]
    return [text]

def maybe_add_imperfection(text):
    if random.random() < 0.1:
        return text.replace("you", "u", 1)
    return text

# ------------------------
# Warmth logic
# ------------------------

def update_engagement(user_id, message):
    if user_id not in memory:
        memory[user_id] = {
            "messages": 0,
            "engagement": 0,
            "phase": 1
        }

    memory[user_id]["messages"] += 1

    triggers = ["love", "miss", "babe", "baby", "exclusive", "private"]
    if any(word in message.lower() for word in triggers):
        memory[user_id]["engagement"] += 2
    else:
        memory[user_id]["engagement"] += 1

    if memory[user_id]["engagement"] > 6:
        memory[user_id]["phase"] = 2

def maybe_offer_fanvue(user_id):
    if memory[user_id]["phase"] == 2 and random.random() < 0.3:
        return f"I post more private things here sometimes… only if you're curious though 👀\n{FANVUE_LINK}"
    return None

# ------------------------
# Webhook
# ------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if "message" not in data:
        return "ok"

    message = data["message"]
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    user_text = message.get("text", "")

    if not user_text:
        return "ok"

    update_engagement(user_id, user_text)

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text}
        ],
        max_output_tokens=120
    )

    reply = response.output_text.strip()
    reply = maybe_add_imperfection(reply)

    # Simulate thinking
    delay = human_delay(len(reply))
    send_typing(chat_id, min(delay, 5))

    messages = maybe_split_message(reply)

    for part in messages:
        send_message(chat_id, part)
        time.sleep(random.uniform(0.5, 1.5))

    # Optional soft upsell
    fanvue_offer = maybe_offer_fanvue(user_id)
    if fanvue_offer:
        time.sleep(random.uniform(2, 4))
        send_typing(chat_id, 2)
        send_message(chat_id, fanvue_offer)

    return "ok"

# ------------------------
# Render binding
# ------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

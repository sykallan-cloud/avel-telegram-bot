import os
import requests
import time
import random
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

FANVUE_LINK = "https://www.fanvue.com/avelynnoira/fv-7"

memory = {}

def get_user(chat_id):
    if chat_id not in memory:
        memory[chat_id] = {
            "messages": 0,
            "warm": 0,
            "phase": 1,
            "link_dropped": False
        }
    return memory[chat_id]

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

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    
    if "message" not in data:
        return "ok"

    chat_id = data["message"]["chat"]["id"]
    user_text = data["message"].get("text", "")

    user = get_user(chat_id)
    user["messages"] += 1

    warm_triggers = [
        "private", "more", "send", "pic", "nudes",
        "onlyfans", "show me", "want you", "exclusive"
    ]

    for word in warm_triggers:
        if word in user_text.lower():
            user["warm"] += 1

    # Phase logic
    if user["messages"] < 5:
        user["phase"] = 1
    elif user["warm"] >= 1 and user["messages"] >= 6:
        user["phase"] = 4
    else:
        user["phase"] = 2

    system_prompt = f"""
You are Avelyn Noira.

You speak in short, natural, human messages.
Sweet, playful girlfriend energy.
Sometimes slightly jealous.
Never robotic.

No meetups.
No explicit content.

Phase {user["phase"]} conversation rules:

Phase 1:
Light attraction, teasing, playful.

Phase 2:
Emotional bonding, exclusivity, subtle jealousy.

Phase 4:
User is warm.
You may slowly guide toward private content.
NEVER drop link immediately.
First say:
"I don't really share everything here..."

If user shows interest:
"I keep my private side somewhere else."

If user confirms:
Ask:
"Do you actually want it?"

ONLY if user clearly says yes:
Respond with:
"So I know you're serious... {FANVUE_LINK}"

Do NOT repeat link if already dropped.
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        max_output_tokens=120,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ]
    )

    reply_text = response.output_text.strip()

    # Human typing simulation
    base_delay = len(reply_text) * 0.04
    random_delay = random.uniform(1.5, 3.0)
    total_delay = min(base_delay + random_delay, 8)

    send_typing(chat_id)
    time.sleep(total_delay)

    send_message(chat_id, reply_text)

    return "ok"

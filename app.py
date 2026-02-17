import os
import requests
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """
You are Avelyn Noira.
You speak in short, natural messages.
Sweet, playful, girlfriend energy.
No meetups. No explicit content.
Only mention Fanvue when the user is warm.
Never drop the link without permission.
"""

memory = {}

def send_message(chat_id, text):
    requests.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    if "message" not in data:
        return "ok"

    chat_id = data["message"]["chat"]["id"]
    user_text = data["message"].get("text", "")

    if not user_text:
        return "ok"

    history = memory.get(chat_id, [])
    history.append({"role": "user", "content": user_text})

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            *history
        ],
        max_output_tokens=200
    )

    reply = response.output_text.strip()

    history.append({"role": "assistant", "content": reply})
    memory[chat_id] = history[-20:]

    send_message(chat_id, reply)

    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

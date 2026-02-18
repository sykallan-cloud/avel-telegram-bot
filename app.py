import os
import time
import re
import json
import random
from collections import defaultdict, deque

import requests
from flask import Flask, request, jsonify

# -----------------------------
# Config
# -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Set your Fanvue link here (only sent on explicit request or high intent)
FANVUE_LINK = os.getenv("FANVUE_LINK", "https://www.fanvue.com/avelvnnoira/").strip()

# If you use OpenAI Responses API, set endpoint accordingly.
# This file uses the OpenAI "Responses API"-style endpoint via HTTPS request.
OPENAI_RESPONSES_URL = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()  # upgrade if you can

# Human-feel tuning
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.9"))
TOP_P = float(os.getenv("TOP_P", "0.9"))
PRESENCE_PENALTY = float(os.getenv("PRESENCE_PENALTY", "0.6"))

# Memory size per user
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "14"))

# -----------------------------
# App
# -----------------------------
app = Flask(__name__)

# In-memory chat history: user_id -> deque of {role, content}
history = defaultdict(lambda: deque(maxlen=HISTORY_TURNS))

# Lightweight per-user state
user_state = defaultdict(dict)

# -----------------------------
# Helpers
# -----------------------------
def tg_send_message(chat_id: int, text: str, reply_to_message_id=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    return requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)

def normalize_text(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s

def contains_link_request(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ["link", "url", "send link", "drop link", "fanvue link", "where can i subscribe", "where can i join"])

def detect_intent(text: str) -> str:
    """
    Very practical intent classifier.
    You can replace with an LLM classifier later, but rules are fast and reliable.
    """
    t = text.lower().strip()

    # Meetup / location requests (we refuse gently)
    meetup_keys = ["meet", "meetup", "date", "come over", "come to", "where are you", "where is she", "location", "address"]
    if any(k in t for k in meetup_keys):
        return "MEETUP"

    # 1-on-1 / private chat
    private_keys = ["1 on 1", "1-on-1", "private", "dm", "direct message", "talk to her", "chat with her", "can i talk"]
    if any(k in t for k in private_keys):
        return "PRIVATE_CHAT"

    # customs / requests
    custom_keys = ["custom", "request", "personalized", "can you make", "can she do", "specific", "video for me", "pic for me"]
    if any(k in t for k in custom_keys):
        return "CUSTOMS"

    # What is on Fanvue / pricing / content
    fanvue_keys = ["fanvue", "what can i expect", "what do i get", "what is on", "subscription", "price", "cost", "worth it"]
    if any(k in t for k in fanvue_keys):
        return "FANVUE_INFO"

    # Confusion / bot suspicion
    bot_keys = ["are you a bot", "bot", "script", "automated", "real", "is this you"]
    if any(k in t for k in bot_keys):
        return "TRUST"

    # Default
    return "CASUAL"

def is_repetitive(user_id: int, candidate: str) -> bool:
    """
    Detect if candidate response is too close to last assistant message.
    """
    past = [m["content"] for m in history[user_id] if m["role"] == "assistant"]
    if not past:
        return False
    last = past[-1]
    # crude similarity: shared bigrams
    def bigrams(x):
        x = normalize_text(x.lower())
        return set([x[i:i+2] for i in range(len(x)-1)])
    a, b = bigrams(candidate), bigrams(last)
    if not a or not b:
        return False
    jacc = len(a & b) / max(1, len(a | b))
    return jacc > 0.68

def strip_ai_tells(text: str) -> str:
    """
    Remove common AI tells:
    - em-dash
    - bullet formatting
    - overly polished lines
    """
    text = text.replace("—", "")
    # Remove leading bullets or numbering
    text = re.sub(r"(?m)^\s*[-•]\s+", "", text)
    text = re.sub(r"(?m)^\s*\d+\.\s+", "", text)
    # Avoid double newlines spam
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def add_human_micro_variation(text: str) -> str:
    """
    Adds subtle human-like elements without forcing it.
    """
    # Occasionally add a tiny interjection
    if random.random() < 0.18 and len(text) < 220:
        inserts = ["haha", "2 sec", "snap ik", "fair", "oké"]
        pick = random.choice(inserts)
        # Put it at start only if it fits
        if not text.lower().startswith(tuple(inserts)):
            text = f"{pick}. {text}"
    # Keep max 4 lines
    lines = text.splitlines()
    if len(lines) > 4:
        text = " ".join(lines)
    return text

def should_soft_direct_to_fanvue(intent: str, text: str) -> bool:
    """
    Only allow Fanvue direction if user shows interest (high intent),
    or explicitly asks.
    """
    if contains_link_request(text):
        return True
    return intent in {"PRIVATE_CHAT", "CUSTOMS", "FANVUE_INFO"}

def system_prompt() -> str:
    return (
        "You are Mina, Avelyn's human assistant, typing on Telegram.\n"
        "You must sound like a real person, not a script.\n\n"
        "Hard rules:\n"
        "- Keep replies short: 1 to 4 short lines.\n"
        "- Answer the user's question directly first.\n"
        "- Ask at most one light follow-up question sometimes.\n"
        "- Never use em dashes (—) and do not use bullet lists.\n"
        "- Avoid corporate sales words (premium, exclusive, unlock, magic, full access).\n"
        "- Do not push Fanvue unless the user shows interest or asks.\n"
        "- Mention Fanvue at most once per message.\n"
        "- Never mention Ava Lin.\n"
        "- No meetups. If asked, politely refuse and offer online options.\n\n"
        "Tone:\n"
        "- Casual, warm, slightly playful.\n"
        "- Small imperfections are okay.\n"
        "- Do not over-explain.\n"
    )

def build_user_context(intent: str, user_text: str) -> str:
    """
    Provide additional guidance without sounding scripted.
    """
    # A small intent-specific nudge, not a template paragraph
    if intent == "MEETUP":
        return "User is asking for meetup/location. Refuse politely. Offer online chat instead."
    if intent == "TRUST":
        return "User doubts if this is real. Explain briefly you're her assistant and she sometimes reads along."
    if intent == "FANVUE_INFO":
        return "User wants details. Give 2-3 concrete examples of what she posts. Keep it simple."
    if intent == "PRIVATE_CHAT":
        return "User wants 1-on-1. Say DM is best on Fanvue. Ask what they want to chat about."
    if intent == "CUSTOMS":
        return "User asks about custom requests. Ask what they have in mind and mention it can be arranged on Fanvue."
    return "Keep it conversational and respond naturally."

def openai_generate(user_id: int, intent: str, user_text: str) -> str:
    """
    Calls OpenAI Responses API (HTTP) with memory.
    """
    # Build input messages
    messages = [{"role": "system", "content": system_prompt()}]

    # Add lightweight "developer" style context as system message
    messages.append({"role": "system", "content": build_user_context(intent, user_text)})

    # Add chat history
    for m in history[user_id]:
        messages.append(m)

    # Add current user message
    messages.append({"role": "user", "content": user_text})

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    # Responses API format
    payload = {
        "model": OPENAI_MODEL,
        "input": messages,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "presence_penalty": PRESENCE_PENALTY
    }

    r = requests.post(OPENAI_RESPONSES_URL, headers=headers, json=payload, timeout=25)
    r.raise_for_status()
    data = r.json()

    # Extract output text (handles common Responses schema)
    out = ""
    if "output" in data and isinstance(data["output"], list):
        # Find the first text content
        for item in data["output"]:
            if item.get("type") == "message":
                content = item.get("content", [])
                for c in content:
                    if c.get("type") == "output_text":
                        out += c.get("text", "")
    if not out:
        # fallback older schema
        out = data.get("output_text") or data.get("text") or ""

    return out.strip()

def finalize_reply(user_id: int, intent: str, user_text: str, draft: str) -> str:
    text = strip_ai_tells(draft)

    # If model tries to push Fanvue when it shouldn't, soften/remove
    if not should_soft_direct_to_fanvue(intent, user_text):
        # Remove "fanvue" lines if present
        if "fanvue" in text.lower():
            text = re.sub(r"(?i).*fanvue.*(\n|$)", "", text).strip()
            if not text:
                text = "Snap ik. Waar ben je precies benieuwd naar?"

    # If intent requires a refusal
    if intent == "MEETUP":
        # Force a consistent boundary, still human
        text = "Dat doen we niet, sorry. Wel gewoon online chat.\nWaar was je precies naar op zoek?"

    # Avoid sending raw link unless requested
    if "http" in text.lower() and not contains_link_request(user_text):
        text = re.sub(r"https?://\S+", " ", text).strip()

    # If user explicitly asks for link, include it once
    if contains_link_request(user_text):
        # Keep it natural, no pitch
        base = text if text else "Tuurlijk. Hier is de link."
        # Ensure link is present only once
        base = re.sub(r"https?://\S+", "", base).strip()
        text = f"{base}\n{FANVUE_LINK}".strip()

    # Keep short
    text = normalize_text(text)
    # Reintroduce line breaks for readability (human)
    if len(text) > 170:
        # split into 2-3 lines max based on punctuation
        parts = re.split(r"([.!?])\s+", text)
        rebuilt = ""
        line = ""
        lines = []
        for i in range(0, len(parts), 2):
            seg = parts[i].strip()
            punct = parts[i+1] if i+1 < len(parts) else ""
            sentence = (seg + punct).strip()
            if not sentence:
                continue
            if len(line) + len(sentence) + 1 < 70:
                line = (line + " " + sentence).strip()
            else:
                if line:
                    lines.append(line)
                line = sentence
            if len(lines) >= 3:
                break
        if line and len(lines) < 3:
            lines.append(line)
        text = "\n".join(lines).strip()

    # Add subtle human variation
    text = add_human_micro_variation(text)

    # Anti repetition: if too similar, nudge variation
    if is_repetitive(user_id, text):
        text = "Snap ik. Vertel eens, wat zoek je precies, meer chat of meer content?"

    return text

# -----------------------------
# Telegram Webhook
# -----------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message.get("text", "")
    if not text:
        return jsonify({"ok": True})

    user_text = text.strip()
    intent = detect_intent(user_text)

    # Store user message into history
    history[user_id].append({"role": "user", "content": user_text})

    # Generate response
    try:
        draft = openai_generate(user_id, intent, user_text)
    except Exception as e:
        # Fail gracefully
        reply = "Oeps, ik liep even vast. Stuur je vraag nog een keer kort?"
        tg_send_message(chat_id, reply, reply_to_message_id=message.get("message_id"))
        return jsonify({"ok": True})

    reply = finalize_reply(user_id, intent, user_text, draft)

    # Store assistant reply into history
    history[user_id].append({"role": "assistant", "content": reply})

    tg_send_message(chat_id, reply, reply_to_message_id=message.get("message_id"))
    return jsonify({"ok": True})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY env var")

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

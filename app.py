"""
app.py — Human-feel Telegram assistant that softly converts to Fanvue.

Key upgrades added (per your request):
- Funnel framework: COLD -> WARM -> HOT per user
- Intent detection + stage progression logic
- A "planner" step: the model outputs JSON (intent, stage, best next move, reply draft)
- Guardrails: short, human, no em-dash, no bullets, no scripted sales lines
- Soft Fanvue guidance ONLY when user is warm/hot or explicitly asks
- Link is only sent on explicit request (or user asks "link/url")

Install:
  pip install flask requests python-dotenv

Env vars:
  TELEGRAM_BOT_TOKEN=...
  OPENAI_API_KEY=...
  FANVUE_LINK=https://...
  OPENAI_MODEL=gpt-4.1-mini   (or better)
  PORT=8080
"""

import os
import re
import json
import time
import random
from dataclasses import dataclass, asdict
from collections import defaultdict, deque
from typing import Dict, Any, Optional, Tuple

import requests
from flask import Flask, request, jsonify


# -----------------------------
# Config
# -----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_RESPONSES_URL = os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()

FANVUE_LINK = os.getenv("FANVUE_LINK", "https://www.fanvue.com/avelvnnoira/").strip()

# Human-feel tuning
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.9"))
TOP_P = float(os.getenv("TOP_P", "0.9"))
PRESENCE_PENALTY = float(os.getenv("PRESENCE_PENALTY", "0.6"))

HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "16"))

# Safety/boundaries
NO_MEETUPS_TEXT = "Dat doen we niet, sorry. Wel gewoon online chat. Waar was je precies naar op zoek?"


# -----------------------------
# App
# -----------------------------
app = Flask(__name__)

# Conversation memory (per Telegram user)
history: Dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS))

# Lightweight user state (stage, counters, last topics)
user_state: Dict[int, Dict[str, Any]] = defaultdict(dict)


# -----------------------------
# Helpers: Telegram
# -----------------------------
def tg_send_message(chat_id: int, text: str, reply_to_message_id: Optional[int] = None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    return requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)


# -----------------------------
# Helpers: Text / Intent
# -----------------------------
def normalize_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def contains_link_request(text: str) -> bool:
    t = (text or "").lower()
    keys = ["link", "url", "send link", "drop link", "fanvue link", "where can i subscribe", "where can i join"]
    return any(k in t for k in keys)


def detect_intent_rulebased(text: str) -> str:
    """
    Fast routing. The planner model will refine, but this keeps behavior stable.
    """
    t = (text or "").lower().strip()

    meetup_keys = ["meet", "meetup", "date", "come over", "come to", "address", "where are you", "location"]
    if any(k in t for k in meetup_keys):
        return "MEETUP"

    private_keys = ["1 on 1", "1-on-1", "private", "dm", "direct message", "talk to her", "chat with her", "can i talk"]
    if any(k in t for k in private_keys):
        return "PRIVATE_CHAT"

    custom_keys = ["custom", "request", "personalized", "can you make", "specific", "video for me", "pic for me"]
    if any(k in t for k in custom_keys):
        return "CUSTOMS"

    fanvue_keys = ["fanvue", "what can i expect", "what do i get", "what is on", "subscription", "price", "cost", "worth it"]
    if any(k in t for k in fanvue_keys):
        return "FANVUE_INFO"

    trust_keys = ["are you a bot", "bot", "script", "automated", "real", "is this you"]
    if any(k in t for k in trust_keys):
        return "TRUST"

    return "CASUAL"


def strip_ai_tells(text: str) -> str:
    """
    Remove common AI tells:
    - em-dash
    - bullets
    - overly formatted list style
    """
    text = (text or "").replace("—", "")
    text = re.sub(r"(?m)^\s*[-•]\s+", "", text)
    text = re.sub(r"(?m)^\s*\d+\.\s+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def add_human_micro_variation(text: str) -> str:
    """
    Small, controlled human-like variation. Avoid cringe.
    """
    if not text:
        return text

    # Occasionally add a tiny interjection at the start
    if random.random() < 0.14 and len(text) < 220:
        inserts = ["haha", "snap ik", "fair", "oké"]
        pick = random.choice(inserts)
        if not text.lower().startswith(tuple(inserts)):
            text = f"{pick}. {text}"

    # Avoid too many emojis
    if text.count("😊") + text.count("😉") + text.count("😅") > 1:
        text = re.sub(r"[😊😉😅]", "", text).strip()

    return text


def is_repetitive(user_id: int, candidate: str) -> bool:
    past = [m["content"] for m in history[user_id] if m["role"] == "assistant"]
    if not past:
        return False
    last = past[-1]

    def bigrams(x: str):
        x = normalize_text(x.lower())
        return set([x[i : i + 2] for i in range(len(x) - 1)])

    a, b = bigrams(candidate), bigrams(last)
    if not a or not b:
        return False
    jacc = len(a & b) / max(1, len(a | b))
    return jacc > 0.68


# -----------------------------
# Funnel / Stage Logic
# -----------------------------
FUNNEL_STAGES = ("COLD", "WARM", "HOT")


def get_stage(user_id: int) -> str:
    stage = user_state[user_id].get("stage")
    if stage not in FUNNEL_STAGES:
        stage = "COLD"
    return stage


def set_stage(user_id: int, stage: str):
    if stage in FUNNEL_STAGES:
        user_state[user_id]["stage"] = stage


def bump_stage(user_id: int, new_stage: str):
    current = get_stage(user_id)
    order = {s: i for i, s in enumerate(FUNNEL_STAGES)}
    if order.get(new_stage, 0) > order.get(current, 0):
        set_stage(user_id, new_stage)


def update_stage_from_signal(user_id: int, intent: str, text: str):
    """
    Move users through funnel based on signals:
    - COLD: casual hi, random chat
    - WARM: asking what she offers, content, pricing, personal questions
    - HOT: asking 1-on-1, customs, link, how to subscribe
    """
    t = (text or "").lower()

    if intent in {"FANVUE_INFO", "TRUST"}:
        bump_stage(user_id, "WARM")

    if intent in {"PRIVATE_CHAT", "CUSTOMS"}:
        bump_stage(user_id, "HOT")

    if contains_link_request(text):
        bump_stage(user_id, "HOT")

    # Extra signal words
    warm_words = ["what do you do", "what can i", "tell me", "content", "posts", "price", "cost", "worth"]
    hot_words = ["subscribe", "join", "dm", "private chat", "custom", "request", "link", "where do i"]

    if any(w in t for w in hot_words):
        bump_stage(user_id, "HOT")
    elif any(w in t for w in warm_words):
        bump_stage(user_id, "WARM")


def should_mention_fanvue(user_id: int, intent: str, user_text: str) -> bool:
    """
    Soft guidance rule:
    - Always ok if user asked directly (fanvue / 1-on-1 / custom / pricing)
    - Otherwise only if stage is WARM/HOT and it's relevant
    """
    if contains_link_request(user_text):
        return True
    if intent in {"FANVUE_INFO", "PRIVATE_CHAT", "CUSTOMS"}:
        return True
    stage = get_stage(user_id)
    return stage in {"WARM", "HOT"} and intent in {"CASUAL", "TRUST"}


def should_send_link(user_text: str) -> bool:
    """
    Hard rule: only send link if explicitly requested.
    """
    return contains_link_request(user_text)


# -----------------------------
# Planner (Model outputs JSON plan)
# -----------------------------
@dataclass
class Plan:
    intent: str
    stage: str
    goal: str
    reply: str
    ask_followup: bool = True
    followup: str = ""
    mention_fanvue: bool = False
    send_link: bool = False


def system_prompt_planner() -> str:
    return (
        "You are Mina, Avelyn's HUMAN assistant typing on Telegram.\n"
        "Your job: understand what the chatter means and answer like a real person.\n"
        "You also aim to convert to Fanvue naturally, but only when it fits.\n\n"
        "Hard style rules:\n"
        "1) Keep replies short: 1 to 4 short lines.\n"
        "2) Answer the question directly first.\n"
        "3) Ask at most one short follow-up question sometimes.\n"
        "4) Never use em dashes (—) and never use bullet lists.\n"
        "5) Avoid scripted sales words: premium, exclusive, unlock, magic, full access.\n"
        "6) Do not mention Ava Lin.\n"
        "7) No meetups. If asked, refuse politely and offer online.\n"
        "8) Mention Fanvue at most once per message.\n"
        "9) Only send the link if the user explicitly asks for the link.\n\n"
        "Output MUST be valid JSON ONLY (no extra text).\n"
        "JSON keys: intent, stage, goal, reply, ask_followup, followup, mention_fanvue, send_link.\n"
        "Make reply feel human, not like a script.\n"
    )


def build_context_messages(user_id: int, rule_intent: str, user_text: str) -> list:
    stage = get_stage(user_id)

    # Provide model with current stage + rule intent + small constraints.
    meta = (
        f"Current funnel stage: {stage}.\n"
        f"Rule-based intent guess: {rule_intent}.\n"
        "Guidance on stages:\n"
        "- COLD: keep it friendly, no pushing.\n"
        "- WARM: give concrete info, light curiosity.\n"
        "- HOT: help them take the next step naturally.\n"
        "Remember: Fanvue only when relevant; link only if asked.\n"
    )

    messages = [{"role": "system", "content": system_prompt_planner()}]
    messages.append({"role": "system", "content": meta})

    # Add recent chat history
    for m in history[user_id]:
        messages.append(m)

    messages.append({"role": "user", "content": user_text})
    return messages


def openai_plan(user_id: int, rule_intent: str, user_text: str) -> Plan:
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    messages = build_context_messages(user_id, rule_intent, user_text)

    payload = {
        "model": OPENAI_MODEL,
        "input": messages,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "presence_penalty": PRESENCE_PENALTY,
        # Encourage structured output
        "response_format": {"type": "json_object"},
    }

    r = requests.post(OPENAI_RESPONSES_URL, headers=headers, json=payload, timeout=25)
    r.raise_for_status()
    data = r.json()

    # Extract text from Responses API
    out = ""
    if "output" in data and isinstance(data["output"], list):
        for item in data["output"]:
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        out += c.get("text", "")
    if not out:
        out = data.get("output_text") or data.get("text") or ""

    out = out.strip()

    # Parse JSON robustly
    try:
        obj = json.loads(out)
    except Exception:
        # Fallback if model returned something slightly off
        obj = {
            "intent": rule_intent,
            "stage": get_stage(user_id),
            "goal": "respond_naturally",
            "reply": out[:400],
            "ask_followup": True,
            "followup": "Wat bedoel je precies?",
            "mention_fanvue": False,
            "send_link": False,
        }

    # Build plan with defaults
    plan = Plan(
        intent=str(obj.get("intent", rule_intent)).upper(),
        stage=str(obj.get("stage", get_stage(user_id))).upper(),
        goal=str(obj.get("goal", "respond_naturally")),
        reply=str(obj.get("reply", "")).strip(),
        ask_followup=bool(obj.get("ask_followup", True)),
        followup=str(obj.get("followup", "")).strip(),
        mention_fanvue=bool(obj.get("mention_fanvue", False)),
        send_link=bool(obj.get("send_link", False)),
    )

    # Normalize stage to known values
    if plan.stage not in FUNNEL_STAGES:
        plan.stage = get_stage(user_id)

    return plan


# -----------------------------
# Finalization / Guardrails
# -----------------------------
def finalize_reply(user_id: int, plan: Plan, user_text: str, rule_intent: str) -> str:
    intent = plan.intent or rule_intent
    stage = get_stage(user_id)

    # Hard meetup refusal
    if rule_intent == "MEETUP" or intent == "MEETUP":
        return NO_MEETUPS_TEXT

    # Decide whether we allow Fanvue mention
    allow_fanvue = should_mention_fanvue(user_id, rule_intent, user_text)
    send_link = should_send_link(user_text)

    # Build base text
    reply = strip_ai_tells(plan.reply)

    # Enforce: no link unless explicitly requested
    reply = re.sub(r"https?://\S+", "", reply).strip()

    # Optionally add follow-up question (one)
    followup = strip_ai_tells(plan.followup or "").strip()
    if plan.ask_followup and followup:
        # Ensure it is a question, short
        if len(followup) > 90:
            followup = followup[:90].rsplit(" ", 1)[0] + "?"
        if not followup.endswith("?"):
            followup = followup.rstrip(".") + "?"
    else:
        followup = ""

    # Keep it short (max ~4 lines)
    parts = []
    if reply:
        parts.append(reply)

    if followup:
        parts.append(followup)

    text = "\n".join([p for p in parts if p]).strip()

    # If the model tried to push Fanvue but it's not allowed, remove references
    if not allow_fanvue:
        if "fanvue" in text.lower():
            text = re.sub(r"(?i)\bfanvue\b", "", text).strip()
            text = normalize_text(text)
            if not text:
                text = "Snap ik. Waar ben je precies benieuwd naar?"

    # If Fanvue mention is allowed, keep it natural and only once
    if allow_fanvue:
        # If user asked about 1-on-1/customs/fanvue, we allow 1 mention.
        # If reply doesn't mention it but it would help, add a soft line depending on intent/stage.
        if "fanvue" not in text.lower() and rule_intent in {"PRIVATE_CHAT", "CUSTOMS", "FANVUE_INFO"}:
            # Soft, no hype
            add = "Als je 1-op-1 wil of iets persoonlijks, dan kan dat het makkelijkst via Fanvue."
            # Keep within 4 lines
            if text:
                text = f"{text}\n{add}"
            else:
                text = add

        # Ensure only one 'Fanvue' word appears
        # If it appears multiple times, collapse to first
        occurrences = len(re.findall(r"(?i)\bfanvue\b", text))
        if occurrences > 1:
            # Remove all but first
            first = re.search(r"(?i)\bfanvue\b", text)
            if first:
                before = text[: first.end()]
                after = re.sub(r"(?i)\bfanvue\b", "", text[first.end():])
                text = (before + after).strip()
                text = normalize_text(text)

    # Add link only if explicitly requested
    if send_link:
        # Avoid sales pitch, just comply
        if text:
            text = f"{text}\n{FANVUE_LINK}".strip()
        else:
            text = f"Tuurlijk.\n{FANVUE_LINK}".strip()

    # Human micro-variation + anti repetition
    text = add_human_micro_variation(text)

    # If too repetitive, force a different line
    if is_repetitive(user_id, text):
        text = "Snap ik. Wat zoek je vooral, chat of iets specifieks?"

    # Final cleanup: no em-dash, no bullets
    text = strip_ai_tells(text)

    # Prevent super long messages
    text = text.strip()
    if len(text) > 500:
        text = text[:500].rsplit(" ", 1)[0].strip()

    return text


# -----------------------------
# Webhook
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

    # 1) Rule-based intent
    rule_intent = detect_intent_rulebased(user_text)

    # 2) Update funnel stage from the new signal BEFORE planning
    update_stage_from_signal(user_id, rule_intent, user_text)

    # 3) Store user message in history
    history[user_id].append({"role": "user", "content": user_text})

    # 4) Get plan from model (JSON)
    try:
        plan = openai_plan(user_id, rule_intent, user_text)
    except Exception:
        reply = "Oeps, ik liep even vast. Stuur je vraag nog een keer kort?"
        tg_send_message(chat_id, reply, reply_to_message_id=message.get("message_id"))
        return jsonify({"ok": True})

    # 5) Apply plan stage update (model suggestion), but never downgrade
    if plan.stage in FUNNEL_STAGES:
        bump_stage(user_id, plan.stage)

    # 6) Finalize reply with guardrails
    reply = finalize_reply(user_id, plan, user_text, rule_intent)

    # 7) Store assistant reply in history
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

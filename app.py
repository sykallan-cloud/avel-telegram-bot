import os
import time
import random
import requests
from datetime import datetime
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

FANVUE_LINK = "https://www.fanvue.com/avelynnoira/fv-7"

# In-memory state (Render restart = reset)
memory = {}

AB_VARIANTS = ["A", "B"]
MOODS = ["playful", "soft", "busy", "jealous_light", "tired"]

# De-dup for Telegram retries (in-memory, TTL-like)
processed = {}  # message_id -> timestamp
PROCESSED_TTL_SECONDS = 60 * 10  # 10 min


# -------------------------
# Telegram helpers
# -------------------------
def tg_post(method: str, payload: dict):
    try:
        return requests.post(f"{BASE_URL}/{method}", json=payload, timeout=12).json()
    except Exception:
        return None


def send_message(chat_id: int, text: str):
    tg_post("sendMessage", {"chat_id": chat_id, "text": text})


def send_typing(chat_id: int):
    tg_post("sendChatAction", {"chat_id": chat_id, "action": "typing"})


def wait_human(chat_id: int, total_seconds: float):
    """
    Realistic wait:
    - small 'seen' delay
    - typing bursts with pauses
    """
    total_seconds = max(0.0, float(total_seconds))

    # seen delay before typing
    seen_delay = min(random.uniform(0.4, 2.5), total_seconds)
    time.sleep(seen_delay)
    remaining = total_seconds - seen_delay

    while remaining > 0:
        burst = min(random.uniform(1.8, 4.8), remaining)
        send_typing(chat_id)
        time.sleep(burst)
        remaining -= burst

        if remaining <= 0:
            break

        pause = min(random.uniform(0.4, 1.8), remaining)
        time.sleep(pause)
        remaining -= pause


# -------------------------
# Humanization utilities
# -------------------------
def cleanup_processed():
    now = time.time()
    stale = [k for k, ts in processed.items() if (now - ts) > PROCESSED_TTL_SECONDS]
    for k in stale:
        processed.pop(k, None)


def maybe_typo(text: str) -> str:
    # ~1% small typo (swap 2 chars)
    if random.random() < 0.01 and len(text) > 14:
        i = random.randint(1, len(text) - 2)
        return text[: i - 1] + text[i] + text[i - 1] + text[i + 1 :]
    return text


def maybe_shorten(text: str) -> str:
    t = " ".join(text.split())
    if len(t) > 240:
        t = t[:240].rsplit(" ", 1)[0] + "…"
    t = t.replace("\n\n", "\n").replace("- ", "")
    return t


def pre_filler():
    return random.choice(["Hmm…", "Wait…", "Okay hold on…", "Lol okay…", "Mmm…"])


def human_delay(phase: int, intent: str, mood: str) -> float:
    if phase == 1:
        d = random.uniform(3.0, 9.0)
    elif phase == 2:
        d = random.uniform(6.0, 16.0)
    else:
        d = random.uniform(9.0, 20.0)

    if mood == "busy":
        d = max(2.5, d - random.uniform(2.0, 6.0))
    if mood in ["tired", "soft"]:
        d = min(22.0, d + random.uniform(1.0, 4.0))
    if intent == "buyer_intent":
        d = max(2.5, d - random.uniform(1.0, 5.0))

    d += random.uniform(0.0, 2.0)
    return min(d, 22.0)


# -------------------------
# Intent + warmth
# -------------------------
def detect_intent(text: str) -> str:
    t = text.lower()

    fan_keywords = ["fanvue", "subscribe", "subscription", "sub", "link", "account", "join"]
    flirty = ["cute", "hot", "pretty", "beautiful", "miss you", "want you", "babe", "baby"]
    loweffort = ["hi", "hey", "yo", "sup"]

    if any(k in t for k in fan_keywords):
        return "buyer_intent"
    if any(k in t for k in flirty):
        return "flirty"
    if t.strip() in loweffort or len(t.strip()) <= 3:
        return "low_effort"
    return "casual"


def warm_trigger(text: str) -> bool:
    t = text.lower()
    triggers = ["private", "exclusive", "more", "only", "subscribe", "fanvue", "link"]
    return any(x in t for x in triggers)


def is_affirmative(text: str) -> bool:
    t = text.strip().lower()
    yes = {
        "yes", "y", "yeah", "yep", "sure", "ok", "okay", "send", "send it", "give", "give it",
        "pls", "please", "drop it", "go on", "do it"
    }
    # allow short confirmations like "yes." "ok!"
    t2 = "".join([c for c in t if c.isalnum() or c.isspace()]).strip()
    return t in yes or t2 in yes


# -------------------------
# Mood engine (Option B)
# -------------------------
def update_mood(u: dict, user_text: str):
    t = user_text.strip()
    u["last_user_len"] = len(t)

    if len(t) <= 4:
        u["short_streak"] = min(u.get("short_streak", 0) + 1, 10)
    else:
        u["short_streak"] = max(u.get("short_streak", 0) - 1, 0)

    if u["short_streak"] >= 3:
        u["mood"] = "busy"
        return

    if u["messages"] >= 10 and random.random() < 0.06:
        u["mood"] = "tired"
        return

    if u["intent"] == "buyer_intent":
        u["mood"] = "soft" if random.random() < 0.6 else "playful"
        return

    if u["intent"] == "flirty" and u["phase"] >= 2:
        u["mood"] = "jealous_light" if random.random() < 0.25 else "playful"
        return

    if u["phase"] == 1:
        u["mood"] = "playful"
    elif u["phase"] == 2:
        u["mood"] = "soft" if random.random() < 0.7 else "playful"
    else:
        u["mood"] = "soft"


def mood_style_line(mood: str) -> str:
    if mood == "busy":
        return "You sound a bit busy. Short replies. Slightly teasing but rushed."
    if mood == "tired":
        return "You sound cozy/tired. Soft and slow. Short replies."
    if mood == "jealous_light":
        return "You are subtly jealous (light), playful, not aggressive."
    if mood == "soft":
        return "Soft girlfriend vibe. Warm. Slightly intimate (non-explicit)."
    return "Playful, teasing, confident, short texts."


# -------------------------
# User state
# -------------------------
def get_user(uid: int):
    if uid not in memory:
        memory[uid] = {
            "messages": 0,
            "warm": 0,
            "phase": 1,
            "intent": "casual",
            "engagement": 0.0,
            "priority": False,
            "link_stage": 0,  # 0 none, 1 teased, 2 link sent
            "history": [],
            "last_seen": datetime.utcnow().isoformat(),
            "variant": random.choice(AB_VARIANTS),
            "mood": "playful",
            "last_user_len": 0,
            "short_streak": 0,
        }
    return memory[uid]


# -------------------------
# Commercial overrides (single-message safe)
# -------------------------
def commercial_reply(u: dict, user_text: str):
    """
    Returns (handled: bool, reply_text: str or None)
    This version fixes:
    - "Yes" after tease -> should send link
    - Avoids weird GPT fallback
    """
    t = user_text.strip().lower()

    # If we already teased and user confirms (even just "Yes"), send link.
    if u["link_stage"] == 1 and is_affirmative(t):
        u["link_stage"] = 2
        return True, f"Okay… only if you’re actually serious 👀\n{FANVUE_LINK}"

    # If link already sent, avoid re-sending; guide.
    if u["link_stage"] == 2:
        if any(k in t for k in ["link", "fanvue", "subscribe", "sub"]):
            return True, "I already sent it 😌 tell me when you’re in."

    # Detect direct buyer asks
    direct = any(k in t for k in ["fanvue", "link", "subscribe", "subscription", "account", "join"])

    # First time direct ask: tease once
    if direct and u["link_stage"] == 0:
        u["link_stage"] = 1
        return True, "Mmm… you’re really about it 😮‍💨\nYou want my Fanvue link, yeah?"

    # If user says "send it / give it" while stage 1, treat as confirmation too
    if u["link_stage"] == 1 and any(k in t for k in ["send", "give", "drop", "ok", "okay", "please", "pls"]):
        u["link_stage"] = 2
        return True, f"Okay… only if you’re actually serious 👀\n{FANVUE_LINK}"

    # Soft steer only sometimes when very warm
    if u["phase"] == 4 and u["link_stage"] == 0 and random.random() < 0.14:
        u["link_stage"] = 1
        return True, "You’re making me curious… I keep my more private side somewhere else.\nYou want that link or you just teasing me? 😇"

    return False, None


# -------------------------
# Webhook
# -------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    cleanup_processed()

    update = request.get_json(silent=True) or {}
    msg = update.get("message")

    if not msg:
        return "ok"

    message_id = msg.get("message_id")
    if message_id is not None:
        if message_id in processed:
            return "ok"
        processed[message_id] = time.time()

    chat_id = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id", chat_id)
    user_text = (msg.get("text") or "").strip()

    if not user_text:
        return "ok"

    u = get_user(uid)

    u["messages"] += 1
    u["intent"] = detect_intent(user_text)
    u["engagement"] += min(len(user_text) * 0.08, 6.0)
    u["last_seen"] = datetime.utcnow().isoformat()

    if warm_trigger(user_text):
        u["warm"] += 1

    # Phase logic
    if u["messages"] < 5:
        u["phase"] = 1
    elif u["warm"] >= 1 and u["messages"] >= 6:
        u["phase"] = 4
    else:
        u["phase"] = 2

    if u["messages"] >= 18 or u["engagement"] >= 30:
        u["priority"] = True

    update_mood(u, user_text)

    # Save history (last 10 turns)
    u["history"].append({"role": "user", "content": user_text})
    u["history"] = u["history"][-10:]

    # A/B lines
    variant_line = (
        "Variant A: more teasing, a bit bolder, quicker to playfully challenge."
        if u["variant"] == "A"
        else "Variant B: softer girlfriend vibe, more reassuring, slightly more affectionate."
    )

    # 1) Commercial override FIRST (pre-GPT)
    handled, reply_text = commercial_reply(u, user_text)
    if handled and reply_text:
        d = human_delay(u["phase"], u["intent"], u["mood"])
        wait_human(chat_id, d)

        # SINGLE MESSAGE ONLY: optionally prefix filler into same message (not separate)
        if random.random() < 0.18:
            reply_text = f"{pre_filler()}\n{reply_text}"

        send_message(chat_id, reply_text)

        print(
            {
                "uid": uid,
                "phase": u["phase"],
                "intent": u["intent"],
                "warm": u["warm"],
                "engagement": round(u["engagement"], 2),
                "priority": u["priority"],
                "link_stage": u["link_stage"],
                "variant": u["variant"],
                "mood": u["mood"],
                "short_streak": u["short_streak"],
                "override": True,
            }
        )
        return "ok"

    # 2) GPT response (strong style constraints)
    system_prompt = f"""
You are Avelyn Noira.

STYLE (very important):
- You text like a real girl on Telegram.
- Keep it SHORT. 1–2 sentences most of the time.
- Use fragments sometimes. Like “mm” “okay…” “lol”.
- Don’t sound like an assistant. No explanations, no lectures.
- Avoid generic phrases like “there are lots of platforms”.
- No lists. No long paragraphs.

VIBE:
- Sweet, playful, a bit flirty.
- Slight jealousy SOMETIMES (subtle).
- Be personal and reactive, not formal.

SAFETY:
- No explicit content.
- No meetups.

MOOD:
{mood_style_line(u["mood"])}

AB VARIANT:
{variant_line}

CONTEXT:
- Intent: {u["intent"]}
- Phase: {u["phase"]} (1=light, 2=bonding, 4=warm)
- Warm count: {u["warm"]}

COMMERCE RULE:
- If the user asks about Fanvue/subscribing/link/account: respond directly (you do have one).
- If they ask for the link: tease once then share.
- If you already sent it, do not repeat it.

Write the next message now.
""".strip()

    resp = client.responses.create(
        model="gpt-4.1-mini",
        max_output_tokens=160,
        input=[{"role": "system", "content": system_prompt}, *u["history"]],
    )

    reply = (resp.output_text or "").strip()
    reply = maybe_shorten(reply)
    reply = maybe_typo(reply)

    u["history"].append({"role": "assistant", "content": reply})
    u["history"] = u["history"][-10:]

    d = human_delay(u["phase"], u["intent"], u["mood"])
    wait_human(chat_id, d)

    # SINGLE MESSAGE ONLY:
    if random.random() < 0.14:
        reply = f"{pre_filler()}\n{reply}"

    send_message(chat_id, reply)

    print(
        {
            "uid": uid,
            "phase": u["phase"],
            "intent": u["intent"],
            "warm": u["warm"],
            "engagement": round(u["engagement"], 2),
            "priority": u["priority"],
            "link_stage": u["link_stage"],
            "variant": u["variant"],
            "mood": u["mood"],
            "short_streak": u["short_streak"],
            "override": False,
        }
    )

    return "ok"


# -------------------------
# Render binding (required)
# -------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

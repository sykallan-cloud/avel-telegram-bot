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
def maybe_typo(text: str) -> str:
    # ~1% small typo (swap 2 chars)
    if random.random() < 0.01 and len(text) > 14:
        i = random.randint(1, len(text) - 2)
        return text[: i - 1] + text[i] + text[i - 1] + text[i + 1 :]
    return text


def maybe_shorten(text: str) -> str:
    """
    Reduce GPT-ish longness. Keep it chatty and short.
    """
    t = " ".join(text.split())
    # hard cap length
    if len(t) > 240:
        t = t[:240].rsplit(" ", 1)[0] + "…"
    # discourage lists / heavy formatting
    t = t.replace("\n\n", "\n").replace("- ", "")
    return t


def maybe_split(text: str):
    """
    Sometimes send in two messages like a human.
    """
    if random.random() < 0.28 and ". " in text and len(text) > 60:
        parts = text.split(". ")
        first = parts[0].strip() + "."
        rest = ". ".join(parts[1:]).strip()
        if len(rest) < 5:
            return None, text
        return first, rest
    return None, text


def pre_filler():
    return random.choice(["Hmm…", "Wait…", "Okay hold on…", "You’re trouble 😮‍💨", "Lol okay…"])


def human_delay(phase: int, intent: str, mood: str) -> float:
    """
    Extreme human timing:
    - Phase 1: 3–9s
    - Phase 2: 6–16s
    - Phase 4 (warm): 9–20s

    Mood:
    - busy -> slightly faster
    - soft/tired -> slightly slower
    Buyer intent -> slightly faster (still human).
    """
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


# -------------------------
# Mood engine (Option B)
# -------------------------
def update_mood(u: dict, user_text: str):
    t = user_text.strip()
    u["last_user_len"] = len(t)

    # detect short spam streak
    if len(t) <= 4:
        u["short_streak"] = min(u.get("short_streak", 0) + 1, 10)
    else:
        u["short_streak"] = max(u.get("short_streak", 0) - 1, 0)

    # base mood rules
    if u["short_streak"] >= 3:
        u["mood"] = "busy"
        return

    # some natural tired mood occasionally in longer convos
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
            # option B
            "variant": random.choice(AB_VARIANTS),
            "mood": "playful",
            "last_user_len": 0,
            "short_streak": 0,
        }
    return memory[uid]


# -------------------------
# Commercial overrides (direct + decisive)
# -------------------------
def commercial_reply(u: dict, user_text: str):
    """
    Returns (handled: bool, text_to_send: str or None)
    """
    t = user_text.lower()
    intent = u["intent"]

    direct = any(k in t for k in ["fanvue", "link", "subscribe", "subscription", "account", "join"])

    # If user directly asks for Fanvue / link / subscribe: do not waffle.
    if intent == "buyer_intent" and direct:
        if u["link_stage"] == 0:
            u["link_stage"] = 1
            return True, "Mmm… you’re really about it 😮‍💨\nYou want my Fanvue link, yeah?"
        if u["link_stage"] == 1:
            if any(x in t for x in ["yes", "yeah", "yep", "send", "give", "drop", "pls", "please", "ok"]):
                u["link_stage"] = 2
                return True, f"Okay… only if you’re actually serious 👀\n{FANVUE_LINK}"
            return True, "Say the word 😌 do you want it?"
        if u["link_stage"] == 2:
            return True, "I already sent it 😌 tell me when you’re in."

    # If warm and link not teased yet: occasional soft steer (not spam)
    if u["phase"] == 4 and u["link_stage"] == 0 and random.random() < 0.18:
        u["link_stage"] = 1
        return True, "You’re making me curious… I keep my more private side somewhere else.\nYou want that link or you just teasing me? 😇"

    return False, None


# -------------------------
# Webhook
# -------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if not data or "message" not in data:
        return "ok"

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id", chat_id)
    user_text = msg.get("text", "")

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

    # Update mood
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

    # 1) Commercial override (pre-GPT)
    handled, text = commercial_reply(u, user_text)
    if handled and text:
        d = human_delay(u["phase"], u["intent"], u["mood"])

        # sometimes a filler before main reply
        if random.random() < 0.22:
            wait_human(chat_id, random.uniform(2.0, 6.0))
            send_message(chat_id, pre_filler())

        wait_human(chat_id, min(d, 12.0))
        send_message(chat_id, text)

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
- Occasional emoji, not every message.

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
- Priority user: {u["priority"]}

COMMERCE RULE:
- If the user asks about Fanvue/subscribing/link/account:
  respond directly and confidently as if you DO have one.
  Do NOT list other platforms.
  If they ask for the link: tease once then share.
- Do NOT spam the link. If already shared, say “I already sent it 😌”.

Write the next message now.
""".strip()

    resp = client.responses.create(
        model="gpt-4.1-mini",
        max_output_tokens=160,
        input=[{"role": "system", "content": system_prompt}, *u["history"]],
    )

    reply = resp.output_text.strip()
    reply = maybe_shorten(reply)
    reply = maybe_typo(reply)

    # Save assistant turn
    u["history"].append({"role": "assistant", "content": reply})
    u["history"] = u["history"][-10:]

    # 3) Human send pattern
    if random.random() < 0.20:
        wait_human(chat_id, random.uniform(2.0, 6.0))
        send_message(chat_id, pre_filler())

    d = human_delay(u["phase"], u["intent"], u["mood"])
    part1, part2 = maybe_split(reply)

    if part1:
        wait_human(chat_id, d)
        send_message(chat_id, part1)
        wait_human(chat_id, random.uniform(2.0, 7.0))
        send_message(chat_id, part2)
    else:
        wait_human(chat_id, d)
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

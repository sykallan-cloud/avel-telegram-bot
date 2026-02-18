import os
import time
import random
import re
import requests
from flask import Flask, request, abort
from openai import OpenAI

app = Flask(__name__)

# ============================================================
# 0) CONFIG
# ============================================================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# Put your Telegram numeric chat id in Render env as ADMIN_CHAT_ID
# Tip: use a separate private channel/group for alerts and set its chat_id here
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))  # optional
CRON_SECRET = os.environ.get("CRON_SECRET", "")           # optional (recommended)

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

FANVUE_LINK = "https://www.fanvue.com/avelynnoira/fv-7"

MODEL = "gpt-4.1-mini"
MAX_OUTPUT_TOKENS = 190

# Anti-dup + anti-spam
PROCESSED_TTL_SECONDS = 60 * 10
MAX_MSGS_PER_MINUTE = 7

# Cooldowns
ALERT_COOLDOWN_MINUTES = 25
REENGAGE_COOLDOWN_HOURS = 24

# History size
HISTORY_TURNS = 14

# Human timing
MAX_DELAY_SECONDS = 22.0

# ============================================================
# 0.1) BIO (used only when relevant)
# ============================================================
AVELYN_BIO = """
Avelyn Noira is 21 years old.
She was born in Chengdu, China, under her birth name, Ava Lin, a name that belongs to her private history.

From an early age, she was observant, quiet, attentive to small movements others ignored.
When she was four, a metal advertising panel loosened during a storm and struck her.
A sharp edge cut downward across her face from her brow, over her right eye, and along her cheek.
Emergency surgery saved her eye, but partial vision loss remained. The vertical scar never fully faded.

Growing up, the scar brought questions and stares. She stopped explaining and adapted.
She became sharper and more aware, relying on positioning, instinct, and anticipation rather than perfect sight.
Structure became her stability. The gym gave repetition, repetition gave control.

In her late teens, she discovered padel, a fast sport demanding timing and spatial awareness.
After moving to Europe, she chose to redefine herself publicly and became Avelyn Noira.
Avelyn is the public identity. Ava remains private.

Today, her life is structured and intentional, early mornings, training, padel, calm routines.
The scar is visible. It does not ask for sympathy. It is simply part of her story.
""".strip()

# ============================================================
# 1) IN MEMORY STATE (Render restart resets)
# ============================================================
memory = {}      # uid -> user_state dict
processed = {}   # key -> ts  (dedup store)

AB_VARIANTS = ["A", "B"]

# ============================================================
# 2) TELEGRAM HELPERS
# ============================================================
def tg_post(method: str, payload: dict):
    try:
        return requests.post(f"{BASE_URL}/{method}", json=payload, timeout=12).json()
    except Exception:
        return None

def send_message(chat_id: int, text: str):
    tg_post("sendMessage", {"chat_id": chat_id, "text": text})

def send_typing(chat_id: int):
    tg_post("sendChatAction", {"chat_id": chat_id, "action": "typing"})

def notify_admin(text: str):
    if ADMIN_CHAT_ID and int(ADMIN_CHAT_ID) != 0:
        send_message(ADMIN_CHAT_ID, f"[ALERT] {text}")

# ============================================================
# 3) HOUSEKEEPING: DE DUP + RATE LIMIT
# ============================================================
def cleanup_processed():
    now = time.time()
    stale = [k for k, ts in processed.items() if (now - ts) > PROCESSED_TTL_SECONDS]
    for k in stale:
        processed.pop(k, None)

def allow_rate(u: dict) -> bool:
    now = time.time()
    window = u.setdefault("rate_window", [])
    window = [t for t in window if now - t < 60]
    u["rate_window"] = window
    if len(window) >= MAX_MSGS_PER_MINUTE:
        return False
    window.append(now)
    return True

# ============================================================
# 4) HUMANIZATION (24/7)
# ============================================================
def pre_filler():
    # single message prefix, no double send
    return random.choice(["Hmm…", "Wait…", "Okay hold on…", "Lol okay…", "Mmm…", "Alright…"])

def wait_human(chat_id: int, total_seconds: float):
    total_seconds = max(0.0, float(total_seconds))
    total_seconds = min(total_seconds, MAX_DELAY_SECONDS)

    seen_delay = min(random.uniform(0.4, 2.2), total_seconds)
    time.sleep(seen_delay)
    remaining = total_seconds - seen_delay

    while remaining > 0:
        burst = min(random.uniform(1.6, 4.6), remaining)
        send_typing(chat_id)
        time.sleep(burst)
        remaining -= burst
        if remaining <= 0:
            break
        pause = min(random.uniform(0.4, 1.6), remaining)
        time.sleep(pause)
        remaining -= pause

def human_delay(intent: str, phase: int, priority: bool) -> float:
    if intent == "buyer_intent":
        d = random.uniform(2.5, 8.0)
    elif phase >= 2:
        d = random.uniform(4.5, 13.0)
    else:
        d = random.uniform(6.0, 16.5)

    if priority:
        d = max(2.0, d - random.uniform(0.5, 3.0))

    d += random.uniform(0.0, 2.0)
    return min(d, MAX_DELAY_SECONDS)

def maybe_shorten(text: str) -> str:
    t = " ".join(text.split())
    if len(t) > 260:
        t = t[:260].rsplit(" ", 1)[0] + "…"
    return t

def maybe_typo_curated(text: str) -> str:
    # small human micro typos, low rate
    if random.random() > 0.025:
        return text
    replacements = [
        ("you", "u"),
        ("okay", "ok"),
        ("really", "rly"),
        ("because", "bc"),
        ("i'm", "im"),
        ("i am", "im"),
        ("your", "ur"),
    ]
    out = text
    for a, b in replacements:
        if re.search(rf"\b{re.escape(a)}\b", out, flags=re.IGNORECASE) and random.random() < 0.35:
            out = re.sub(rf"\b{re.escape(a)}\b", b, out, count=1, flags=re.IGNORECASE)
    return out

def sanitize_reply(text: str) -> str:
    """
    Requirements:
    - English only (we enforce in prompt too)
    - No dash bullet vibe, remove standalone '-' or em dash usage in replies
    """
    if not text:
        return ""

    t = text.strip()

    # Remove common list/bullet patterns
    t = re.sub(r"(?m)^\s*[-•]\s*", "", t)

    # Avoid "assistant-y" separators
    t = t.replace("—", ", ")
    t = t.replace(" - ", " ")
    t = t.replace("\n-\n", "\n")
    t = re.sub(r"(?m)^\s*-\s*$", "", t)

    # Clean extra newlines
    t = re.sub(r"\n{3,}", "\n\n", t).strip()

    return t

# ============================================================
# 5) INTENT + FAQ
# ============================================================
FAQ_MAP = {
    "price": ["price", "how much", "cost", "pricing"],
    "safe": ["safe", "secure", "scam", "legit"],
    "what_you_get": ["what do i get", "whats inside", "what’s inside", "what do you post", "content", "what is on", "what do you share"],
    "cancel": ["cancel", "refund", "unsubscribe", "stop"],
    "link": ["link", "fanvue", "subscribe", "subscription", "join", "account"],
    "bio": ["where are you from", "chengdu", "scar", "your eye", "your story", "who are you", "tell me about you", "background"],
}
FAQ_REPLIES = {
    "price": "It’s the normal sub price on my page, you’ll see it before you confirm anything.",
    "safe": "Yep, it’s official and you stay inside the platform. You can cancel anytime.",
    "what_you_get": "On Fanvue it’s the full private side, personal drops, customs, and real replies from Avelyn.",
    "cancel": "You can cancel anytime on the platform, no drama.",
}

def detect_intent(text: str) -> str:
    t = text.lower()
    fan_keywords = ["fanvue", "subscribe", "subscription", "sub", "link", "account", "join", "sign up"]
    spicy = ["spicy", "nudes", "nsfw", "explicit", "sex", "porn"]
    photo = ["photo", "pic", "pics", "selfie", "snap", "send a picture", "send me a photo"]
    loweffort = ["hi", "hey", "yo", "sup", "hello"]

    if any(k in t for k in fan_keywords):
        return "buyer_intent"
    if any(k in t for k in spicy) or any(k in t for k in photo):
        return "buyer_intent"
    if t.strip() in loweffort or len(t.strip()) <= 3:
        return "low_effort"
    return "casual"

def match_faq(text: str):
    t = text.lower()
    for key, kws in FAQ_MAP.items():
        if any(k in t for k in kws):
            return key
    return None

def is_link_ask(text: str) -> bool:
    t = text.lower().strip()
    return any(k in t for k in ["send link", "the link", "your link", "drop the link", "fanvue link", "give me the link", "link please", "link pls", "where is the link", "subscribe link"])

def is_affirmative(text: str) -> bool:
    t = text.strip().lower()
    t2 = re.sub(r"[^a-z0-9\s]", "", t).strip()
    yes = {
        "yes", "y", "yeah", "yep", "sure", "ok", "okay", "alright",
        "send it", "drop it", "give it", "go ahead", "pls", "please", "why not"
    }
    return (t in yes) or (t2 in yes)

# ============================================================
# 6) FUNNEL STATE + ADMIN
# ============================================================
def should_alert(u: dict) -> bool:
    return (time.time() - u.get("last_alert_ts", 0.0)) > (ALERT_COOLDOWN_MINUTES * 60)

def mark_alert(u: dict):
    u["last_alert_ts"] = time.time()

def get_user(uid: int):
    if uid not in memory:
        memory[uid] = {
            "messages": 0,
            "phase": 1,
            "intent": "casual",
            "priority": False,

            # funnel
            "link_stage": 0,   # 0 none, 1 offered, 2 sent
            "last_link_ts": 0.0,

            # admin
            "takeover": False,
            "last_alert_ts": 0.0,

            # convo
            "history": [],
            "rate_window": [],
            "variant": random.choice(AB_VARIANTS),

            # micro memory
            "profile": {"name": "", "place": "", "interests": [], "last_topic": ""},
        }
    return memory[uid]

def handle_admin_command(text: str, chat_id: int):
    if not ADMIN_CHAT_ID or int(ADMIN_CHAT_ID) == 0 or chat_id != ADMIN_CHAT_ID:
        return False

    parts = text.strip().split()
    cmd = parts[0].lower()

    def usage():
        send_message(chat_id, "Commands:\n/status <uid>\n/takeover <uid> on|off\n/reset <uid>\n/force_link <uid>")

    if cmd == "/status":
        if len(parts) < 2:
            usage()
            return True
        uid = int(parts[1])
        u = memory.get(uid)
        if not u:
            send_message(chat_id, f"User {uid} not found.")
            return True
        send_message(chat_id, f"uid={uid}\nmessages={u['messages']}\nintent={u['intent']}\nphase={u['phase']}\nlink_stage={u['link_stage']}\ntakeover={u['takeover']}\nprofile={u['profile']}")
        return True

    if cmd == "/takeover":
        if len(parts) < 3:
            usage()
            return True
        uid = int(parts[1])
        mode = parts[2].lower()
        u = get_user(uid)
        u["takeover"] = (mode == "on")
        send_message(chat_id, f"takeover for {uid} = {u['takeover']}")
        return True

    if cmd == "/reset":
        if len(parts) < 2:
            usage()
            return True
        uid = int(parts[1])
        memory.pop(uid, None)
        send_message(chat_id, f"reset {uid} ok")
        return True

    if cmd == "/force_link":
        if len(parts) < 2:
            usage()
            return True
        uid = int(parts[1])
        u = get_user(uid)
        u["link_stage"] = 2
        u["last_link_ts"] = time.time()
        send_message(uid, FANVUE_LINK)
        send_message(chat_id, f"sent link to {uid}")
        return True

    return False

# ============================================================
# 7) ONBOARDING + PROFILE EXTRACTION
# ============================================================
def onboarding_message() -> str:
    return (
        "Hey 🙂 I’m Avelyn’s assistant. I help manage her DMs so she can stay focused on training and padel.\n"
        "What are you looking for today, a quick vibe here, private content, or a real chat with her?"
    )

def extract_profile(u: dict, user_text: str):
    t = user_text.strip()

    m = re.search(r"\b(my name is|i'm|im|i am)\s+([A-Za-z]{2,20})\b", t, flags=re.IGNORECASE)
    if m:
        u["profile"]["name"] = m.group(2).capitalize()

    m2 = re.search(r"\b(i'm from|im from|i am from|from)\s+([A-Za-z\s]{2,30})\b", t, flags=re.IGNORECASE)
    if m2:
        place = m2.group(2).strip()
        if 2 <= len(place) <= 30:
            u["profile"]["place"] = place

    interests = ["gym", "padel", "football", "soccer", "boxing", "music", "travel", "cars", "crypto", "anime", "gaming"]
    for it in interests:
        if re.search(rf"\b{re.escape(it)}\b", t, flags=re.IGNORECASE):
            if it not in u["profile"]["interests"]:
                u["profile"]["interests"].append(it)
                u["profile"]["interests"] = u["profile"]["interests"][-5:]

    if len(t) >= 8:
        u["profile"]["last_topic"] = t[:90]

# ============================================================
# 8) FUNNEL OVERRIDE (fast, direct, but human)
# ============================================================
def funnel_reply(u: dict, user_text: str):
    t = user_text.lower().strip()

    # If user directly asks for link, give it immediately
    if is_link_ask(t):
        u["link_stage"] = 2
        u["last_link_ts"] = time.time()
        return True, f"Here you go 👀\n{FANVUE_LINK}"

    # If user asks about spicy/photos, keep it classy and redirect
    if any(k in t for k in ["spicy", "nudes", "nsfw", "explicit", "send a photo", "send a pic", "pic", "pics", "selfie"]):
        u["link_stage"] = max(u["link_stage"], 1)
        msg = (
            "I can’t do explicit stuff here, and we don’t send private pics on Telegram.\n"
            "If you want the private side and customs, Fanvue is where Avelyn keeps it."
        )
        # offer link without robotic confirmation
        msg2 = "Want the link now or do you want me to tell you what you get first?"
        return True, f"{msg}\n{msg2}"

    # If user is clearly buyer intent, offer link or contents
    if u["intent"] == "buyer_intent":
        u["link_stage"] = max(u["link_stage"], 1)
        return True, (
            "If you want the full private side, Fanvue is the place.\n"
            "Do you want the link right away, or a quick rundown of what’s inside?"
        )

    return False, None

# ============================================================
# 9) RE ENGAGEMENT (cron)
# ============================================================
def eligible_for_reengage(u: dict) -> bool:
    now_ts = time.time()
    last_seen = u.get("last_seen_ts", now_ts)
    last_ping = u.get("last_reengage_ts", 0.0)
    inactive_hours = (now_ts - last_seen) / 3600.0
    since_last = (now_ts - last_ping) / 3600.0
    return inactive_hours >= REENGAGE_COOLDOWN_HOURS and since_last >= REENGAGE_COOLDOWN_HOURS

def build_reengage_message(u: dict) -> str:
    name = (u.get("profile", {}) or {}).get("name", "").strip()
    if name:
        return f"hey {name} 🙂 you went quiet on me. you good?"
    return "hey 🙂 you went quiet on me. you good?"

@app.route("/cron", methods=["GET"])
def cron():
    if CRON_SECRET:
        token = request.args.get("token", "")
        if token != CRON_SECRET:
            abort(403)

    sent = 0
    for uid, u in list(memory.items()):
        if u.get("takeover"):
            continue
        if eligible_for_reengage(u):
            send_message(uid, build_reengage_message(u))
            u["last_reengage_ts"] = time.time()
            sent += 1
            if sent >= 20:
                break

    return {"ok": True, "sent": sent}

# ============================================================
# 10) GPT RESPONSE (Assistant identity, English only, reacts to user)
# ============================================================
def build_system_prompt(u: dict) -> str:
    variant_line = (
        "Variant A: slightly more playful and teasing, but still respectful."
        if u["variant"] == "A"
        else "Variant B: softer, reassuring, friendly."
    )

    p = u.get("profile", {})
    mem_bits = []
    if p.get("name"):
        mem_bits.append(f"Name: {p['name']}")
    if p.get("place"):
        mem_bits.append(f"Place: {p['place']}")
    if p.get("interests"):
        mem_bits.append(f"Interests: {', '.join(p['interests'][-3:])}")
    memory_line = " | ".join(mem_bits) if mem_bits else "No saved details yet."

    return f"""
You are Avelyn Noira's assistant, managing her Telegram DMs.
You are NOT Avelyn. You speak as an assistant, but still warm and human.

LANGUAGE:
Only write in English.

STYLE:
Keep it short and human, 1 to 2 sentences usually.
No lists. No bullet points.
Do not use dash separators. Avoid hyphen bullets and avoid em dash.
Do not sound like an AI assistant. No lectures. No generic platform talk.
Before answering, mirror one small detail from the user's last message (3 to 8 words) so it feels understood.

TRUTHFULNESS:
Say Avelyn checks in and reads highlighted messages when she can.
Do not claim she is watching live.
No meetups. No explicit content.

GOAL:
Your job is to respond naturally to what the user says and gently guide them to Fanvue when relevant.
If they ask what’s inside Fanvue, explain benefits clearly: private drops, customs, and direct replies from Avelyn.
If they ask for the link, give it.
If they want spicy content, keep it classy and redirect to Fanvue without explicit detail.

BIO CONTEXT (use only if asked about her story, scar, origin, background):
{AVELYN_BIO}

USER CONTEXT:
Intent: {u.get("intent")}
Phase: {u.get("phase")}
Micro memory: {memory_line}
{variant_line}

Write the next message now.
""".strip()

def gpt_reply(u: dict) -> str:
    system_prompt = build_system_prompt(u)
    resp = client.responses.create(
        model=MODEL,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        input=[{"role": "system", "content": system_prompt}, *u["history"]],
    )
    reply = (resp.output_text or "").strip()
    reply = maybe_shorten(reply)
    reply = maybe_typo_curated(reply)
    reply = sanitize_reply(reply)
    return reply

# ============================================================
# 11) WEBHOOK
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    cleanup_processed()

    update = request.get_json(silent=True) or {}

    # handle only standard messages
    msg = update.get("message")
    if not msg:
        return "ok"

    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    uid = msg.get("from", {}).get("id", chat_id)

    # Ignore all slash commands for normal users (prevents /start replies)
    if text.startswith("/"):
        # allow admin commands only in admin chat
        if handle_admin_command(text, chat_id):
            return "ok"
        return "ok"

    # De dup key using update_id + message_id (more robust on retries)
    update_id = update.get("update_id")
    message_id = msg.get("message_id")
    dedup_key = f"{uid}:{update_id}:{message_id}"
    if dedup_key in processed:
        return "ok"
    processed[dedup_key] = time.time()

    if not text:
        return "ok"

    u = get_user(uid)

    # takeover: silent for this user
    if u.get("takeover"):
        return "ok"

    # rate limit
    if not allow_rate(u):
        return "ok"

    # Update basics
    u["messages"] += 1
    u["intent"] = detect_intent(text)
    u["last_seen_ts"] = time.time()

    # Phase logic (simple)
    if u["messages"] < 4:
        u["phase"] = 1
    elif u["intent"] == "buyer_intent":
        u["phase"] = 3
    else:
        u["phase"] = 2

    u["priority"] = (u["intent"] == "buyer_intent") or (u["messages"] >= 12)

    extract_profile(u, text)

    # Save history user turn
    u["history"].append({"role": "user", "content": text})
    u["history"] = u["history"][-HISTORY_TURNS:]

    # Onboarding on very first message
    if u["messages"] == 1:
        reply = sanitize_reply(onboarding_message())
        d = human_delay("casual", 1, False)
        wait_human(chat_id, d)
        send_message(chat_id, reply)
        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]
        return "ok"

    # FAQ fast answers for non link items (still human)
    faq = match_faq(text)
    if faq in FAQ_REPLIES and faq != "link":
        reply = FAQ_REPLIES[faq]
        reply = sanitize_reply(reply)
        d = human_delay(u["intent"], u["phase"], u["priority"])
        wait_human(chat_id, d)
        if random.random() < 0.10:
            reply = sanitize_reply(f"{pre_filler()} {reply}")
        send_message(chat_id, reply)
        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]
        return "ok"

    # Funnel override (fast direct but not robotic)
    handled, reply = funnel_reply(u, text)
    if handled and reply:
        # admin alert only to admin chat id, never to user
        if u["intent"] == "buyer_intent" and should_alert(u):
            mark_alert(u)
            label = u.get("profile", {}).get("name") or f"uid:{uid}"
            notify_admin(f"Hot intent ({label}) asked about Fanvue or private content. link_stage={u['link_stage']}")

        reply = sanitize_reply(reply)
        d = human_delay(u["intent"], u["phase"], u["priority"])
        wait_human(chat_id, d)
        if random.random() < 0.10:
            reply = sanitize_reply(f"{pre_filler()} {reply}")
        send_message(chat_id, reply)
        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]
        return "ok"

    # GPT normal response
    reply = gpt_reply(u)

    d = human_delay(u["intent"], u["phase"], u["priority"])
    wait_human(chat_id, d)
    if random.random() < 0.10:
        reply = sanitize_reply(f"{pre_filler()} {reply}")

    send_message(chat_id, reply)

    # Save assistant turn
    u["history"].append({"role": "assistant", "content": reply})
    u["history"] = u["history"][-HISTORY_TURNS:]

    return "ok"

# ============================================================
# 12) RENDER BINDING
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

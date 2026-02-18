import os
import time
import random
import re
import threading
import requests
from datetime import datetime
from flask import Flask, request, abort
from openai import OpenAI

app = Flask(__name__)

# ============================================================
# 0) CONFIG
# ============================================================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))  # optional
CRON_SECRET = os.environ.get("CRON_SECRET", "")            # optional but recommended

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

FANVUE_LINK = "https://www.fanvue.com/avelynnoira/fv-7"

MODEL = "gpt-4.1-mini"
MAX_OUTPUT_TOKENS = 180

# Anti-dup + anti-spam
PROCESSED_TTL_SECONDS = 60 * 20
MAX_MSGS_PER_MINUTE = 7

# Cooldowns
CTA_COOLDOWN_MINUTES = 12
ALERT_COOLDOWN_MINUTES = 25
REENGAGE_COOLDOWN_HOURS = 24

# History
HISTORY_TURNS = 12

# Human timing
MAX_DELAY_SECONDS = 22.0

# Intro pacing (avoid repeating)
INTRO_COOLDOWN_SECONDS = 60 * 30

# Link repeat safety
MAX_LINK_REPEATS = 1

# ============================================================
# 1) IN-MEMORY STATE (Render restart resets)
# ============================================================
memory = {}          # uid -> user_state
processed = {}       # key -> ts (message_id/update_id)
_lock = threading.Lock()

AB_VARIANTS = ["A", "B"]

# ============================================================
# 2) BIO (INTEGRATED)
# ============================================================
AVELYNS_BIO = """
Avelyn Noira is 21 years old.
She was born in Chengdu, China, under her birth name, Ava Lin — a name that belongs to her private history.

From an early age, Ava was observant. Quiet. Attentive to small movements others ignored. She learned to read rooms before she spoke in them.

When she was four years old, her life was marked — literally.

One afternoon, while playing near her family’s apartment courtyard, a metal advertising panel loosened during a sudden storm. The structure collapsed without warning. Ava was struck as she turned toward the sound. A sharp edge cut downward across her face — from her brow, over her right eye, and along her cheek.

The injury required emergency surgery. Doctors managed to save her eye, but partial vision loss remained. The vertical scar never fully faded.

Growing up, the scar separated her from other children. Questions. Stares. Silence. Over time, she stopped explaining. Instead, she adapted. She became sharper, more aware. She learned to rely on positioning, instinct, and anticipation rather than perfect sight.

As she grew older, structure became her form of stability. The gym offered repetition. Repetition offered control. Control offered peace.

In her late teens, she discovered padel — fast, reactive, timing-focused. For someone who learned to compensate her whole life, it felt natural.

When she moved to Europe, she chose to redefine herself publicly. Lin was inherited. Expected. Rooted in a life shaped by others.
She became Avelyn Noira.

Avelyn Noira is a boundary, not a rejection. Ava remains private. Avelyn is who the world meets.
""".strip()

# ============================================================
# 3) TELEGRAM HELPERS
# ============================================================
def tg_post(method: str, payload: dict):
    try:
        return requests.post(f"{BASE_URL}/{method}", json=payload, timeout=12).json()
    except Exception:
        return None

def send_message(chat_id: int, text: str):
    # sanitize: avoid leading dash lines
    text = sanitize_outgoing(text)
    tg_post("sendMessage", {"chat_id": chat_id, "text": text})

def send_typing(chat_id: int):
    tg_post("sendChatAction", {"chat_id": chat_id, "action": "typing"})

def notify_admin(text: str):
    if ADMIN_CHAT_ID:
        send_message(ADMIN_CHAT_ID, f"[ALERT] {text}")

# ============================================================
# 4) HOUSEKEEPING: DE-DUP + RATE LIMIT
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
# 5) HUMANIZATION
# ============================================================
def pre_filler():
    return random.choice(["hmm…", "wait…", "ok hold on…", "lol…", "mmm…"])

def wait_human(chat_id: int, total_seconds: float):
    total_seconds = max(0.0, float(total_seconds))
    total_seconds = min(total_seconds, MAX_DELAY_SECONDS)

    seen_delay = min(random.uniform(0.4, 2.0), total_seconds)
    time.sleep(seen_delay)
    remaining = total_seconds - seen_delay

    while remaining > 0:
        burst = min(random.uniform(1.2, 4.2), remaining)
        send_typing(chat_id)
        time.sleep(burst)
        remaining -= burst
        if remaining <= 0:
            break
        pause = min(random.uniform(0.3, 1.4), remaining)
        time.sleep(pause)
        remaining -= pause

def human_delay(intent: str, phase: int, priority: bool) -> float:
    if intent == "buyer_intent":
        d = random.uniform(2.0, 7.5)
    elif phase >= 2:
        d = random.uniform(4.0, 12.5)
    else:
        d = random.uniform(5.5, 16.0)

    if priority:
        d = max(1.8, d - random.uniform(0.5, 2.8))

    d += random.uniform(0.0, 1.8)
    return min(d, MAX_DELAY_SECONDS)

def maybe_shorten(text: str) -> str:
    t = " ".join(text.split())
    if len(t) > 260:
        t = t[:260].rsplit(" ", 1)[0] + "…"
    return t

def maybe_typo_curated(text: str) -> str:
    # small human vibe, low rate
    if random.random() > 0.03:
        return text
    replacements = [
        ("you", "u"),
        ("okay", "ok"),
        ("really", "rly"),
        ("because", "bc"),
        ("i'm", "im"),
        ("I’m", "Im"),
    ]
    out = text
    for a, b in replacements:
        if re.search(rf"\b{re.escape(a)}\b", out, flags=re.IGNORECASE) and random.random() < 0.30:
            out = re.sub(rf"\b{re.escape(a)}\b", b, out, count=1, flags=re.IGNORECASE)
    return out

def sanitize_outgoing(text: str) -> str:
    # Remove bullet/list vibes and dash lines
    t = text.replace("\n- ", "\n").replace("\n• ", "\n")
    # Avoid lines that start with "-"
    t = re.sub(r"(?m)^\-\s*", "", t)
    # Remove double blank lines
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t

# ============================================================
# 6) INTENT + FAQ
# ============================================================
def detect_intent(text: str) -> str:
    t = text.lower()
    fan_keywords = ["fanvue", "subscribe", "subscription", "sub", "link", "account", "join", "full access"]
    spicy = ["spicy", "nudes", "nude", "nsfw", "onlyfans", "explicit"]
    if any(k in t for k in fan_keywords) or any(k in t for k in spicy):
        return "buyer_intent"
    if len(t.strip()) <= 3:
        return "low_effort"
    return "casual"

def warm_trigger(text: str) -> bool:
    t = text.lower()
    triggers = ["private", "exclusive", "more", "only", "subscribe", "fanvue", "link", "full access"]
    return any(x in t for x in triggers)

FAQ_REPLIES = {
    "age": "she’s 21 😌",
    "where": "she’s originally from Chengdu, China, and lives in Europe now 🤍",
    "scar": "yeah… she got it when she was 4. freak accident. she never hides it 😌",
    "name": "Avelyn is her public name. Ava Lin is private.",
    "safe": "Fanvue is official, and you’ll always see the final price before confirming 😌",
    "price": "you’ll see the exact price on the page before you confirm anything 🤍",
    "what_you_get": "Telegram is the inner circle preview. Fanvue is the full private access + customs + personal replies 😇",
}

def match_bio_faq(text: str):
    t = text.lower()
    if any(k in t for k in ["how old", "age"]):
        return "age"
    if any(k in t for k in ["where are you from", "from where", "chengdu", "china"]):
        return "where"
    if any(k in t for k in ["scar", "eye", "accident"]):
        return "scar"
    if any(k in t for k in ["real name", "ava", "lin", "avel"]):
        return "name"
    if any(k in t for k in ["safe", "secure", "legit", "scam"]):
        return "safe"
    if any(k in t for k in ["price", "cost", "how much"]):
        return "price"
    if any(k in t for k in ["what do i get", "what’s inside", "whats inside", "what do you post", "content"]):
        return "what_you_get"
    return None

def is_affirmative(text: str) -> bool:
    t = re.sub(r"[^a-z0-9\s]", "", text.strip().lower()).strip()
    yes = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "send", "send it", "give", "give it", "please", "pls"}
    return t in yes

# ============================================================
# 7) USER STATE + ADMIN CONTROL
# ============================================================
def get_user(uid: int):
    if uid not in memory:
        memory[uid] = {
            "messages": 0,
            "warm": 0,
            "phase": 1,
            "intent": "casual",
            "priority": False,

            "history": [],
            "rate_window": [],
            "variant": random.choice(AB_VARIANTS),

            "link_stage": 0,           # 0 none, 1 teased, 2 sent
            "last_cta_ts": 0.0,
            "link_sent_count": 0,

            "last_alert_ts": 0.0,
            "last_seen_ts": time.time(),
            "last_reengage_ts": 0.0,

            "intro_sent_ts": 0.0,
        }
    return memory[uid]

def can_cta(u: dict) -> bool:
    return (time.time() - u.get("last_cta_ts", 0.0)) > (CTA_COOLDOWN_MINUTES * 60)

def mark_cta(u: dict):
    u["last_cta_ts"] = time.time()

def should_alert(u: dict) -> bool:
    return (time.time() - u.get("last_alert_ts", 0.0)) > (ALERT_COOLDOWN_MINUTES * 60)

def mark_alert(u: dict):
    u["last_alert_ts"] = time.time()

def handle_admin_command(text: str, chat_id: int):
    if not ADMIN_CHAT_ID or chat_id != ADMIN_CHAT_ID:
        return False

    parts = text.strip().split()
    cmd = parts[0].lower()

    def usage():
        send_message(chat_id, "Commands:\n/status <uid>\n/reset <uid>")

    if cmd == "/status":
        if len(parts) < 2:
            usage()
            return True
        uid = int(parts[1])
        u = memory.get(uid)
        if not u:
            send_message(chat_id, f"User {uid} not found in memory.")
            return True
        send_message(chat_id, f"uid={uid}\nmsg={u['messages']}\nintent={u['intent']}\nphase={u['phase']}\nwarm={u['warm']}\nlink_stage={u['link_stage']}\nlink_sent_count={u['link_sent_count']}")
        return True

    if cmd == "/reset":
        if len(parts) < 2:
            usage()
            return True
        uid = int(parts[1])
        memory.pop(uid, None)
        send_message(chat_id, f"reset {uid} ok")
        return True

    return False

# ============================================================
# 8) INTRO + FUNNEL (DIRECT, WARM, NOT BOTTY)
# ============================================================
INTRO_LINES = [
    "hey, I’m Avelyn’s assistant 🤍\nthis is her inner circle preview. Fanvue is where you get the full private side + personal replies.",
    "hi love, I help manage Avelyn’s private messages 😌\nTelegram is for quick vibes. Fanvue is the full access."
]

def maybe_send_intro(u: dict, chat_id: int):
    now = time.time()
    if u["messages"] <= 2 and (now - u.get("intro_sent_ts", 0.0)) > INTRO_COOLDOWN_SECONDS:
        u["intro_sent_ts"] = now
        intro = random.choice(INTRO_LINES)
        d = human_delay(u["intent"], u["phase"], u["priority"])
        wait_human(chat_id, d)
        send_message(chat_id, intro)
        return True
    return False

def commercial_reply(u: dict, user_text: str):
    t = user_text.strip().lower()
    direct = any(k in t for k in ["fanvue", "link", "subscribe", "subscription", "account", "join", "full access"])
    spicy = any(k in t for k in ["spicy", "nudes", "nude", "nsfw", "onlyfans", "explicit"])

    # If user asks spicy -> redirect to Fanvue (no explicit)
    if spicy and u["link_stage"] == 0:
        u["link_stage"] = 1
        mark_cta(u)
        return True, "she keeps that side off Telegram 😇\nFanvue is where you get the private drops + customs. want the link?"

    # If teased and user confirms -> send link
    if u["link_stage"] == 1 and is_affirmative(user_text):
        u["link_stage"] = 2
        u["link_sent_count"] = u.get("link_sent_count", 0) + 1
        return True, f"here you go 🤍\n{FANVUE_LINK}"

    # If link already sent, avoid repeating too much
    if u["link_stage"] == 2 and (direct or spicy):
        if u.get("link_sent_count", 0) <= MAX_LINK_REPEATS:
            u["link_sent_count"] += 1
            return True, f"here you go again 🤍\n{FANVUE_LINK}"
        return True, "I already sent it 😌 if you want, I can tell you what you unlock there."

    # Direct ask: send fast (direct funnel)
    if direct and u["link_stage"] == 0:
        u["link_stage"] = 2
        u["link_sent_count"] = u.get("link_sent_count", 0) + 1
        return True, f"got you 😮‍💨\n{FANVUE_LINK}"

    # Soft bridge (subtle funnel)
    if u["phase"] >= 2 and u["link_stage"] == 0 and can_cta(u) and random.random() < 0.22:
        u["link_stage"] = 1
        mark_cta(u)
        return True, "real question… are you here for the inner circle preview, or for her private side? 😌"

    return False, None

# ============================================================
# 9) GPT RESPONSE (ASSISTANT, ENGLISH ONLY, NO DASHES)
# ============================================================
def build_system_prompt(u: dict) -> str:
    variant_line = (
        "Variant A: slightly bolder, playful, confident."
        if u["variant"] == "A"
        else "Variant B: softer, calm, reassuring."
    )

    return f"""
You are Avelyn Noira’s assistant.
You manage DMs in her Telegram “inner circle preview” and guide users to Fanvue for full private access.

NON-NEGOTIABLE:
- Reply in English only, even if the user writes in another language.
- No bullet points. No lists. No lines starting with "-".
- Keep replies short: 1–2 sentences usually.
- Do not sound like an AI assistant. No lectures. No generic filler.

ROLE + TONE:
- Warm, human, slightly flirty but classy.
- Telegram is a preview. Fanvue is full private access.
- You can chat lightly on Telegram, but avoid making it feel like the user “already has her”.
- Keep it inviting, not cold. No robotic phrases like “Say yes to…”.

SAFETY:
- No explicit sexual content.
- No meetups.

BIO (use when asked about her story):
{AVELYNS_BIO}

FANVUE POSITIONING (use naturally when relevant):
- Full private side, private drops, customs, and personal replies happen on Fanvue.
- If user asks for link, give it quickly and smoothly, without hoops.

{variant_line}

Write the next message now.
""".strip()

def gpt_reply(u: dict, user_text: str) -> str:
    system_prompt = build_system_prompt(u)
    resp = client.responses.create(
        model=MODEL,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        input=[
            {"role": "system", "content": system_prompt},
            *u["history"],
        ],
    )
    reply = (resp.output_text or "").strip()
    reply = sanitize_outgoing(reply)
    reply = maybe_shorten(reply)
    reply = maybe_typo_curated(reply)
    return reply

# ============================================================
# 10) RE-ENGAGEMENT (CRON)
# ============================================================
def eligible_for_reengage(u: dict) -> bool:
    now_ts = time.time()
    inactive_hours = (now_ts - u.get("last_seen_ts", now_ts)) / 3600.0
    since_last = (now_ts - u.get("last_reengage_ts", 0.0)) / 3600.0
    return inactive_hours >= REENGAGE_COOLDOWN_HOURS and since_last >= REENGAGE_COOLDOWN_HOURS

def build_reengage_message() -> str:
    return "hey… you disappeared 😌 you still around?"

@app.route("/cron", methods=["GET"])
def cron():
    if CRON_SECRET:
        token = request.args.get("token", "")
        if token != CRON_SECRET:
            abort(403)

    sent = 0
    with _lock:
        items = list(memory.items())

    for uid, u in items:
        if eligible_for_reengage(u):
            send_message(uid, build_reengage_message())
            u["last_reengage_ts"] = time.time()
            sent += 1
            if sent >= 20:
                break

    return {"ok": True, "sent": sent}

# ============================================================
# 11) CORE HANDLER (RUNS IN BACKGROUND THREAD)
#     Fixes duplicate messages caused by Telegram retries/timeouts.
# ============================================================
def process_update_async(update: dict):
    try:
        msg = update.get("message")
        if not msg:
            return

        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        if not text:
            return

        # Ignore /start and any command-like (except admin in admin chat)
        if text.startswith("/"):
            if handle_admin_command(text, chat_id):
                return
            return  # ignore all other commands including /start

        uid = msg.get("from", {}).get("id", chat_id)

        with _lock:
            u = get_user(uid)

            # rate limit
            if not allow_rate(u):
                return

            # update basic state
            u["messages"] += 1
            u["intent"] = detect_intent(text)
            u["last_seen_ts"] = time.time()

            if warm_trigger(text):
                u["warm"] += 1

            # phase (simple)
            if u["messages"] < 4:
                u["phase"] = 1
            elif u["warm"] >= 1:
                u["phase"] = 2
            else:
                u["phase"] = 2

            # priority if buyer-intent
            u["priority"] = (u["intent"] == "buyer_intent")

            # history: user turn
            u["history"].append({"role": "user", "content": text})
            u["history"] = u["history"][-HISTORY_TURNS:]

        # intro (only on first interactions)
        if maybe_send_intro(u, chat_id):
            return

        # bio/faq quick replies if asked
        faq = match_bio_faq(text)
        if faq and faq in FAQ_REPLIES and random.random() < 0.70:
            d = human_delay(u["intent"], u["phase"], u["priority"])
            wait_human(chat_id, d)
            reply = FAQ_REPLIES[faq]
            if random.random() < 0.10:
                reply = f"{pre_filler()} {reply}"
            send_message(chat_id, reply)
            return

        # funnel override
        handled, reply = commercial_reply(u, text)
        if handled and reply:
            if u["intent"] == "buyer_intent" and should_alert(u):
                mark_alert(u)
                notify_admin(f"Hot intent (uid:{uid}) asked about Fanvue/link. stage={u['link_stage']}")

            d = human_delay(u["intent"], u["phase"], u["priority"])
            wait_human(chat_id, d)
            if random.random() < 0.10:
                reply = f"{pre_filler()} {reply}"
            send_message(chat_id, reply)
            return

        # GPT fallback
        reply = gpt_reply(u, text)

        with _lock:
            u["history"].append({"role": "assistant", "content": reply})
            u["history"] = u["history"][-HISTORY_TURNS:]

        d = human_delay(u["intent"], u["phase"], u["priority"])
        wait_human(chat_id, d)

        if random.random() < 0.10:
            reply = f"{pre_filler()} {reply}"

        send_message(chat_id, reply)

    except Exception as e:
        # Keep server healthy; optional admin ping
        try:
            if ADMIN_CHAT_ID:
                notify_admin(f"Error in process_update_async: {type(e).__name__}")
        except Exception:
            pass

# ============================================================
# 12) WEBHOOK (FAST ACK)
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    cleanup_processed()
    update = request.get_json(silent=True) or {}

    # Dedup using update_id + message_id keys (important for retries)
    update_id = update.get("update_id")
    msg = update.get("message") or {}
    message_id = msg.get("message_id")

    key_parts = []
    if update_id is not None:
        key_parts.append(f"u{update_id}")
    if message_id is not None:
        key_parts.append(f"m{message_id}")

    key = "|".join(key_parts) if key_parts else None
    if key:
        with _lock:
            if key in processed:
                return "ok"
            processed[key] = time.time()

    # Return immediately, process in background
    t = threading.Thread(target=process_update_async, args=(update,), daemon=True)
    t.start()
    return "ok"

# ============================================================
# 13) HEALTHCHECK (OPTIONAL)
# ============================================================
@app.route("/", methods=["GET"])
def home():
    return {"ok": True, "service": "avel-telegram-bot", "time": datetime.utcnow().isoformat()}

# ============================================================
# 14) RENDER BINDING
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

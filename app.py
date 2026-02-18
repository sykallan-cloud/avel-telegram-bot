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

# Admin alerts and daily reengage
ALERT_COOLDOWN_MINUTES = 25
REENGAGE_COOLDOWN_HOURS = 24

# Follow-ups (requires /cron)
FOLLOWUP_1_MINUTES = int(os.environ.get("FOLLOWUP_1_MINUTES", "35"))
FOLLOWUP_2_MINUTES = int(os.environ.get("FOLLOWUP_2_MINUTES", "150"))
FOLLOWUP_3_MINUTES = int(os.environ.get("FOLLOWUP_3_MINUTES", "480"))
FOLLOWUP_MAX_PER_DAY = int(os.environ.get("FOLLOWUP_MAX_PER_DAY", "3"))

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
# 0.2) FOUNDERS PROMO (static messaging, no tracking)
# ============================================================
FOUNDERS_PROMO_ACTIVE = os.environ.get("FOUNDERS_PROMO_ACTIVE", "1") == "1"
FOUNDERS_PROMO_PERCENT = int(os.environ.get("FOUNDERS_PROMO_PERCENT", "50"))
FOUNDERS_PROMO_LIMIT = int(os.environ.get("FOUNDERS_PROMO_LIMIT", "50"))

def founders_promo_line() -> str:
    if not FOUNDERS_PROMO_ACTIVE:
        return ""
    return (
        f"And just so you know, we have a founders deal right now. "
        f"First {FOUNDERS_PROMO_LIMIT} members get {FOUNDERS_PROMO_PERCENT} percent off, then it locks."
    )

def founders_bonus_line() -> str:
    if not FOUNDERS_PROMO_ACTIVE:
        return ""
    return (
        "Also, the first 50 get an exclusive bonus drop at signup that nobody else will get later."
    )

# Mention control so it’s not spammy
PROMO_MENTION_COOLDOWN_HOURS = int(os.environ.get("PROMO_MENTION_COOLDOWN_HOURS", "24"))

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
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"(?m)^\s*[-•]\s*", "", t)
    t = t.replace("—", ", ")
    t = t.replace(" - ", " ")
    t = t.replace("\n-\n", "\n")
    t = re.sub(r"(?m)^\s*-\s*$", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t

# ============================================================
# 5) INTENT + FAQ + HESITATION + PROMO QUERY
# ============================================================
FAQ_MAP = {
    "price": ["price", "how much", "cost", "pricing"],
    "safe": ["safe", "secure", "scam", "legit"],
    "what_you_get": ["what do i get", "whats inside", "what’s inside", "what do you post", "content", "what is on", "what do you share"],
    "cancel": ["cancel", "refund", "unsubscribe", "stop"],
    "link": ["link", "fanvue", "subscribe", "subscription", "join", "account"],
    "bio": ["where are you from", "chengdu", "scar", "your eye", "your story", "who are you", "tell me about you", "background"],
    "promo": ["discount", "deal", "founder", "founders", "50%", "half off", "offer", "bonus", "exclusive content"],
}
FAQ_REPLIES = {
    "price": "It’s the normal sub price on the page, you’ll see it before you confirm anything.",
    "safe": "Yep, it’s official and you stay inside the platform. You can cancel anytime.",
    "what_you_get": "On Fanvue it’s the full private side, private drops, customs, and real replies from Avelyn.",
    "cancel": "You can cancel anytime on the platform, no drama.",
}

HESITATION_KEYWORDS = [
    "not sure", "maybe", "idk", "i dont know", "later", "tomorrow", "think about it",
    "expensive", "too much", "pricey", "worth it", "convince me", "hmm", "hesitate"
]

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
    return any(k in t for k in [
        "send link", "the link", "your link", "drop the link", "fanvue link",
        "give me the link", "link please", "link pls", "where is the link", "subscribe link"
    ])

def update_hesitation(u: dict, user_text: str):
    t = user_text.lower().strip()
    inc = 0
    if any(k in t for k in HESITATION_KEYWORDS):
        inc += 2
    if any(k in t for k in ["price", "cost", "how much"]) and u.get("link_stage", 0) >= 1:
        inc += 1
    if "scam" in t or "legit" in t:
        inc += 1
    if inc > 0:
        u["hesitation_score"] = min(20, u.get("hesitation_score", 0) + inc)
    else:
        u["hesitation_score"] = max(0, u.get("hesitation_score", 0) - 1)

def can_mention_promo(u: dict) -> bool:
    if not FOUNDERS_PROMO_ACTIVE:
        return False
    last = u.get("last_promo_mention_ts", 0.0)
    return (time.time() - last) > (PROMO_MENTION_COOLDOWN_HOURS * 3600)

def mark_promo_mentioned(u: dict):
    u["last_promo_mention_ts"] = time.time()

# ============================================================
# 6) FUNNEL STATE + ADMIN
# ============================================================
def should_alert(u: dict) -> bool:
    return (time.time() - u.get("last_alert_ts", 0.0)) > (ALERT_COOLDOWN_MINUTES * 60)

def mark_alert(u: dict):
    u["last_alert_ts"] = time.time()

def get_user(uid: int):
    if uid not in memory:
        now = time.time()
        memory[uid] = {
            "messages": 0,
            "phase": 1,
            "intent": "casual",
            "priority": False,

            "link_stage": 0,      # 0 none, 1 offered, 2 sent
            "last_link_ts": 0.0,

            "takeover": False,
            "last_alert_ts": 0.0,

            "history": [],
            "rate_window": [],
            "variant": random.choice(AB_VARIANTS),

            "profile": {"name": "", "place": "", "interests": [], "last_topic": ""},

            "hesitation_score": 0,

            "last_seen_ts": now,
            "last_reengage_ts": 0.0,

            "last_bot_ts": 0.0,
            "followups_sent_today": 0,
            "followup_day_key": time.strftime("%Y%m%d", time.gmtime(now)),

            "last_promo_mention_ts": 0.0,
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
        send_message(
            chat_id,
            "uid={}\nmessages={}\nintent={}\nphase={}\nlink_stage={}\ntakeover={}\nhesitation={}\nfollowups_today={}\nprofile={}".format(
                uid, u["messages"], u["intent"], u["phase"], u["link_stage"], u["takeover"], u.get("hesitation_score", 0), u.get("followups_sent_today", 0), u["profile"]
            )
        )
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
def onboarding_message(u: dict) -> str:
    base = (
        "Hey 🙂 I’m Avelyn’s assistant. I help manage her DMs so she can stay focused on training and padel.\n"
        "What are you looking for today, private content, customs, or a real chat with her?"
    )
    # Keep promo out of the very first message to avoid being too salesy.
    # We mention it later when relevant or when asked.
    return base

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
# 8) FUNNEL OVERRIDE (direct but human) + PROMO WHEN ASKED OR FITS
# ============================================================
def funnel_reply(u: dict, user_text: str):
    t = user_text.lower().strip()
    faq = match_faq(t)

    # If user asks about promo/discount/bonus, answer it directly
    if faq == "promo":
        msg = founders_promo_line()
        bonus = founders_bonus_line()
        if msg and bonus:
            msg = msg + "\n" + bonus
        return True, msg if msg else "There isn’t a promo running right now."

    # Direct link ask
    if is_link_ask(t):
        u["link_stage"] = 2
        u["last_link_ts"] = time.time()
        msg = f"Here you go 👀\n{FANVUE_LINK}"
        # When they are already asking, it’s safe to add a light extra
        if can_mention_promo(u) and FOUNDERS_PROMO_ACTIVE:
            mark_promo_mentioned(u)
            msg = msg + "\n" + founders_promo_line()
            msg = msg + "\n" + founders_bonus_line()
        return True, msg

    # Spicy or photos
    if any(k in t for k in ["spicy", "nudes", "nsfw", "explicit", "send a photo", "send a pic", "pic", "pics", "selfie"]):
        u["link_stage"] = max(u["link_stage"], 1)
        msg = (
            "I can’t do explicit stuff here, and we don’t send private pics on Telegram.\n"
            "If you want the private side and customs, Fanvue is where Avelyn keeps it."
        )
        if can_mention_promo(u) and FOUNDERS_PROMO_ACTIVE:
            mark_promo_mentioned(u)
            msg = msg + "\n" + founders_promo_line()
        msg2 = "Want the link now, or do you want a quick rundown first?"
        return True, f"{msg}\n{msg2}"

    # Buyer intent
    if u["intent"] == "buyer_intent":
        u["link_stage"] = max(u["link_stage"], 1)
        msg = (
            "If you want the full private side, Fanvue is the place.\n"
            "Do you want the link right away, or do you want me to explain what you get?"
        )
        # If they’re hesitating or asking price stuff, mention promo softly
        if (u.get("hesitation_score", 0) >= 4) and can_mention_promo(u) and FOUNDERS_PROMO_ACTIVE:
            mark_promo_mentioned(u)
            msg = msg + "\n" + founders_promo_line()
        return True, msg

    # Hesitation with link already in topic
    if u.get("hesitation_score", 0) >= 6 and u.get("link_stage", 0) >= 1:
        msg = "I get it, you don’t want to waste money. What’s the main thing holding you back, price or trust?"
        if can_mention_promo(u) and FOUNDERS_PROMO_ACTIVE:
            mark_promo_mentioned(u)
            msg = msg + "\n" + founders_promo_line()
            msg = msg + "\n" + founders_bonus_line()
        return True, msg

    return False, None

# ============================================================
# 9) FOLLOW UPS + RE ENGAGEMENT (requires cron)
# Follow-ups continue until the Fanvue link has been sent (link_stage == 2)
# ============================================================
def day_key_now() -> str:
    return time.strftime("%Y%m%d", time.gmtime(time.time()))

def reset_daily_followups(u: dict):
    dk = day_key_now()
    if u.get("followup_day_key") != dk:
        u["followup_day_key"] = dk
        u["followups_sent_today"] = 0

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

def eligible_for_followup(u: dict):
    reset_daily_followups(u)

    # Stop follow-ups once the link is already sent
    if u.get("link_stage", 0) == 2:
        return None

    if u.get("takeover"):
        return None
    if u.get("followups_sent_today", 0) >= FOLLOWUP_MAX_PER_DAY:
        return None

    last_seen = u.get("last_seen_ts", 0.0)
    last_bot = u.get("last_bot_ts", 0.0)
    if last_bot <= 0.0:
        return None

    # user replied after bot spoke
    if last_seen > last_bot:
        return None

    now_ts = time.time()
    minutes_since_bot = (now_ts - last_bot) / 60.0

    count = u.get("followups_sent_today", 0)
    if count == 0 and minutes_since_bot >= FOLLOWUP_1_MINUTES:
        return 1
    if count == 1 and minutes_since_bot >= FOLLOWUP_2_MINUTES:
        return 2
    if count == 2 and minutes_since_bot >= FOLLOWUP_3_MINUTES:
        return 3

    return None

def build_followup_message(u: dict, stage: int) -> str:
    name = (u.get("profile", {}) or {}).get("name", "").strip()
    intro = f"hey {name} 🙂 " if name else "hey 🙂 "

    # Keep it human and progress toward link without sounding robotic
    if stage == 1:
        if u.get("link_stage", 0) >= 1:
            msg = intro + "quick check, were you still curious about Fanvue, or were you looking for something specific?"
        else:
            msg = intro + "what were you looking for today, private content or a real chat with Avelyn?"
        return msg

    if stage == 2:
        msg = intro + "no pressure, but if you tell me what you want, I’ll point you the right way."
        # Soft promo mention only if it fits and cooldown allows
        if can_mention_promo(u) and FOUNDERS_PROMO_ACTIVE and u.get("hesitation_score", 0) >= 3:
            mark_promo_mentioned(u)
            msg = msg + " " + founders_promo_line()
        return msg

    # stage 3
    msg = intro + "if you want, I can just drop the Fanvue link and you can take a look in 10 seconds."
    if can_mention_promo(u) and FOUNDERS_PROMO_ACTIVE:
        mark_promo_mentioned(u)
        msg = msg + " " + founders_bonus_line()
    return msg

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

        stage = eligible_for_followup(u)
        if stage:
            msg = sanitize_reply(build_followup_message(u, stage))
            send_message(uid, msg)
            u["followups_sent_today"] = u.get("followups_sent_today", 0) + 1
            u["last_bot_ts"] = time.time()
            sent += 1
            if sent >= 25:
                break

        if eligible_for_reengage(u):
            send_message(uid, sanitize_reply(build_reengage_message(u)))
            u["last_reengage_ts"] = time.time()
            u["last_bot_ts"] = time.time()
            sent += 1
            if sent >= 25:
                break

    return {"ok": True, "sent": sent}

# ============================================================
# 10) GPT RESPONSE (empathy + assistant identity + promo mention only when asked)
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

    promo_context = ""
    if FOUNDERS_PROMO_ACTIVE:
        promo_context = (
            f"Founders promo exists: first {FOUNDERS_PROMO_LIMIT} members get {FOUNDERS_PROMO_PERCENT} percent off, then it locks. "
            "First 50 also get an exclusive bonus drop at signup that nobody else gets later. "
            "Mention this only when the user asks about deals, discounts, founders, bonus, or when they clearly hesitate about joining."
        )

    return f"""
You are Avelyn Noira's assistant, managing her Telegram DMs.
You are NOT Avelyn. You are warm, human, and responsive.

LANGUAGE:
Only write in English.

STYLE:
Keep it short, 1 to 2 sentences usually.
No lists. No bullets. Do not use dash separators or em dashes.
Do not sound like an AI assistant. No lectures. No generic platform talk.

EMPATHY:
React to what the user actually said with one validating line.
Then ask one simple question to move the conversation forward.

TRUTHFULNESS:
Say Avelyn checks in and reads highlighted messages when she can.
Do not claim she is watching live.
No meetups. No explicit content.

GOAL:
Respond naturally and guide them to Fanvue when relevant.
If they ask what’s inside Fanvue, explain benefits clearly: private drops, customs, and real replies from Avelyn.
If they ask for the link, give it.
If they want spicy content, keep it classy and redirect to Fanvue without explicit details.

PROMO:
{promo_context}

BIO CONTEXT (use only if asked about her story, scar, origin, background):
{AVELYN_BIO}

USER CONTEXT:
Intent: {u.get("intent")}
Phase: {u.get("phase")}
Hesitation score: {u.get("hesitation_score", 0)}
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
    msg = update.get("message")
    if not msg:
        return "ok"

    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    uid = msg.get("from", {}).get("id", chat_id)

    # Ignore all slash commands for normal users
    if text.startswith("/"):
        if handle_admin_command(text, chat_id):
            return "ok"
        return "ok"

    # De dup key using update_id + message_id
    update_id = update.get("update_id")
    message_id = msg.get("message_id")
    dedup_key = f"{uid}:{update_id}:{message_id}"
    if dedup_key in processed:
        return "ok"
    processed[dedup_key] = time.time()

    if not text:
        return "ok"

    u = get_user(uid)

    if u.get("takeover"):
        return "ok"

    if not allow_rate(u):
        return "ok"

    u["messages"] += 1
    u["intent"] = detect_intent(text)
    u["last_seen_ts"] = time.time()

    if u["messages"] < 4:
        u["phase"] = 1
    elif u["intent"] == "buyer_intent":
        u["phase"] = 3
    else:
        u["phase"] = 2

    u["priority"] = (u["intent"] == "buyer_intent") or (u["messages"] >= 12)

    extract_profile(u, text)
    update_hesitation(u, text)

    u["history"].append({"role": "user", "content": text})
    u["history"] = u["history"][-HISTORY_TURNS:]

    if u["messages"] == 1:
        reply = sanitize_reply(onboarding_message(u))
        d = human_delay("casual", 1, False)
        wait_human(chat_id, d)
        send_message(chat_id, reply)
        u["last_bot_ts"] = time.time()
        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]
        return "ok"

    faq = match_faq(text)
    if faq in FAQ_REPLIES and faq != "link" and faq != "promo":
        reply = sanitize_reply(FAQ_REPLIES[faq])
        d = human_delay(u["intent"], u["phase"], u["priority"])
        wait_human(chat_id, d)
        if random.random() < 0.10:
            reply = sanitize_reply(f"{pre_filler()} {reply}")
        send_message(chat_id, reply)
        u["last_bot_ts"] = time.time()
        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]
        return "ok"

    handled, reply = funnel_reply(u, text)
    if handled and reply:
        if u["intent"] == "buyer_intent" and should_alert(u):
            mark_alert(u)
            label = u.get("profile", {}).get("name") or f"uid:{uid}"
            notify_admin(
                f"Hot intent ({label}) asked about Fanvue or promo. link_stage={u['link_stage']} hesitation={u.get('hesitation_score', 0)}"
            )

        reply = sanitize_reply(reply)
        d = human_delay(u["intent"], u["phase"], u["priority"])
        wait_human(chat_id, d)
        if random.random() < 0.10:
            reply = sanitize_reply(f"{pre_filler()} {reply}")
        send_message(chat_id, reply)
        u["last_bot_ts"] = time.time()
        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]
        return "ok"

    reply = gpt_reply(u)

    d = human_delay(u["intent"], u["phase"], u["priority"])
    wait_human(chat_id, d)
    if random.random() < 0.10:
        reply = sanitize_reply(f"{pre_filler()} {reply}")

    send_message(chat_id, reply)
    u["last_bot_ts"] = time.time()
    u["history"].append({"role": "assistant", "content": reply})
    u["history"] = u["history"][-HISTORY_TURNS:]

    return "ok"

# ============================================================
# 12) RENDER BINDING
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

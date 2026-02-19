import os
import time
import random
import re
import requests
from flask import Flask, request, abort
from openai import OpenAI

import json
import gspread
from google.oauth2.service_account import Credentials
from typing import Optional

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

# Admin alerts + re-engage
ALERT_COOLDOWN_MINUTES = 25
REENGAGE_COOLDOWN_HOURS = 24

# Follow-ups (requires /cron pinging)
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
PROMO_MENTION_COOLDOWN_HOURS = int(os.environ.get("PROMO_MENTION_COOLDOWN_HOURS", "24"))

def founders_promo_line() -> str:
    if not FOUNDERS_PROMO_ACTIVE:
        return ""
    return (
        f"Just so you know, we have a founders deal right now. "
        f"First {FOUNDERS_PROMO_LIMIT} members get {FOUNDERS_PROMO_PERCENT} percent off, then it locks."
    )

def founders_bonus_line() -> str:
    if not FOUNDERS_PROMO_ACTIVE:
        return ""
    return "Also, the first 50 get an exclusive bonus drop at signup that nobody else will get later."

# ============================================================
# 0.3) GOOGLE SHEETS DASHBOARD (optional)
# ============================================================
SHEET_LOGGING_ENABLED = os.environ.get("SHEET_LOGGING_ENABLED", "1") == "1"
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

sheet = None
if SHEET_LOGGING_ENABLED and GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID:
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        # Render env often escapes newlines in the private key
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

        # Ensure header row exists
        if sheet.row_values(1) == []:
            sheet.append_row([
                "ts_utc",
                "event",
                "uid",
                "name",
                "intent",
                "phase",
                "link_stage",
                "hesitation_score",
                "messages",
                "followups_today",
                "last_seen_utc",
                "text_preview",
                "status",
            ], value_input_option="USER_ENTERED")
        print("✅ Google Sheet connected")
    except Exception as e:
        sheet = None
        print("❌ Google Sheet connect failed:", e)

# ============================================================
# 1) IN-MEMORY STATE (Render restart resets)
# ============================================================
memory = {}      # uid -> user_state dict
processed = {}   # dedup_key -> ts

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

def notify_admin(text: str, current_uid: Optional[int] = None):
    """
    Important: never send admin alerts to the same uid that is chatting.
    This avoids the common misconfig where ADMIN_CHAT_ID accidentally equals a test user id.
    """
    if not ADMIN_CHAT_ID or int(ADMIN_CHAT_ID) == 0:
        return
    if current_uid is not None and int(ADMIN_CHAT_ID) == int(current_uid):
        return
    send_message(ADMIN_CHAT_ID, f"[ALERT] {text}")

# ============================================================
# 2.5) SHEET LOGGING HELPERS
# ============================================================
def _utc_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time()))

def calculate_status(u: dict) -> str:
    if u.get("link_stage", 0) == 2:
        return "LinkSent"
    if u.get("intent") == "buyer_intent":
        return "Hot"
    if u.get("hesitation_score", 0) >= 4:
        return "Warm"
    return "Cold"

def sheet_log(event: str, uid: int, u: dict, text_preview: str = ""):
    if not sheet:
        return
    try:
        p = u.get("profile", {}) or {}
        name = p.get("name", "")
        last_seen = u.get("last_seen_ts", 0.0)
        last_seen_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(last_seen)) if last_seen else ""
        preview = (text_preview or "").strip().replace("\n", " ")
        if len(preview) > 140:
            preview = preview[:140] + "…"

        row = [
            _utc_ts(),
            event,
            uid,
            name,
            u.get("intent", ""),
            u.get("phase", ""),
            u.get("link_stage", ""),
            u.get("hesitation_score", ""),
            u.get("messages", ""),
            u.get("followups_sent_today", ""),
            last_seen_utc,
            preview,
            calculate_status(u),
        ]
        sheet.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print("❌ Sheet logging error:", e)

# ============================================================
# 3) HOUSEKEEPING: DE-DUP + RATE LIMIT
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
    """
    Remove bullet/list vibe and dash separators.
    """
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"(?m)^\s*[-•]\s*", "", t)
    t = t.replace("—", ", ")
    t = t.replace(" - ", " ")
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
# 6) USER STATE + ADMIN
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

            # funnel
            "link_stage": 0,      # 0 none, 1 offered, 2 sent
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

            # persuasion signals
            "hesitation_score": 0,
            "last_promo_mention_ts": 0.0,

            # timing
            "last_seen_ts": now,
            "last_reengage_ts": 0.0,
            "last_bot_ts": 0.0,

            # follow-ups per day
            "followups_sent_today": 0,
            "followup_day_key": time.strftime("%Y%m%d", time.gmtime(now)),
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
    return (
        "Hey 🙂 I’m Avelyn’s assistant. I help manage her DMs so she can stay focused on training and padel.\n"
        "What are you looking for today, private content, customs, or a real chat with her?"
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
# 8) FUNNEL OVERRIDE (direct but human) + PROMO WHEN ASKED / FITS
# ============================================================
def funnel_reply(u: dict, user_text: str):
    t = user_text.lower().strip()
    faq = match_faq(t)

    # Promo questions
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
        if can_mention_promo(u) and FOUNDERS_PROMO_ACTIVE:
            mark_promo_mentioned(u)
            msg = msg + "\n" + founders_promo_line() + "\n" + founders_bonus_line()
        return True, msg

    # Explicit asks or photo demands get redirected
    if any(k in t for k in ["spicy", "nudes", "nsfw", "explicit", "sex", "porn", "send a photo", "send a pic", "pics", "selfie"]):
        u["link_stage"] = max(u["link_stage"], 1)
        msg = (
            "I can’t do explicit stuff here, and we don’t send private pics on Telegram.\n"
            "If you want the private side and customs, Fanvue is where Avelyn keeps it."
        )
        if can_mention_promo(u) and FOUNDERS_PROMO_ACTIVE:
            mark_promo_mentioned(u)
            msg = msg + "\n" + founders_promo_line()
        return True, msg + "\nWant the link now, or do you want a quick rundown first?"

    # Buyer intent
    if u["intent"] == "buyer_intent":
        u["link_stage"] = max(u["link_stage"], 1)
        msg = (
            "If you want the full private side, Fanvue is the place.\n"
            "Do you want the link right away, or do you want me to explain what you get?"
        )
        if (u.get("hesitation_score", 0) >= 4) and can_mention_promo(u) and FOUNDERS_PROMO_ACTIVE:
            mark_promo_mentioned(u)
            msg = msg + "\n" + founders_promo_line()
        return True, msg

    # Hesitation nudges
    if u.get("hesitation_score", 0) >= 6 and u.get("link_stage", 0) >= 1:
        msg = "I get it, you don’t want to waste money. What’s the main thing holding you back, price or trust?"
        if can_mention_promo(u) and FOUNDERS_PROMO_ACTIVE:
            mark_promo_mentioned(u)
            msg = msg + "\n" + founders_promo_line() + "\n" + founders_bonus_line()
        return True, msg

    return False, None

# ============================================================
# 9) FOLLOW-UPS + RE-ENGAGE (requires /cron pinging)
# Follow-ups keep contact until link_stage == 2
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

    if u.get("link_stage", 0) == 2:
        return None
    if u.get("takeover"):
        return None
    if u.get("followups_sent_today", 0) >= FOLLOWUP_MAX_PER_DAY:
        return None

    last_seen = u.get("last_seen_ts", 0.0)
    last_bot = u.get("last_bot_ts", 0.0)

    # Only follow up if bot spoke last and user didn’t respond after that
    if last_bot <= 0.0:
        return None
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

    if stage == 1:
        if u.get("link_stage", 0) >= 1:
            return intro + "quick check, were you still curious about Fanvue, or were you looking for something specific?"
        return intro + "what were you looking for today, private content or a real chat with Avelyn?"

    if stage == 2:
        msg = intro + "no pressure, but if you tell me what you want, I’ll point you the right way."
        if FOUNDERS_PROMO_ACTIVE and u.get("hesitation_score", 0) >= 3 and can_mention_promo(u):
            mark_promo_mentioned(u)
            msg = msg + " " + founders_promo_line()
        return msg

    msg = intro + "if you want, I can just drop the Fanvue link and you can take a look in 10 seconds."
    if FOUNDERS_PROMO_ACTIVE and can_mention_promo(u):
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
            sheet_log("followup", uid, u, msg)
            sent += 1
            if sent >= 25:
                break

        if eligible_for_reengage(u):
            msg = sanitize_reply(build_reengage_message(u))
            send_message(uid, msg)
            u["last_reengage_ts"] = time.time()
            u["last_bot_ts"] = time.time()
            sheet_log("reengage", uid, u, msg)
            sent += 1
            if sent >= 25:
                break

    return {"ok": True, "sent": sent}

# ============================================================
# 10) GPT RESPONSE (assistant identity, empathy, reacts to user)
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

ASSISTANT IDENTITY:
In the first 2 messages with a new user, make it clear you manage DMs and Avelyn checks in when she can.
Say she reads highlighted messages when possible, but do not claim she is watching live.

EMPATHY:
Respond directly to what the user said, then ask one simple question to move the chat forward.

SAFETY:
No meetups.
No explicit content.

GOAL:
Help users understand what Telegram is for and what Fanvue unlocks.
If they ask what’s inside Fanvue, explain benefits clearly: private drops, customs, and real replies from Avelyn.
If they ask for the link, give it immediately.
If they ask for explicit content, keep it classy and redirect to Fanvue without explicit detail.

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
# 10.5) HEALTH (for keep-alive monitor)
# ============================================================
@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return {"ok": True}, 200

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
    uid = msg.get("from", {}).get("id", chat_id)

    text = (msg.get("text") or "").strip()
    if not text:
        return "ok"

    # Ignore slash commands for normal users
    if text.startswith("/"):
        if handle_admin_command(text, chat_id):
            return "ok"
        return "ok"

    # De-dup key using update_id + message_id
    update_id = update.get("update_id")
    message_id = msg.get("message_id")
    dedup_key = f"{uid}:{update_id}:{message_id}"
    if dedup_key in processed:
        return "ok"
    processed[dedup_key] = time.time()

    u = get_user(uid)

    if u.get("takeover"):
        return "ok"

    if not allow_rate(u):
        return "ok"

    # update basics
    u["messages"] += 1
    u["intent"] = detect_intent(text)
    u["last_seen_ts"] = time.time()

    # phase logic
    if u["messages"] < 4:
        u["phase"] = 1
    elif u["intent"] == "buyer_intent":
        u["phase"] = 3
    else:
        u["phase"] = 2

    u["priority"] = (u["intent"] == "buyer_intent") or (u["messages"] >= 12)

    extract_profile(u, text)
    update_hesitation(u, text)

    # log inbound
    sheet_log("inbound_user", uid, u, text)

    # save history user turn
    u["history"].append({"role": "user", "content": text})
    u["history"] = u["history"][-HISTORY_TURNS:]

    # onboarding
    if u["messages"] == 1:
        reply = sanitize_reply(onboarding_message(u))
        d = human_delay("casual", 1, False)
        wait_human(chat_id, d)
        send_message(chat_id, reply)
        u["last_bot_ts"] = time.time()
        sheet_log("outbound_bot", uid, u, reply)

        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]
        return "ok"

    # FAQ fast answers (non-link, non-promo handled in funnel)
    faq = match_faq(text)
    if faq in FAQ_REPLIES and faq not in ["link", "promo"]:
        reply = sanitize_reply(FAQ_REPLIES[faq])
        d = human_delay(u["intent"], u["phase"], u["priority"])
        wait_human(chat_id, d)
        if random.random() < 0.10:
            reply = sanitize_reply(f"{pre_filler()} {reply}")
        send_message(chat_id, reply)
        u["last_bot_ts"] = time.time()
        sheet_log("outbound_bot", uid, u, reply)

        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]
        return "ok"

    # funnel override
    handled, reply = funnel_reply(u, text)
    if handled and reply:
        if u["intent"] == "buyer_intent" and should_alert(u):
            mark_alert(u)
            label = u.get("profile", {}).get("name") or f"uid:{uid}"
            alert_text = f"Hot intent ({label}) asked about Fanvue or promo. link_stage={u['link_stage']} hesitation={u.get('hesitation_score', 0)}"
            notify_admin(alert_text, current_uid=uid)
            sheet_log("admin_alert", uid, u, alert_text)

        reply = sanitize_reply(reply)
        d = human_delay(u["intent"], u["phase"], u["priority"])
        wait_human(chat_id, d)
        if random.random() < 0.10:
            reply = sanitize_reply(f"{pre_filler()} {reply}")
        send_message(chat_id, reply)
        u["last_bot_ts"] = time.time()
        sheet_log("outbound_bot", uid, u, reply)

        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]
        return "ok"

    # GPT response
    reply = gpt_reply(u)
    d = human_delay(u["intent"], u["phase"], u["priority"])
    wait_human(chat_id, d)
    if random.random() < 0.10:
        reply = sanitize_reply(f"{pre_filler()} {reply}")
    send_message(chat_id, reply)

    u["last_bot_ts"] = time.time()
    sheet_log("outbound_bot", uid, u, reply)

    u["history"].append({"role": "assistant", "content": reply})
    u["history"] = u["history"][-HISTORY_TURNS:]

    return "ok"

# ============================================================
# 12) RENDER BINDING
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

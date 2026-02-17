import os
import time
import random
import re
import threading
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
CRON_SECRET = os.environ.get("CRON_SECRET", "")            # optional but recommended

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

FANVUE_LINK = "https://www.fanvue.com/avelynnoira/fv-7"

MODEL = "gpt-4.1-mini"
MAX_OUTPUT_TOKENS = 170

# Anti-dup + anti-spam
PROCESSED_TTL_SECONDS = 60 * 15
MAX_MSGS_PER_MINUTE = 6

# Cooldowns
CTA_COOLDOWN_MINUTES = 20
ALERT_COOLDOWN_MINUTES = 30
REENGAGE_COOLDOWN_HOURS = 24

# History size
HISTORY_TURNS = 12

# Human timing
MAX_DELAY_SECONDS = 22.0

# Funnel stages
STAGE_NONE = 0       # no link convo started
STAGE_TEASED = 1     # asked about link/sub -> we teased + asked for explicit yes
STAGE_SENT = 2       # link sent

# ============================================================
# 1) IN-MEMORY STATE (Render restart resets)
# ============================================================
memory = {}      # uid -> user_state dict
processed = {}   # key -> ts (message_id + update_id)

AB_VARIANTS = ["A", "B"]
MOODS = ["playful", "soft", "busy", "jealous_light", "tired"]

# ============================================================
# 2) BIO (Integrated)
# ============================================================
BIO_CANON = """
Avelyn Noira is 21. Born in Chengdu, China. Birth name: Ava Lin (private).
At age 4, a metal advertising panel fell during a storm and cut a vertical scar across her right eye and cheek.
Her eye was saved but she has partial vision loss. The scar shaped her: observant, sharp, calm under pressure.
She found stability in disciplined training (gym) and later padel (timing/positioning fits her instincts).
She moved to Europe and chose the name “Avelyn Noira” as a boundary: Ava stays private, Avelyn is public.
She is structured: early mornings, gym, padel; not chasing perfection, just control and clarity.
She wears the scar without apology.
""".strip()

BIO_SHORT = """
Avelyn is 21, born in Chengdu. She has a vertical scar over her right eye from an accident when she was 4, with partial vision loss.
Gym + padel are her anchors. She lives in Europe now. “Ava Lin” is her private birth name; “Avelyn Noira” is her public identity.
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

def mark_processed(key: str) -> bool:
    """
    Returns False if already processed.
    """
    now = time.time()
    if key in processed:
        return False
    processed[key] = now
    return True

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
# 5) HUMANIZATION (24/7)
# ============================================================
def pre_filler():
    return random.choice(["Hmm…", "Wait…", "Okay hold on…", "Lol ok…", "Mmm…"])

def wait_human(chat_id: int, total_seconds: float):
    total_seconds = max(0.0, float(total_seconds))
    total_seconds = min(total_seconds, MAX_DELAY_SECONDS)

    # small "seen" delay
    seen_delay = min(random.uniform(0.3, 2.0), total_seconds)
    time.sleep(seen_delay)
    remaining = total_seconds - seen_delay

    # typing bursts
    while remaining > 0:
        burst = min(random.uniform(1.4, 4.4), remaining)
        send_typing(chat_id)
        time.sleep(burst)
        remaining -= burst
        if remaining <= 0:
            break
        pause = min(random.uniform(0.3, 1.4), remaining)
        time.sleep(pause)
        remaining -= pause

def human_delay(intent: str, phase: int, mood: str, priority: bool) -> float:
    if intent == "buyer_intent":
        d = random.uniform(2.2, 7.5)
    elif phase >= 2:
        d = random.uniform(4.0, 12.5)
    else:
        d = random.uniform(6.0, 16.0)

    if mood == "busy":
        d = max(2.0, d - random.uniform(1.0, 4.0))
    if mood in ["soft", "tired"]:
        d = min(MAX_DELAY_SECONDS, d + random.uniform(0.6, 3.0))
    if priority:
        d = max(1.8, d - random.uniform(0.4, 2.5))

    d += random.uniform(0.0, 1.8)
    return min(d, MAX_DELAY_SECONDS)

def maybe_shorten(text: str) -> str:
    t = " ".join(text.split())
    if len(t) > 260:
        t = t[:260].rsplit(" ", 1)[0] + "…"
    t = t.replace("\n\n", "\n").replace("- ", "")
    return t

def maybe_typo_curated(text: str) -> str:
    if random.random() > 0.03:
        return text
    replacements = [
        ("you", "u"),
        ("okay", "ok"),
        ("really", "rly"),
        ("because", "bc"),
        ("i'm", "im"),
        ("i am", "im"),
    ]
    out = text
    for a, b in replacements:
        if re.search(rf"\b{re.escape(a)}\b", out, flags=re.IGNORECASE) and random.random() < 0.35:
            out = re.sub(rf"\b{re.escape(a)}\b", b, out, count=1, flags=re.IGNORECASE)
    return out

# ============================================================
# 6) INTENT + FAQ
# ============================================================
FAQ_MAP = {
    "price": ["price", "how much", "cost", "pricing"],
    "safe": ["safe", "secure", "scam", "legit"],
    "what_you_get": ["what do i get", "what’s inside", "whats inside", "what do you post", "content", "what is on"],
    "cancel": ["cancel", "refund", "unsubscribe", "stop"],
    "link": ["link", "fanvue", "subscribe", "subscription", "join", "account"],
    "bio": ["scar", "eye", "chengdu", "china", "ava lin", "avel", "avelyn", "padel", "gym", "background", "story", "where are you from"],
}
FAQ_REPLIES = {
    "price": "It’s the normal sub price on my page 😌 you’ll see it before you confirm anything.",
    "safe": "Yeah, it’s official + you stay inside the platform. You can cancel anytime too 😌",
    "what_you_get": "More personal stuff + my private side… but still classy 😇 Want me to send the link?",
    "cancel": "You can cancel anytime on the platform, no drama 😊",
    "bio": None,  # handled via bio helper
}

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

def match_faq(text: str):
    t = text.lower()
    for key, kws in FAQ_MAP.items():
        if any(k in t for k in kws):
            return key
    return None

def normalize_affirmative(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"[^a-z0-9\s]", "", t).strip()
    return t

def is_affirmative(text: str) -> bool:
    t = normalize_affirmative(text)
    yes = {
        "yes", "y", "yeah", "yep", "sure", "ok", "okay",
        "send", "send it", "give", "give it", "pls", "please",
        "drop it", "go on", "do it", "sure ok", "ok send it"
    }
    return t in yes

def is_direct_link_demand(text: str) -> bool:
    t = text.lower()
    strong = [
        "send the link", "give me the link", "drop the link", "just send the link",
        "fanvue link", "send fanvue", "give fanvue", "link now"
    ]
    return any(s in t for s in strong)

# ============================================================
# 7) MOOD + MICRO MEMORY
# ============================================================
def update_mood(u: dict, user_text: str):
    t = user_text.strip()
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
        u["mood"] = "jealous_light" if random.random() < 0.20 else "playful"
        return

    if u["phase"] == 1:
        u["mood"] = "playful"
    elif u["phase"] == 2:
        u["mood"] = "soft" if random.random() < 0.7 else "playful"
    else:
        u["mood"] = "soft"

def mood_style_line(mood: str) -> str:
    if mood == "busy":
        return "Short replies. Slightly teasing but rushed."
    if mood == "tired":
        return "Cozy/tired vibe. Soft and slow. Short replies."
    if mood == "jealous_light":
        return "Subtle jealousy sometimes, playful, not aggressive."
    if mood == "soft":
        return "Soft girlfriend vibe. Warm. Slightly intimate (non-explicit)."
    return "Playful, teasing, confident, short texts."

def extract_profile(u: dict, user_text: str):
    t = user_text.strip()

    m = re.search(r"\b(my name is|i'm|im|i am)\s+([A-Za-z]{2,20})\b", t, flags=re.IGNORECASE)
    if m:
        name = m.group(2)
        u["profile"]["name"] = name.capitalize()

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
# 8) LEAD SCORING + FUNNEL STATE
# ============================================================
def lead_score_update(u: dict, user_text: str):
    t = user_text.lower().strip()
    inc = 0
    if len(t) >= 20:
        inc += 2
    if any(k in t for k in ["fanvue", "subscribe", "link", "join", "account"]):
        inc += 6
    if any(k in t for k in ["price", "cost", "how much"]):
        inc += 4
    if any(k in t for k in ["safe", "secure", "legit"]):
        inc += 2
    if any(k in t for k in ["pls", "please", "send it", "give it", "now"]):
        inc += 2

    u["lead_score"] = min(200, u.get("lead_score", 0) + inc)
    ls = u["lead_score"]
    if ls < 10:
        u["lead_level"] = "cold"
    elif ls < 25:
        u["lead_level"] = "warm"
    elif ls < 50:
        u["lead_level"] = "hot"
    else:
        u["lead_level"] = "buyer_ready"

def can_cta(u: dict) -> bool:
    return (time.time() - u.get("last_cta_ts", 0.0)) > (CTA_COOLDOWN_MINUTES * 60)

def mark_cta(u: dict):
    u["last_cta_ts"] = time.time()

def should_alert(u: dict) -> bool:
    return (time.time() - u.get("last_alert_ts", 0.0)) > (ALERT_COOLDOWN_MINUTES * 60)

def mark_alert(u: dict):
    u["last_alert_ts"] = time.time()

# ============================================================
# 9) USER STATE + ADMIN CONTROL
# ============================================================
def get_user(uid: int):
    if uid not in memory:
        memory[uid] = {
            "messages": 0,
            "warm": 0,
            "phase": 1,
            "intent": "casual",
            "engagement": 0.0,
            "priority": False,

            # funnel
            "link_stage": STAGE_NONE,
            "last_cta_ts": 0.0,

            # admin
            "takeover": False,
            "last_alert_ts": 0.0,

            # analytics
            "lead_score": 0,
            "lead_level": "cold",

            # re-engage
            "last_seen_ts": time.time(),
            "last_reengage_ts": 0.0,

            # convo
            "history": [],
            "rate_window": [],
            "variant": random.choice(AB_VARIANTS),
            "mood": "playful",
            "short_streak": 0,

            # micro memory
            "profile": {"name": "", "place": "", "interests": [], "last_topic": ""},
        }
    return memory[uid]

def handle_admin_command(text: str, chat_id: int):
    if not ADMIN_CHAT_ID or chat_id != ADMIN_CHAT_ID:
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
            send_message(chat_id, f"User {uid} not found in memory.")
            return True
        send_message(chat_id, f"""uid={uid}
phase={u['phase']} intent={u['intent']} mood={u['mood']} variant={u['variant']}
warm={u['warm']} link_stage={u['link_stage']} takeover={u['takeover']}
lead_score={u['lead_score']} lead_level={u['lead_level']}
messages={u['messages']} priority={u['priority']}
profile={u['profile']}""")
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
        u["link_stage"] = STAGE_SENT
        send_message(uid, f"{FANVUE_LINK}")
        send_message(chat_id, f"sent link to {uid}")
        return True

    return False

# ============================================================
# 10) FUNNEL (Upgraded + deterministic)
#    - GPT NEVER sends links
#    - One tease -> explicit YES -> send link
# ============================================================
TEASE_LINE = "Mmm… you want my Fanvue link, yeah? 👀\nSay “yes” and I’ll drop it."
SEND_LINK_LINE = f"Okay… only if you’re actually serious 👀\n{FANVUE_LINK}"
ALREADY_SENT_LINE = "I already sent it 😌 tell me when you’re in."

def funnel_reply(u: dict, user_text: str):
    t = user_text.strip()

    # If link already sent: don't repeat
    if u["link_stage"] == STAGE_SENT:
        if "fanvue" in t.lower() or "link" in t.lower() or "subscribe" in t.lower():
            return True, ALREADY_SENT_LINE
        return False, None

    wants_link = any(k in t.lower() for k in ["fanvue", "link", "subscribe", "subscription", "account", "join"])
    direct_demand = is_direct_link_demand(t)

    # If user is already saying yes-like + asking link in same message -> send immediately
    if wants_link and (is_affirmative(t) or direct_demand or "yes" in t.lower()):
        u["link_stage"] = STAGE_SENT
        mark_cta(u)
        return True, SEND_LINK_LINE

    # If we already teased and user now confirms -> send link
    if u["link_stage"] == STAGE_TEASED and is_affirmative(t):
        u["link_stage"] = STAGE_SENT
        mark_cta(u)
        return True, SEND_LINK_LINE

    # If user asks for link/sub -> tease once
    if wants_link and u["link_stage"] == STAGE_NONE:
        u["link_stage"] = STAGE_TEASED
        return True, TEASE_LINE

    # Soft CTA only when warm & cooldown ok (rare)
    if u["phase"] == 4 and u["link_stage"] == STAGE_NONE and can_cta(u) and random.random() < 0.08:
        u["link_stage"] = STAGE_TEASED
        mark_cta(u)
        return True, "You’re kinda making me curious… I keep my more private side somewhere else 😇\nWant the link?"

    return False, None

# ============================================================
# 11) RE-ENGAGEMENT (requires cron calling /cron)
# ============================================================
def eligible_for_reengage(u: dict) -> bool:
    now_ts = time.time()
    inactive_hours = (now_ts - u.get("last_seen_ts", now_ts)) / 3600.0
    since_last = (now_ts - u.get("last_reengage_ts", 0.0)) / 3600.0
    return inactive_hours >= REENGAGE_COOLDOWN_HOURS and since_last >= REENGAGE_COOLDOWN_HOURS

def build_reengage_message(u: dict) -> str:
    p = u.get("profile", {})
    name = p.get("name", "")
    if name:
        return f"hey {name}… you disappeared on me 😌 you good?"
    return "hey… you disappeared on me 😌 you good?"

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
# 12) BIO HELPER (fast, consistent answers)
# ============================================================
def bio_quick_answer(user_text: str) -> str:
    t = user_text.lower()
    # Keep it short + human. Always English.
    if "ava" in t or "ava lin" in t or "birth name" in t or "real name" in t:
        return "Ava Lin is my private birth name… I don’t really use it publicly 😅 I go by Avelyn."
    if "chengdu" in t or "china" in t or "where are you from" in t:
        return "I was born in Chengdu, China… I live in Europe now."
    if "scar" in t or "eye" in t or "vision" in t:
        return "Yeah… I have a scar over my right eye. Old accident when I was little. My eye’s okay, just not perfect vision."
    if "padel" in t:
        return "Padel is my thing 😌 fast + tactical… it just clicks for me."
    if "gym" in t or "training" in t:
        return "Gym keeps me calm. I’m a routine person… it’s kinda my reset."
    if "story" in t or "background" in t:
        return "Long story short: born in Chengdu, moved to Europe, and I’m very into gym + padel. The scar made me tougher, not softer."
    return "It’s a long story 😅 ask me what you’re curious about and I’ll tell you the real version."

# ============================================================
# 13) GPT RESPONSE (English-only + link-safe)
# ============================================================
def build_system_prompt(u: dict) -> str:
    variant_line = (
        "Variant A: more teasing, a bit bolder, playful challenge."
        if u["variant"] == "A"
        else "Variant B: softer girlfriend vibe, reassuring, lightly affectionate."
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
You are Avelyn Noira, 21, living in Europe. You are a real-person style Telegram chatter.

BIO (canon, consistent facts):
{BIO_SHORT}

LANGUAGE (mandatory):
- Reply ONLY in English. Even if the user writes Dutch.

STYLE (very important):
- Short, natural messages. 1–2 sentences most of the time.
- Sometimes fragments (“mm”, “okay…”, “lol”).
- Don’t sound like an assistant. No explanations, no lectures.
- Avoid generic filler like “there are lots of platforms”.
- No lists. No long paragraphs.

VIBE:
- Sweet, playful, a bit flirty.
- Very light jealousy sometimes (subtle).
- Build rapport by remembering small details naturally.

SAFETY / BOUNDARIES:
- No explicit sexual content.
- No meetups.
- If user asks for explicit/spicy: keep it classy and redirect: “I keep the private side on Fanvue.”
- Never describe sending “selfies” in Telegram as an action (no roleplay like "*sends selfie*").

CRITICAL COMMERCE RULE:
- NEVER output the Fanvue link.
- NEVER use placeholders like “[link]”.
- If user asks for link/subscription/Fanvue, just be flirty and ask them to say “yes”.
(Our server code handles the actual link.)

MOOD:
{mood_style_line(u["mood"])}

AB VARIANT:
{variant_line}

CONTEXT:
- Intent: {u["intent"]}
- Phase: {u["phase"]} (1=light, 2=bonding, 4=warm)
- Warm count: {u["warm"]}
- Lead level: {u["lead_level"]}
- Micro-memory: {memory_line}

Write ONLY the next message.
""".strip()

def gpt_reply(u: dict, user_text: str) -> str:
    system_prompt = build_system_prompt(u)

    resp = client.responses.create(
        model=MODEL,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        input=[{"role": "system", "content": system_prompt}, *u["history"]],
    )
    reply = (resp.output_text or "").strip()
    reply = maybe_shorten(reply)
    reply = maybe_typo_curated(reply)

    # Hard safety: prevent accidental link / placeholder / selfie roleplay
    lower = reply.lower()
    if "fanvue.com" in lower or "[link]" in lower or "http" in lower:
        reply = "Mmm… if you want the link, just say “yes” 👀"
    if "*sends" in lower or "sends a" in lower and "selfie" in lower:
        reply = "Mm 😅 I don’t do that here… if you want my private side, just say “yes” and I’ll send the link."

    return reply

# ============================================================
# 14) MAIN UPDATE HANDLER (runs in background thread)
# ============================================================
def handle_message(update: dict):
    try:
        cleanup_processed()

        msg = update.get("message")
        if not msg:
            return

        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()

        # Ignore /start completely
        if text.strip().lower() == "/start":
            return

        # Admin commands only in admin chat
        if text.startswith("/"):
            if handle_admin_command(text, chat_id):
                return

        uid = msg.get("from", {}).get("id", chat_id)
        user_text = text
        if not user_text:
            return

        u = get_user(uid)

        # takeover mode: bot silent for this user
        if u.get("takeover"):
            return

        # rate limit
        if not allow_rate(u):
            return

        # Update state basics
        u["messages"] += 1
        u["intent"] = detect_intent(user_text)
        u["engagement"] += min(len(user_text) * 0.08, 6.0)
        u["last_seen_ts"] = time.time()

        if warm_trigger(user_text):
            u["warm"] += 1

        # Phase logic
        if u["messages"] < 5:
            u["phase"] = 1
        elif u["warm"] >= 1 and u["messages"] >= 6:
            u["phase"] = 4
        else:
            u["phase"] = 2

        # priority / scoring
        lead_score_update(u, user_text)
        if u["messages"] >= 18 or u["engagement"] >= 30 or u["lead_level"] in ["hot", "buyer_ready"]:
            u["priority"] = True

        # mood + micro memory
        update_mood(u, user_text)
        extract_profile(u, user_text)

        # Save history (user turn)
        u["history"].append({"role": "user", "content": user_text})
        u["history"] = u["history"][-HISTORY_TURNS:]

        # 1) Bio questions: quick consistent answers
        faq = match_faq(user_text)
        if faq == "bio":
            reply = bio_quick_answer(user_text)
            d = human_delay(u["intent"], u["phase"], u["mood"], u["priority"])
            wait_human(chat_id, d)
            if random.random() < 0.12:
                reply = f"{pre_filler()}\n{reply}"
            send_message(chat_id, reply)
            return

        # 2) Funnel override (deterministic)
        handled, reply = funnel_reply(u, user_text)
        if handled and reply:
            # alert admin if link/sub intent
            if (u["intent"] == "buyer_intent") and should_alert(u):
                mark_alert(u)
                name = u["profile"].get("name") or f"uid:{uid}"
                notify_admin(f"Hot lead ({name}) asked about link/sub. stage={u['link_stage']} lead={u['lead_level']}")

            d = human_delay("buyer_intent", u["phase"], u["mood"], True)
            wait_human(chat_id, d)
            if random.random() < 0.14:
                reply = f"{pre_filler()}\n{reply}"
            send_message(chat_id, reply)
            return

        # 3) FAQ quick replies (non-link)
        if faq and faq in FAQ_REPLIES and faq != "link":
            reply = FAQ_REPLIES[faq]
            d = human_delay(u["intent"], u["phase"], u["mood"], u["priority"])
            wait_human(chat_id, d)
            if random.random() < 0.12:
                reply = f"{pre_filler()}\n{reply}"
            send_message(chat_id, reply)
            return

        # 4) GPT (never handles link)
        reply = gpt_reply(u, user_text)

        # Save assistant turn
        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]

        d = human_delay(u["intent"], u["phase"], u["mood"], u["priority"])
        wait_human(chat_id, d)

        if random.random() < 0.12:
            reply = f"{pre_filler()}\n{reply}"

        send_message(chat_id, reply)

        # Debug log (server only)
        print({
            "uid": uid,
            "phase": u["phase"],
            "intent": u["intent"],
            "warm": u["warm"],
            "lead_score": u["lead_score"],
            "lead_level": u["lead_level"],
            "priority": u["priority"],
            "link_stage": u["link_stage"],
            "variant": u["variant"],
            "mood": u["mood"],
            "name": u["profile"].get("name", ""),
        })

    except Exception as e:
        # Avoid crashing worker thread
        print("handle_message error:", str(e))

# ============================================================
# 15) WEBHOOK (ACK fast to stop Telegram retries)
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    # De-dup as early as possible (prevents double threading)
    update_id = update.get("update_id")
    msg = update.get("message") or {}
    message_id = msg.get("message_id")

    # Create a stable key
    key_parts = []
    if update_id is not None:
        key_parts.append(f"u:{update_id}")
    if message_id is not None:
        key_parts.append(f"m:{message_id}")
    key = "|".join(key_parts) if key_parts else str(time.time())

    if not mark_processed(key):
        return "ok"

    # Start background processing and ACK immediately
    t = threading.Thread(target=handle_message, args=(update,), daemon=True)
    t.start()

    return "ok"

# ============================================================
# 16) RENDER BINDING
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

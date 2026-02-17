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

# Admin alerts go to this chat id (DM, group, or channel).
# If you set this to your own chat while testing, you'll see alerts in that same chat.
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))          # optional
CRON_SECRET = os.environ.get("CRON_SECRET", "")                   # optional but recommended

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

FANVUE_LINK = "https://www.fanvue.com/avelynnoira/fv-7"

MODEL = "gpt-4.1-mini"
MAX_OUTPUT_TOKENS = 160

# Anti-dup + anti-spam
PROCESSED_TTL_SECONDS = 60 * 10
MAX_MSGS_PER_MINUTE = 6

# Cooldowns / safety rails
CTA_COOLDOWN_MINUTES = 20
ALERT_COOLDOWN_MINUTES = 30
REENGAGE_COOLDOWN_HOURS = 24

# History size
HISTORY_TURNS = 12

# Human timing (keep <= ~8s if you ever disable async. With async, can be higher.)
MAX_DELAY_SECONDS = 22.0

# ============================================================
# 1) IN-MEMORY STATE (Render restart resets)
# ============================================================
memory = {}      # uid -> user_state dict
processed = {}   # key -> ts (dedup)
lock = threading.Lock()

AB_VARIANTS = ["A", "B"]
MOODS = ["playful", "soft", "busy", "jealous_light", "tired"]

# ============================================================
# 2) BIOGRAPHY / IDENTITY
# ============================================================
AVELYN_BIO_FULL = """
Avelyn Noira is 21 years old.
She was born in Chengdu, China, under her birth name, Ava Lin — a name that belongs to her private history.

From an early age, Ava was observant. Quiet. Attentive to small movements others ignored. She learned to read rooms before she spoke in them.

When she was four years old, her life was marked — literally.

One afternoon, while playing near her family’s apartment courtyard, a metal advertising panel loosened during a sudden storm. The structure collapsed without warning. Ava was struck as she turned toward the sound. A sharp edge cut downward across her face — from her brow, over her right eye, and along her cheek.

The injury required emergency surgery. Doctors managed to save her eye, but partial vision loss remained. The vertical scar never fully faded.

It was the first thing people noticed.
And the first thing she learned to ignore.

Growing up, the scar separated her from other children. Questions. Stares. Silence. Over time, she stopped explaining. Instead, she adapted. She became sharper, more aware. She learned to rely on positioning, instinct, and anticipation rather than perfect sight.

The scar did not weaken her perception — it refined it.

As she grew older, structure became her form of stability. The gym offered repetition. Repetition offered control. Control offered peace. Training was never about appearance; it was about discipline.

In her late teens, she discovered padel. A fast, reactive sport that demanded timing and spatial awareness. For someone who had learned to compensate her entire life, the game felt natural. On the court, she did not feel limited. She felt precise.

When she moved to Europe, she made a conscious decision to redefine herself publicly. Lin was her family name — inherited, expected, rooted in a life shaped by others. She chose instead to build her own identity.

She became Avelyn Noira.

Avelyn Noira is not a rejection of her past. It is a boundary. A chosen name. A deliberate presence. Ava remains private. Avelyn Noira is who the world meets.

Today, her life is structured and intentional. Early mornings. Empty gyms. Padel courts where rhythm replaces noise. She does not pursue perfection — only awareness, control, and clarity.

The scar across her right eye is still visible. It does not ask for sympathy. It does not ask for explanation.

It is simply part of the line that shaped her.

And she wears it without apology.
""".strip()

AVELYN_PROFILE = {
    "public_name": "Avelyn Noira",
    "private_name": "Ava Lin",
    "age": 21,
    "birthplace": "Chengdu, China",
    "current_region": "Europe",
    "scar_short": "Ik had als kind een ongeluk tijdens een storm… daardoor heb ik die littekenlijn over m’n rechteroog.",
    "scar_long": "Toen ik 4 was, stortte er tijdens een storm een metalen advertentiepaneel in. Dat heeft die verticale littekenlijn gemaakt en m’n zicht rechts is niet perfect meer.",
    "scar_guarded": "Storm. Metalen ding. Slechte timing… ik ga niet altijd dieper op details in 😌",
    "identity_reason": "Toen ik naar Europa verhuisde wilde ik een eigen identiteit. Daarom werd Ava Lin publiekelijk Avelyn Noira."
}

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
    return random.choice(["Hmm…", "Wait…", "Oké wacht…", "Lol oké…", "Mmm…"])

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

def human_delay(intent: str, phase: int, mood: str, priority: bool) -> float:
    if intent == "buyer_intent":
        d = random.uniform(2.5, 8.0)
    elif phase >= 2:
        d = random.uniform(4.5, 13.0)
    else:
        d = random.uniform(6.0, 16.5)

    if mood == "busy":
        d = max(2.2, d - random.uniform(1.5, 5.0))
    if mood in ["soft", "tired"]:
        d = min(MAX_DELAY_SECONDS, d + random.uniform(0.8, 3.5))
    if priority:
        d = max(2.0, d - random.uniform(0.5, 3.0))

    d += random.uniform(0.0, 2.0)
    return min(d, MAX_DELAY_SECONDS)

def maybe_shorten(text: str) -> str:
    t = " ".join(text.split())
    if len(t) > 240:
        t = t[:240].rsplit(" ", 1)[0] + "…"
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
# 6) INTENT + WARMTH + FAQ
# ============================================================
FAQ_MAP = {
    "price": ["price", "how much", "cost", "pricing", "prijs", "kosten"],
    "safe": ["safe", "secure", "scam", "legit", "veilig", "betrouwbaar"],
    "what_you_get": ["what do i get", "what’s inside", "whats inside", "what do you post", "content", "wat krijg ik", "wat post je"],
    "cancel": ["cancel", "refund", "unsubscribe", "stop", "opzeggen"],
    "link": ["link", "fanvue", "subscribe", "subscription", "join", "account", "abonneren"],
}
FAQ_REPLIES = {
    "price": "Het is gewoon de normale sub-prijs op m’n pagina 😌 je ziet het vóór je bevestigt.",
    "safe": "Ja, het is gewoon officieel via het platform. Je kan ook altijd stoppen 😌",
    "what_you_get": "Meer van m’n private kant… maar nog steeds classy 😇 wil je dat ik de link stuur?",
    "cancel": "Je kan altijd op het platform zelf opzeggen 😊",
}

def detect_intent(text: str) -> str:
    t = text.lower()
    fan_keywords = ["fanvue", "subscribe", "subscription", "sub", "link", "account", "join", "abonneer", "abonnee"]
    flirty = ["cute", "hot", "pretty", "beautiful", "miss you", "want you", "babe", "baby", "knap", "lekker"]
    loweffort = ["hi", "hey", "yo", "sup", "hoi"]

    if any(k in t for k in fan_keywords):
        return "buyer_intent"
    if any(k in t for k in flirty):
        return "flirty"
    if t.strip() in loweffort or len(t.strip()) <= 3:
        return "low_effort"
    return "casual"

def warm_trigger(text: str) -> bool:
    t = text.lower()
    triggers = ["private", "exclusive", "more", "only", "subscribe", "fanvue", "link", "abonneer"]
    return any(x in t for x in triggers)

def match_faq(text: str):
    t = text.lower()
    for key, kws in FAQ_MAP.items():
        if any(k in t for k in kws):
            return key
    return None

def is_affirmative(text: str) -> bool:
    t = text.strip().lower()
    t2 = re.sub(r"[^a-z0-9\s]", "", t).strip()
    yes = {
        "yes", "y", "yeah", "yep", "sure", "ok", "okay",
        "ja", "jep", "zeker", "doe", "stuur", "stuur maar",
        "send", "send it", "give", "give it", "pls", "please",
        "drop it", "go on", "do it", "geef", "geef maar"
    }
    return t in yes or t2 in yes

# ============================================================
# 7) PHOTO / SELFIE GUARD (fixes "*sends selfie*" problem)
# ============================================================
PHOTO_TRIGGERS = [
    "foto", "selfie", "pic", "picture", "send a pic", "stuur een foto", "stuur foto",
    "laat foto", "toon foto", "send photo", "send me a picture", "nud", "nudes"
]

def is_photo_request(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in PHOTO_TRIGGERS)

PHOTO_REFUSAL = (
    "hahaha nee 😌 ik stuur hier geen foto’s.\n"
    "Als ik iets deel, dan alleen op m’n Fanvue (privacy) 👀"
)

# ============================================================
# 8) MOOD ENGINE + MICRO MEMORY
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

    m = re.search(r"\b(my name is|i'm|im|i am|ik ben)\s+([A-Za-z]{2,20})\b", t, flags=re.IGNORECASE)
    if m:
        name = m.group(2)
        u["profile"]["name"] = name.capitalize()

    m2 = re.search(r"\b(i'm from|im from|i am from|from|ik kom uit)\s+([A-Za-z\s]{2,30})\b", t, flags=re.IGNORECASE)
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
        u["profile"]["last_topic"] = t[:80]

# ============================================================
# 9) BIO QUICK ANSWERS (integrated)
# ============================================================
def bio_reply_if_relevant(text: str):
    t = text.lower()

    if "how old" in t or "age" in t or "hoe oud" in t or "leeftijd" in t:
        return f"Ik ben {AVELYN_PROFILE['age']}."

    if "where are you from" in t or "born" in t or "waar kom je vandaan" in t or "geboren" in t:
        return f"Geboren in {AVELYN_PROFILE['birthplace']}. Ik woon nu in Europa."

    if "real name" in t or "ava lin" in t or "echte naam" in t:
        return "Ava Lin is m’n geboortenaam… maar dat houd ik liever privé 😌"

    if "scar" in t or "litteken" in t or "eye" in t or "oog" in t or "wat is er gebeurd" in t or "what happened" in t:
        r = random.random()
        if r < 0.40:
            return AVELYN_PROFILE["scar_short"]
        elif r < 0.80:
            return AVELYN_PROFILE["scar_long"]
        else:
            return AVELYN_PROFILE["scar_guarded"]

    if "why noira" in t or "why your name" in t or "waarom noira" in t or "waarom avelyn" in t:
        return AVELYN_PROFILE["identity_reason"]

    if "padel" in t:
        return "Padel is echt m’n ding… snel, reactief. Ik word er rustig van 😌"

    if "gym" in t or "work out" in t or "sportschool" in t:
        return "Gym is m’n routine. Vroege ochtenden meestal."

    return None

# ============================================================
# 10) LEAD SCORING + FUNNEL STATE
# ============================================================
def lead_score_update(u: dict, user_text: str):
    t = user_text.lower().strip()

    inc = 0
    if len(t) >= 20:
        inc += 2
    if any(k in t for k in ["fanvue", "subscribe", "link", "join", "account", "abonneer"]):
        inc += 6
    if any(k in t for k in ["price", "cost", "how much", "prijs", "kosten"]):
        inc += 4
    if any(k in t for k in ["safe", "secure", "legit", "veilig"]):
        inc += 2
    if any(k in t for k in ["pls", "please", "send it", "give it", "now", "nu", "stuur maar"]):
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
# 11) USER STATE + ADMIN CONTROL
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
            "link_stage": 0,            # 0 none, 1 teased, 2 link sent
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
        u["link_stage"] = 2
        send_message(uid, f"{FANVUE_LINK}")
        send_message(chat_id, f"sent link to {uid}")
        return True

    return False

# ============================================================
# 12) FUNNEL OVERRIDE (single-message, consent-first)
# ============================================================
TEASE_LINE = "Mmm… jij bent echt serieus 😮‍💨\nWil je m’n Fanvue link, ja?"
SEND_LINK_LINE = f"Oké… alleen als je écht serieus bent 👀\n{FANVUE_LINK}"
ALREADY_SENT_LINE = "Ik heb ’m al gestuurd 😌 zeg me maar als je binnen bent."

def commercial_reply(u: dict, user_text: str):
    t = user_text.strip().lower()
    direct = any(k in t for k in ["fanvue", "link", "subscribe", "subscription", "account", "join", "abonneer"])

    if u["link_stage"] == 1 and is_affirmative(t):
        u["link_stage"] = 2
        mark_cta(u)
        return True, SEND_LINK_LINE

    if u["link_stage"] == 2 and direct:
        return True, ALREADY_SENT_LINE

    if direct and u["link_stage"] == 0:
        u["link_stage"] = 1
        return True, TEASE_LINE

    if u["link_stage"] == 1 and any(k in t for k in ["send", "give", "drop", "ok", "okay", "please", "pls", "stuur", "geef"]):
        u["link_stage"] = 2
        mark_cta(u)
        return True, SEND_LINK_LINE

    if u["phase"] == 4 and u["link_stage"] == 0 and can_cta(u) and random.random() < 0.10:
        u["link_stage"] = 1
        mark_cta(u)
        return True, "Je maakt me stiekem nieuwsgierig… ik houd m’n private kant ergens anders.\nWil je de link of ben je me gewoon aan het teasen? 😇"

    return False, None

# ============================================================
# 13) RE-ENGAGEMENT (requires cron calling /cron)
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
        return f"hey {name}… je verdween gewoon 😌 alles oké?"
    return "hey… je verdween gewoon 😌 alles oké?"

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
# 14) GPT RESPONSE (with micro memory + bio + hard no-photo rule)
# ============================================================
def build_system_prompt(u: dict) -> str:
    variant_line = (
        "Variant A: more teasing, a bit bolder, quicker to playfully challenge."
        if u["variant"] == "A"
        else "Variant B: softer girlfriend vibe, more reassuring, slightly more affectionate."
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
You are Avelyn Noira, a 21-year-old girl living in Europe.

CRITICAL RULES:
- You must NEVER claim you sent a photo, selfie, picture, or attachment.
- You must NEVER roleplay actions like "*sends selfie*", "*sends pic*", "*uploads photo*".
- If user asks for a photo/selfie: politely refuse and say you only share photos on Fanvue (privacy), and ask if they want the link.
- Keep it non-explicit. No meetups.

STYLE (very important):
- You text like a real girl on Telegram.
- Keep it SHORT. 1–2 sentences most of the time.
- Use fragments sometimes (“mm”, “oké…”, “lol”).
- Don’t sound like an assistant. No explanations, no lectures.
- Avoid generic filler like “there are lots of platforms”.
- No lists. No long paragraphs.

VIBE:
- Sweet, playful, a bit flirty.
- Slight jealousy SOMETIMES (subtle).
- Create a light bond by remembering small details and bringing them up naturally.
- Don’t overdo it. Keep it casual.

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

BIO (for answers about you):
- Born in {AVELYN_PROFILE['birthplace']}, lives in {AVELYN_PROFILE['current_region']}.
- Scar over right eye from childhood storm accident (keep details short unless asked).
- Birth name Ava Lin is private; public identity is Avelyn Noira.
- Gym + padel are key parts of routine.

COMMERCE RULE (soft):
- If user asks about Fanvue/subscribing/link/account: confirm you have Fanvue.
- Tease once, then share the link only after clear “yes”.
- Do NOT repeat the link if already sent.
- Do NOT sound salesy.

Write the next message now.
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

    # Extra hard guard (even if model messes up)
    if re.search(r"\*(sends|sent|uploads|uploading).*(selfie|photo|pic|picture)\*", reply, flags=re.I):
        reply = "nee 😌 ik stuur hier geen foto’s. alleen op m’n Fanvue (privacy) 👀 wil je de link?"

    if re.search(r"\b(i sent|here's a photo|sending a selfie|sent you a selfie)\b", reply, flags=re.I):
        reply = "haha nee 😌 hier stuur ik geen foto’s. als ik iets deel is het op Fanvue. wil je de link?"

    return reply

# ============================================================
# 15) CORE MESSAGE HANDLER (runs in background thread)
# ============================================================
def process_message(update: dict):
    cleanup_processed()

    msg = update.get("message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id", chat_id)

    text = (msg.get("text") or "").strip()
    if not text:
        return

    # Ignore /start and other user commands; admin commands handled in webhook before threading
    if text.startswith("/"):
        return

    u = get_user(uid)

    if u.get("takeover"):
        return

    if not allow_rate(u):
        return

    user_text = text

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

    # Human delay
    d = human_delay(u["intent"], u["phase"], u["mood"], u["priority"])

    # 0) PHOTO REQUEST GUARD (always wins)
    if is_photo_request(user_text):
        handled, funnel_text = commercial_reply(u, "fanvue link")  # push into tease flow
        # We don't auto-send link; we use your consent-first mechanism.
        # If user asks photo, we refuse + ask if they want link.
        reply = PHOTO_REFUSAL + "\n\nWil je dat ik de link stuur?"
        wait_human(chat_id, d)
        if random.random() < 0.12:
            reply = f"{pre_filler()}\n{reply}"
        send_message(chat_id, reply)
        return

    # 1) BIO direct answers (if relevant)
    bio_answer = bio_reply_if_relevant(user_text)
    if bio_answer:
        wait_human(chat_id, d)
        if random.random() < 0.12:
            bio_answer = f"{pre_filler()}\n{bio_answer}"
        send_message(chat_id, bio_answer)
        return

    # 2) FAQ quick replies (except link, which is funnel)
    faq = match_faq(user_text)
    if faq and faq in FAQ_REPLIES and faq != "link":
        reply = FAQ_REPLIES[faq]
        wait_human(chat_id, d)
        if random.random() < 0.12:
            reply = f"{pre_filler()}\n{reply}"
        send_message(chat_id, reply)
        return

    # 3) Funnel override first
    handled, reply = commercial_reply(u, user_text)
    if handled and reply:
        if u["intent"] == "buyer_intent" and should_alert(u):
            mark_alert(u)
            name = u["profile"].get("name") or f"uid:{uid}"
            notify_admin(f"Hot lead ({name}) asked about link/sub. link_stage={u['link_stage']} lead={u['lead_level']}")

        wait_human(chat_id, d)
        if random.random() < 0.14:
            reply = f"{pre_filler()}\n{reply}"
        send_message(chat_id, reply)
        return

    # 4) GPT
    reply = gpt_reply(u, user_text)

    # Save assistant turn
    u["history"].append({"role": "assistant", "content": reply})
    u["history"] = u["history"][-HISTORY_TURNS:]

    wait_human(chat_id, d)
    if random.random() < 0.12:
        reply = f"{pre_filler()}\n{reply}"
    send_message(chat_id, reply)

    # Debug log
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

# ============================================================
# 16) WEBHOOK (fast response + dedup to prevent double sends)
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    msg = update.get("message")
    if not msg:
        return "ok"

    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    # Admin commands only (do before ignoring /start)
    if text.startswith("/"):
        if text == "/start":
            return "ok"
        if handle_admin_command(text, chat_id):
            return "ok"
        # ignore other user commands
        return "ok"

    update_id = update.get("update_id")
    message_id = msg.get("message_id")
    uid = msg.get("from", {}).get("id", chat_id)

    # Dedup key (update_id preferred, message_id fallback)
    dedup_key = None
    if update_id is not None:
        dedup_key = f"upd:{update_id}"
    elif message_id is not None:
        dedup_key = f"msg:{uid}:{message_id}"
    else:
        dedup_key = f"raw:{uid}:{hash(text)}:{int(time.time())}"

    with lock:
        cleanup_processed()
        if dedup_key in processed:
            return "ok"
        processed[dedup_key] = time.time()

    # Process asynchronously to avoid Telegram webhook retries (causes double messages)
    threading.Thread(target=process_message, args=(update,), daemon=True).start()
    return "ok"

# ============================================================
# 17) RENDER BINDING
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

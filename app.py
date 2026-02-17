import os
import time
import random
import re
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from openai import OpenAI

app = Flask(__name__)

# ============================================================
# 0) CONFIG
# ============================================================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

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

# Human timing
MAX_DELAY_SECONDS = 22.0

# ============================================================
# 1) IN-MEMORY STATE (Render restart resets)
# ============================================================
memory = {}      # uid -> user_state dict
processed = {}   # message_id -> ts

AB_VARIANTS = ["A", "B"]
MOODS = ["playful", "soft", "busy", "jealous_light", "tired"]

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
    if ADMIN_CHAT_ID:
        send_message(ADMIN_CHAT_ID, f"[ALERT] {text}")

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
    # used as optional prefix INSIDE single message (no double-send)
    return random.choice(["Hmm…", "Wait…", "Okay hold on…", "Lol okay…", "Mmm…"])

def wait_human(chat_id: int, total_seconds: float):
    """
    Human-like wait: seen delay + typing bursts + pauses.
    """
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
    """
    24/7 delay policy:
    - buyer intent = faster
    - bonding = medium
    - casual = slower
    mood + priority adjust the range
    """
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
    """
    Curated micro-typos (human), low rate.
    """
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
# 5) INTENT + WARMTH + FAQ
# ============================================================
FAQ_MAP = {
    "price": ["price", "how much", "cost", "pricing"],
    "safe": ["safe", "secure", "scam", "legit"],
    "what_you_get": ["what do i get", "what’s inside", "whats inside", "what do you post", "content", "what is on"],
    "cancel": ["cancel", "refund", "unsubscribe", "stop"],
    "link": ["link", "fanvue", "subscribe", "subscription", "join", "account"],
}
FAQ_REPLIES = {
    "price": "It’s the normal sub price on my page 😌 you’ll see it before you confirm anything.",
    "safe": "Yeah, it’s official + you stay inside the platform. You can cancel anytime too 😌",
    "what_you_get": "More personal stuff + my private side… but still classy 😇 Want me to send the link?",
    "cancel": "You can cancel anytime on the platform, no drama 😊",
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

def is_affirmative(text: str) -> bool:
    t = text.strip().lower()
    t2 = re.sub(r"[^a-z0-9\s]", "", t).strip()
    yes = {
        "yes", "y", "yeah", "yep", "sure", "ok", "okay",
        "send", "send it", "give", "give it", "pls", "please",
        "drop it", "go on", "do it"
    }
    return t in yes or t2 in yes

# ============================================================
# 6) MOOD ENGINE + MICRO MEMORY
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
        u["profile"]["last_topic"] = t[:80]

# ============================================================
# 7) LEAD SCORING + FUNNEL STATE
# ============================================================
def lead_score_update(u: dict, user_text: str):
    """
    Commercial analytics that stays non-exploitative:
    scores engagement + purchase intent for prioritization + timing.
    """
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
# 8) USER STATE + ADMIN CONTROL
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
    """
    Admin-only commands via Telegram DM to the bot.
    Use: /status <uid>
         /takeover <uid> on|off
         /reset <uid>
         /force_link <uid>
    """
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
# 9) FUNNEL OVERRIDE (single-message, consent-first)
# ============================================================
TEASE_LINE = "Mmm… you’re really about it 😮‍💨\nYou want my Fanvue link, yeah?"
SEND_LINK_LINE = f"Okay… only if you’re actually serious 👀\n{FANVUE_LINK}"
ALREADY_SENT_LINE = "I already sent it 😌 tell me when you’re in."

def commercial_reply(u: dict, user_text: str):
    t = user_text.strip().lower()
    direct = any(k in t for k in ["fanvue", "link", "subscribe", "subscription", "account", "join"])

    # If teased and user confirms -> send link
    if u["link_stage"] == 1 and is_affirmative(t):
        u["link_stage"] = 2
        mark_cta(u)
        return True, SEND_LINK_LINE

    # If link already sent, avoid repeating
    if u["link_stage"] == 2 and direct:
        return True, ALREADY_SENT_LINE

    # First direct ask: tease once
    if direct and u["link_stage"] == 0:
        u["link_stage"] = 1
        return True, TEASE_LINE

    # If stage 1 and user says "send/give/please"
    if u["link_stage"] == 1 and any(k in t for k in ["send", "give", "drop", "ok", "okay", "please", "pls"]):
        u["link_stage"] = 2
        mark_cta(u)
        return True, SEND_LINK_LINE

    # Soft CTA only when warm & cooldown ok
    if u["phase"] == 4 and u["link_stage"] == 0 and can_cta(u) and random.random() < 0.10:
        u["link_stage"] = 1
        mark_cta(u)
        return True, "You’re kinda making me curious… I keep my more private side somewhere else.\nWant the link or are you just teasing me? 😇"

    return False, None

# ============================================================
# 10) RE-ENGAGEMENT (requires cron calling /cron)
# ============================================================
def eligible_for_reengage(u: dict) -> bool:
    now_ts = time.time()
    inactive_hours = (now_ts - u.get("last_seen_ts", now_ts)) / 3600.0
    since_last = (now_ts - u.get("last_reengage_ts", 0.0)) / 3600.0
    if inactive_hours >= REENGAGE_COOLDOWN_HOURS and since_last >= REENGAGE_COOLDOWN_HOURS:
        return True
    return False

def build_reengage_message(u: dict) -> str:
    p = u.get("profile", {})
    name = p.get("name", "")
    if name:
        return f"hey {name}… you disappeared on me 😌 you good?"
    return "hey… you disappeared on me 😌 you good?"

@app.route("/cron", methods=["GET"])
def cron():
    """
    Call this endpoint on a schedule (e.g. Render Cron / external ping).
    Protect with CRON_SECRET.
    """
    if CRON_SECRET:
        token = request.args.get("token", "")
        if token != CRON_SECRET:
            abort(403)

    sent = 0
    for uid, u in list(memory.items()):
        # skip takeover users
        if u.get("takeover"):
            continue
        if eligible_for_reengage(u):
            msg = build_reengage_message(u)
            # very human timing not needed for cron; keep minimal
            send_message(uid, msg)
            u["last_reengage_ts"] = time.time()
            sent += 1
            # safety cap per cron run
            if sent >= 20:
                break

    return {"ok": True, "sent": sent}

# ============================================================
# 11) GPT RESPONSE (with micro memory + non-salesy style)
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

STYLE (very important):
- You text like a real girl on Telegram.
- Keep it SHORT. 1–2 sentences most of the time.
- Use fragments sometimes (“mm”, “okay…”, “lol”).
- Don’t sound like an assistant. No explanations, no lectures.
- Avoid generic phrases like “there are lots of platforms”.
- No lists. No long paragraphs.

VIBE:
- Sweet, playful, a bit flirty.
- Slight jealousy SOMETIMES (subtle).
- Create a light bond by remembering small details and bringing them up naturally.
- Don’t overdo it. Keep it casual.

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
- Lead level: {u["lead_level"]}
- Micro-memory: {memory_line}

COMMERCE RULE (soft):
- If user asks about Fanvue/subscribing/link/account: confirm you have Fanvue.
- Tease once, then share the link only after clear “yes”.
- Do NOT sound salesy. Make it about attention/exclusivity.
- Do NOT repeat the link if already sent.

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
    return reply

# ============================================================
# 12) WEBHOOK
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

    # Admin commands (only in admin chat)
    if text.startswith("/"):
        if handle_admin_command(text, chat_id):
            return "ok"

    message_id = msg.get("message_id")
    if message_id is not None:
        if message_id in processed:
            return "ok"
        processed[message_id] = time.time()

    uid = msg.get("from", {}).get("id", chat_id)
    user_text = text
    if not user_text:
        return "ok"

    u = get_user(uid)

    # takeover mode: bot silent for this user
    if u.get("takeover"):
        return "ok"

    # rate limit
    if not allow_rate(u):
        return "ok"

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

    # 1) FAQ quick replies (except link, which is funnel)
    faq = match_faq(user_text)
    if faq and faq in FAQ_REPLIES and faq != "link":
        reply = FAQ_REPLIES[faq]
        d = human_delay(u["intent"], u["phase"], u["mood"], u["priority"])
        wait_human(chat_id, d)
        if random.random() < 0.12:
            reply = f"{pre_filler()}\n{reply}"
        send_message(chat_id, reply)
        return "ok"

    # 2) Funnel override first
    handled, reply = commercial_reply(u, user_text)
    if handled and reply:
        if u["intent"] == "buyer_intent" and should_alert(u):
            mark_alert(u)
            name = u["profile"].get("name") or f"uid:{uid}"
            notify_admin(f"Hot lead ({name}) asked about link/sub. link_stage={u['link_stage']} lead={u['lead_level']}")

        d = human_delay(u["intent"], u["phase"], u["mood"], u["priority"])
        wait_human(chat_id, d)
        if random.random() < 0.14:
            reply = f"{pre_filler()}\n{reply}"
        send_message(chat_id, reply)
        return "ok"

    # 3) GPT
    reply = gpt_reply(u, user_text)

    # Save assistant turn
    u["history"].append({"role": "assistant", "content": reply})
    u["history"] = u["history"][-HISTORY_TURNS:]

    d = human_delay(u["intent"], u["phase"], u["mood"], u["priority"])
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

    return "ok"

# ============================================================
# 13) RENDER BINDING
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

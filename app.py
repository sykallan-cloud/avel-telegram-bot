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

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = OpenAI(api_key=OPENAI_API_KEY)

FANVUE_LINK = "https://www.fanvue.com/avelynnoira/fv-7"

MODEL = "gpt-4.1-mini"
MAX_OUTPUT_TOKENS = 170

# Funnel mode: 1 / 2 / 3 / 4 / mix
FUNNEL_MODE = os.environ.get("FUNNEL_MODE", "mix").strip().lower()
FORCE_LINK_AFTER = int(os.environ.get("FORCE_LINK_AFTER", "5"))

# Human timing
MAX_DELAY_SECONDS = 10.0

# De-dup TTL
PROCESSED_TTL_SECONDS = 60 * 15

# History
HISTORY_TURNS = 8

# ============================================================
# 0.1) BIO (Canon)
# ============================================================
AVELYN_BIO = """
Avelyn Noira is 21.
Born in Chengdu, China. Birth name Ava Lin is private.
At age 4, a collapsing metal advertising panel caused a vertical scar across her right eye and cheek.
Her eye was saved but she has partial vision loss.
She became observant, calm, and relies on instinct and positioning.
She uses gym discipline as structure and discovered padel later because timing and awareness fit her.
She moved to Europe and chose the public identity “Avelyn Noira” as a boundary. Ava remains private.
""".strip()

# ============================================================
# 1) STATE (in-memory)
# ============================================================
memory = {}              # uid -> dict
processed_msg = {}       # key -> ts
processed_updates = {}   # update_id -> ts


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
# 3) CLEANUP + DEDUP
# ============================================================
def cleanup_dict(d: dict, ttl: int):
    now = time.time()
    stale = [k for k, ts in d.items() if (now - ts) > ttl]
    for k in stale:
        d.pop(k, None)

def cleanup_processed():
    cleanup_dict(processed_msg, PROCESSED_TTL_SECONDS)
    cleanup_dict(processed_updates, PROCESSED_TTL_SECONDS)

def mark_once(key: str) -> bool:
    """
    Returns False if already processed.
    """
    now = time.time()
    if key in processed_msg:
        return False
    processed_msg[key] = now
    return True

# ============================================================
# 4) HUMANIZATION
# ============================================================
def human_wait(chat_id: int, min_s: float = 1.0, max_s: float = 4.0):
    d = random.uniform(min_s, max_s)
    d = min(d, MAX_DELAY_SECONDS)
    send_typing(chat_id)
    time.sleep(d)

# ============================================================
# 5) TEXT SANITIZATION (no "-" / "—")
# ============================================================
def sanitize_out(text: str) -> str:
    t = (text or "").strip()

    # remove bullet lines starting with dash or dot bullets
    lines = []
    for line in t.splitlines():
        s = line.strip()
        if s.startswith("- ") or s.startswith("•"):
            continue
        lines.append(line)
    t = "\n".join(lines).strip()

    # remove em dash and hyphen use (keep URLs intact)
    t = t.replace("—", " ")
    # remove spaced hyphen patterns
    t = re.sub(r"(?<!https:)(?<!http:)\s-\s", " ", t)

    # last resort remove standalone hyphens (can slightly affect words, acceptable per request)
    t = t.replace("-", "")

    # avoid double spaces
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t

# ============================================================
# 6) USER STATE + INTENT
# ============================================================
def get_user(uid: int):
    if uid not in memory:
        memory[uid] = {
            "messages": 0,
            "history": [],
            "link_sent": False,
            "mode": pick_mode(),
            "last_in_text": "",
            "last_in_ts": 0.0,
        }
    return memory[uid]

def pick_mode() -> str:
    if FUNNEL_MODE in ["1", "2", "3", "4"]:
        return FUNNEL_MODE
    # mix: weighted
    return random.choices(["1", "2", "3", "4"], weights=[30, 30, 20, 20], k=1)[0]

def detect_interest(text: str) -> bool:
    t = text.lower()
    triggers = [
        "fanvue", "link", "subscribe", "subscription", "join",
        "private", "exclusive", "custom", "request",
        "pics", "photos", "selfie", "spicy", "nude", "nudes",
        "what do you post", "what's inside", "pricing", "price", "how much"
    ]
    return any(k in t for k in triggers)

def asks_bio(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in [
        "scar", "eye", "vision", "chengdu", "china", "ava", "ava lin",
        "background", "story", "where are you from", "padel", "gym"
    ])

def bio_quick_reply(text: str) -> str:
    t = text.lower()
    if "ava" in t or "ava lin" in t or "real name" in t or "birth name" in t:
        return "Ava Lin is my private birth name. I go by Avelyn."
    if "chengdu" in t or "china" in t or "where are you from" in t:
        return "I was born in Chengdu, China. I live in Europe now."
    if "scar" in t or "eye" in t or "vision" in t:
        return "Yeah, I have a scar over my right eye from when I was little. My eye is okay, just not perfect vision."
    if "padel" in t:
        return "Padel is my thing. It is fast and tactical, it just fits me."
    if "gym" in t or "training" in t:
        return "Gym keeps me calm. Routine is my reset."
    return "Long story short, I am disciplined, a bit private, and I keep the deeper side off Telegram."

# ============================================================
# 7) FUNNEL COPY (4 MODES)
# ============================================================
def fanvue_value_line(mode: str) -> str:
    # All subtle, no explicit. No false "always real time"
    if mode == "1":
        return "Telegram is just the inner circle preview. Fanvue is where I share the real private side."
    if mode == "2":
        return "On Fanvue you get private drops, more personal content, and you can request customs."
    if mode == "3":
        return "If you want closer access, Fanvue is where I actually reply properly and keep it personal."
    # mode 4: FOMO subtle
    return "Most of what I post never stays on Telegram. The private drops go on Fanvue."

def fanvue_link_drop_line(mode: str) -> str:
    options = [
        f"Here you go 👀 {FANVUE_LINK}",
        f"Alright. This is the private door 👀 {FANVUE_LINK}",
        f"If you want the full access, it is here {FANVUE_LINK}",
        f"Private side is here 👀 {FANVUE_LINK}",
    ]
    # mode 4 gets a tiny fomo flavor
    if mode == "4":
        options += [
            f"Before I post the next private drop, this is where it goes 👀 {FANVUE_LINK}",
            f"If you want to catch the private drops, it is here {FANVUE_LINK}",
        ]
    return random.choice(options)

def should_send_link(u: dict) -> bool:
    if u["link_sent"]:
        return False
    # Option A: force within N messages
    return u["messages"] >= FORCE_LINK_AFTER

def funnel_response(u: dict, user_text: str):
    """
    Returns (handled: bool, reply: str or None)
    Strategy:
      - Always educate quickly
      - Link drop fast if interest OR forced by message count
    """
    mode = u.get("mode", "1")
    interested = detect_interest(user_text)

    # If forced by count or high intent keywords -> drop link
    if should_send_link(u) or interested:
        u["link_sent"] = True
        # 1 line value + link
        value = fanvue_value_line(mode)
        link = fanvue_link_drop_line(mode)
        return True, f"{value}\n{link}"

    # Otherwise: short positioning without long chat
    starters = [
        fanvue_value_line(mode),
        "I keep Telegram light. My private side is on Fanvue.",
        "Telegram is the preview. Fanvue is the full access.",
        "I do not do long chats here. Fanvue is where I keep it personal.",
    ]
    return True, random.choice(starters)

# ============================================================
# 8) GPT (only for short human filler, but still sales-aligned)
# ============================================================
def build_system_prompt(u: dict) -> str:
    mode = u.get("mode", "1")
    return f"""
You are Avelyn Noira. Reply ONLY in English.

Canon bio:
{AVELYN_BIO}

Rules:
Keep replies very short, 1 or 2 sentences.
No long bonding. Telegram is a preview only.
Do not use dashes or bullets.
No explicit sexual content.
No meetups.
If asked for photos, keep it classy and redirect to Fanvue without sounding harsh.
You can mention: private drops, customs, priority replies on Fanvue.
Do not claim guaranteed real time chatting. Say "priority replies" or "more personal chat" instead.
Do not paste the Fanvue link. The server handles the link.

Goal:
Subtly guide people to Fanvue quickly.
""".strip()

def gpt_short(u: dict, user_text: str) -> str:
    sys = build_system_prompt(u)
    resp = client.responses.create(
        model=MODEL,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        input=[{"role": "system", "content": sys}, *u["history"], {"role": "user", "content": user_text}],
    )
    out = (resp.output_text or "").strip()
    out = sanitize_out(out)

    # hard block accidental link / placeholders
    low = out.lower()
    if "http" in low or "fanvue.com" in low or "[link]" in low:
        out = "If you want the private side, I keep it on Fanvue."
    return out

# ============================================================
# 9) MAIN MESSAGE HANDLER (runs in background thread)
# ============================================================
def handle_update(update: dict):
    try:
        cleanup_processed()

        msg = update.get("message")
        if not msg:
            return

        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()

        # ignore /start
        if text.lower().startswith("/start"):
            return

        uid = msg.get("from", {}).get("id", chat_id)
        u = get_user(uid)

        # anti double-send for repeated same message
        now = time.time()
        if u.get("last_in_text", "") == text and (now - u.get("last_in_ts", 0.0)) < 2.0:
            return
        u["last_in_text"] = text
        u["last_in_ts"] = now

        # increment messages
        u["messages"] += 1

        # store user turn in history (short)
        u["history"].append({"role": "user", "content": text})
        u["history"] = u["history"][-HISTORY_TURNS:]

        # bio Qs get quick human answer
        if asks_bio(text):
            reply = bio_quick_reply(text)
            reply = sanitize_out(reply)
            human_wait(chat_id, 1.2, 3.8)
            send_message(chat_id, reply)
            return

        # funnel always handled, fast conversion
        handled, reply = funnel_response(u, text)
        if handled and reply:
            # optional admin alert when link sent
            if u.get("link_sent") and ADMIN_CHAT_ID:
                notify_admin(f"Link sent to uid={uid}, mode={u.get('mode')}, msg_count={u.get('messages')}")

            reply = sanitize_out(reply)
            human_wait(chat_id, 1.4, 4.5)
            send_message(chat_id, reply)
            return

        # fallback GPT (rare)
        reply = gpt_short(u, text)
        u["history"].append({"role": "assistant", "content": reply})
        u["history"] = u["history"][-HISTORY_TURNS:]
        human_wait(chat_id, 1.4, 4.5)
        send_message(chat_id, reply)

    except Exception as e:
        print("handle_update error:", str(e))

# ============================================================
# 10) WEBHOOK (ACK fast to avoid retries)
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    # dedup on update_id first
    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in processed_updates:
            return "ok"
        processed_updates[update_id] = time.time()

    msg = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")

    # stable key for message-level dedup
    key = f"{chat_id}:{message_id}" if chat_id is not None and message_id is not None else str(time.time())
    if not mark_once(key):
        return "ok"

    # process in background
    t = threading.Thread(target=handle_update, args=(update,), daemon=True)
    t.start()

    return "ok"

# ============================================================
# 11) RUN
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

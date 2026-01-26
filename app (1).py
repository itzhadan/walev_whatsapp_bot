import json
import hmac
import hashlib
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================
# תמלא רק את זה (4 שורות)
# =========================
VERIFY_TOKEN = "walev_verify_123"
PHONE_NUMBER_ID = "931796590022288"
WHATSAPP_TOKEN = "EAAdeaX8RHTUBQp3mLOxdMZAlL40zqxJUi5muDK9LrqgiFIkyVg83nEE2VS1KBznDfkzoFHt0ZB7NvlSByEenZCwX3M3laLQDZB7MmtD4zr131hoXwG81QZARsbeMxaIrmeghi7cx9IdIhGLuuSAeYZC8RLtW4i5jqTYvi9QgJ7se0a4LiqLfA8uJOxEli7yQZDZD"
META_APP_SECRET = ""   # אופציונלי, אפשר להשאיר ריק לטסט

GRAPH_VERSION = "v22.0"


# =========================
# אבטחת חתימה (אופציונלי)
# =========================
def verify_signature(req) -> bool:
    if not META_APP_SECRET:
        return True

    sig = req.headers.get("X-Hub-Signature-256", "")
    if not sig.startswith("sha256="):
        return False

    expected = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        msg=req.get_data(),
        digestmod=hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(sig.split("=", 1)[1], expected)


# =========================
# שליחת טקסט
# =========================
def send_text(to_phone: str, text: str):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    print("SEND TEXT STATUS:", r.status_code)
    print("SEND TEXT BODY:", r.text)
    return r.status_code, r.text


# =========================
# שליחת כפתורים (Interactive Buttons)
# =========================
def send_menu_buttons(to_phone: str):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": "ברוך הבא ל־Xpresphone 👋\nבחר מה תרצה לעשות:"
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": "BTN_IPHONE", "title": "📱 תיקון iPhone"}
                    },
                    {
                        "type": "reply",
                        "reply": {"id": "BTN_ANDROID", "title": "🤖 תיקון Android"}
                    },
                    {
                        "type": "reply",
                        "reply": {"id": "BTN_HOURS", "title": "🕒 שעות פעילות"}
                    }
                ]
            }
        }
    }

    r = requests.post(url, headers=headers, json=payload, timeout=20)
    print("SEND MENU STATUS:", r.status_code)
    print("SEND MENU BODY:", r.text)
    return r.status_code, r.text


# =========================
# אימות Webhook (GET)
# =========================
@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("WEBHOOK VERIFIED ✅")
        return challenge, 200

    print("WEBHOOK VERIFY FAILED ❌", mode, token)
    return "Forbidden", 403


# =========================
# קבלת הודעות (POST)
# =========================
@app.post("/webhook")
def webhook_receive():
    if not verify_signature(request):
        return "Invalid signature", 403

    data = request.get_json(silent=True) or {}
    print("INCOMING:", json.dumps(data, ensure_ascii=False))

    try:
        value = data["entry"][0]["changes"][0]["value"]

        messages = value.get("messages", [])
        if messages:
            msg = messages[0]
            from_phone = msg.get("from")

            # 1) טקסט רגיל
            text_body = (msg.get("text") or {}).get("body", "")
            text_lower = (text_body or "").strip().lower()

            # 2) תשובה מכפתור (interactive)
            button_id = None
            interactive = msg.get("interactive")
            if interactive and interactive.get("type") == "button_reply":
                button_id = interactive["button_reply"].get("id")

            print("FROM:", from_phone, "TEXT:", text_body, "BUTTON_ID:", button_id)

            # --- לוגיקה ---
            if not from_phone:
                return jsonify({"ok": True}), 200

            # אם המשתמש כתב "שלום" / "היי" וכו' => שלח תפריט כפתורים
            if text_lower in ["היי", "שלום", "הי", "hi", "hello", "start", "menu", "תפריט"]:
                send_menu_buttons(from_phone)
                return jsonify({"ok": True}), 200

            # אם המשתמש לחץ כפתור:
            if button_id == "BTN_IPHONE":
                send_text(from_phone, "מעולה 📱 איזה דגם iPhone ומה התקלה?")
            elif button_id == "BTN_ANDROID":
                send_text(from_phone, "מעולה 🤖 איזה דגם Android ומה התקלה?")
            elif button_id == "BTN_HOURS":
                send_text(from_phone, "שעות פעילות: א׳-ה׳ 10:00–19:00 | ו׳ 10:00–14:00")
            else:
                # ברירת מחדל (לא אקו)
                send_text(from_phone, "כתוב 'שלום' כדי לפתוח תפריט כפתורים 🙂")

    except Exception as e:
        print("PARSE ERROR:", str(e))

    return jsonify({"ok": True}), 200


# =========================
# בדיקת חיים
# =========================
@app.get("/")
def home():
    return "OK - WhatsApp bot is running", 200

import os
import sqlite3
import datetime
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from flask import Flask, request, abort, jsonify
import requests

# Optional RTL helpers (if missing, app still runs)
try:
    import arabic_reshaper
except Exception:
    arabic_reshaper = None

try:
    from bidi.algorithm import get_display
except Exception:
    def get_display(s): return s

from reportlab.pdfgen import canvas
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm

app = Flask(__name__)

# =========================
# 0) WHATSAPP CONFIG (תמלא 4 שורות)
# =========================
VERIFY_TOKEN = "walev_verify_123"
PHONE_NUMBER_ID = "931796590022288"
WHATSAPP_TOKEN = "EAAdeaX8RHTUBQp3mLOxdMZAlL40zqxJUi5muDK9LrqgiFIkyVg83nEE2VS1KBznDfkzoFHt0ZB7NvlSByEenZCwX3M3laLQDZB7MmtD4zr131hoXwG81QZARsbeMxaIrmeghi7cx9IdIhGLuuSAeYZC8RLtW4i5jqTYvi9QgJ7se0a4LiqLfA8uJOxEli7yQZDZD"
META_APP_SECRET = ""  # רשות

GRAPH_VERSION = "v22.0"

# אם אין לך Environment Variables (חבילה חינם) – נשארים hardcoded פה.
PUBLIC_BASE_URL = "https://walev.pythonanywhere.com"  # חשוב שיהיה הדומיין שלך!

# Admin phones (בלי +)
ADMIN_PHONES = {"972547474646"}  # תוסיף עוד אם צריך

# ======================
# PATHS / CONFIG
# ======================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "app.db"
INVOICES_DIR = BASE_DIR / "invoices"
INVOICES_DIR.mkdir(exist_ok=True)

PDF_FONT_FILE = str(BASE_DIR / "NotoSansHebrew-Regular.ttf")
LOGO_FILE = str(BASE_DIR / "logo.png")  # optional watermark

LOG_FILE = str(BASE_DIR / "bot.log")
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

def log(msg: str):
    logging.info(msg)

# ======================
# BUSINESS
# ======================
BUSINESS_NAME = "Expresphone"
BUSINESS_SUB = "מעבדה לתיקון סלולר עד בית הלקוח"
BUSINESS_PHONE = "054-7474646"
BUSINESS_NOTE_1 = "עוסק פטור – ללא מע״מ"
BUSINESS_NOTE_2 = "אחריות על תיקון לפי סוג עבודה • ללא אחריות על נזקי מים"
NOTE_DEFAULT = "יתכנו שינויים לרכיבים מקוריים/פירוק"

SITE_URL = "https://expresphone.com/"
WAZE_URL = "https://waze.com/ul/hsv8vkpy8j"
GOOGLE_REVIEW_URL = "https://www.google.com/search?q=Expresphone+ביקורות"
EASY_REVIEW_URL = "https://easy.co.il/page/10118064"

# ======================
# PAYPAL (LIVE) - אותו דבר כמו בטלגרם
# ======================
PAYPAL_CLIENT_ID = ""       # אם תרצה – נכניס כמו בטלגרם
PAYPAL_CLIENT_SECRET = ""   # אם תרצה – נכניס כמו בטלגרם
PAYPAL_API_BASE = "https://api-m.paypal.com"
CURRENCY = "ILS"

# ======================
# PRICELIST
# ======================
ITEMS = {
    "screen":   ("📱 מסך", 399.00),
    "battery":  ("🔋 סוללה", 299.00),
    "charge":   ("🔌 שקע טעינה", 349.00),
    "delivery": ("🚚 שליחות", 69.90),
    "glass":    ("🛡️ מגן זכוכית", 3.99),
}

# ======================
# DB
# ======================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def now_iso() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def init_db_and_migrate():
    conn = db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        wa_id TEXT,                 -- whatsapp user id (המספר של הלקוח בלי +)
        customer_name TEXT,
        customer_phone TEXT,
        item1_key TEXT,
        item1_label TEXT,
        item1_amount REAL,
        item2_key TEXT,
        item2_label TEXT,
        item2_amount REAL,
        total_amount REAL,
        note TEXT,
        pay_link TEXT,
        status TEXT,
        created_at TEXT,
        paid_at TEXT,
        invoice_no INTEGER,
        invoice_pdf_path TEXT,
        paypal_order_id TEXT,
        paypal_capture_id TEXT,
        paypal_status TEXT
    )
    """)
    conn.commit()
    conn.close()

def next_invoice_no(conn) -> int:
    r = conn.execute("SELECT MAX(COALESCE(invoice_no,0)) AS m FROM orders").fetchone()
    return int(r["m"] or 0) + 1

# ======================
# RTL / PDF
# ======================
def rtl(text: str) -> str:
    if not text:
        return ""
    s = str(text)
    if arabic_reshaper is not None:
        try:
            s = arabic_reshaper.reshape(s)
        except Exception:
            pass
    try:
        return get_display(s)
    except Exception:
        return s

def register_font() -> str:
    try:
        if os.path.isfile(PDF_FONT_FILE):
            pdfmetrics.registerFont(TTFont("HEB", PDF_FONT_FILE))
            return "HEB"
    except Exception as e:
        log(f"FONT register error: {e}")
    return "Helvetica"

def money(x: float) -> str:
    try:
        return f"{float(x):,.2f} ₪"
    except Exception:
        return f"{x} ₪"

def try_alpha(c, a: float) -> bool:
    try:
        c.setFillAlpha(a)
        c.setStrokeAlpha(a)
        return True
    except Exception:
        return False

def watermark(c, w, h):
    if not os.path.isfile(LOGO_FILE):
        return
    c.saveState()
    try_alpha(c, 0.07)
    c.translate(w/2, h/2)
    c.rotate(35)
    img_w = 260 * mm
    img_h = 260 * mm
    c.drawImage(LOGO_FILE, -img_w/2, -img_h/2, width=img_w, height=img_h,
                mask="auto", preserveAspectRatio=True)
    c.restoreState()

def build_invoice(order: Dict[str, Any], invoice_no: int) -> str:
    font = register_font()
    w, h = A4
    path = str(INVOICES_DIR / f"invoice_{invoice_no}.pdf")
    c = canvas.Canvas(path, pagesize=A4)

    watermark(c, w, h)

    c.setFillColor(colors.HexColor("#111827"))
    c.setFont(font, 18)
    c.drawRightString(w - 15*mm, h - 20*mm, rtl("חשבונית מס / קבלה"))

    c.setFont(font, 11)
    c.setFillColor(colors.HexColor("#374151"))
    c.drawRightString(w - 15*mm, h - 28*mm, rtl(BUSINESS_NAME))
    c.drawRightString(w - 15*mm, h - 34*mm, rtl(BUSINESS_SUB))
    c.drawRightString(w - 15*mm, h - 40*mm, rtl(f"טלפון: {BUSINESS_PHONE}"))

    c.setFillColor(colors.HexColor("#111827"))
    c.setFont(font, 12)
    c.drawRightString(w - 15*mm, h - 55*mm, rtl(f"מס׳ חשבונית: {invoice_no}"))
    c.drawRightString(w - 15*mm, h - 62*mm, rtl(f"תאריך: {now_iso()}"))

    c.setFont(font, 12)
    c.drawRightString(w - 15*mm, h - 78*mm, rtl(f"לקוח: {order.get('customer_name','')}"))
    c.drawRightString(w - 15*mm, h - 86*mm, rtl(f"טלפון: {order.get('customer_phone','')}"))

    y = h - 98*mm
    c.setStrokeColor(colors.HexColor("#111827"))
    c.line(15*mm, y, w - 15*mm, y)

    y -= 12*mm
    c.setFont(font, 12)
    c.setFillColor(colors.HexColor("#111827"))
    c.drawRightString(w - 15*mm, y, rtl("פריט"))
    c.drawString(15*mm, y, rtl("סכום"))

    y -= 10*mm
    c.setFont(font, 11)
    c.setFillColor(colors.HexColor("#111827"))
    c.drawRightString(w - 15*mm, y, rtl(order.get("item1_label","")))
    c.drawString(15*mm, y, money(float(order.get("item1_amount") or 0)))

    if (order.get("item2_label") or "").strip():
        y -= 8*mm
        c.drawRightString(w - 15*mm, y, rtl(order.get("item2_label","")))
        c.drawString(15*mm, y, money(float(order.get("item2_amount") or 0)))

    y -= 14*mm
    c.line(15*mm, y, w - 15*mm, y)

    y -= 12*mm
    total = float(order.get("total_amount") or 0)
    c.setFont(font, 14)
    c.drawRightString(w - 15*mm, y, rtl("סה״כ לתשלום"))
    c.drawString(15*mm, y, money(total))

    c.setFont(font, 10)
    c.setFillColor(colors.HexColor("#111827"))
    c.drawRightString(w - 15*mm, 18*mm, rtl(BUSINESS_NOTE_1))
    c.setFont(font, 9)
    c.setFillColor(colors.HexColor("#6B7280"))
    c.drawRightString(w - 15*mm, 12*mm, rtl(BUSINESS_NOTE_2))

    c.save()
    return path

# ======================
# WhatsApp API helpers
# ======================
def wa_post(payload: dict):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    log(f"WA SEND {r.status_code} {r.text[:300]}")
    return r.status_code, r.text

def wa_send_text(to_wa_id: str, text: str):
    return wa_post({
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": text}
    })

def wa_send_list(to_wa_id: str, title: str, body: str, button: str, sections: list):
    return wa_post({
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": title},
            "body": {"text": body},
            "action": {"button": button, "sections": sections}
        }
    })

def wa_upload_media(file_path: str, mime_type: str = "application/pdf") -> str:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    with open(file_path, "rb") as f:
        files = {"file": (Path(file_path).name, f, mime_type)}
        data = {"messaging_product": "whatsapp", "type": mime_type}
        r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    r.raise_for_status()
    return r.json()["id"]

def wa_send_document(to_wa_id: str, file_path: str, caption: str = "🧾 חשבונית"):
    media_id = wa_upload_media(file_path, "application/pdf")
    return wa_post({
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "document",
        "document": {"id": media_id, "caption": caption, "filename": Path(file_path).name}
    })

def is_admin_wa(wa_id: str) -> bool:
    return str(wa_id) in ADMIN_PHONES

# ======================
# Sessions (כמו בטלגרם)
# ======================
sessions: Dict[str, Dict[str, Any]] = {}

# ======================
# Menus (List) - כמו טלגרם
# ======================
def show_main_menu(wa_id: str):
    sections = [{
        "title": "תפריט ראשי",
        "rows": [
            {"id": "menu:pay", "title": "💳 הזמנה ותשלום", "description": "יצירת תשלום + חשבונית"},
            {"id": "menu:pricelist", "title": "📋 מחירון", "description": "מחירי שירותים"},
            {"id": "menu:reviews", "title": "⭐ ביקורות", "description": "גוגל + איזי"},
            {"id": "menu:navigate", "title": "🧭 ניווט", "description": "Waze"},
            {"id": "menu:restore", "title": "🧾 שחזור חשבונית", "description": "לפי טלפון"},
        ]
    }]

    # אדמין
    if is_admin_wa(wa_id):
        sections[0]["rows"].append(
            {"id": "admin:manual_invoice", "title": "🛠️ אדמין (חשבונית ידנית)", "description": "יצירה בלי תשלום"}
        )

    wa_send_list(
        wa_id,
        title=f"{BUSINESS_NAME}",
        body="בחר פעולה 👇",
        button="פתח תפריט",
        sections=sections
    )

def show_items_menu(wa_id: str, step: str, include_none: bool):
    rows = [
        {"id": f"{step}:screen", "title": f"📱 מסך — {ITEMS['screen'][1]:.0f} ₪"},
        {"id": f"{step}:battery", "title": f"🔋 סוללה — {ITEMS['battery'][1]:.0f} ₪"},
        {"id": f"{step}:charge", "title": f"🔌 שקע — {ITEMS['charge'][1]:.0f} ₪"},
        {"id": f"{step}:delivery", "title": f"🚚 שליחות — {ITEMS['delivery'][1]:.2f} ₪"},
        {"id": f"{step}:glass", "title": f"🛡️ זכוכית — {ITEMS['glass'][1]:.2f} ₪"},
    ]
    if include_none:
        rows.append({"id": f"{step}:none", "title": "➖ בלי פריט 2"})

    wa_send_list(
        wa_id,
        title="בחירת פריט",
        body="בחר מהשירותים:",
        button="בחר פריט",
        sections=[{"title": "פריטים", "rows": rows}]
    )

# ======================
# Order logic (דומה לטלגרם)
# ======================
def create_order_local(wa_id: str, name: str, phone: str, item1: str, item2: str) -> Dict[str, Any]:
    if item1 not in ITEMS:
        raise ValueError("bad_item1")
    if item2 and item2 != "none" and item2 not in ITEMS:
        raise ValueError("bad_item2")

    i1_label, i1_amount = ITEMS[item1]
    i2_key, i2_label, i2_amount = "", "", 0.0
    if item2 and item2 != "none":
        i2_key = item2
        i2_label, i2_amount = ITEMS[item2]

    total = float(i1_amount) + float(i2_amount)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders (
            wa_id, customer_name, customer_phone,
            item1_key, item1_label, item1_amount,
            item2_key, item2_label, item2_amount,
            total_amount, note, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(wa_id), name, phone,
        item1, i1_label, float(i1_amount),
        i2_key, i2_label, float(i2_amount) if i2_key else None,
        total, NOTE_DEFAULT, "pending", now_iso()
    ))
    conn.commit()
    order_id = int(cur.lastrowid)
    conn.close()

    # כאן יש 2 אפשרויות:
    # A) אם אתה רוצה PayPal מלא כמו בטלגרם – נחבר את פונקציות PayPal שלך (אותן בדיוק) ונשמור pay_link
    # B) אם כרגע רק לינק קבוע לתשלום/PayPal.me – שמים אותו פה.
    pay_link = "https://expresphone.com/pay"  # תחליף ללינק האמיתי שלך
    conn = db()
    conn.execute("UPDATE orders SET pay_link=? WHERE id=?", (pay_link, order_id))
    conn.commit()
    conn.close()

    return {"order_id": order_id, "total": total, "pay_link": pay_link,
            "items": [
                {"label": i1_label, "amount": float(i1_amount)},
                *([{"label": i2_label, "amount": float(i2_amount)}] if i2_key else []),
            ]}

def finalize_paid_and_send_invoice(order_id: int) -> str:
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.close()
        raise FileNotFoundError("order_not_found")

    if row["invoice_pdf_path"] and os.path.isfile(row["invoice_pdf_path"]):
        conn.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (now_iso(), order_id))
        conn.commit()
        conn.close()
        return row["invoice_pdf_path"]

    inv_no = next_invoice_no(conn)
    pdf_path = build_invoice(dict(row), inv_no)

    conn.execute("""
        UPDATE orders
        SET status='paid', paid_at=?, invoice_no=?, invoice_pdf_path=?
        WHERE id=?
    """, (now_iso(), inv_no, pdf_path, order_id))
    conn.commit()
    conn.close()
    return pdf_path

# ======================
# Webhook Verify (GET)
# ======================
@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# ======================
# Webhook Receive (POST)
# ======================
@app.post("/webhook")
def webhook_receive():
    data = request.get_json(silent=True) or {}
    log(f"INCOMING {str(data)[:800]}")

    try:
        value = data["entry"][0]["changes"][0]["value"]
        messages = value.get("messages", [])
        if not messages:
            return jsonify(ok=True), 200

        msg = messages[0]
        wa_id = msg.get("from")  # הלקוח
        msg_type = msg.get("type")

        # לחיצה על List/Buttons
        if msg_type == "interactive":
            inter = msg.get("interactive", {})
            if inter.get("type") == "list_reply":
                action_id = inter["list_reply"]["id"]
                return handle_action(wa_id, action_id)

        # טקסט רגיל
        text = (msg.get("text") or {}).get("body", "").strip()
        return handle_text(wa_id, text)

    except Exception as e:
        log(f"PARSE ERROR {e}")
        return jsonify(ok=True), 200

def handle_action(wa_id: str, action_id: str):
    # תפריט
    if action_id == "menu:pay":
        sessions[wa_id] = {"step": "name"}
        wa_send_text(wa_id, "שם לקוח?")
        return jsonify(ok=True), 200

    if action_id == "menu:pricelist":
        pricelist = (
            f"📋 מחירון {BUSINESS_NAME}\n\n"
            f"📱 מסך — {ITEMS['screen'][1]:.2f} ₪\n"
            f"🔋 סוללה — {ITEMS['battery'][1]:.2f} ₪\n"
            f"🔌 שקע טעינה — {ITEMS['charge'][1]:.2f} ₪\n"
            f"🚚 שליחות — {ITEMS['delivery'][1]:.2f} ₪\n"
            f"🛡️ מגן זכוכית — {ITEMS['glass'][1]:.2f} ₪\n\n"
            f"ℹ️ {NOTE_DEFAULT}"
        )
        wa_send_text(wa_id, pricelist)
        show_main_menu(wa_id)
        return jsonify(ok=True), 200

    if action_id == "menu:reviews":
        wa_send_text(wa_id, f"⭐ ביקורות:\nגוגל:\n{GOOGLE_REVIEW_URL}\n\nאיזי:\n{EASY_REVIEW_URL}")
        show_main_menu(wa_id)
        return jsonify(ok=True), 200

    if action_id == "menu:navigate":
        wa_send_text(wa_id, f"🧭 ניווט:\n{WAZE_URL}")
        show_main_menu(wa_id)
        return jsonify(ok=True), 200

    if action_id == "menu:restore":
        sessions[wa_id] = {"step": "restore_phone"}
        wa_send_text(wa_id, "הזן מספר טלפון לשחזור חשבוניות:")
        return jsonify(ok=True), 200

    if action_id == "admin:manual_invoice":
        if not is_admin_wa(wa_id):
            wa_send_text(wa_id, "אין הרשאה.")
            show_main_menu(wa_id)
            return jsonify(ok=True), 200
        sessions[wa_id] = {"step": "admin_amount"}
        wa_send_text(wa_id, "🛠️ אדמין: הזן סכום (לדוגמה 250.00):")
        return jsonify(ok=True), 200

    # בחירת פריטים
    if action_id.startswith("item1:"):
        key = action_id.split(":", 1)[1]
        st = sessions.setdefault(wa_id, {})
        st["item1"] = key
        st["step"] = "item2"
        show_items_menu(wa_id, "item2", include_none=True)
        return jsonify(ok=True), 200

    if action_id.startswith("item2:"):
        key2 = action_id.split(":", 1)[1]
        st = sessions.get(wa_id) or {}
        name = (st.get("name") or "").strip()
        phone = (st.get("phone") or "").strip()
        item1 = (st.get("item1") or "").strip()

        if not name or not phone or not item1:
            sessions.pop(wa_id, None)
            wa_send_text(wa_id, "משהו התבלבל. כתוב 'תפריט' להתחלה מחדש.")
            show_main_menu(wa_id)
            return jsonify(ok=True), 200

        data2 = create_order_local(wa_id, name, phone, item1, key2)
        order_id = data2["order_id"]
        total = data2["total"]
        pay_link = data2["pay_link"]
        items_txt = "\n".join([f"• {it['label']} — {money(it['amount'])}" for it in data2["items"]])

        wa_send_text(
            wa_id,
            f"✅ הזמנה #{order_id} נוצרה\n"
            f"👤 {name} | {phone}\n\n"
            f"{items_txt}\n"
            f"💳 סה״כ: {money(total)}\n"
            f"ℹ️ {NOTE_DEFAULT}\n\n"
            f"לתשלום:\n{pay_link}\n\n"
            f"אחרי ששילמת – כתוב לי: 'שילמתי {order_id}' ואשלח חשבונית."
        )
        sessions.pop(wa_id, None)
        show_main_menu(wa_id)
        return jsonify(ok=True), 200

    # ברירת מחדל
    wa_send_text(wa_id, "בחר מהתפריט 👇")
    show_main_menu(wa_id)
    return jsonify(ok=True), 200

def handle_text(wa_id: str, text: str):
    text_l = (text or "").strip().lower()
    st = sessions.get(wa_id)

    # להתחלה
    if text_l in ("start", "/start", "תפריט", "menu", "התחל"):
        sessions.pop(wa_id, None)
        show_main_menu(wa_id)
        return jsonify(ok=True), 200

    # “שילמתי 123” -> מפיק חשבונית ושולח PDF
    if text_l.startswith("שילמתי"):
        parts = text.replace("#", " ").split()
        oid = None
        for p in parts:
            if p.isdigit():
                oid = int(p)
                break
        if not oid:
            wa_send_text(wa_id, "שלח: שילמתי 123 (מספר הזמנה)")
            return jsonify(ok=True), 200

        try:
            pdf_path = finalize_paid_and_send_invoice(oid)
            wa_send_text(wa_id, "✅ מעולה. שולח חשבונית…")
            wa_send_document(wa_id, pdf_path, caption="🧾 חשבונית ✅")
        except Exception:
            wa_send_text(wa_id, "לא מצאתי הזמנה כזו. בדוק מספר הזמנה.")
        show_main_menu(wa_id)
        return jsonify(ok=True), 200

    # flow של הזמנה
    if st and st.get("step") == "name":
        st["name"] = text.strip()
        st["step"] = "phone"
        wa_send_text(wa_id, "מספר טלפון?")
        return jsonify(ok=True), 200

    if st and st.get("step") == "phone":
        st["phone"] = text.strip()
        st["step"] = "item1"
        show_items_menu(wa_id, "item1", include_none=False)
        return jsonify(ok=True), 200

    # restore
    if st and st.get("step") == "restore_phone":
        phone = text.strip()
        conn = db()
        rows = conn.execute(
            "SELECT id, invoice_pdf_path FROM orders WHERE customer_phone=? AND invoice_pdf_path IS NOT NULL ORDER BY id DESC LIMIT 5",
            (phone,)
        ).fetchall()
        conn.close()
        if not rows:
            wa_send_text(wa_id, "לא נמצאו חשבוניות לטלפון הזה.")
        else:
            wa_send_text(wa_id, f"נמצאו {len(rows)} חשבוניות. שולח…")
            for r in rows:
                p = r["invoice_pdf_path"]
                if p and os.path.isfile(p):
                    wa_send_document(wa_id, p, caption="🧾 שחזור חשבונית")
        sessions.pop(wa_id, None)
        show_main_menu(wa_id)
        return jsonify(ok=True), 200

    # admin manual invoice
    if st and st.get("step") == "admin_amount":
        if not is_admin_wa(wa_id):
            sessions.pop(wa_id, None)
            wa_send_text(wa_id, "אין הרשאה.")
            show_main_menu(wa_id)
            return jsonify(ok=True), 200
        try:
            amt = float(text.replace(",", ".").strip())
            if amt <= 0:
                raise ValueError()
        except Exception:
            wa_send_text(wa_id, "סכום לא תקין. לדוגמה: 250.00")
            return jsonify(ok=True), 200
        st["amount"] = amt
        st["step"] = "admin_name"
        wa_send_text(wa_id, "שם לקוח (לקבלה ידנית):")
        return jsonify(ok=True), 200

    if st and st.get("step") == "admin_name":
        st["name"] = text.strip()
        st["step"] = "admin_phone"
        wa_send_text(wa_id, "טלפון לקוח:")
        return jsonify(ok=True), 200

    if st and st.get("step") == "admin_phone":
        if not is_admin_wa(wa_id):
            sessions.pop(wa_id, None)
            wa_send_text(wa_id, "אין הרשאה.")
            show_main_menu(wa_id)
            return jsonify(ok=True), 200

        cust_phone = text.strip()
        amt = float(st.get("amount") or 0)

        conn = db()
        inv_no = next_invoice_no(conn)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO orders (
                wa_id, customer_name, customer_phone,
                item1_key, item1_label, item1_amount,
                total_amount, note, status, created_at, paid_at, invoice_no
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            wa_id, st.get("name",""), cust_phone,
            "manual", "קבלה ידנית (אדמין)", amt,
            amt, NOTE_DEFAULT, "paid", now_iso(), now_iso(), inv_no
        ))
        conn.commit()
        order_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        pdf_path = build_invoice(dict(row), inv_no)
        conn.execute("UPDATE orders SET invoice_pdf_path=? WHERE id=?", (pdf_path, order_id))
        conn.commit()
        conn.close()

        wa_send_document(wa_id, pdf_path, caption="🧾 חשבונית ידנית ✅")
        sessions.pop(wa_id, None)
        show_main_menu(wa_id)
        return jsonify(ok=True), 200

    # אחרת: מציג תפריט
    wa_send_text(wa_id, "לא הבנתי 🙂 כתוב 'תפריט' או בחר מהתפריט.")
    show_main_menu(wa_id)
    return jsonify(ok=True), 200

# ======================
# Health
# ======================
@app.get("/")
def home():
    return "OK - WhatsApp Expresphone bot running", 200

# init
init_db_and_migrate()

import os
import sqlite3
import datetime
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

from flask import Flask, request, jsonify
import requests

# Optional RTL helpers
try:
    from bidi.algorithm import get_display
except Exception:
    def get_display(s, base_dir=None):
        return s

# ReportLab
from reportlab.pdfgen import canvas
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm


app = Flask(__name__)

# =========================
# 0) WHATSAPP CONFIG
# =========================
VERIFY_TOKEN = "walev_verify_123"
PHONE_NUMBER_ID = "931796590022288"
WHATSAPP_TOKEN = "EAAdeaX8RHTUBQp3mLOxdMZAlL40zqxJUi5muDK9LrqgiFIkyVg83nEE2VS1KBznDfkzoFHt0ZB7NvlSByEenZCwX3M3laLQDZB7MmtD4zr131hoXwG81QZARsbeMxaIrmeghi7cx9IdIhGLuuSAeYZC8RLtW4i5jqTYvi9QgJ7se0a4LiqLfA8uJOxEli7yQZDZD"
GRAPH_VERSION = "v22.0"

PUBLIC_BASE_URL = "https://walev.pythonanywhere.com"
ADMIN_PHONES = {"972547474646"}  # בלי +

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
# PAYPAL (LIVE)
# ======================
PAYPAL_CLIENT_ID = "AU3U52qcEE20apC4OUTB88PIyKw7ol9nexrciMGrmMbetc94e2kN0bUdlZdBHfRXu49FEUFZKKQ2JgIq"
PAYPAL_CLIENT_SECRET = "EAsylE4gN6dpIqt0i8FpSxcA0Dt7phe9D2UF3LQ33MACLP25uOZv0qzzC5iZB3KuYF2JLjqyK0KC2lS-"
PAYPAL_API_BASE = "https://api-m.paypal.com"  # LIVE
CURRENCY = "ILS"

# ======================
# PRICELIST
# ======================
ITEMS = {
    "screen":   ("📱 מסך", 399.00),
    "battery":  ("🔋 סוללה", 299.00),
    "charge":   ("🔌 שקע טעינה", 349.00),
    "delivery": ("🚚 שליחות", 69.90),
    "glass":    ("🛡️ מגן זכוכית", 49.00),  # ✅ תיקון
}

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
    try:
        print(msg, flush=True)  # יופיע גם ב-Server log
    except Exception:
        pass

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

    # orders - כמו שהיה
    conn.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        wa_id TEXT,
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

    # customers - לזכור לקוח חוזר
    conn.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        wa_id TEXT PRIMARY KEY,
        name TEXT,
        phone TEXT,
        last_seen TEXT
    )
    """)

    # tickets - פניות ללא תשלום
    conn.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        wa_id TEXT,
        customer_name TEXT,
        customer_phone TEXT,
        device TEXT,
        issue TEXT,
        created_at TEXT,
        status TEXT
    )
    """)

    conn.commit()
    conn.close()

def upsert_customer(wa_id: str, name: Optional[str], phone: Optional[str]):
    conn = db()
    existing = conn.execute("SELECT * FROM customers WHERE wa_id=?", (str(wa_id),)).fetchone()
    if existing:
        # עדכון רק אם יש ערך
        new_name = name or existing["name"]
        new_phone = phone or existing["phone"]
        conn.execute("UPDATE customers SET name=?, phone=?, last_seen=? WHERE wa_id=?",
                     (new_name, new_phone, now_iso(), str(wa_id)))
    else:
        conn.execute("INSERT INTO customers (wa_id, name, phone, last_seen) VALUES (?, ?, ?, ?)",
                     (str(wa_id), name or "", phone or "", now_iso()))
    conn.commit()
    conn.close()

def get_customer(wa_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute("SELECT * FROM customers WHERE wa_id=?", (str(wa_id),)).fetchone()
    conn.close()
    return row

def next_invoice_no(conn) -> int:
    r = conn.execute("SELECT MAX(COALESCE(invoice_no,0)) AS m FROM orders").fetchone()
    return int(r["m"] or 0) + 1

# ======================
# RTL / PDF
# ======================
def has_hebrew(s: str) -> bool:
    return any("\u0590" <= ch <= "\u05FF" for ch in (s or ""))

def rtl(text: str) -> str:
    if text is None:
        return ""
    s = str(text)
    if has_hebrew(s):
        try:
            return get_display(s, base_dir="R")
        except Exception:
            return get_display(s)
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
    r = requests.post(url, headers=headers, json=payload, timeout=25)
    log(f"WA SEND {r.status_code} {r.text[:500]}")
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
            "header": {"type": "text", "text": title[:60]},
            "body": {"text": body[:1024]},
            "action": {"button": button[:20], "sections": sections}
        }
    })

def wa_upload_media(file_path: str, mime_type: str = "application/pdf") -> str:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    with open(file_path, "rb") as f:
        files = {"file": (Path(file_path).name, f, mime_type)}
        data = {"messaging_product": "whatsapp", "type": mime_type}
        r = requests.post(url, headers=headers, files=files, data=data, timeout=90)
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

def admin_broadcast(text: str):
    for a in ADMIN_PHONES:
        wa_send_text(a, text)

# ======================
# PAYPAL HELPERS (LIVE)
# ======================
_pp_token: Dict[str, Any] = {"value": None, "exp": 0}

def paypal_access_token() -> str:
    now = int(datetime.datetime.now().timestamp())
    if _pp_token["value"] and now < int(_pp_token["exp"]) - 60:
        return _pp_token["value"]

    r = requests.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json", "Accept-Language": "en_US"},
        timeout=25,
    )
    log(f"PAYPAL TOKEN {r.status_code} {r.text[:400]}")
    r.raise_for_status()
    j = r.json()
    _pp_token["value"] = j["access_token"]
    _pp_token["exp"] = now + int(j.get("expires_in", 300))
    return _pp_token["value"]

def paypal_create_order(order_id: int, total_amount: float) -> Tuple[str, str]:
    token = paypal_access_token()
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": "PU1",
            "custom_id": str(order_id),
            "invoice_id": f"EXP-{order_id}",
            "amount": {"currency_code": CURRENCY, "value": f"{total_amount:.2f}"},
        }],
        "application_context": {
            "brand_name": BUSINESS_NAME,
            "landing_page": "BILLING",
            "user_action": "PAY_NOW",
            "return_url": f"{PUBLIC_BASE_URL}/paypal/return?oid={order_id}",
            "cancel_url": f"{PUBLIC_BASE_URL}/paypal/cancel?oid={order_id}",
        },
    }

    r = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=25,
    )
    log(f"PAYPAL CREATE {r.status_code} {r.text[:600]}")
    r.raise_for_status()
    j = r.json()
    pp_order_id = j["id"]

    approve_url = ""
    for l in j.get("links", []) or []:
        if l.get("rel") in ("approve", "payer-action"):
            approve_url = l.get("href", "")
            break
    if not approve_url:
        raise RuntimeError("PayPal approve URL not found")
    return pp_order_id, approve_url

def paypal_get_order(pp_order_id: str) -> Dict[str, Any]:
    token = paypal_access_token()
    r = requests.get(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{pp_order_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=25,
    )
    log(f"PAYPAL GET {r.status_code} {r.text[:300]}")
    r.raise_for_status()
    return r.json()

def paypal_capture_order(pp_order_id: str) -> Dict[str, Any]:
    token = paypal_access_token()
    r = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{pp_order_id}/capture",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={},
        timeout=25,
    )
    log(f"PAYPAL CAPTURE {r.status_code} {r.text[:600]}")
    r.raise_for_status()
    return r.json()

def extract_capture_id(capture_json: dict) -> Optional[str]:
    try:
        for pu in capture_json.get("purchase_units", []) or []:
            pay = pu.get("payments", {}) or {}
            caps = pay.get("captures", []) or []
            if caps:
                return caps[0].get("id")
    except Exception:
        pass
    return None

# ======================
# ORDER LOGIC
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

    pp_order_id, approve_url = paypal_create_order(order_id, total)
    conn.execute(
        "UPDATE orders SET paypal_order_id=?, pay_link=?, paypal_status=? WHERE id=?",
        (pp_order_id, approve_url, "CREATED", order_id),
    )
    conn.commit()
    conn.close()

    items_list = [{"label": i1_label, "amount": float(i1_amount)}]
    if i2_key:
        items_list.append({"label": i2_label, "amount": float(i2_amount)})

    return {
        "order_id": order_id,
        "paypal_order_id": pp_order_id,
        "approve_url": approve_url,
        "total": total,
        "items": items_list,
    }

def create_order_custom_amount(wa_id: str, name: str, phone: str, amount: float, label: str = "💳 מקדמה / סכום חופשי") -> Dict[str, Any]:
    amount = float(amount)
    if amount <= 0:
        raise ValueError("bad_amount")

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders (
            wa_id, customer_name, customer_phone,
            item1_key, item1_label, item1_amount,
            total_amount, note, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(wa_id), name, phone,
        "custom", label, float(amount),
        float(amount), NOTE_DEFAULT, "pending", now_iso()
    ))
    conn.commit()
    order_id = int(cur.lastrowid)

    pp_order_id, approve_url = paypal_create_order(order_id, float(amount))
    conn.execute(
        "UPDATE orders SET paypal_order_id=?, pay_link=?, paypal_status=? WHERE id=?",
        (pp_order_id, approve_url, "CREATED", order_id),
    )
    conn.commit()
    conn.close()

    return {
        "order_id": order_id,
        "paypal_order_id": pp_order_id,
        "approve_url": approve_url,
        "total": float(amount),
        "items": [{"label": label, "amount": float(amount)}],
    }

def create_manual_invoice_and_pdf_detailed(customer_name: str, customer_phone: str, reason: str, amount: float) -> Tuple[int, str]:
    amount = float(amount)
    if amount <= 0:
        raise ValueError("bad_amount")

    label = f"🧾 {reason}".strip()
    if not label:
        label = "🧾 תשלום"

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO orders (
            wa_id, customer_name, customer_phone,
            item1_key, item1_label, item1_amount,
            total_amount, note, status, created_at, paid_at,
            paypal_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        customer_phone, customer_name, customer_phone,
        "manual", label, float(amount),
        float(amount), NOTE_DEFAULT, "paid", now_iso(), now_iso(),
        "MANUAL"
    ))
    conn.commit()
    order_id = int(cur.lastrowid)

    inv_no = next_invoice_no(conn)
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    pdf_path = build_invoice(dict(row), inv_no)

    conn.execute("UPDATE orders SET invoice_no=?, invoice_pdf_path=? WHERE id=?", (inv_no, pdf_path, order_id))
    conn.commit()
    conn.close()
    return order_id, pdf_path

def finalize_paid_and_send_invoice(order_id: int, capture_id: Optional[str] = None, paypal_status: Optional[str] = None) -> str:
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.close()
        raise FileNotFoundError("order_not_found")

    if row["invoice_pdf_path"] and os.path.isfile(row["invoice_pdf_path"]):
        conn.execute(
            "UPDATE orders SET status='paid', paid_at=?, paypal_capture_id=COALESCE(paypal_capture_id, ?), paypal_status=COALESCE(paypal_status, ?) WHERE id=?",
            (now_iso(), capture_id, paypal_status, order_id)
        )
        conn.commit()
        conn.close()
        return row["invoice_pdf_path"]

    inv_no = next_invoice_no(conn)
    pdf_path = build_invoice(dict(row), inv_no)

    conn.execute("""
        UPDATE orders
        SET status='paid', paid_at=?, invoice_no=?, invoice_pdf_path=?,
            paypal_capture_id=COALESCE(paypal_capture_id, ?),
            paypal_status=COALESCE(paypal_status, ?)
        WHERE id=?
    """, (now_iso(), inv_no, pdf_path, capture_id, paypal_status, order_id))
    conn.commit()
    conn.close()
    return pdf_path

def find_last_pending_order(wa_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute(
        "SELECT * FROM orders WHERE wa_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
        (str(wa_id),)
    ).fetchone()
    conn.close()
    return row

# ======================
# Sessions
# ======================
sessions: Dict[str, Dict[str, Any]] = {}

def end_with_menu(wa_id: str):
    show_main_menu(wa_id)
    return jsonify(ok=True), 200

# ======================
# MENUS
# ======================
def show_main_menu(wa_id: str):
    # Titles קצרים! (מגבלת 24 תווים בערך)
    rows = [
        {"id": "menu:pay",      "title": "💳 הזמנה ותשלום", "description": "לינק PayPal"},
        {"id": "menu:pricelist","title": "📋 מחירון",        "description": "מחירים"},
        {"id": "menu:delivery", "title": "🚚 שליחות",        "description": "מחיר שליחות"},
        {"id": "menu:where",    "title": "🧭 איפה אנחנו",    "description": "ניווט Waze"},
        {"id": "menu:reviews",  "title": "⭐ ביקורות",       "description": "גוגל + איזי"},
        {"id": "menu:checkpay", "title": "🔄 בדיקת תשלום",  "description": "אימות PayPal"},
        {"id": "menu:restore",  "title": "🧾 שחזור חשבונית", "description": "לפי טלפון"},
        {"id": "menu:rep",      "title": "👤 נציג",          "description": "פנייה לנציג"},
    ]

    if is_admin_wa(wa_id):
        rows.append({"id": "admin:pay_any",  "title": "💳 אדמין: סכום", "description": "לינק לכל סכום"})
        rows.append({"id": "admin:invoice",  "title": "🧾 חשבונית אדמין", "description": "PDF בלי תשלום"})

    sections = [{"title": "תפריט", "rows": rows}]
    wa_send_list(
        wa_id,
        title=BUSINESS_NAME,
        body="בחר פעולה 👇",
        button="פתח",
        sections=sections
    )

def show_items_menu(wa_id: str, step: str, include_none: bool):
    # Titles קצרים כדי לא לקבל 131009
    rows = [
        {"id": f"{step}:screen",   "title": "📱 מסך 399₪"},
        {"id": f"{step}:battery",  "title": "🔋 סוללה 299₪"},
        {"id": f"{step}:charge",   "title": "🔌 שקע 349₪"},
        {"id": f"{step}:delivery", "title": "🚚 שליחות 69₪"},
        {"id": f"{step}:glass",    "title": "🛡️ זכוכית 49₪"},
    ]
    if include_none:
        rows.append({"id": f"{step}:none", "title": "➖ בלי פריט 2"})

    wa_send_list(
        wa_id,
        title="בחירת פריט",
        body="בחר שירות:",
        button="בחר",
        sections=[{"title": "פריטים", "rows": rows}]
    )

# ======================
# SMART INTENTS
# ======================
def detect_intent(text: str) -> str:
    t = (text or "").strip().lower()

    if any(w in t for w in ["שליחות", "שליח", "משלוח", "עד הבית", "הגעה", "מגיעים אלי"]):
        return "delivery"
    if any(w in t for w in ["איפה", "כתובת", "מיקום", "ניווט", "וויז", "waze", "איך מגיעים"]):
        return "where"
    if any(w in t for w in ["נשבר", "נישבר", "שבור", "נשברה", "מסך נשבר", "נפל ונשבר"]):
        return "broken"
    if any(w in t for w in ["מחירון", "מחירים", "כמה עולה", "עלות"]):
        return "pricelist"
    if any(w in t for w in ["ביקורות", "חוות דעת", "המלצות"]):
        return "reviews"
    if any(w in t for w in ["נציג", "אדם", "טלפון", "דבר איתי", "לחזור אלי"]):
        return "rep"
    if any(w in t for w in ["תשלום", "לשלם", "לינק", "paypal", "פייפאל", "מקדמה"]):
        return "pay"
    return "unknown"

# ======================
# PAYPAL RETURN/CANCEL
# ======================
@app.get("/paypal/return")
def paypal_return():
    oid = (request.args.get("oid") or "").strip()
    if not oid.isdigit():
        return "Missing oid", 400
    order_id = int(oid)

    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    if not row or not row["paypal_order_id"]:
        return "Order not found", 404

    pp_order_id = row["paypal_order_id"]
    wa_id = row["wa_id"]

    try:
        cap = paypal_capture_order(pp_order_id)
        status = cap.get("status")
        capture_id = extract_capture_id(cap)

        conn = db()
        conn.execute(
            "UPDATE orders SET paypal_status=?, paypal_capture_id=? WHERE id=?",
            (status, capture_id, order_id)
        )
        conn.commit()
        conn.close()

        if status == "COMPLETED":
            pdf_path = finalize_paid_and_send_invoice(order_id, capture_id=capture_id, paypal_status=status)
            wa_send_text(wa_id, "✅ התשלום נקלט! שולח חשבונית…")
            wa_send_document(wa_id, pdf_path, caption="🧾 חשבונית ✅")
            return "<h2>תודה! התשלום נקלט ✅</h2><p>אפשר לחזור ל-WhatsApp — החשבונית נשלחה.</p>"

        return "<h2>התשלום עדיין לא הושלם</h2><p>חזור ל-WhatsApp ולחץ 'בדיקת תשלום'.</p>", 200

    except Exception as e:
        log(f"paypal_return ERROR order_id={order_id}: {e}")
        return "<h2>שגיאה בעיבוד התשלום</h2><p>חזור ל-WhatsApp ולחץ 'בדיקת תשלום'.</p>", 500

@app.get("/paypal/cancel")
def paypal_cancel():
    return "<h2>התשלום בוטל</h2><p>אפשר לחזור ל-WhatsApp ולהתחיל מחדש.</p>", 200

# ======================
# WEBHOOK VERIFY (GET)
# ======================
@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log("WEBHOOK VERIFIED ✅")
        return challenge, 200
    log(f"WEBHOOK VERIFY FAILED ❌ mode={mode} token={token}")
    return "Forbidden", 403

# ======================
# WEBHOOK RECEIVE (POST)
# ======================
@app.post("/webhook")
def webhook_receive():
    data = request.get_json(silent=True) or {}
    log(f"INCOMING {str(data)[:1200]}")

    try:
        value = data["entry"][0]["changes"][0]["value"]
        messages = value.get("messages", [])
        if not messages:
            return jsonify(ok=True), 200

        msg = messages[0]
        wa_id = msg.get("from")
        msg_type = msg.get("type")

        # update "last seen"
        upsert_customer(wa_id, None, None)

        if msg_type == "interactive":
            inter = msg.get("interactive", {})
            if inter.get("type") == "list_reply":
                action_id = inter["list_reply"]["id"]
                return handle_action(wa_id, action_id)

        text = (msg.get("text") or {}).get("body", "").strip()
        return handle_text(wa_id, text)

    except Exception as e:
        log(f"PARSE ERROR {e}")
        return jsonify(ok=True), 200

# ======================
# ACTIONS
# ======================
def handle_action(wa_id: str, action_id: str):
    # MAIN MENU
    if action_id == "menu:pay":
        # אם לקוח חוזר – נשתמש בפרטים ונחסוך שאלות
        cust = get_customer(wa_id)
        if cust and (cust["name"] or "").strip() and (cust["phone"] or "").strip():
            sessions[wa_id] = {"step": "item1", "name": cust["name"], "phone": cust["phone"]}
            wa_send_text(wa_id, f"👋 {cust['name']} ממשיכים להזמנה.\nבחר פריט:")
            show_items_menu(wa_id, "item1", include_none=False)
            return jsonify(ok=True), 200

        sessions[wa_id] = {"step": "name"}
        wa_send_text(wa_id, "שם לקוח?")
        return jsonify(ok=True), 200

    if action_id == "menu:pricelist":
        pricelist = (
            f"📋 מחירון {BUSINESS_NAME}\n\n"
            f"📱 מסך — {money(ITEMS['screen'][1])}\n"
            f"🔋 סוללה — {money(ITEMS['battery'][1])}\n"
            f"🔌 שקע טעינה — {money(ITEMS['charge'][1])}\n"
            f"🚚 שליחות — {money(ITEMS['delivery'][1])}\n"
            f"🛡️ מגן זכוכית — {money(ITEMS['glass'][1])}\n\n"
            f"ℹ️ {NOTE_DEFAULT}"
        )
        wa_send_text(wa_id, pricelist)
        return end_with_menu(wa_id)

    if action_id == "menu:delivery":
        wa_send_text(wa_id, f"🚚 יש שליחות עד הבית.\nעלות: {money(ITEMS['delivery'][1])}\n\nרוצה לשלם/להזמין? פתח תפריט 👇")
        return end_with_menu(wa_id)

    if action_id == "menu:where":
        wa_send_text(wa_id, f"🧭 ניווט ב-Waze:\n{WAZE_URL}")
        return end_with_menu(wa_id)

    if action_id == "menu:reviews":
        wa_send_text(wa_id, f"⭐ ביקורות:\nגוגל:\n{GOOGLE_REVIEW_URL}\n\nאיזי:\n{EASY_REVIEW_URL}")
        return end_with_menu(wa_id)

    if action_id == "menu:rep":
        wa_send_text(wa_id, "👤 מעולה. כתוב: שם + עיר + מה התקלה, ונחזור אליך מהר.")
        admin_broadcast(f"📞 בקשת נציג\nמאת: {wa_id}\nזמן: {now_iso()}")
        return end_with_menu(wa_id)

    if action_id == "menu:restore":
        sessions[wa_id] = {"step": "restore_phone"}
        wa_send_text(wa_id, "הזן מספר טלפון לשחזור חשבוניות:")
        return jsonify(ok=True), 200

    if action_id == "menu:checkpay":
        row = find_last_pending_order(wa_id)
        if not row:
            wa_send_text(wa_id, "לא מצאתי הזמנה ממתינה. פתח תפריט → הזמנה ותשלום.")
            return end_with_menu(wa_id)

        try:
            j = paypal_get_order(row["paypal_order_id"])
            st = j.get("status", "")
            log(f"CHECKPAY order_id={row['id']} paypal_status={st}")

            conn = db()
            conn.execute("UPDATE orders SET paypal_status=? WHERE id=?", (st, row["id"]))
            conn.commit()
            conn.close()

            if st == "COMPLETED":
                pdf_path = finalize_paid_and_send_invoice(int(row["id"]), paypal_status=st)
                wa_send_text(wa_id, "✅ התשלום אומת מול PayPal! שולח חשבונית…")
                wa_send_document(wa_id, pdf_path, caption="🧾 חשבונית ✅")
            else:
                wa_send_text(wa_id, f"סטטוס תשלום כרגע: {st}\n\nאם עוד לא שילמת, הנה הלינק:\n{row['pay_link']}")
        except Exception as e:
            log(f"CHECKPAY ERROR: {e}")
            wa_send_text(wa_id, "❌ לא הצלחתי לבדוק מול PayPal כרגע. נסה שוב בעוד רגע.")

        return end_with_menu(wa_id)

    # ADMIN: Pay any
    if action_id == "admin:pay_any":
        if not is_admin_wa(wa_id):
            wa_send_text(wa_id, "אין הרשאה.")
            return end_with_menu(wa_id)
        sessions[wa_id] = {"step": "admin_pay_name"}
        wa_send_text(wa_id, "💳 אדמין: שם לקוח?")
        return jsonify(ok=True), 200

    # ADMIN: Invoice detailed
    if action_id == "admin:invoice":
        if not is_admin_wa(wa_id):
            wa_send_text(wa_id, "אין הרשאה.")
            return end_with_menu(wa_id)
        sessions[wa_id] = {"step": "admin_inv_name"}
        wa_send_text(wa_id, "🧾 שם הלקוח לחשבונית?")
        return jsonify(ok=True), 200

    # Broken flow actions
    if action_id == "broken:form":
        st = sessions.get(wa_id) or {}
        device = st.get("device", "")
        sessions[wa_id] = {"step": "broken_issue", "device": device}
        wa_send_text(wa_id, f"✅ פנייה נפתחה.\nמכשיר: {device}\nכתוב בקצרה מה התקלה/מה נשבר:")
        admin_broadcast(f"📩 פנייה חדשה (נפתחה)\nמאת: {wa_id}\nמכשיר: {device}\nזמן: {now_iso()}")
        return jsonify(ok=True), 200

    if action_id == "broken:pay":
        sessions.pop(wa_id, None)
        wa_send_text(wa_id, "מעולה. פתח תפריט → 💳 הזמנה ותשלום.\nאם אין מחיר מדויק, אפשר גם מקדמה דרך אדמין.")
        return end_with_menu(wa_id)

    if action_id == "broken:rep":
        sessions.pop(wa_id, None)
        wa_send_text(wa_id, "✅ הבנתי. כתוב: שם + עיר + מה נשבר, ונציג יחזור אליך.")
        admin_broadcast(f"📞 בקשת נציג (נשבר לי)\nמאת: {wa_id}\nזמן: {now_iso()}")
        return end_with_menu(wa_id)

    # Items selection
    if action_id.startswith("item1:"):
        key = action_id.split(":", 1)[1]
        if key not in ITEMS:
            wa_send_text(wa_id, "בחירה לא תקינה. כתוב 'תפריט' ונסה שוב.")
            return end_with_menu(wa_id)
        st = sessions.setdefault(wa_id, {})
        st["item1"] = key
        st["step"] = "item2"
        show_items_menu(wa_id, "item2", include_none=True)
        return jsonify(ok=True), 200

    if action_id.startswith("item2:"):
        key2 = action_id.split(":", 1)[1]
        if key2 != "none" and key2 not in ITEMS:
            wa_send_text(wa_id, "בחר פריט 2 מהכפתורים 👇")
            show_items_menu(wa_id, "item2", include_none=True)
            return jsonify(ok=True), 200

        st = sessions.get(wa_id) or {}
        name = (st.get("name") or "").strip()
        phone = (st.get("phone") or "").strip()
        item1 = (st.get("item1") or "").strip()

        if not name or not phone or not item1:
            sessions.pop(wa_id, None)
            wa_send_text(wa_id, "משהו התבלבל. כתוב 'תפריט' להתחלה מחדש.")
            return end_with_menu(wa_id)

        try:
            data2 = create_order_local(wa_id, name, phone, item1, key2)
            order_id = data2["order_id"]
            approve_url = data2["approve_url"]
            total = data2["total"]
            items_txt = "\n".join([f"• {it['label']} — {money(it['amount'])}" for it in data2["items"]])

            upsert_customer(wa_id, name, phone)

            wa_send_text(
                wa_id,
                f"✅ הזמנה #{order_id}\n"
                f"👤 {name} | {phone}\n\n"
                f"{items_txt}\n"
                f"💳 סה״כ: {money(total)}\n"
                f"ℹ️ {NOTE_DEFAULT}\n\n"
                f"לתשלום מאובטח (PayPal):\n{approve_url}\n\n"
                f"🔄 אחרי התשלום: תפריט → 'בדיקת תשלום'\n"
                f"או סיים בדפדפן – החשבונית תישלח אוטומטית."
            )

        except Exception as e:
            log(f"CREATE ORDER ERROR: {e}")
            wa_send_text(wa_id, "❌ לא הצלחתי ליצור לינק תשלום כרגע. נסה שוב עוד רגע.")
            admin_broadcast(f"❌ PayPal create failed\nWA:{wa_id}\nERR:{repr(e)}")

        sessions.pop(wa_id, None)
        return end_with_menu(wa_id)

    # unknown action -> menu
    wa_send_text(wa_id, "בחר מהתפריט 👇")
    return end_with_menu(wa_id)

# ======================
# TEXT FLOW
# ======================
def handle_text(wa_id: str, text: str):
    text_l = (text or "").strip().lower()
    st = sessions.get(wa_id)

    # תפריט / התחלה
    if text_l in ("start", "/start", "תפריט", "menu", "התחל"):
        sessions.pop(wa_id, None)
        return end_with_menu(wa_id)

    # אם אין סשן — ננסה לענות חכם
    if not st:
        intent = detect_intent(text)

        if intent == "delivery":
            wa_send_text(wa_id, f"🚚 כן, יש שליחות עד הבית.\nעלות: {money(ITEMS['delivery'][1])}")
            return end_with_menu(wa_id)

        if intent == "where":
            wa_send_text(wa_id, f"🧭 ניווט ב-Waze:\n{WAZE_URL}")
            return end_with_menu(wa_id)

        if intent == "pricelist":
            # שולח מחירון קצר + תפריט
            wa_send_text(
                wa_id,
                f"📋 מחירון קצר:\n"
                f"📱 מסך {money(ITEMS['screen'][1])}\n"
                f"🔋 סוללה {money(ITEMS['battery'][1])}\n"
                f"🔌 שקע {money(ITEMS['charge'][1])}\n"
                f"🚚 שליחות {money(ITEMS['delivery'][1])}\n"
                f"🛡️ זכוכית {money(ITEMS['glass'][1])}"
            )
            return end_with_menu(wa_id)

        if intent == "reviews":
            wa_send_text(wa_id, f"⭐ ביקורות:\n{GOOGLE_REVIEW_URL}\n\n{EASY_REVIEW_URL}")
            return end_with_menu(wa_id)

        if intent == "rep":
            wa_send_text(wa_id, "👤 כתוב: שם + עיר + מה התקלה, ונחזור מהר.")
            admin_broadcast(f"📞 בקשת נציג\nמאת: {wa_id}\nהודעה: {text[:200]}")
            return end_with_menu(wa_id)

        if intent == "pay":
            # אם לקוח חוזר — נוביל ישר לתשלום
            cust = get_customer(wa_id)
            if cust and (cust["name"] or "").strip() and (cust["phone"] or "").strip():
                sessions[wa_id] = {"step": "item1", "name": cust["name"], "phone": cust["phone"]}
                wa_send_text(wa_id, f"👋 {cust['name']} בחר פריט לתשלום:")
                show_items_menu(wa_id, "item1", include_none=False)
                return jsonify(ok=True), 200
            # אחרת תפריט
            wa_send_text(wa_id, "כדי ליצור תשלום, פתח תפריט → 💳 הזמנה ותשלום.")
            return end_with_menu(wa_id)

        if intent == "broken":
            sessions[wa_id] = {"step": "broken_device"}
            wa_send_text(wa_id, "הבנתי שנשבר לך משהו 😅 איזה מכשיר זה? (דגם מלא)")
            return jsonify(ok=True), 200

        # לקוח חדש? נציג תפריט ישר במקום "לא הבנתי"
        cust = get_customer(wa_id)
        if not cust or not (cust["name"] or "").strip():
            wa_send_text(wa_id, f"ברוך הבא ל-{BUSINESS_NAME} 👋\nאיך אפשר לעזור?")
            return end_with_menu(wa_id)

    # FLOW: הזמנה רגילה
    if st and st.get("step") == "name":
        st["name"] = text.strip()
        st["step"] = "phone"
        wa_send_text(wa_id, "מספר טלפון?")
        return jsonify(ok=True), 200

    if st and st.get("step") == "phone":
        st["phone"] = text.strip()
        upsert_customer(wa_id, st.get("name"), st.get("phone"))
        st["step"] = "item1"
        show_items_menu(wa_id, "item1", include_none=False)
        return jsonify(ok=True), 200

    # FLOW: נשבר לי -> דגם -> בחירה
    if st and st.get("step") == "broken_device":
        st["device"] = text.strip()
        wa_send_list(
            wa_id,
            title="מה תרצה?",
            body=f"מכשיר: {st['device']}\nבחר אפשרות:",
            button="בחר",
            sections=[{
                "title": "אפשרויות",
                "rows": [
                    {"id": "broken:form", "title": "📝 לפתוח פנייה"},
                    {"id": "broken:pay",  "title": "💳 לשלם תיקון"},
                    {"id": "broken:rep",  "title": "👤 נציג"},
                ]
            }]
        )
        return jsonify(ok=True), 200

    # FLOW: פנייה נפתחה -> לתאר תקלה -> שמירה + התראה
    if st and st.get("step") == "broken_issue":
        device = st.get("device", "")
        issue = text.strip()

        cust = get_customer(wa_id)
        name = (cust["name"] if cust else "") or ""
        phone = (cust["phone"] if cust else "") or ""

        conn = db()
        conn.execute("""
            INSERT INTO tickets (wa_id, customer_name, customer_phone, device, issue, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (str(wa_id), name, phone, device, issue, now_iso(), "open"))
        conn.commit()
        tid = conn.execute("SELECT last_insert_rowid() AS x").fetchone()["x"]
        conn.close()

        wa_send_text(wa_id, f"✅ תודה! הפנייה נקלטה (# {tid}). נחזור אליך בהקדם.\nאם תרצה – אפשר גם מקדמה/תשלום דרך התפריט.")
        admin_broadcast(f"📩 פנייה חדשה #{tid}\nWA:{wa_id}\nשם:{name}\nטלפון:{phone}\nמכשיר:{device}\nתקלה:{issue[:250]}")
        sessions.pop(wa_id, None)
        return end_with_menu(wa_id)

    # ADMIN: pay any amount
    if st and st.get("step") == "admin_pay_name":
        st["name"] = text.strip()
        st["step"] = "admin_pay_phone"
        wa_send_text(wa_id, "טלפון הלקוח?")
        return jsonify(ok=True), 200

    if st and st.get("step") == "admin_pay_phone":
        st["phone"] = text.strip()
        st["step"] = "admin_pay_amount"
        wa_send_text(wa_id, "סכום לחיוב? (למשל 250)")
        return jsonify(ok=True), 200

    if st and st.get("step") == "admin_pay_amount":
        try:
            amount = float((text or "").replace(",", "").strip())
            data2 = create_order_custom_amount(wa_id, st.get("name",""), st.get("phone",""), amount, label="💳 מקדמה / סכום חופשי")
            wa_send_text(
                wa_id,
                f"✅ נוצר חיוב #{data2['order_id']}\n"
                f"👤 {st.get('name','')} | {st.get('phone','')}\n"
                f"💳 סכום: {money(amount)}\n\n"
                f"לינק תשלום:\n{data2['approve_url']}\n\n"
                f"אחרי תשלום: תפריט → בדיקת תשלום"
            )
        except Exception as e:
            log(f"ADMIN PAY ANY ERROR: {e}")
            wa_send_text(wa_id, "❌ סכום לא תקין או בעיה ביצירת תשלום. נסה שוב (לדוגמה 250).")

        sessions.pop(wa_id, None)
        return end_with_menu(wa_id)

    # ADMIN: invoice detailed
    if st and st.get("step") == "admin_inv_name":
        st["cust_name"] = text.strip()
        st["step"] = "admin_inv_phone"
        wa_send_text(wa_id, "טלפון הלקוח לחשבונית?")
        return jsonify(ok=True), 200

    if st and st.get("step") == "admin_inv_phone":
        st["cust_phone"] = text.strip()
        st["step"] = "admin_inv_reason"
        wa_send_text(wa_id, "עבור מה התשלום? (למשל: החלפת מסך iPhone 13)")
        return jsonify(ok=True), 200

    if st and st.get("step") == "admin_inv_reason":
        st["reason"] = text.strip()
        st["step"] = "admin_inv_amount"
        wa_send_text(wa_id, "סכום לחשבונית? (למשל 350)")
        return jsonify(ok=True), 200

    if st and st.get("step") == "admin_inv_amount":
        try:
            amount = float((text or "").replace(",", "").strip())
            order_id, pdf_path = create_manual_invoice_and_pdf_detailed(
                customer_name=st.get("cust_name",""),
                customer_phone=st.get("cust_phone",""),
                reason=st.get("reason",""),
                amount=amount
            )
            wa_send_text(wa_id, f"✅ הופקה חשבונית #{order_id}. שולח PDF…")
            wa_send_document(wa_id, pdf_path, caption="🧾 חשבונית אדמין ✅")
        except Exception as e:
            log(f"ADMIN INVOICE ERROR: {e}")
            wa_send_text(wa_id, "❌ סכום לא תקין או בעיה בהפקה. נסה שוב (לדוגמה 350).")

        sessions.pop(wa_id, None)
        return end_with_menu(wa_id)

    # RESTORE invoices
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
        return end_with_menu(wa_id)

    # fallback תמיד עם תפריט
    wa_send_text(wa_id, "לא הבנתי 🙂 כתוב מה אתה צריך (שליחות/איפה/נשבר לי/תשלום) או פתח תפריט.")
    return end_with_menu(wa_id)

# ======================
# Health
# ======================
@app.get("/")
def home():
    return "OK - WhatsApp Expresphone bot running", 200

# init
init_db_and_migrate()

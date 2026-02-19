# Ezba Telegram Bot – Improved AI Understanding Version
# Same structure, improved intelligence layer

from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta, date
import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

TELEGRAM_BOT_TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY              = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID              = os.environ.get("SPREADSHEET_ID")

ALLOWED_USERS = {
    47329648:   "Khaled",
    6894180427: "Hamad",
}

UAE_TZ = timezone(timedelta(hours=4))
openai_client = OpenAI(api_key=OPENAI_API_KEY)

S_TRANSACTIONS = "Transactions"
S_INVENTORY    = "Inventory"
S_PENDING      = "Pending"

D = "──────────────"


# ─────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────

def send(chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )

def sheets_svc():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)

def read_sheet(svc, sheet, rng="A2:Z"):
    res = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{rng}",
    ).execute()
    return res.get("values", [])

def append_row(svc, sheet, row: list):
    svc.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()

def now_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")

def today_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d")

def cur_month():
    return datetime.now(UAE_TZ).strftime("%Y-%m")

def fmt(x):
    try:
        f = float(x)
        return int(f) if f.is_integer() else round(f, 2)
    except Exception:
        return x


# ─────────────────────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────────────────────

def load_transactions(svc):
    rows = read_sheet(svc, S_TRANSACTIONS)
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        try:
            out.append({
                "date":     r[0],
                "type":     r[1],
                "item":     r[2],
                "category": r[3],
                "amount":   float(r[4]),
                "user":     r[5] if len(r) > 5 else "",
            })
        except:
            continue
    return out

def add_transaction(svc, kind, item, category, amount, user):
    append_row(svc, S_TRANSACTIONS, [now_str(), kind, item, category, amount, user])

def totals_all(data):
    inc = sum(x["amount"] for x in data if x["type"] == "دخل")
    exp = sum(x["amount"] for x in data if x["type"] == "صرف")
    return inc, exp


# ─────────────────────────────────────────────────────────
# AI INTENT DETECTION (Improved)
# ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
أنت مدير مالي ذكي لعزبة في الإمارات.

افهم الجملة حتى لو كانت:
- عامية
- ناقصة
- فيها أخطاء إملائية
- فيها مقارنة غير مباشرة

فكر في المعنى وليس الكلمات فقط.

أرجع JSON فقط بدون أي كلام إضافي:

{
  "intent": "",
  "direction": "in | out | none",
  "item": "",
  "category": "",
  "amount": 0,
  "quantity": 0,
  "animal_type": "",
  "worker_name": "",
  "period": "today | week | month | all"
}

intent الممكن:
add_income
add_expense
add_livestock
sell_livestock
pay_salary
income_total
expense_total
profit
inventory
last_transactions
category_total
daily_report
clarify

قواعد فهم:
- أي جملة فيها مبلغ + فعل بيع → دخل
- أي جملة فيها مبلغ + فعل دفع/شراء → صرف
- "كم" → استعلام
- "قارن" → profit
- "آخر" → last_transactions
- إذا غير واضح → clarify

لا تخترع أرقام.
لا تستخدم Markdown.
لا تضف نص خارج JSON.
"""

def detect_intent(text: str) -> dict:
    try:
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": text},
            ],
        )
        return json.loads(completion.choices[0].message.content)
    except:
        return {"intent": "clarify"}


# ─────────────────────────────────────────────────────────
# MAIN HANDLER
# ─────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self._ok()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length).decode()
            update = json.loads(body)
        except:
            self._ok()
            return

        msg = update.get("message")
        if not msg or "text" not in msg:
            self._ok()
            return

        chat_id  = msg["chat"]["id"]
        user_id  = msg["from"]["id"]
        text     = msg["text"].strip()

        if user_id not in ALLOWED_USERS:
            send(chat_id, "⛔ غير مصرح.")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]

        try:
            svc  = sheets_svc()
            data = load_transactions(svc)
        except Exception as e:
            send(chat_id, f"❌ خطأ في Google Sheets:\n{e}")
            self._ok()
            return

        d      = detect_intent(text)
        intent = d.get("intent", "clarify")

        # تسجيل دخل
        if intent == "add_income":
            item = d.get("item")
            amount = d.get("amount")
            category = d.get("category") or item
            if item and amount:
                add_transaction(svc, "دخل", item, category, amount, user_name)
                inc, exp = totals_all(load_transactions(svc))
                send(chat_id,
                     f"{D}\n"
                     f"دخل مسجل: {item}\n"
                     f"المبلغ: {fmt(amount)}\n"
                     f"{D}\n"
                     f"إجمالي الدخل: {fmt(inc)}")
            else:
                send(chat_id, "حدد البند والمبلغ.")
        
        # تسجيل صرف
        elif intent == "add_expense":
            item = d.get("item")
            amount = d.get("amount")
            category = d.get("category") or item
            if item and amount:
                add_transaction(svc, "صرف", item, category, amount, user_name)
                inc, exp = totals_all(load_transactions(svc))
                warn = "\n⚠️ المصروفات أعلى من الدخل." if exp > inc else ""
                send(chat_id,
                     f"{D}\n"
                     f"صرف مسجل: {item}\n"
                     f"المبلغ: {fmt(amount)}\n"
                     f"{D}\n"
                     f"إجمالي المصروفات: {fmt(exp)}{warn}")
            else:
                send(chat_id, "حدد البند والمبلغ.")

        # الربح
        elif intent == "profit":
            inc, exp = totals_all(data)
            net = inc - exp
            send(chat_id,
                 f"{D}\n"
                 f"الدخل: {fmt(inc)}\n"
                 f"المصروف: {fmt(exp)}\n"
                 f"الصافي: {fmt(net)}\n"
                 f"{D}")

        # آخر العمليات
        elif intent == "last_transactions":
            recent = sorted(data, key=lambda x: x["date"], reverse=True)[:5]
            lines = [D, "آخر العمليات"]
            for r in recent:
                lines.append(f"{r['date']} | {r['item']} | {fmt(r['amount'])}")
            lines.append(D)
            send(chat_id, "\n".join(lines))

        else:
            send(chat_id, "وضح أكثر." )

        self._ok()

# api/telegram-webhook.py
from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta, date

import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

ALLOWED_USERS = {
    47329648: "Khaled",
    6894180427: "Hamad",
}

UAE_TZ = timezone(timedelta(hours=4))
openai_client = OpenAI(api_key=OPENAI_API_KEY)


# ---------- Utilities ----------

def send(chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )


def sheets():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def now_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")


def fmt(x):
    return int(x) if float(x).is_integer() else x


# ---------- Data Layer ----------

def load_transactions(service):
    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A2:E",
    ).execute()

    rows = res.get("values", [])
    data = []

    for r in rows:
        if len(r) < 4:
            continue
        data.append({
            "date": r[0],
            "type": r[1],
            "item": r[2],
            "amount": float(r[3]),
            "user": r[4] if len(r) > 4 else ""
        })

    return data


def append_transaction(service, kind, item, amount, user):
    values = [[now_str(), kind, item, amount, user]]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A1:E1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def totals(data):
    income = sum(x["amount"] for x in data if x["type"] == "دخل")
    expense = sum(x["amount"] for x in data if x["type"] == "صرف")
    return income, expense


# ---------- AI Intent Only ----------

def detect_intent(text):
    prompt = """
حدد نوع الطلب فقط.

أرجع JSON فقط بالشكل:

{
  "intent": "add | income_total | expense_total | profit | last | clarify",
  "direction": "in | out | none",
  "item": "",
  "amount": number
}

قواعد:
- بيع / دخل = in
- دفع / شراء / راتب / مصروف = out
- كم الدخل = income_total
- كم المصروف = expense_total
- كم الربح / الصافي = profit
- آخر العمليات = last
- إذا الجملة غير واضحة = clarify
"""

    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text}
        ],
    )

    raw = completion.choices[0].message.content

    try:
        return json.loads(raw)
    except:
        return {"intent": "clarify"}


# ---------- Telegram Handler ----------

class handler(BaseHTTPRequestHandler):

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self._ok()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        update = json.loads(body)
        msg = update.get("message")

        if not msg or "text" not in msg:
            self._ok()
            return

        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = msg["text"].strip()

        if user_id not in ALLOWED_USERS:
            send(chat_id, "غير مصرح.")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]
        service = sheets()
        data = load_transactions(service)

        intent_data = detect_intent(text)
        intent = intent_data.get("intent")

        # ----- Add Transaction -----
        if intent == "add":
            direction = intent_data.get("direction")
            item = intent_data.get("item")
            amount = intent_data.get("amount")

            if not item or not amount:
                send(chat_id, "حدد المبلغ أو البند.")
                self._ok()
                return

            kind = "دخل" if direction == "in" else "صرف"
            append_transaction(service, kind, item, amount, user_name)

            income, expense = totals(load_transactions(service))

            block = (
                "────────────\n"
                f"التاريخ: {now_str()}\n"
                f"النوع: {kind}\n"
                f"البند: {item}\n"
                f"المبلغ: {fmt(amount)}\n"
                f"المستخدم: {user_name}\n"
                "────────────"
            )

            if kind == "دخل":
                extra = f"\nإجمالي الدخل: {fmt(income)}"
            else:
                extra = f"\nإجمالي المصروفات: {fmt(expense)}"

            send(chat_id, block + extra)
            self._ok()
            return

        # ----- Income Total -----
        if intent == "income_total":
            income, _ = totals(data)
            send(chat_id, f"إجمالي الدخل: {fmt(income)}")
            self._ok()
            return

        # ----- Expense Total -----
        if intent == "expense_total":
            _, expense = totals(data)
            send(chat_id, f"إجمالي المصروفات: {fmt(expense)}")
            self._ok()
            return

        # ----- Profit -----
        if intent == "profit":
            income, expense = totals(data)
            net = income - expense
            send(chat_id,
                 "────────────\n"
                 f"الدخل: {fmt(income)}\n"
                 f"المصروف: {fmt(expense)}\n"
                 f"الصافي: {fmt(net)}\n"
                 "────────────")
            self._ok()
            return

        # ----- Last 5 Operations -----
        if intent == "last":
            data.sort(key=lambda x: x["date"], reverse=True)
            last5 = data[:5]

            if not last5:
                send(chat_id, "لا توجد عمليات.")
                self._ok()
                return

            blocks = []
            for t in last5:
                block = (
                    "────────────\n"
                    f"التاريخ: {t['date']}\n"
                    f"النوع: {t['type']}\n"
                    f"البند: {t['item']}\n"
                    f"المبلغ: {fmt(t['amount'])}\n"
                    f"المستخدم: {t['user']}\n"
                    "────────────"
                )
                blocks.append(block)

            send(chat_id, "\n".join(blocks))
            self._ok()
            return

        # ----- Clarify -----
        send(chat_id, "وضح أكثر.")
        self._ok()

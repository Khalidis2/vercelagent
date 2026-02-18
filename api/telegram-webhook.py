# api/telegram-webhook.py
from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta

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


def now_ts():
    return datetime.now(UAE_TZ)


def fmt(x):
    return int(x) if float(x).is_integer() else x


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
    ts = now_ts().strftime("%Y-%m-%d %H:%M")
    values = [[ts, kind, item, amount, user]]
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


def ai_parse(text):
    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": """
Return JSON only:

{
  "intent": "add | profit | other",
  "direction": "in | out",
  "item": "",
  "amount": number
}

Rules:
- بيع / دخل = in
- دفع / شراء / راتب / مصروف = out
- كم الربح / الصافي = profit
"""
            },
            {"role": "user", "content": text},
        ],
    )

    raw = completion.choices[0].message.content

    try:
        return json.loads(raw)
    except:
        return {"intent": "other"}


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

        parsed = ai_parse(text)
        intent = parsed.get("intent")

        # ===== إضافة عملية =====
        if intent == "add":
            direction = parsed.get("direction")
            item = parsed.get("item")
            amount = parsed.get("amount")

            if not item or not amount:
                send(chat_id, "العملية غير واضحة.")
                self._ok()
                return

            kind = "دخل" if direction == "in" else "صرف"

            append_transaction(service, kind, item, amount, user_name)

            data = load_transactions(service)
            total_income, total_expense = totals(data)

            base = (
                "────────────\n"
                f"التاريخ: {now_ts().strftime('%Y-%m-%d %H:%M')}\n"
                f"النوع: {kind}\n"
                f"البند: {item}\n"
                f"المبلغ: {fmt(amount)}\n"
                f"المستخدم: {user_name}\n"
                "────────────"
            )

            if kind == "دخل":
                extra = f"\nإجمالي الدخل: {fmt(total_income)}"
            else:
                extra = f"\nإجمالي المصروفات: {fmt(total_expense)}"

            send(chat_id, base + extra)
            self._ok()
            return

        # ===== حساب الربح فقط عند الطلب =====
        if intent == "profit":
            data = load_transactions(service)
            total_income, total_expense = totals(data)
            net = total_income - total_expense

            send(chat_id,
                 "────────────\n"
                 f"الدخل: {fmt(total_income)}\n"
                 f"المصروف: {fmt(total_expense)}\n"
                 f"الصافي: {fmt(net)}\n"
                 "────────────")

            self._ok()
            return

        send(chat_id, "ما فهمت.")
        self._ok()

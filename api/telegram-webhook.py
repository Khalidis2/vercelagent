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


def now_ts():
    return datetime.now(UAE_TZ)


def fmt(x):
    return int(x) if float(x).is_integer() else x


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


def load_tx(service):
    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A2:F",
    ).execute()
    rows = res.get("values", [])
    data = []
    for r in rows:
        if len(r) < 4:
            continue
        try:
            ts = datetime.strptime(r[0], "%Y-%m-%d %H:%M")
            amount = float(r[3])
        except:
            continue
        data.append({
            "timestamp": ts,
            "kind": r[1],
            "item": r[2],
            "amount": amount,
            "user": r[4] if len(r) > 4 else "",
            "note": r[5] if len(r) > 5 else "",
        })
    return data


def append_tx(service, ts, kind, item, amount, user, note=""):
    values = [[ts.strftime("%Y-%m-%d %H:%M"), kind, item, amount, user, note]]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A1:F1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def summarize(txs):
    income = sum(t["amount"] for t in txs if t["kind"] == "دخل")
    expense = sum(t["amount"] for t in txs if t["kind"] == "صرف")
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
  "intent": "add | report | other",
  "direction": "in | out",
  "item": "",
  "amount": number
}

Rules:
- بيع / دخل = in
- شراء / دفع / راتب / مصروف = out
- كم / اجمالي / الربح = report
"""
            },
            {"role": "user", "content": text},
        ],
    )
    return json.loads(completion.choices[0].message.content)


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

        try:
            parsed = ai_parse(text)
        except:
            send(chat_id, "غير مفهوم.")
            self._ok()
            return

        intent = parsed.get("intent")

        # ===== تسجيل عملية =====
        if intent == "add":
            direction = parsed.get("direction")
            item = parsed.get("item")
            amount = parsed.get("amount")

            if not item or not amount:
                send(chat_id, "العملية غير واضحة.")
                self._ok()
                return

            kind = "دخل" if direction == "in" else "صرف"
            ts = now_ts()
            append_tx(service, ts, kind, item, amount, user_name)

            # احسب الاجمالي بعد التسجيل
            txs = load_tx(service)
            total_income, total_expense = summarize(txs)

            if kind == "دخل":
                msg = (
                    f"تم تسجيل دخل:\n"
                    f"{fmt(amount)}\n\n"
                    f"إجمالي الدخل الحالي: {fmt(total_income)}"
                )
            else:
                msg = (
                    f"تم تسجيل مصروف:\n"
                    f"{fmt(amount)}\n\n"
                    f"إجمالي المصروفات الحالية: {fmt(total_expense)}"
                )

            send(chat_id, msg)
            self._ok()
            return

        # ===== تقارير =====
        if intent == "report":
            txs = load_tx(service)
            total_income, total_expense = summarize(txs)
            net = total_income - total_expense

            total_income = fmt(total_income)
            total_expense = fmt(total_expense)
            net = fmt(net)

            send(chat_id,
                 f"الدخل: {total_income}\n"
                 f"المصروف: {total_expense}\n"
                 f"الصافي: {net}")
            self._ok()
            return

        send(chat_id, "ما فهمت.")
        self._ok()

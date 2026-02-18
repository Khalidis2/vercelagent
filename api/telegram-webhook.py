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
            "amount": r[3],
            "user": r[4] if len(r) > 4 else ""
        })

    return data


def append_transaction(service, kind, item, amount, user):
    ts = datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")
    values = [[ts, kind, item, amount, user]]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A1:E1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def ask_ai(user_text, transactions):
    system_prompt = """
أنت محاسب رسمي لعزبة.

افهم رسالة المستخدم.

إذا كانت عملية بيع أو دفع:
- أرجع action = add
- حدد النوع: دخل أو صرف
- حدد البند
- حدد المبلغ

إذا كانت طلب تقرير:
- أرجع action = none
- واكتب الرد النهائي المنظم فقط

لا تستخدم نجوم أو Markdown.
لا تستخدم ترقيم.
لا تضف جمل ختامية.

أرجع JSON فقط:

{
  "action": "add | none",
  "type": "دخل | صرف",
  "item": "",
  "amount": number,
  "reply": "النص النهائي"
}
"""

    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"العمليات الحالية:\n{json.dumps(transactions, ensure_ascii=False)}"},
            {"role": "user", "content": user_text},
        ],
    )

    raw = completion.choices[0].message.content

    try:
        return json.loads(raw)
    except:
        return {
            "action": "none",
            "reply": raw.strip()
        }


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
        transactions = load_transactions(service)

        # ===== عرض آخر العمليات =====
        if "آخر العمليات" in text or "اخر العمليات" in text:
            transactions.sort(key=lambda x: x["date"], reverse=True)
            transactions = transactions[:10]

            if not transactions:
                send(chat_id, "لا توجد عمليات.")
                self._ok()
                return

            blocks = []
            for t in transactions:
                block = (
                    "────────────\n"
                    f"التاريخ: {t['date']}\n"
                    f"النوع: {t['type']}\n"
                    f"البند: {t['item']}\n"
                    f"المبلغ: {t['amount']}\n"
                    f"المستخدم: {t['user']}\n"
                    "────────────"
                )
                blocks.append(block)

            send(chat_id, "\n".join(blocks))
            self._ok()
            return

        # ===== باقي الطلبات =====
        ai_result = ask_ai(text, transactions)

        if ai_result.get("action") == "add":
            kind = ai_result.get("type")
            item = ai_result.get("item")
            amount = ai_result.get("amount")

            if kind and item and amount:
                append_transaction(service, kind, item, amount, user_name)

        send(chat_id, ai_result.get("reply", "ما فهمت المطلوب."))
        self._ok()

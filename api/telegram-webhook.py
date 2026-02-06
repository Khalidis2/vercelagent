from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta

import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ================= CONFIG =================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

ALLOWED_USERS = {
    47329648: "Khaled",
    6894180427: "Hamad",
}

# UAE timezone (UTC+4)
UAE_TZ = timezone(timedelta(hours=4))

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ================= HELPERS =================

def now_timestamp():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")

def send_telegram_message(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)

def get_sheets_service():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)

def get_last_balance(service):
    """Read last balance from column G"""
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!G2:G"
    ).execute()

    values = result.get("values", [])
    if not values:
        return 0

    try:
        return float(values[-1][0])
    except Exception:
        return 0

# ================= AI =================

def call_ai_to_parse(text, person_name):
    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": f"""
أنت مساعد لتسجيل عمليات العزبة.

أجب بصيغة JSON فقط.

الصيغة:

{{
  "action": "buy | sell",
  "item": "وصف مختصر",
  "amount": رقم,
  "notes": "ملاحظات مختصرة"
}}

القواعد:
- شراء / مصروف = buy
- بيع / دخل = sell
- افهم العربية
- لا تخمّن
                """.strip()
            },
            {"role": "user", "content": text},
        ],
    )

    raw = completion.choices[0].message.content
    return json.loads(raw)

# ================= MAIN HANDLER =================

class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        update = json.loads(body)

        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            self._ok()
            return

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message["text"].strip()

        if user_id not in ALLOWED_USERS:
            send_telegram_message(chat_id, "⛔ هذا البوت خاص.")
            self._ok()
            return

        person = ALLOWED_USERS[user_id]

        # Commands
        if text == "/help":
            send_telegram_message(
                chat_id,
                "✍️ أمثلة:\n"
                "• اشتريت علف بـ 500\n"
                "• بعت خروف بـ 1200\n\n"
                "📊 يتم حساب الرصيد تلقائياً"
            )
            self._ok()
            return

        # Parse with AI
        try:
            parsed = call_ai_to_parse(text, person)
        except Exception:
            send_telegram_message(chat_id, "❌ لم أفهم العملية.")
            self._ok()
            return

        action = parsed.get("action")
        amount = float(parsed.get("amount", 0))
        item = parsed.get("item", "")
        notes = parsed.get("notes", "")

        if action not in ("buy", "sell"):
            send_telegram_message(chat_id, "❌ العملية غير واضحة.")
            self._ok()
            return

        # Arabic type + balance delta
        if action == "buy":
            type_ar = "شراء"
            delta = -amount
        else:
            type_ar = "بيع"
            delta = amount

        service = get_sheets_service()
        last_balance = get_last_balance(service)
        new_balance = last_balance + delta

        # Append row
        values = [[
            now_timestamp(),   # A Timestamp
            type_ar,           # B Type (Arabic)
            item,              # C Item
            amount,            # D Amount
            person,            # E Paid By
            notes,             # F Note
            new_balance        # G Balance
        ]]

        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Transactions!A1:G1",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

        # Reply
        sign = "+" if delta > 0 else "-"
        send_telegram_message(
            chat_id,
            f"✅ تم التسجيل\n"
            f"النوع: {type_ar}\n"
            f"المبلغ: {amount} ({sign})\n"
            f"الرصيد الحالي: {new_balance}"
        )

        self._ok()

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

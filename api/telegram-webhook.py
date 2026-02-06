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

openai_client = OpenAI(api_key=OPENAI_API_KEY)


def get_sheets_service():
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not SPREADSHEET_ID:
        raise RuntimeError("Missing Google Sheets env vars")

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    service = build("sheets", "v4", credentials=creds)
    return service


def get_local_timestamp():
    """
    Return timestamp like '2026-02-06 11:25' in UAE time (UTC+4).
    """
    # timezone for UAE (UTC+4)
    uae_tz = timezone(timedelta(hours=4))
    now = datetime.now(uae_tz)
    return now.strftime("%Y-%m-%d %H:%M")


def append_transaction_row(parsed):
    try:
        service = get_sheets_service()

        timestamp = get_local_timestamp()

        values = [
            [
                timestamp,                       # A: Timestamp (clean, no quotes)
                parsed.get("action", ""),        # B: Type
                parsed.get("item", ""),          # C: Item
                parsed.get("amount", ""),        # D: Amount
                parsed.get("person", ""),        # E: Paid By / Person
                parsed.get("notes", ""),         # F: Note
            ]
        ]

        body = {"values": values}
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Transactions!A1",            # starts at col A, only 6 cols used
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()
        return True
    except Exception as e:
        print("Sheets error:", e)
        return False


def send_telegram_message(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        print("Missing TELEGRAM_BOT_TOKEN")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        print("Telegram send error:", e)


def call_ai_to_parse(text, person_name):
    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": """
أنت مساعد لتسجيل عمليات مزرعة (عزبة).

أجب بصيغة JSON فقط بدون أي نص إضافي.

الصيغة المطلوبة:

{
  "action": "expense | income | inventory",
  "item": "وصف مختصر",
  "amount": رقم أو null,
  "person": "اسم الشخص",
  "notes": "ملاحظات مختصرة"
}

تعليمات:
- افهم العربية
- حوّل المبالغ إلى أرقام
- لا تخمّن
- استخدم اسم الشخص التالي في الحقل person متى كان منطقيًا: %s
            """.strip()
                % person_name,
            },
            {"role": "user", "content": text},
        ],
    )

    raw = completion.choices[0].message.content
    try:
        parsed = json.loads(raw)
    except Exception:
        print("AI returned invalid JSON:", raw)
        raise

    parsed.setdefault("person", person_name)
    return parsed


class handler(BaseHTTPRequestHandler):
    def _send_text(self, code, body):
        body_bytes = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_json(self, code, obj):
        body = json.dumps(obj)
        body_bytes = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self):
        # Health check
        self._send_text(200, "OK")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length > 0 else b"{}"

        try:
            update = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self._send_text(200, "no json")
            return

        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            self._send_json(200, {"ok": True})
            return

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message["text"].strip()

        # Security: only allowed users
        if user_id not in ALLOWED_USERS:
            send_telegram_message(chat_id, "⛔ هذا البوت خاص.")
            self._send_json(200, {"ok": True})
            return

        person_name = ALLOWED_USERS[user_id]

        # Commands (no AI)
        if text == "/start":
            send_telegram_message(
                chat_id,
                f"مرحباً {person_name} 👋\nأنا بوت تسجيل عمليات العزبة.\nاكتب /help لمعرفة الاستخدام.",
            )
            self._send_json(200, {"ok": True})
            return

        if text == "/help":
            send_telegram_message(
                chat_id,
                (
                    "📌 طريقة الاستخدام\n\n"
                    "✍️ اكتب العملية بشكل طبيعي، أمثلة:\n"
                    "• اشتريت علف بـ 500\n"
                    "• بعت خروف بـ 1200\n"
                    "• دخل 300 من بيع حليب\n"
                    "• زاد عدد الغنم 5\n"
                    "• نقص عدد الغنم 2\n\n"
                    "🔒 هذا البوت خاص بالعائلة فقط"
                ),
            )
            self._send_json(200, {"ok": True})
            return

        # Normal message → AI + Sheets
        try:
            parsed = call_ai_to_parse(text, person_name)
        except Exception as e:
            print("AI error:", e)
            send_telegram_message(
                chat_id,
                "صار خطأ في فهم الرسالة. حاول تكتبها بجملة أوضح مثل: اشتريت علف بـ 500",
            )
            self._send_json(200, {"ok": False})
            return

        action = parsed.get("action")
        if action not in {"expense", "income", "inventory"}:
            send_telegram_message(
                chat_id,
                "ما فهمت العملية 🤔\nحاول تكتبها مثل:\nاشتريت علف بـ 500",
            )
            self._send_json(200, {"ok": True})
            return

        saved = append_transaction_row(parsed)

        amount = parsed.get("amount")
        amount_text = f"{amount} درهم" if amount is not None else "بدون مبلغ"

        if action == "expense":
            type_text = "مصروف"
        elif action == "income":
            type_text = "دخل"
        else:
            type_text = "تعديل مخزون"

        reply = (
            "تم تسجيل العملية ✅\n"
            f"النوع: {type_text}\n"
            f"البند: {parsed.get('item','')}\n"
            f"المبلغ: {amount_text}\n"
            f"الشخص: {parsed.get('person','')}"
        )

        if not saved:
            reply += "\n\n⚠️ لم يتم الحفظ في Google Sheets (تحقق من الإعدادات)"

        send_telegram_message(chat_id, reply)
        self._send_json(200, {"ok": True})

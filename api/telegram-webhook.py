# api/telegram-webhook.py

from http.server import BaseHTTPRequestHandler
import json
import os
import logging
from datetime import datetime, timezone, timedelta

import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

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

DIVIDER = "────────────"

# How many recent rows to pass to the AI (token safety)
HISTORY_LIMIT = 40

# ─────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────

def send(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception as e:
        log.error(f"Failed to send Telegram message: {e}")


# ─────────────────────────────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────────────────────────────

def get_sheets_service():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def load_transactions(service):
    """Load all rows from sheet. Returns list of dicts. Never raises."""
    try:
        res = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Transactions!A2:E",
        ).execute()
        rows = res.get("values", [])
    except Exception as e:
        log.error(f"Failed to load transactions: {e}")
        return []

    data = []
    for r in rows:
        if len(r) < 4:
            continue
        data.append({
            "date": r[0],
            "type": r[1],
            "item": r[2],
            "amount": r[3],
            "user": r[4] if len(r) > 4 else "",
        })
    return data


def append_transaction(service, kind, item, amount, user):
    """Append one transaction row. Returns True on success."""
    try:
        ts = datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Transactions!A1:E1",
            valueInputOption="USER_ENTERED",
            body={"values": [[ts, kind, item, amount, user]]},
        ).execute()
        log.info(f"Saved: {kind} | {item} | {amount} | {user}")
        return True
    except Exception as e:
        log.error(f"Failed to append transaction: {e}")
        return False


# ─────────────────────────────────────────────────────────────────
# AI Engine
# ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
أنت محاسب رسمي لعزبة صغيرة. مهمتك تحليل رسائل المستخدم وإعادة JSON فقط.

قواعد صارمة:
- أعد JSON فقط. لا نص خارجه. لا Markdown. لا ```.
- لا تخترع أرقاماً. الأرقام تأتي فقط من رسالة المستخدم أو من سجل العمليات المعطى.
- الردود رسمية وبالعربية الفصحى دائماً.
- لا نجوم. لا ترقيم. لا جمل شرح أو نصائح.

أنواع النوايا:

1. إضافة عملية (دخل أو صرف):
{
  "intent": "transaction",
  "transaction": {
    "type": "دخل | صرف",
    "item": "اسم البند",
    "amount": <رقم موجب>
  },
  "reply": "نص التأكيد بالتنسيق المطلوب"
}

2. تقرير (ملخص الدخل والمصروف والصافي):
{
  "intent": "report",
  "filter": "all | دخل | صرف",
  "reply": "التقرير بالتنسيق المطلوب"
}

3. عرض تفاصيل عمليات:
{
  "intent": "details",
  "reply": "قائمة العمليات بالتنسيق المطلوب"
}

4. مقارنة بين فترتين أو تصنيفين:
{
  "intent": "comparison",
  "reply": "المقارنة بالتنسيق المطلوب"
}

5. محادثة عادية:
{
  "intent": "conversation",
  "reply": "الرد بالعربية الرسمية"
}

تنسيق عملية واحدة في reply:

────────────
التاريخ: ....
النوع: ....
البند: ....
المبلغ: ....
المستخدم: ....
────────────

تنسيق تقرير في reply:

────────────
الدخل: ....
المصروف: ....
الصافي: ....
────────────

لا تخرج عن هذه الهياكل أبداً.
""".strip()


def build_history_context(transactions):
    """Convert recent transactions to compact text for AI context."""
    if not transactions:
        return "لا توجد عمليات مسجلة."
    recent = transactions[-HISTORY_LIMIT:]
    lines = []
    for t in recent:
        lines.append(
            f"- {t['date']} | {t['type']} | {t['item']} | {t['amount']} | {t['user']}"
        )
    return "\n".join(lines)


def ask_ai(user_text, transactions):
    """
    Call OpenAI and return a validated intent dict.
    Never raises — returns a safe fallback dict on any failure.
    """
    history_text = build_history_context(transactions)

    try:
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            max_tokens=600,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "system",
                    "content": (
                        "سجل العمليات الأخيرة للمرجعية فقط — لا تخترع أرقاماً من خارجه:\n"
                        + history_text
                    ),
                },
                {"role": "user", "content": user_text},
            ],
        )
        raw = completion.choices[0].message.content or ""
        log.info(f"AI raw: {raw[:300]}")
        return parse_ai_response(raw)

    except Exception as e:
        log.error(f"OpenAI error: {e}")
        return fallback_response("حدث خطأ في الاتصال بالذكاء الاصطناعي.")


def parse_ai_response(raw):
    """
    Safely extract and validate JSON from AI response.
    Returns validated dict or a safe fallback.
    """
    text = raw.strip()

    # Strip accidental markdown fences
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    # Attempt direct parse
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to extract first { ... } block
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                pass

    if data is None:
        log.warning(f"Could not parse JSON: {text[:200]}")
        return fallback_response("لم أستطع فهم الرسالة. يرجى إعادة الصياغة.")

    return validate_intent(data)


def validate_intent(data):
    """Enforce required fields per intent. Return fallback on violation."""
    intent = data.get("intent")
    reply = data.get("reply", "").strip()

    if not reply:
        return fallback_response("لم يتم إنشاء رد.")

    if intent == "transaction":
        tx = data.get("transaction", {})
        if not tx.get("type") or not tx.get("item"):
            return fallback_response("بيانات العملية غير مكتملة.")
        try:
            tx["amount"] = abs(float(tx["amount"]))
        except (ValueError, TypeError):
            return fallback_response("المبلغ غير صالح. يرجى إدخال رقم صحيح.")
        if tx["type"] not in ("دخل", "صرف"):
            return fallback_response("نوع العملية غير معروف.")
        data["transaction"] = tx
        return data

    elif intent in ("report", "details", "comparison", "conversation"):
        return data

    else:
        log.warning(f"Unknown intent: {intent}")
        return fallback_response("نية غير معروفة.")


def fallback_response(message):
    return {"intent": "conversation", "reply": message}


# ─────────────────────────────────────────────────────────────────
# Webhook Handler
# ─────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self._ok()

    def do_POST(self):
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            update = json.loads(body)
        except Exception as e:
            log.error(f"Failed to parse update: {e}")
            self._ok()
            return

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

        try:
            service = get_sheets_service()
            transactions = load_transactions(service)
        except Exception as e:
            log.error(f"Sheets connection failed: {e}")
            send(chat_id, "⚠️ تعذّر الاتصال بقاعدة البيانات.")
            self._ok()
            return

        ai_result = ask_ai(text, transactions)
        intent = ai_result.get("intent")

        # Save transaction immediately — no confirmation step
        if intent == "transaction":
            tx = ai_result.get("transaction", {})
            saved = append_transaction(
                service,
                tx["type"],
                tx["item"],
                tx["amount"],
                user_name,
            )
            if not saved:
                send(chat_id, "⚠️ حدث خطأ أثناء حفظ العملية. يرجى المحاولة مرة أخرى.")
                self._ok()
                return

        reply = ai_result.get("reply") or "ما فهمت المطلوب."
        send(chat_id, reply)
        self._ok()

from http.server import BaseHTTPRequestHandler
import json
import os
import re
from datetime import datetime, timezone, timedelta, date

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
    """Read last balance from column G."""
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!G2:G"
    ).execute()
    values = result.get("values", [])
    if not values:
        return 0.0
    try:
        return float(values[-1][0])
    except Exception:
        return 0.0


def load_all_transactions(service):
    """Load all rows A2:G as dicts."""
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A2:G"
    ).execute()
    rows = result.get("values", [])
    txs = []
    for r in rows:
        if len(r) < 4:
            continue
        ts_str = r[0]
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
        except Exception:
            continue
        type_ar = r[1] if len(r) > 1 else ""
        item = r[2] if len(r) > 2 else ""
        try:
            amount = float(r[3])
        except Exception:
            amount = 0.0
        person = r[4] if len(r) > 4 else ""
        note = r[5] if len(r) > 5 else ""
        try:
            balance = float(r[6]) if len(r) > 6 else None
        except Exception:
            balance = None
        txs.append(
            {
                "timestamp": ts,
                "type_ar": type_ar,
                "item": item,
                "amount": amount,
                "person": person,
                "note": note,
                "balance": balance,
            }
        )
    return txs


def summarize_transactions(txs):
    income = sum(t["amount"] for t in txs if t["type_ar"] == "بيع")
    expense = sum(t["amount"] for t in txs if t["type_ar"] == "شراء")
    net = income - expense
    return income, expense, net


# ================= AI PARSING =================

def call_ai_to_parse(text, person_name):
    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": """
أنت مساعد لتسجيل عمليات العزبة.

أجب بصيغة JSON فقط.

الصيغة:

{
  "action": "buy | sell",
  "item": "وصف مختصر",
  "amount": رقم,
  "notes": "ملاحظات مختصرة"
}

القواعد:
- شراء / مصروف = buy
- بيع / دخل = sell
- افهم العربية
- لا تخمّن
                """.strip(),
            },
            {"role": "user", "content": text},
        ],
    )

    raw = completion.choices[0].message.content
    parsed = json.loads(raw)
    parsed.setdefault("person", person_name)
    return parsed


# ================= MAIN HANDLER =================

class handler(BaseHTTPRequestHandler):
    # ---------- Low-level helpers ----------
    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    # ---------- HTTP methods ----------
    def do_GET(self):
        self._ok()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        update = json.loads(body)

        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            self._ok()
            return

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message["text"].strip()
        lower = text.lower()

        # Security
        if user_id not in ALLOWED_USERS:
            send_telegram_message(chat_id, "⛔ هذا البوت خاص.")
            self._ok()
            return

        person = ALLOWED_USERS[user_id]

        # ---------- Commands (no AI) ----------
        if text == "/start":
            send_telegram_message(
                chat_id,
                f"مرحباً {person} 👋\n"
                "أنا بوت تسجيل عمليات العزبة.\n"
                "اكتب /help لعرض الأوامر المتاحة.",
            )
            self._ok()
            return

        if text == "/help":
            help_text = (
                "📋 الأوامر المتاحة:\n"
                "/help - عرض هذه القائمة\n"
                "/day - ملخص اليوم\n"
                "/week - ملخص آخر ٧ أيام\n\n"
                "❓ ويمكنك أيضاً السؤال عن يوم محدد مثلاً:\n"
                "i want to know what happens in 1-1-2026\n"
                "أو\n"
                "ابغى اعرف ايش صار في 1-1-2026"
            )
            send_telegram_message(chat_id, help_text)
            self._ok()
            return

        service = get_sheets_service()

        # ---------- /day summary ----------
        if text == "/day":
            today = datetime.now(UAE_TZ).date()
            txs = load_all_transactions(service)
            todays = [t for t in txs if t["timestamp"].date() == today]
            msg = self._build_summary_message(todays, f"ملخص اليوم {today}")
            send_telegram_message(chat_id, msg)
            self._ok()
            return

        # ---------- /week summary (last 7 days) ----------
        if text == "/week":
            today = datetime.now(UAE_TZ).date()
            start = today - timedelta(days=6)
            txs = load_all_transactions(service)
            week_txs = [
                t for t in txs if start <= t["timestamp"].date() <= today
            ]
            msg = self._build_summary_message(
                week_txs,
                f"ملخص آخر ٧ أيام من {start} إلى {today}",
            )
            send_telegram_message(chat_id, msg)
            self._ok()
            return

        # ---------- Natural-language date query ----------
        # pattern like 1-1-2026 or 01/01/2026
        date_match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
        if date_match and not text.startswith("/"):
            d, m, y = map(int, date_match.groups())
            try:
                target = date(y, m, d)
            except ValueError:
                send_telegram_message(chat_id, "❌ التاريخ غير صحيح.")
                self._ok()
                return

            txs = load_all_transactions(service)
            day_txs = [t for t in txs if t["timestamp"].date() == target]
            msg = self._build_summary_message(day_txs, f"ملخص يوم {target}")
            send_telegram_message(chat_id, msg)
            self._ok()
            return

        # ---------- Normal message → AI + log ----------
        try:
            parsed = call_ai_to_parse(text, person)
        except Exception:
            send_telegram_message(
                chat_id, "❌ لم أفهم العملية. حاول تكتبها بشكل أوضح."
            )
            self._ok()
            return

        action = parsed.get("action")
        try:
            amount = float(parsed.get("amount", 0))
        except Exception:
            amount = 0.0
        item = parsed.get("item", "")
        notes = parsed.get("notes", "")

        if action not in ("buy", "sell") or amount <= 0:
            send_telegram_message(
                chat_id, "❌ العملية غير واضحة. مثال: بعت خروف بـ 1200"
            )
            self._ok()
            return

        # Arabic type + delta
        if action == "buy":
            type_ar = "شراء"
            delta = -amount
        else:
            type_ar = "بيع"
            delta = amount

        last_balance = get_last_balance(service)
        new_balance = last_balance + delta

        values = [[
            now_timestamp(),  # A Timestamp
            type_ar,          # B Type (Arabic)
            item,             # C Item
            amount,           # D Amount
            person,           # E Paid By
            notes,            # F Note
            new_balance,      # G Balance
        ]]

        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Transactions!A1:G1",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

        sign = "+" if delta > 0 else "-"
        send_telegram_message(
            chat_id,
            f"✅ تم تسجيل العملية\n"
            f"النوع: {type_ar}\n"
            f"المبلغ: {amount} ({sign})\n"
            f"الرصيد الحالي: {new_balance}",
        )

        self._ok()

    # ---------- helpers for summaries ----------
    def _build_summary_message(self, txs, title):
        if not txs:
            return f"{title}\nلا توجد عمليات في هذه الفترة."

        income, expense, net = summarize_transactions(txs)

        lines = [
            f"📊 {title}",
            f"عدد العمليات: {len(txs)}",
            f"إجمالي البيع: {income}",
            f"إجمالي الشراء: {expense}",
            f"الصافي: {net}",
            "",
            "تفاصيل:"
        ]

        for t in txs[:20]:  # limit details to first 20
            time_str = t["timestamp"].strftime("%H:%M")
            lines.append(
                f"- {time_str} | {t['type_ar']} | {t['item']} | {t['amount']} | {t['person']}"
            )

        if len(txs) > 20:
            lines.append(f"... وأكثر ({len(txs) - 20}) عملية أخرى")

        return "\n".join(lines)

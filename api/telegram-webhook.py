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


def send_telegram(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception:
        pass


def get_sheets():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def load_transactions(service):
    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A2:G",
    ).execute()
    rows = res.get("values", [])
    txs = []
    for r in rows:
        if len(r) < 4:
            continue
        ts_str = r[0]
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
        except Exception:
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d")
            except Exception:
                continue
        kind = r[1] if len(r) > 1 else ""
        item = r[2] if len(r) > 2 else ""
        try:
            amount = float(r[3])
        except Exception:
            amount = 0.0
        qty = r[4] if len(r) > 4 else ""
        user = r[5] if len(r) > 5 else ""
        notes = r[6] if len(r) > 6 else ""
        try:
            quantity = float(qty) if qty else 0.0
        except Exception:
            quantity = 0.0
        txs.append(
            {
                "timestamp": ts,
                "kind": kind,
                "item": item,
                "amount": amount,
                "quantity": quantity,
                "user": user,
                "notes": notes,
            }
        )
    return txs


def append_transaction(service, ts, kind_ar, item, amount, quantity, user, notes):
    ts_str = ts.strftime("%Y-%m-%d %H:%M")
    values = [[ts_str, kind_ar, item, amount, quantity, user, notes]]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A1:G1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def undo_last_transaction(service):
    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A2:G",
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return None
    last_idx = len(rows) + 1
    last_row = rows[-1]
    ts = last_row[0] if len(last_row) > 0 else ""
    kind = last_row[1] if len(last_row) > 1 else ""
    item = last_row[2] if len(last_row) > 2 else ""
    amt_str = last_row[3] if len(last_row) > 3 else "0"
    try:
        amount = float(amt_str)
    except Exception:
        amount = 0.0
    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Transactions!A{last_idx}:G{last_idx}",
        body={},
    ).execute()
    return {"timestamp": ts, "kind": kind, "item": item, "amount": amount}


def summarize(txs):
    income = sum(t["amount"] for t in txs if t["kind"] == "دخل")
    expense = sum(t["amount"] for t in txs if t["kind"] == "صرف")
    net = income - expense
    return income, expense, net


def period_range(kind, base_date=None):
    today = now_ts().date()
    if base_date:
        try:
            today = datetime.strptime(base_date, "%Y-%m-%d").date()
        except Exception:
            pass
    if kind == "day":
        start = today
        end = today
    elif kind == "week":
        start = today - timedelta(days=6)
        end = today
    elif kind == "month":
        first = date(today.year, today.month, 1)
        if today.month == 12:
            next_m = date(today.year + 1, 1, 1)
        else:
            next_m = date(today.year, today.month + 1, 1)
        start = first
        end = next_m - timedelta(days=1)
    else:
        start = date(1970, 1, 1)
        end = date(2999, 12, 31)
    return start, end


def filter_by_period(txs, kind, base_date=None):
    start, end = period_range(kind, base_date)
    return [t for t in txs if start <= t["timestamp"].date() <= end]


def ai_parse_intent(text):
    try:
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": """
أنت العقل الرئيسي لبوت محاسبة عزبة.
افهم أي رسالة عربية بشكل طبيعي.

أعد دائماً JSON فقط بدون أي كلام زائد.

الشكل:

{
  "intent": "add_transaction | report | details | smalltalk",
  "transaction": {
    "kind": "income | expense",
    "item": "string",
    "amount": number,
    "quantity": number,
    "date": "YYYY-MM-DD أو null",
    "notes": "string"
  },
  "report": {
    "metric": "sales | purchases | net | all",
    "period": "day | week | month | all",
    "date": "YYYY-MM-DD أو null"
  },
  "details": {
    "kind": "income | expense | all",
    "period": "day | week | month | all",
    "date": "YYYY-MM-DD أو null",
    "limit": 10
  }
}

التفسير:

- أي كلام فيه دفع، راتب، مصروف، فاتورة، سلفة، بونس، اكرامية، شراء → kind = "expense".
- أي كلام فيه بيع، دخل، استلمنا، إيجار دخل للصندوق → kind = "income".
- إذا كان سؤال عن "كم" أو "إجمالي" أو "الربح" أو "العجز" أو "الصافي" → intent = "report".
- إذا طلب تفاصيل أو قائمة عمليات (مثلاً: عطنا تفاصيل الشراء، شو بعنا اليوم) → intent = "details".
- غير ذلك → intent = "smalltalk".

لا تكتب أي نص آخر خارج JSON.
""".strip(),
                },
                {"role": "user", "content": text},
            ],
        )
        raw = completion.choices[0].message.content
        return json.loads(raw)
    except Exception:
        return {"intent": "smalltalk"}


def ai_smalltalk_reply(user_text):
    try:
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.4,
            messages=[
                {
                    "role": "system",
                    "content": "أنت مساعد قصير الردود، ترد بالعربية الفصحى البسيطة أو لهجة خليجية خفيفة، بدون إطالة.",
                },
                {"role": "user", "content": user_text},
            ],
        )
        return completion.choices[0].message.content.strip()
    except Exception:
        return "ما فهمت الرسالة، حاول تعيد صياغتها."


def format_transaction_block(t):
    ts = t["timestamp"].astimezone(UAE_TZ)
    date_str = ts.strftime("%Y-%m-%d")
    time_str = ts.strftime("%H:%M")
    lines = [
        f"التاريخ: {date_str} {time_str}",
        f"العملية: {t['kind']}",
        f"البند: {t['item']}",
        f"المبلغ: {t['amount']}",
        f"المستخدم: {t['user']}",
    ]
    if t["quantity"]:
        lines.insert(3, f"الكمية: {int(t['quantity'])}")
    if t["notes"]:
        lines.append(f"ملاحظة: {t['notes']}")
    return "\n".join(lines)


class handler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self._ok()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            update = json.loads(body)
        except Exception:
            self._ok()
            return

        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            self._ok()
            return

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message["text"].strip()

        if user_id not in ALLOWED_USERS:
            send_telegram(chat_id, "هذا البوت خاص.")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]
        service = get_sheets()

        if text == "/start":
            send_telegram(
                chat_id,
                "أهلاً، سجل عملياتك بالكلام العادي.\nمثال: بعت خروف 1500\nأو: دفعت راتب العامل 1200\nواستخدم /day /week /month /balance /undo للتقارير.",
            )
            self._ok()
            return

        if text == "/help":
            send_telegram(
                chat_id,
                "الأوامر:\n/day ملخص اليوم\n/week ملخص آخر ٧ أيام\n/month ملخص الشهر\n/balance ملخص كامل\n/undo إلغاء آخر عملية\nأو اسألني بالعربي: كم صرفنا؟ كم بعنا؟ عطنا تفاصيل المبيعات.",
            )
            self._ok()
            return

        if text == "/undo":
            info = undo_last_transaction(service)
            if not info:
                send_telegram(chat_id, "ما في عمليات نحذفها.")
            else:
                send_telegram(
                    chat_id,
                    f"تم حذف آخر عملية:\nالتاريخ: {info['timestamp']}\nالعملية: {info['kind']}\nالبند: {info['item']}\nالمبلغ: {info['amount']}",
                )
            self._ok()
            return

        if text == "/day":
            parsed = {
                "intent": "report",
                "report": {"metric": "all", "period": "day", "date": None},
            }
        elif text == "/week":
            parsed = {
                "intent": "report",
                "report": {"metric": "all", "period": "week", "date": None},
            }
        elif text == "/month":
            parsed = {
                "intent": "report",
                "report": {"metric": "all", "period": "month", "date": None},
            }
        elif text == "/balance":
            parsed = {
                "intent": "report",
                "report": {"metric": "all", "period": "all", "date": None},
            }
        else:
            parsed = ai_parse_intent(text)

        intent = parsed.get("intent", "smalltalk")

        if intent == "add_transaction":
            tx = parsed.get("transaction") or {}
            kind = tx.get("kind")
            item = (tx.get("item") or "").strip()
            try:
                amount = float(tx.get("amount", 0))
            except Exception:
                amount = 0.0
            try:
                quantity = float(tx.get("quantity", 0) or 0)
            except Exception:
                quantity = 0.0
            notes = (tx.get("notes") or "").strip()
            date_str = tx.get("date")
            if not item or amount <= 0 or kind not in ("income", "expense"):
                send_telegram(chat_id, "ما قدرت أسجل العملية، حاول تكتبها أوضح.")
                self._ok()
                return
            if date_str:
                try:
                    base = datetime.strptime(date_str, "%Y-%m-%d")
                    ts = datetime(
                        base.year,
                        base.month,
                        base.day,
                        now_ts().hour,
                        now_ts().minute,
                        tzinfo=UAE_TZ,
                    )
                except Exception:
                    ts = now_ts()
            else:
                ts = now_ts()
            kind_ar = "دخل" if kind == "income" else "صرف"
            append_transaction(service, ts, kind_ar, item, amount, quantity, user_name, notes)
            block = format_transaction_block(
                {
                    "timestamp": ts,
                    "kind": kind_ar,
                    "item": item,
                    "amount": amount,
                    "quantity": quantity,
                    "user": user_name,
                    "notes": notes,
                }
            )
            send_telegram(chat_id, f"تم التسجيل:\n{block}")
            self._ok()
            return

        if intent == "report":
            rep = parsed.get("report") or {}
            metric = (rep.get("metric") or "all").lower()
            period = (rep.get("period") or "all").lower()
            date_str = rep.get("date")
            txs = load_transactions(service)
            period_txs = filter_by_period(txs, period, date_str)
            income, expense, net = summarize(period_txs)
            label = {
                "day": "اليوم",
                "week": "آخر ٧ أيام",
                "month": "هذا الشهر",
                "all": "كل الفترة",
            }.get(period, "الفترة")
            if metric == "sales":
                msg = f"إجمالي المبيعات في {label}: {income}"
            elif metric == "purchases":
                msg = f"إجمالي المصروفات في {label}: {expense}"
            elif metric == "net":
                msg = f"الربح/العجز في {label}: {net}"
            else:
                msg = (
                    f"ملخص {label}:\n"
                    f"المبيعات: {income}\n"
                    f"المصروفات: {expense}\n"
                    f"الصافي (البيع - المصروف): {net}"
                )
            send_telegram(chat_id, msg)
            self._ok()
            return

        if intent == "details":
            det = parsed.get("details") or {}
            kind_filter = (det.get("kind") or "all").lower()
            period = (det.get("period") or "day").lower()
            date_str = det.get("date")
            limit = det.get("limit") or 10
            txs = load_transactions(service)
            period_txs = filter_by_period(txs, period, date_str)
            if kind_filter == "income":
                period_txs = [t for t in period_txs if t["kind"] == "دخل"]
            elif kind_filter == "expense":
                period_txs = [t for t in period_txs if t["kind"] == "صرف"]
            period_txs.sort(key=lambda t: t["timestamp"], reverse=True)
            period_txs = period_txs[:limit]
            if not period_txs:
                send_telegram(chat_id, "ما في عمليات في هذه الفترة.")
                self._ok()
                return
            blocks = [format_transaction_block(t) for t in period_txs]
            send_telegram(chat_id, "\n\n".join(blocks))
            self._ok()
            return

        reply = ai_smalltalk_reply(text)
        send_telegram(chat_id, reply)
        self._ok()

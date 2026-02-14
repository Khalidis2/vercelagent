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


def fmt_num(x: float):
    return int(x) if float(x).is_integer() else x


def send_telegram(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception:
        pass


def get_sheets_service():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def append_transaction(service, ts_str, kind_ar, item, amount, user, note):
    values = [[ts_str, kind_ar, item, amount, user, note]]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A1:F1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def load_transactions(service):
    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A2:F",
    ).execute()
    rows = res.get("values", [])
    txs = []
    for r in rows:
        if len(r) < 4:
            continue
        ts_raw = r[0]
        try:
            ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M").replace(tzinfo=UAE_TZ)
        except Exception:
            try:
                ts = datetime.strptime(ts_raw, "%Y-%m-%d").replace(tzinfo=UAE_TZ)
            except Exception:
                continue
        kind = r[1] if len(r) > 1 else ""
        item = r[2] if len(r) > 2 else ""
        try:
            amount = float(r[3])
        except Exception:
            amount = 0.0
        user = r[4] if len(r) > 4 else ""
        note = r[5] if len(r) > 5 else ""
        txs.append(
            {
                "timestamp": ts,
                "kind": kind,
                "item": item,
                "amount": amount,
                "user": user,
                "note": note,
            }
        )
    return txs


def summarize(txs):
    income = sum(t["amount"] for t in txs if t["kind"] == "دخل")
    expense = sum(t["amount"] for t in txs if t["kind"] == "صرف")
    net = income - expense
    return income, expense, net


def save_pending_transaction(service, user_id, kind_ar, item, amount, user_name, note, date_str):
    meta = {"notes": note, "date": date_str}
    values = [[str(user_id), now_ts().strftime("%Y-%m-%d %H:%M"), "transaction", kind_ar, item, amount, user_name, json.dumps(meta, ensure_ascii=False)]]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Pending!A1:H1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def get_last_pending_for_user(service, user_id):
    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Pending!A2:H",
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return None, None
    last_idx = None
    last_row = None
    for i, r in enumerate(rows, start=2):
        if r and r[0] == str(user_id):
            last_idx = i
            last_row = r
    return last_row, last_idx


def clear_pending_row(service, idx):
    if not idx:
        return
    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Pending!A{idx}:H{idx}",
        body={},
    ).execute()


def filter_by_period(txs, period, date_str):
    if period == "all":
        return txs
    today = now_ts().date()
    if period == "day":
        if date_str:
            try:
                target = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                target = today
        else:
            target = today
        return [t for t in txs if t["timestamp"].date() == target]
    if period == "week":
        end = today
        start = end - timedelta(days=6)
        return [t for t in txs if start <= t["timestamp"].date() <= end]
    if period == "month":
        if date_str:
            try:
                base = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                base = today
        else:
            base = today
        month_start = date(base.year, base.month, 1)
        if base.month == 12:
            next_month = date(base.year + 1, 1, 1)
        else:
            next_month = date(base.year, base.month + 1, 1)
        month_end = next_month - timedelta(days=1)
        return [t for t in txs if month_start <= t["timestamp"].date() <= month_end]
    return txs


def label_for_period(period, date_str):
    today = now_ts().date()
    if period == "day":
        if date_str:
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                d = today
        else:
            d = today
        return f"يوم {d}"
    if period == "week":
        end = today
        start = end - timedelta(days=6)
        return f"من {start} إلى {end}"
    if period == "month":
        if date_str:
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                d = today
        else:
            d = today
        return f"شهر {d.year}-{d.month:02d}"
    return "كل الفترة"


def call_ai(text):
    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": """
أنت محلل أوامر لروبوت تلغرام يسجل مصاريف ومبيعات عزبة في جوجل شيت.

أرجع دائماً JSON فقط بدون أي كلام إضافي.

الشكل:

{
  "intent": "add_transaction | report | details | other",

  "transaction": {
    "direction": "out | in",
    "item": "نص قصير",
    "amount": رقم,
    "date": "YYYY-MM-DD أو null",
    "notes": "نص قصير أو فارغ"
  },

  "report": {
    "metric": "income | expense | net | all",
    "period": "day | week | month | all",
    "date": "YYYY-MM-DD أو null"
  },

  "details": {
    "kind": "income | expense | both",
    "period": "day | week | month | all",
    "date": "YYYY-MM-DD أو null",
    "limit": عدد صحيح (مثلاً 10)
  }
}

القواعد:

- أي جملة فيها دفع أو شراء أو راتب أو فاتورة أو مصروف → direction = "out".
- أي جملة فيها بيع أو استلمنا أو دخل للصندوق أو إيجار لنا → direction = "in".
- لا تسجل عملية إذا كان السؤال فقط عن كم صرفنا أو كم بعنا أو الربح → هذه تقارير report.
- أمثلة تقرير:
  - "كم صرفنا هالشهر؟" → report.metric="expense", period="month".
  - "كم بعنا؟" → report.metric="income", period="all".
  - "كم الربح هالأسبوع؟" → report.metric="net", period="week".
  - "قارن المبيعات مع المصروفات" → report.metric="all", period="all".
- أمثلة تفاصيل:
  - "اعرض آخر العمليات" → details.kind="both", period="all", limit=10.
  - "اعطني تفاصيل الشراء" → details.kind="expense".
  - "شو بعنا اليوم؟" → details.kind="income", period="day".

إذا لم تفهم الرسالة اجعل:
"intent": "other".
""".strip(),
            },
            {"role": "user", "content": text},
        ],
    )
    raw = completion.choices[0].message.content
    return json.loads(raw)


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
        update = json.loads(body)

        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            self._ok()
            return

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message["text"].strip()

        if user_id not in ALLOWED_USERS:
            send_telegram(chat_id, "هذا البوت خاص بالعائلة.")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]
        service = get_sheets_service()

        if text == "/start":
            msg = (
                "الأوامر:\n"
                "/help مساعدة\n"
                "/day ملخص اليوم\n"
                "/week ملخص آخر ٧ أيام\n"
                "/month ملخص هذا الشهر\n"
                "/balance ملخص كامل\n"
                "/last آخر العمليات\n"
                "/undo حذف آخر عملية\n"
                "/confirm تأكيد العملية المعلقة\n"
                "/cancel إلغاء العملية المعلقة"
            )
            send_telegram(chat_id, msg)
            self._ok()
            return

        if text == "/help":
            msg = (
                "اكتب الكلام عادي مثل:\n"
                "  دفعت راتب العامل 1200\n"
                "  بعنا خروفين 5000\n"
                "واسأل:\n"
                "  كم صرفنا هالشهر؟\n"
                "  كم بعنا؟\n"
                "  كم الربح الكلي؟\n"
                "  اعرض آخر العمليات\n\n"
                "الأوامر السريعة:\n"
                "/day /week /month /balance /last /undo /confirm /cancel"
            )
            send_telegram(chat_id, msg)
            self._ok()
            return

        if text == "/balance":
            txs = load_transactions(service)
            income, expense, net = summarize(txs)
            income = fmt_num(income)
            expense = fmt_num(expense)
            net_v = fmt_num(net)
            if net > 0:
                status = f"ربح {net_v}"
            elif net < 0:
                status = f"عجز {abs(net_v)}"
            else:
                status = "لا ربح ولا عجز"
            msg = (
                "ملخص كل الفترة:\n"
                f"الدخل: {income}\n"
                f"المصروف: {expense}\n"
                f"الصافي: {net_v} ({status})"
            )
            send_telegram(chat_id, msg)
            self._ok()
            return

        if text == "/day":
            txs = load_transactions(service)
            period_txs = filter_by_period(txs, "day", None)
            income, expense, net = summarize(period_txs)
            income = fmt_num(income)
            expense = fmt_num(expense)
            net_v = fmt_num(net)
            if net > 0:
                status = f"ربح {net_v}"
            elif net < 0:
                status = f"عجز {abs(net_v)}"
            else:
                status = "لا ربح ولا عجز"
            msg = (
                "ملخص اليوم:\n"
                f"الدخل: {income}\n"
                f"المصروف: {expense}\n"
                f"الصافي: {net_v} ({status})"
            )
            send_telegram(chat_id, msg)
            self._ok()
            return

        if text == "/week":
            txs = load_transactions(service)
            period_txs = filter_by_period(txs, "week", None)
            income, expense, net = summarize(period_txs)
            income = fmt_num(income)
            expense = fmt_num(expense)
            net_v = fmt_num(net)
            if net > 0:
                status = f"ربح {net_v}"
            elif net < 0:
                status = f"عجز {abs(net_v)}"
            else:
                status = "لا ربح ولا عجز"
            label = label_for_period("week", None)
            msg = (
                f"ملخص {label}:\n"
                f"الدخل: {income}\n"
                f"المصروف: {expense}\n"
                f"الصافي: {net_v} ({status})"
            )
            send_telegram(chat_id, msg)
            self._ok()
            return

        if text == "/month":
            txs = load_transactions(service)
            period_txs = filter_by_period(txs, "month", None)
            income, expense, net = summarize(period_txs)
            income = fmt_num(income)
            expense = fmt_num(expense)
            net_v = fmt_num(net)
            if net > 0:
                status = f"ربح {net_v}"
            elif net < 0:
                status = f"عجز {abs(net_v)}"
            else:
                status = "لا ربح ولا عجز"
            label = label_for_period("month", None)
            msg = (
                f"ملخص {label}:\n"
                f"الدخل: {income}\n"
                f"المصروف: {expense}\n"
                f"الصافي: {net_v} ({status})"
            )
            send_telegram(chat_id, msg)
            self._ok()
            return

        if text == "/last":
            txs = load_transactions(service)
            txs.sort(key=lambda t: t["timestamp"], reverse=True)
            txs = txs[:10]
            if not txs:
                send_telegram(chat_id, "لا توجد عمليات.")
                self._ok()
                return
            blocks = []
            for t in txs:
                ts = t["timestamp"].strftime("%Y-%m-%d %H:%M")
                amt = fmt_num(t["amount"])
                block = (
                    "────────────\n"
                    f"التاريخ: {ts}\n"
                    f"النوع: {t['kind']}\n"
                    f"البند: {t['item']}\n"
                    f"المبلغ: {amt}\n"
                    f"المستخدم: {t['user']}\n"
                    f"ملاحظات: {t['note'] or '-'}"
                )
                blocks.append(block)
            send_telegram(chat_id, "\n".join(blocks))
            self._ok()
            return

        if text == "/undo":
            txs = load_transactions(service)
            if not txs:
                send_telegram(chat_id, "لا توجد عمليات لحذفها.")
                self._ok()
                return
            txs.sort(key=lambda t: t["timestamp"])
            last = txs[-1]
            ts_str = last["timestamp"].strftime("%Y-%m-%d %H:%M")
            values = service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range="Transactions!A2:F",
            ).execute().get("values", [])
            if values:
                last_index = len(values) + 1
                service.spreadsheets().values().clear(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"Transactions!A{last_index}:F{last_index}",
                    body={},
                ).execute()
            amt = fmt_num(last["amount"])
            msg = (
                "تم حذف آخر عملية:\n"
                f"التاريخ: {ts_str}\n"
                f"النوع: {last['kind']}\n"
                f"البند: {last['item']}\n"
                f"المبلغ: {amt}\n"
                f"المستخدم: {last['user']}"
            )
            send_telegram(chat_id, msg)
            self._ok()
            return

        if text == "/cancel":
            pending, idx = get_last_pending_for_user(service, user_id)
            if not pending:
                send_telegram(chat_id, "لا توجد عملية معلقة.")
            else:
                clear_pending_row(service, idx)
                send_telegram(chat_id, "تم إلغاء العملية المعلقة.")
            self._ok()
            return

        if text == "/confirm":
            pending, idx = get_last_pending_for_user(service, user_id)
            if not pending:
                send_telegram(chat_id, "لا توجد عملية معلقة.")
                self._ok()
                return
            _, _, op_type, kind_ar, item, amount_str, user_name_row, meta_json = (pending + [""] * 8)[:8]
            if op_type != "transaction":
                send_telegram(chat_id, "نوع العملية المعلقة غير معروف.")
                self._ok()
                return
            try:
                amount = float(amount_str)
            except Exception:
                amount = 0.0
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except Exception:
                meta = {}
            note = meta.get("notes") or ""
            date_str = meta.get("date")
            if date_str:
                try:
                    base_date = datetime.strptime(date_str, "%Y-%m-%d")
                    ts = base_date.replace(hour=now_ts().hour, minute=now_ts().minute, tzinfo=UAE_TZ)
                except Exception:
                    ts = now_ts()
            else:
                ts = now_ts()
            ts_str = ts.strftime("%Y-%m-%d %H:%M")
            append_transaction(service, ts_str, kind_ar, item, amount, user_name_row or user_name, note)
            clear_pending_row(service, idx)
            amt = fmt_num(amount)
            msg = (
                "تم تسجيل العملية:\n"
                f"التاريخ: {ts_str}\n"
                f"النوع: {kind_ar}\n"
                f"البند: {item}\n"
                f"المبلغ: {amt}\n"
                f"المستخدم: {user_name_row or user_name}\n"
                f"ملاحظات: {note or '-'}"
            )
            send_telegram(chat_id, msg)
            self._ok()
            return

        try:
            parsed = call_ai(text)
        except Exception:
            send_telegram(chat_id, "ما فهمت الرسالة، حاول تكتبها أوضح.")
            self._ok()
            return

        intent = parsed.get("intent") or "other"

        if intent == "add_transaction":
            tx = parsed.get("transaction") or {}
            direction = tx.get("direction")
            item = (tx.get("item") or "").strip()
            try:
                amount = float(tx.get("amount") or 0)
            except Exception:
                amount = 0.0
            date_str = tx.get("date")
            notes = tx.get("notes") or ""
            if direction not in ("out", "in") or amount <= 0 or not item:
                send_telegram(chat_id, "العملية غير واضحة، حاول تكتب: بعت أو اشتريت أو دفعت مع المبلغ.")
                self._ok()
                return
            kind_ar = "صرف" if direction == "out" else "دخل"
            save_pending_transaction(service, user_id, kind_ar, item, amount, user_name, notes, date_str)
            amt = fmt_num(amount)
            when_txt = date_str if date_str else now_ts().strftime("%Y-%m-%d")
            preview = (
                "تأكيد العملية:\n"
                f"التاريخ: {when_txt}\n"
                f"النوع: {kind_ar}\n"
                f"البند: {item}\n"
                f"المبلغ: {amt}\n"
                f"المستخدم: {user_name}\n"
                f"ملاحظات: {notes or '-'}\n\n"
                "اكتب /confirm للتسجيل أو /cancel للإلغاء."
            )
            send_telegram(chat_id, preview)
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
            income = fmt_num(income)
            expense = fmt_num(expense)
            net_v = fmt_num(net)
            label = label_for_period(period, date_str)
            if metric == "income":
                msg = f"الدخل في {label}: {income}"
            elif metric == "expense":
                msg = f"المصروف في {label}: {expense}"
            elif metric == "net":
                if net > 0:
                    msg = f"الربح في {label}: {net_v}"
                elif net < 0:
                    msg = f"العجز في {label}: {abs(net_v)}"
                else:
                    msg = f"لا يوجد ربح أو عجز في {label}"
            else:
                if net > 0:
                    status = f"ربح {net_v}"
                elif net < 0:
                    status = f"عجز {abs(net_v)}"
                else:
                    status = "لا ربح ولا عجز"
                msg = (
                    f"ملخص {label}:\n"
                    f"الدخل: {income}\n"
                    f"المصروف: {expense}\n"
                    f"الصافي: {net_v} ({status})"
                )
            send_telegram(chat_id, msg)
            self._ok()
            return

        if intent == "details":
            det = parsed.get("details") or {}
            kind_filter = (det.get("kind") or "both").lower()
            period = (det.get("period") or "all").lower()
            date_str = det.get("date")
            try:
                limit = int(det.get("limit") or 10)
            except Exception:
                limit = 10
            txs = load_transactions(service)
            period_txs = filter_by_period(txs, period, date_str)
            if kind_filter == "income":
                period_txs = [t for t in period_txs if t["kind"] == "دخل"]
            elif kind_filter == "expense":
                period_txs = [t for t in period_txs if t["kind"] == "صرف"]
            period_txs.sort(key=lambda t: t["timestamp"], reverse=True)
            period_txs = period_txs[:limit]
            if not period_txs:
                send_telegram(chat_id, "لا توجد عمليات في هذه الفترة.")
                self._ok()
                return
            blocks = []
            for t in period_txs:
                ts = t["timestamp"].strftime("%Y-%m-%d %H:%M")
                amt = fmt_num(t["amount"])
                block = (
                    "────────────\n"
                    f"التاريخ: {ts}\n"
                    f"النوع: {t['kind']}\n"
                    f"البند: {t['item']}\n"
                    f"المبلغ: {amt}\n"
                    f"المستخدم: {t['user']}\n"
                    f"ملاحظات: {t['note'] or '-'}"
                )
                blocks.append(block)
            send_telegram(chat_id, "\n".join(blocks))
            self._ok()
            return

        send_telegram(chat_id, "ما فهمت، حاول تكتب الجملة أوضح أو استخدم /help.")
        self._ok()

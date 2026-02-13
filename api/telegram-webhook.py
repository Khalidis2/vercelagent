from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta, date
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

ALLOWED_USERS = {
    47329648: "Khaled",
    6894180427: "Hamad",
}

UAE_TZ = timezone(timedelta(hours=4))


def now():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")


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
    r = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A2:E",
    ).execute()
    rows = r.get("values", [])
    data = []
    for row in rows:
        if len(row) < 5:
            continue
        try:
            ts = datetime.strptime(row[0], "%Y-%m-%d %H:%M")
            amount = float(row[3])
        except:
            continue
        data.append(
            {
                "ts": ts,
                "type": row[1],
                "item": row[2],
                "amount": amount,
                "user": row[4],
            }
        )
    return data


def totals(data):
    sales = sum(x["amount"] for x in data if x["type"] == "بيع")
    purchases = sum(x["amount"] for x in data if x["type"] == "شراء")
    return sales, purchases, sales - purchases


def append_tx(service, ttype, item, amount, user):
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A1:E1",
        valueInputOption="USER_ENTERED",
        body={
            "values": [[now(), ttype, item, amount, user]]
        },
    ).execute()


def detect_direction(text):
    t = text.lower()
    if any(w in t for w in ["بيع", "بعنا", "مبيعات", "دخل", "استلم"]):
        return "بيع"
    if any(w in t for w in ["شراء", "اشترينا", "دفع", "صرف", "راتب", "بونس", "فاتورة"]):
        return "شراء"
    return None


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
        data = load_tx(service)

        # ---------- NUMERIC QUESTIONS ----------
        if "كم" in text:
            s, p, n = totals(data)

            if "ربح" in text or "صافي" in text:
                send(chat_id, f"الربح: {n}")
            elif "بيع" in text or "مبيعات" in text or "دخل" in text:
                send(chat_id, f"المبيعات: {s}")
            elif "شراء" in text or "صرف" in text or "مشتريات" in text:
                send(chat_id, f"المصروفات: {p}")
            else:
                send(chat_id, f"المبيعات: {s}\nالمصروفات: {p}")

            self._ok()
            return

        # ---------- DETAILS ----------
        if any(w in text for w in ["شو", "ماذا", "عرض", "تفاصيل"]):

            if "بيع" in text:
                filtered = [x for x in data if x["type"] == "بيع"]
                title = "آخر المبيعات:"
            elif "شراء" in text or "صرف" in text:
                filtered = [x for x in data if x["type"] == "شراء"]
                title = "آخر المشتريات:"
            else:
                filtered = data
                title = "آخر العمليات:"

            filtered = filtered[-5:]

            if not filtered:
                send(chat_id, "لا توجد عمليات.")
                self._ok()
                return

            blocks = []
            for x in filtered:
                block = (
                    "────────────\n"
                    f"التاريخ: {x['ts'].strftime('%d-%m-%Y %H:%M')}\n"
                    f"العملية: {x['type']}\n"
                    f"البند: {x['item']}\n"
                    f"المبلغ: {x['amount']}\n"
                    f"المستخدم: {x['user']}\n"
                    "────────────"
                )
                blocks.append(block)

            send(chat_id, f"{title}\n\n" + "\n\n".join(blocks))
            self._ok()
            return

        # ---------- NEW TRANSACTION ----------
        direction = detect_direction(text)

        if direction:
            numbers = [float(s) for s in text.split() if s.replace('.', '', 1).isdigit()]
            if not numbers:
                send(chat_id, "حدد المبلغ.")
                self._ok()
                return

            amount = numbers[0]
            item = text.replace(str(int(amount)), "").strip()

            handler.pending = {
                "type": direction,
                "item": item,
                "amount": amount,
                "user": user_name,
            }

            send(chat_id,
                 f"{now()}\n"
                 f"المستخدم: {user_name}\n"
                 f"العملية: {direction}\n"
                 f"البند: {item}\n"
                 f"المبلغ: {amount}\n\n"
                 "/confirm أو /cancel")

            self._ok()
            return

        if text == "/confirm" and hasattr(handler, "pending"):
            p = handler.pending
            append_tx(service, p["type"], p["item"], p["amount"], p["user"])
            del handler.pending
            send(chat_id, "تم التسجيل.")
            self._ok()
            return

        if text == "/cancel":
            if hasattr(handler, "pending"):
                del handler.pending
            send(chat_id, "تم الإلغاء.")
            self._ok()
            return

        send(chat_id, "غير مفهوم.")
        self._ok()

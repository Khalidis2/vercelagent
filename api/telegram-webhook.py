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

    sell_words = ["بيع", "بعنا", "sell", "مبيعات", "دخل", "استلمنا"]
    buy_words = ["شراء", "اشترينا", "دفع", "صرف", "راتب", "بونس", "فاتورة"]

    if any(w in t for w in sell_words):
        return "in"
    if any(w in t for w in buy_words):
        return "out"
    return None


def parse_ai(text):
    r = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": """
Return JSON only.

{
  "intent": "transaction | report | details | other",
  "direction": "in | out",
  "item": "",
  "amount": number,
  "period": "day | week | month | all",
  "metric": "sales | purchases | net | all"
}
"""
            },
            {"role": "user", "content": text},
        ],
    )
    return json.loads(r.choices[0].message.content)


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

        if text == "/balance":
            data = load_tx(service)
            s, p, _ = totals(data)
            send(chat_id, f"المبيعات: {s}\nالمصروفات: {p}")
            self._ok()
            return

        if text == "/week":
            today = datetime.now(UAE_TZ).date()
            start = today - timedelta(days=6)
            data = [x for x in load_tx(service) if start <= x["ts"].date() <= today]
            s, p, _ = totals(data)
            send(chat_id, f"هذا الأسبوع:\nالمبيعات: {s}\nالمصروفات: {p}")
            self._ok()
            return

        if "تفاصيل" in text:
            data = load_tx(service)[-10:]
            if not data:
                send(chat_id, "لا توجد عمليات.")
                self._ok()
                return

            blocks = []
            for t in data:
                block = (
                    "────────────\n"
                    f"التاريخ: {t['ts'].strftime('%d-%m-%Y %H:%M')}\n"
                    f"العملية: {t['type']}\n"
                    f"البند: {t['item']}\n"
                    f"المبلغ: {t['amount']}\n"
                    f"المستخدم: {t['user']}\n"
                    "────────────"
                )
                blocks.append(block)

            send(chat_id, "\n\n".join(blocks))
            self._ok()
            return

        direction = detect_direction(text)

        try:
            ai = parse_ai(text)
        except:
            send(chat_id, "غير مفهوم.")
            self._ok()
            return

        if ai["intent"] == "transaction":
            item = ai.get("item")
            amount = ai.get("amount")

            if not direction:
                direction = ai.get("direction")

            if not item or not amount:
                send(chat_id, "العملية غير واضحة.")
                self._ok()
                return

            ttype = "بيع" if direction == "in" else "شراء"

            handler.pending = {
                "type": ttype,
                "item": item,
                "amount": amount,
                "user": user_name,
            }

            send(chat_id,
                 f"{now()}\n"
                 f"المستخدم: {user_name}\n"
                 f"العملية: {ttype}\n"
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

        if ai["intent"] == "report":
            data = load_tx(service)
            s, p, n = totals(data)

            if ai["metric"] == "sales":
                send(chat_id, f"{s}")
            elif ai["metric"] == "purchases":
                send(chat_id, f"{p}")
            elif ai["metric"] == "net":
                send(chat_id, f"{n}")
            else:
                send(chat_id, f"المبيعات: {s}\nالمصروفات: {p}")

            self._ok()
            return

        send(chat_id, "غير مفهوم.")
        self._ok()

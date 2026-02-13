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

Rules:
- Payment or expense = out
- Income or selling = in
- Questions starting with كم = report
- Asking for details = details
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

        if text == "/day":
            today = datetime.now(UAE_TZ).date()
            data = [x for x in load_tx(service) if x["ts"].date() == today]
            s, p, _ = totals(data)
            send(chat_id, f"اليوم:\nالمبيعات: {s}\nالمصروفات: {p}")
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

        try:
            ai = parse_ai(text)
        except:
            send(chat_id, "غير مفهوم.")
            self._ok()
            return

        if ai["intent"] == "transaction":
            if ai["amount"] <= 0 or not ai["item"]:
                send(chat_id, "العملية غير واضحة.")
                self._ok()
                return

            ttype = "بيع" if ai["direction"] == "in" else "شراء"

            handler.pending = {
                "type": ttype,
                "item": ai["item"],
                "amount": ai["amount"],
                "user": user_name,
            }

            send(chat_id, f"{now()} | {user_name} | {ttype} | {ai['item']} | {ai['amount']}\n/confirm أو /cancel")
            self._ok()
            return

        if text == "/confirm" and hasattr(handler, "pending"):
            p = handler.pending
            append_tx(service, p["type"], p["item"], p["amount"], p["user"])
            del handler.pending
            send(chat_id, "تم.")
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
            today = datetime.now(UAE_TZ).date()

            if ai["period"] == "day":
                data = [x for x in data if x["ts"].date() == today]
            elif ai["period"] == "week":
                start = today - timedelta(days=6)
                data = [x for x in data if start <= x["ts"].date() <= today]
            elif ai["period"] == "month":
                start = date(today.year, today.month, 1)
                data = [x for x in data if x["ts"].date() >= start]

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

        if ai["intent"] == "details":
            data = load_tx(service)[-10:]
            if not data:
                send(chat_id, "لا توجد عمليات.")
                self._ok()
                return
            lines = []
            for t in data:
                lines.append(f"{t['ts'].strftime('%d-%m %H:%M')} | {t['type']} | {t['item']} | {t['amount']} | {t['user']}")
            send(chat_id, "\n".join(lines))
            self._ok()
            return

        send(chat_id, "غير مفهوم.")
        self._ok()

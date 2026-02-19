"""
GET  /api/data          → returns all Transactions + Inventory + Pending as JSON
POST /api/data          → adds a new transaction from the HTML app
"""

from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ── ENV ────────────────────────────────────────────────────────────────────────
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID              = os.environ.get("SPREADSHEET_ID")
TELEGRAM_BOT_TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")

# Allowed origins for CORS (add your Vercel domain here too if needed)
ALLOWED_ORIGINS = ["*"]

UAE_TZ = timezone(timedelta(hours=4))

S_TRANSACTIONS = "Transactions"
S_INVENTORY    = "Inventory"
S_PENDING      = "Pending"

# ── SHEETS ─────────────────────────────────────────────────────────────────────
def sheets_svc():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)

def read_sheet(svc, sheet, rng="A1:Z"):
    res = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{rng}",
    ).execute()
    return res.get("values", [])

def append_row(svc, sheet, row: list):
    svc.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()

# ── UTILS ──────────────────────────────────────────────────────────────────────
def now_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")

def fmt(x):
    try:
        f = float(x)
        return int(f) if f.is_integer() else round(f, 2)
    except Exception:
        return 0

def rows_to_dicts(rows):
    """Convert rows (list of lists) to list of dicts using first row as headers."""
    if not rows:
        return []
    headers = rows[0]
    result  = []
    for r in rows[1:]:
        if not any(r):
            continue
        d = {}
        for i, h in enumerate(headers):
            d[h] = r[i] if i < len(r) else ""
        result.append(d)
    return result

# ── PARSE TRANSACTIONS ──────────────────────────────────────────────────────────
def parse_transactions(rows):
    """
    Transactions sheet: A=التاريخ B=النوع C=البند D=التصنيف E=المبلغ F=المستخدم
    Skip header row (row 1).
    """
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        # skip header row
        if r[0] == "التاريخ":
            continue
        try:
            out.append({
                "id":       f"{r[0]}-{r[2]}-{r[4]}",
                "date":     r[0],
                "type":     r[1],       # دخل | صرف
                "item":     r[2],
                "category": r[3] if len(r) > 3 else "",
                "amount":   fmt(r[4]),
                "user":     r[5] if len(r) > 5 else "",
            })
        except (ValueError, IndexError):
            continue
    return out

def parse_inventory(rows):
    """
    Inventory sheet: A=Item B=Type C=Quantity D=Notes
    """
    out = []
    for r in rows:
        if not r or r[0] in ("Item", ""):
            continue
        try:
            out.append({
                "item":  r[0],
                "type":  r[1] if len(r) > 1 else "",
                "qty":   int(r[2]) if len(r) > 2 and r[2] else 0,
                "notes": r[3] if len(r) > 3 else "",
            })
        except (ValueError, IndexError):
            continue
    return out

# ── CORS HEADERS ───────────────────────────────────────────────────────────────
CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json",
}

# ── HANDLER ────────────────────────────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def _send(self, code, body: dict):
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        # preflight
        self.send_response(204)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        """Return all data from Sheets."""
        try:
            svc = sheets_svc()

            t_rows = read_sheet(svc, S_TRANSACTIONS, "A1:F")
            i_rows = read_sheet(svc, S_INVENTORY,    "A1:D")

            transactions = parse_transactions(t_rows)
            inventory    = parse_inventory(i_rows)

            # compute quick summary
            income  = sum(x["amount"] for x in transactions if x["type"] == "دخل")
            expense = sum(x["amount"] for x in transactions if x["type"] == "صرف")

            self._send(200, {
                "ok": True,
                "transactions": transactions,
                "inventory":    inventory,
                "summary": {
                    "income":  income,
                    "expense": expense,
                    "profit":  income - expense,
                },
            })

        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})

    def do_POST(self):
        """
        Add a transaction from the HTML app.
        Body JSON: { type, item, category, amount, user }
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length).decode())

            kind     = body.get("type", "")       # دخل | صرف
            item     = body.get("item", "")
            category = body.get("category") or item
            amount   = body.get("amount", 0)
            user     = body.get("user", "App")

            if not kind or not item or not amount:
                self._send(400, {"ok": False, "error": "type, item, amount required"})
                return

            svc = sheets_svc()
            append_row(svc, S_TRANSACTIONS,
                       [now_str(), kind, item, category, amount, user])

            # Notify via Telegram (optional — comment out if not needed)
            _notify_telegram(kind, item, amount, user)

            self._send(200, {"ok": True, "message": "تم التسجيل"})

        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})


def _notify_telegram(kind, item, amount, user):
    """Send a short Telegram message when the HTML app records a transaction."""
    if not TELEGRAM_BOT_TOKEN:
        return
    # notify all allowed users
    allowed_chat_ids = [47329648, 6894180427]
    emoji = "💰" if kind == "دخل" else "📤"
    text  = f"{emoji} [من التطبيق]\n{kind}: {item}\nالمبلغ: {amount} د.إ\nبواسطة: {user}"
    for chat_id in allowed_chat_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=5,
            )
        except Exception:
            pass

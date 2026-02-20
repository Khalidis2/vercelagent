"""
Ezba (Farm) Telegram Bot – AI Intent Version
يحافظ على نفس هيكل Google Sheets:
Transactions: A=التاريخ B=النوع(دخل/صرف) C=البند D=التصنيف E=المبلغ F=المستخدم
"""

from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta, date
import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ────────────── ENV ──────────────
TELEGRAM_BOT_TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY              = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID              = os.environ.get("SPREADSHEET_ID")

ALLOWED_USERS = {
    47329648:   "Khaled",
    6894180427: "Hamad",
}

UAE_TZ = timezone(timedelta(hours=4))
openai_client = OpenAI(api_key=OPENAI_API_KEY)

S_TRANSACTIONS = "Transactions"
S_PENDING      = "Pending"

D = "──────────────"


# ────────────── TELEGRAM ──────────────
def send(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception:
        pass


# ────────────── SHEETS ──────────────
def sheets_svc():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def read_sheet(svc, sheet, rng="A2:Z"):
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


def now_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")


def fmt(x):
    try:
        f = float(x)
        return int(f) if f.is_integer() else round(f, 2)
    except Exception:
        return x


# ────────────── TRANSACTIONS ──────────────
def load_transactions(svc):
    rows = read_sheet(svc, S_TRANSACTIONS)
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        try:
            out.append({
                "date":     r[0],
                "type":     r[1],          # دخل / صرف
                "item":     r[2],
                "category": r[3],
                "amount":   float(r[4]),
                "user":     r[5] if len(r) > 5 else "",
            })
        except Exception:
            continue
    return out


def add_transaction(svc, kind, item, category, amount, user):
    append_row(svc, S_TRANSACTIONS, [now_str(), kind, item, category, amount, user])
    append_row(svc, S_PENDING, [user, now_str(), "transaction", kind, item, amount, "", user, category])


def totals_all(data):
    inc = sum(x["amount"] for x in data if x["type"] == "دخل")
    exp = sum(x["amount"] for x in data if x["type"] == "صرف")
    return inc, exp


def totals_period(data, period):
    """يرجع (inc, exp, filtered_rows) حسب الفترة."""
    if period == "all":
        inc, exp = totals_all(data)
        return inc, exp, data

    today = datetime.now(UAE_TZ).date()
    filtered = []

    if period == "today":
        prefix = today.strftime("%Y-%m-%d")
        filtered = [x for x in data if x["date"].startswith(prefix)]

    elif period == "week":
        start = today - timedelta(days=6)
        for x in data:
            try:
                d = datetime.strptime(x["date"][:10], "%Y-%m-%d").date()
            except Exception:
                continue
            if start <= d <= today:
                filtered.append(x)

    else:  # month (افتراضي)
        prefix = today.strftime("%Y-%m")
        filtered = [x for x in data if x["date"].startswith(prefix)]

    inc = sum(x["amount"] for x in filtered if x["type"] == "دخل")
    exp = sum(x["amount"] for x in filtered if x["type"] == "صرف")
    return inc, exp, filtered


def period_label(period):
    return {
        "today": "اليوم",
        "week":  "هذا الأسبوع",
        "month": "هذا الشهر",
        "all":   "لكل الفترة المسجلة",
    }.get(period, "هذا الشهر")


# ────────────── AI INTENT ──────────────
SYSTEM_PROMPT = """
أنت مدير مالي ذكي لعزبة.

افهم الجملة حتى لو كانت قصيرة أو لهجة إماراتية.

أرجع JSON فقط:

{
  "intent": "",
  "direction": "in | out | none",
  "item": "",
  "category": "",
  "amount": 0,
  "period": "today | week | month | all"
}

intents:

- add_income        : تسجيل دخل (بيع، وردة، استلمنا فلوس)
- add_expense       : تسجيل صرف (دفعنا، صرفنا، اشترينا، راتب، فاتورة...)
- income_total      : سؤال عن إجمالي الدخل (كم الدخل؟ كم دخلنا؟ كم اجمالي المبيعات؟)
- expense_total     : سؤال عن إجمالي المصروف (كم صرفنا؟ كم المصاريف؟)
- profit            : صافي الربح (كم الربح؟ كم الصافي؟)
- last_transactions : آخر العمليات (آخر العمليات، عطنا آخر الحركات)
- category_total    : إجمالي تصنيف معيّن (كم صرفنا على الأعلاف؟ كم دخلنا من البيض؟)
- clarify           : لم يتم فهم الرسالة كتسجيل ولا تقرير

قواعد:
- إذا احتوت الجملة على "بعت" أو "بيع" أو "وردة" أو "دخل" ومعها مبلغ → add_income
- إذا احتوت الجملة على "اشترينا" أو "شراء" أو "صرفنا" أو "دفعنا" أو "فاتورة" أو "راتب" ومعها مبلغ → add_expense

أمثلة:

"بعنا بيض ب 200" →
  intent=add_income, direction=in, item="بيض", category="بيض", amount=200

"صرفنا على الاعلاف 500" →
  intent=add_expense, direction=out, item="أعلاف", category="أعلاف", amount=500

"كم الدخل؟" →
  intent=income_total, period="all"

"كم الدخل هالشهر؟" →
  intent=income_total, period="month"

"كم صرفنا؟" →
  intent=expense_total, period="all"

"كم صرفنا هالأسبوع؟" →
  intent=expense_total, period="week"

"كم الربح هذا الشهر؟" →
  intent=profit, period="month"

"كم دخلنا من البيض؟" →
  intent=category_total, direction="in", category="بيض", period="all"

"كم صرفنا على الأعلاف هالشهر؟" →
  intent=category_total, direction="out", category="أعلاف", period="month"

أي كلام عام مثل: كيفك؟ شو تسوي؟ → intent=clarify
"""

def detect_intent(text: str) -> dict:
    try:
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": text},
            ],
        )
        return json.loads(completion.choices[0].message.content)
    except Exception:
        return {"intent": "clarify"}


# ────────────── MAIN HANDLER ──────────────
class handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self._ok()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length).decode()
            update = json.loads(body)
        except Exception:
            self._ok()
            return

        msg = update.get("message")
        if not msg or "text" not in msg:
            self._ok()
            return

        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text    = msg["text"].strip()

        if user_id not in ALLOWED_USERS:
            send(chat_id, "⛔ هذا البوت خاص.")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]

        # Sheets
        try:
            svc  = sheets_svc()
            data = load_transactions(svc)
        except Exception as e:
            send(chat_id, f"{D}\nخطأ في Google Sheets:\n{e}\n{D}")
            self._ok()
            return

        d      = detect_intent(text)
        intent = d.get("intent", "clarify")
        period = d.get("period", "month")

        # 1) تسجيل دخل
        if intent == "add_income":
            item     = d.get("item") or d.get("category") or "عملية دخل"
            amount   = d.get("amount", 0)
            category = d.get("category") or item

            if not amount:
                send(chat_id, "حدد المبلغ.")
            else:
                add_transaction(svc, "دخل", item, category, amount, user_name)
                inc, exp = totals_all(load_transactions(svc))
                send(
                    chat_id,
                    f"{D}\nتم تسجيل دخل:\n"
                    f"البند: {item}\n"
                    f"التصنيف: {category}\n"
                    f"المبلغ: {fmt(amount)}\n"
                    f"{D}\nإجمالي الدخل: {fmt(inc)}"
                )

        # 2) تسجيل صرف
        elif intent == "add_expense":
            item     = d.get("item") or d.get("category") or "عملية صرف"
            amount   = d.get("amount", 0)
            category = d.get("category") or item

            if not amount:
                send(chat_id, "حدد المبلغ.")
            else:
                add_transaction(svc, "صرف", item, category, amount, user_name)
                inc, exp = totals_all(load_transactions(svc))
                warn = "\n⚠️ المصروفات أعلى من الدخل." if exp > inc else ""
                send(
                    chat_id,
                    f"{D}\nتم تسجيل صرف:\n"
                    f"البند: {item}\n"
                    f"التصنيف: {category}\n"
                    f"المبلغ: {fmt(amount)}\n"
                    f"{D}\nإجمالي المصروفات: {fmt(exp)}{warn}"
                )

        # 3) إجمالي الدخل
        elif intent == "income_total":
            inc, exp, _ = totals_period(data, period)
            label = period_label(period)
            send(chat_id, f"{D}\nإجمالي الدخل ({label}): {fmt(inc)} د.إ\n{D}")

        # 4) إجمالي المصروف
        elif intent == "expense_total":
            inc, exp, _ = totals_period(data, period)
            label = period_label(period)
            send(chat_id, f"{D}\nإجمالي المصروفات ({label}): {fmt(exp)} د.إ\n{D}")

        # 5) الربح / الصافي
        elif intent == "profit":
            inc, exp, _ = totals_period(data, period)
            label = period_label(period)
            net = inc - exp
            emoji = "📈" if net >= 0 else "📉"
            send(
                chat_id,
                f"{D}\nصافي الربح ({label}):\n"
                f"الدخل: {fmt(inc)}\n"
                f"المصروف: {fmt(exp)}\n"
                f"{emoji} الصافي: {fmt(net)}\n{D}"
            )

        # 6) آخر العمليات
        elif intent == "last_transactions":
            recent = sorted(data, key=lambda x: x["date"], reverse=True)[:5]
            if not recent:
                send(chat_id, "لا توجد عمليات مسجلة.")
            else:
                lines = [D, "آخر العمليات:"]
                for r in recent:
                    lines.append(
                        f"التاريخ: {r['date']}\n"
                        f"النوع: {r['type']}\n"
                        f"البند: {r['item']}\n"
                        f"التصنيف: {r['category']}\n"
                        f"المبلغ: {fmt(r['amount'])}\n"
                        f"المستخدم: {r['user']}\n{D}"
                    )
                send(chat_id, "\n".join(lines))

        # 7) إجمالي تصنيف معيّن
        elif intent == "category_total":
            category = d.get("category", "").strip()
            direction = d.get("direction", "none")
            if not category:
                send(chat_id, "حدد التصنيف (مثال: البيض، الأعلاف).")
            else:
                _, _, filtered = totals_period(data, period)
                rows = [r for r in filtered if r["category"] == category]
                if direction == "in":
                    rows = [r for r in rows if r["type"] == "دخل"]
                elif direction == "out":
                    rows = [r for r in rows if r["type"] == "صرف"]
                total = sum(r["amount"] for r in rows)
                label = period_label(period)
                kind_text = "الدخل" if direction == "in" else ("المصروف" if direction == "out" else "الإجمالي")
                send(
                    chat_id,
                    f"{D}\n{kind_text} من {category} ({label}): {fmt(total)} د.إ\n{D}"
                )

        # 8) أي شيء آخر → Smalltalk مع ChatGPT
        else:
            try:
                completion = openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    temperature=0.5,
                    messages=[
                        {
                            "role": "system",
                            "content": "أنت مساعد لإدارة عزبة. جاوب باختصار وبساطة، بدون نصائح كثيرة، وبنفس أسلوب المستخدم تقريباً."
                        },
                        {"role": "user", "content": text},
                    ],
                )
                reply = completion.choices[0].message.content.strip()
                send(chat_id, f"{D}\n{reply}\n{D}")
            except Exception:
                send(chat_id, f"{D}\nما فهمت، حاول تعيد صياغة الجملة أو اكتبها أوضح.\n{D}")

        self._ok()

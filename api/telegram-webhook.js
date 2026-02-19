"""
Ezba (Farm) Telegram Bot – AI Enhanced Version
Same structure, improved understanding
"""

from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta, date
import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ── ENV ────────────────────────────────────────────────────────
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
S_INVENTORY    = "Inventory"
S_PENDING      = "Pending"

D = "──────────────"


# ── TELEGRAM ──────────────────────────────────────────────────
def send(chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )


# ── GOOGLE SHEETS ─────────────────────────────────────────────
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


# ── TRANSACTIONS ──────────────────────────────────────────────
def load_transactions(svc):
    rows = read_sheet(svc, S_TRANSACTIONS)
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        try:
            out.append({
                "date":     r[0],
                "type":     r[1],
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


def totals_all(data):
    inc = sum(x["amount"] for x in data if x["type"] == "دخل")
    exp = sum(x["amount"] for x in data if x["type"] == "صرف")
    return inc, exp


# ── PERIOD & CATEGORY HELPERS ─────────────────────────────────
def parse_tx_date(s: str):
    """Parse 'YYYY-MM-DD HH:MM' into datetime in UAE timezone."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UAE_TZ)
    except Exception:
        return None


def filter_transactions_by_period(transactions, period: str):
    period = (period or "all").lower()
    if period not in ("today", "week", "month"):
        return transactions

    now = datetime.now(UAE_TZ)
    today = now.date()

    if period == "today":
        start = datetime(today.year, today.month, today.day, tzinfo=UAE_TZ)
    elif period == "week":
        start = now - timedelta(days=7)
    else:  # "month"
        start = datetime(today.year, today.month, 1, tzinfo=UAE_TZ)

    out = []
    for tx in transactions:
        dt = parse_tx_date(tx.get("date", ""))
        if dt and dt >= start:
            out.append(tx)
    return out


def totals_for_period(transactions, period: str):
    txs = filter_transactions_by_period(transactions, period)
    inc = sum(x["amount"] for x in txs if x["type"] == "دخل")
    exp = sum(x["amount"] for x in txs if x["type"] == "صرف")
    return inc, exp, txs


def period_label(period: str):
    mapping = {
        "today": "اليوم",
        "week": "آخر ٧ أيام",
        "month": "هذا الشهر",
        "all": "كل الفترات",
    }
    return mapping.get((period or "all").lower(), "كل الفترات")


def category_total_for_period(transactions, category: str, period: str):
    if not category:
        return 0
    txs = filter_transactions_by_period(transactions, period)
    cat_lower = category.strip().lower()
    total = 0
    for tx in txs:
        cat = (tx.get("category") or "").strip().lower()
        item = (tx.get("item") or "").strip().lower()
        if cat == cat_lower or item == cat_lower:
            total += tx.get("amount", 0)
    return total


# ── AI INTENT DETECTION ───────────────────────────────────────
SYSTEM_PROMPT = """
أنت مدير مالي ذكي لعزبة (مزرعة صغيرة).

مهمتك:
- فهم جمل المستخدم حتى لو كانت عامية أو ناقصة.
- تحديد هل هو:
  • تسجيل عملية (دخل / صرف)
  • طلب تقرير / مجموعات
  • سؤال عن فئة معيّنة
  • كلام عابر (سلام، مزاح، أسئلة عامة)
- ترجع JSON صالح فقط بدون أي نص آخر.

صيغة JSON (نفس المفاتيح دائمًا):

{
  "intent": "add_income | add_expense | income_total | expense_total | profit | last_transactions | category_total | smalltalk | clarify",
  "direction": "in | out | none",
  "item": "",
  "category": "",
  "amount": 0,
  "period": "today | week | month | all"
}

تعريف الحقول:

- intent:
  • add_income      → المستخدم يريد تسجيل دخل
  • add_expense     → المستخدم يريد تسجيل صرف
  • income_total    → يسأل عن إجمالي الدخل
  • expense_total   → يسأل عن إجمالي المصروف
  • profit          → يسأل عن الربح (الدخل - المصروف)
  • last_transactions → يسأل عن آخر العمليات
  • category_total  → يسأل عن مجموع فئة معيّنة (مثال: علف، ماعز، كهرباء)
  • smalltalk       → سلام، شكر، مزاح، أسئلة عامة غير مالية
  • clarify         → طلب غير واضح لتسجيل أو تقرير (ينقصه مبلغ أو نوع أو معنى)

- direction:
  • "in"  مع الدخل (add_income)
  • "out" مع الصرف (add_expense)
  • "none" مع باقي الـ intents.

- amount:
  • رقم فقط بدون نص (مثال 120 أو 45.5).
  • إذا لم يذكر المستخدم مبلغًا → 0.

- category:
  • مثلاً: "علف", "ماعز", "كهرباء".
  • إذا لم يذكر تصنيف واضح استخدم "".

- period:
  • today  → اليوم فقط
  • week   → آخر ٧ أيام
  • month  → هذا الشهر
  • all    → كل الفترات
  • اختر القيمة الأنسب حسب كلام المستخدم:
    - "اليوم", "قبل شوي" → today
    - "هالأسبوع", "آخر كم يوم" → week
    - "هذا الشهر", "هالشهر" → month
    - "من البداية", "كل شيء" → all

إذا كانت الجملة ليست تسجيلًا ولا تقريرًا بل مجرد كلام عادي → intent = "smalltalk".
إذا كانت الجملة عن المال ولكن لا يمكن تنفيذها (مجهولة جدًا/ناقصة) → intent = "clarify".
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


# ── MAIN HANDLER ──────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        # Silence logs (Vercel)
        pass

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        # Health check
        self._ok()

    def do_POST(self):
        # Parse Telegram update
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

        chat_id  = msg["chat"]["id"]
        user_id  = msg["from"]["id"]
        text     = msg["text"].strip()

        # Auth
        if user_id not in ALLOWED_USERS:
            send(chat_id, "⛔ غير مصرح.")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]

        # Load data from Sheets
        try:
            svc  = sheets_svc()
            data = load_transactions(svc)
        except Exception as e:
            send(chat_id, f"❌ خطأ في Google Sheets:\n{e}")
            self._ok()
            return

        # Detect intent
        d      = detect_intent(text)
        intent = d.get("intent", "clarify")
        period = d.get("period", "all")

        # تسجيل دخل
        if intent == "add_income":
            item = d.get("item")
            amount = d.get("amount")
            category = d.get("category") or item
            if item and amount:
                add_transaction(svc, "دخل", item, category, amount, user_name)
                inc, exp = totals_all(load_transactions(svc))
                send(
                    chat_id,
                    f"{D}\nدخل مسجل: {item}\nالمبلغ: {fmt(amount)}\n"
                    f"{D}\nإجمالي الدخل: {fmt(inc)}"
                )
            else:
                send(chat_id, "حدد البند والمبلغ.")

        # تسجيل صرف
        elif intent == "add_expense":
            item = d.get("item")
            amount = d.get("amount")
            category = d.get("category") or item
            if item and amount:
                add_transaction(svc, "صرف", item, category, amount, user_name)
                inc, exp = totals_all(load_transactions(svc))
                warn = "\n⚠️ المصروفات أعلى من الدخل." if exp > inc else ""
                send(
                    chat_id,
                    f"{D}\nصرف مسجل: {item}\nالمبلغ: {fmt(amount)}\n"
                    f"{D}\nإجمالي المصروفات: {fmt(exp)}{warn}"
                )
            else:
                send(chat_id, "حدد البند والمبلغ.")

        # إجمالي الدخل لفترة
        elif intent == "income_total":
            inc, exp, _ = totals_for_period(data, period)
            send(
                chat_id,
                f"{D}\nإجمالي الدخل ({period_label(period)}): {fmt(inc)}\n{D}"
            )

        # إجمالي المصروف لفترة
        elif intent == "expense_total":
            inc, exp, _ = totals_for_period(data, period)
            send(
                chat_id,
                f"{D}\nإجمالي المصروف ({period_label(period)}): {fmt(exp)}\n{D}"
            )

        # الربح (الدخل - المصروف) لفترة
        elif intent == "profit":
            inc, exp, _ = totals_for_period(data, period)
            net = inc - exp
            send(
                chat_id,
                f"{D}\nالفترة: {period_label(period)}\n"
                f"الدخل: {fmt(inc)}\nالمصروف: {fmt(exp)}\nالصافي: {fmt(net)}\n{D}"
            )

        # إجمالي فئة معيّنة
        elif intent == "category_total":
            category = d.get("category") or d.get("item")
            if not category:
                send(chat_id, "حدد التصنيف أو البند المطلوب (مثال: العلف، الماعز).")
            else:
                total = category_total_for_period(data, category, period)
                send(
                    chat_id,
                    f"{D}\nإجمالي {category} ({period_label(period)}): {fmt(total)}\n{D}"
                )

        # آخر العمليات (مع فترة)
        elif intent == "last_transactions":
            _, _, txs = totals_for_period(data, period)
            recent = sorted(txs, key=lambda x: x["date"], reverse=True)[:5]
            if not recent:
                send(chat_id, f"{D}\nلا توجد عمليات في {period_label(period)}.\n{D}")
            else:
                lines = [D, f"آخر العمليات ({period_label(period)})"]
                for r in recent:
                    lines.append(
                        f"{r['date']} | {r['type']} | {r['item']} | {fmt(r['amount'])}"
                    )
                lines.append(D)
                send(chat_id, "\n".join(lines))

        # كلام عابر / مزاح
        elif intent == "smalltalk":
            try:
                completion = openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    temperature=0.5,
                    messages=[
                        {
                            "role": "system",
                            "content": "أنت مساعد إدارة عزبة ودود. أجب باختصار وبأسلوب المستخدم."
                        },
                        {"role": "user", "content": text},
                    ],
                )
                reply = completion.choices[0].message.content.strip()
                send(chat_id, f"{D}\n{reply}\n{D}")
            except Exception:
                send(chat_id, f"{D}\nحصل خطأ بسيط، جرّب تعيد الرسالة.\n{D}")

        # طلب غير واضح
        elif intent == "clarify":
            send(
                chat_id,
                f"{D}\nما فهمت طلبك بالضبط 🤔\n"
                "اكتب مثلاً:\n"
                "- سجل دخل ٢٠٠ من بيع ماعز\n"
                "- كم صرفنا على العلف هذا الشهر؟\n"
                "- عطنا ربح الأسبوع\n"
                f"{D}"
            )

        # أي شيء غريب جدًا → fallback بسيط
        else:
            send(chat_id, f"{D}\nما فهمت، جرّب صيغة أبسط.\n{D}")

        self._ok()

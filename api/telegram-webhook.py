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
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY")
GOOGLE_SA_JSON     = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID     = os.environ.get("SPREADSHEET_ID")

# ── Users ──────────────────────────────────────────────────────────
# role: "admin" → كل الصلاحيات | "viewer" → تقارير فقط، لا إضافة
USERS = {
    47329648:   {"name": "Khaled", "role": "admin"},
    6894180427: {"name": "Hamad",  "role": "admin"},
}

# ── Alerts ────────────────────────────────────────────────────────
# لو إجمالي المصروف الشهري تجاوز هذا الرقم → تنبيه لكل الـ admins
MONTHLY_EXPENSE_ALERT_THRESHOLD = float(os.environ.get("EXPENSE_ALERT", "10000"))

UAE_TZ        = timezone(timedelta(hours=4))
DIVIDER       = "────────────"
HISTORY_LIMIT = 50

openai_client = OpenAI(api_key=OPENAI_API_KEY)


# ─────────────────────────────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────────────────────────────

def send(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception as e:
        log.error(f"Telegram send error: {e}")


def broadcast_admins(text):
    """أرسل رسالة لكل المستخدمين من نوع admin."""
    for uid, info in USERS.items():
        if info["role"] == "admin":
            send(uid, text)


# ─────────────────────────────────────────────────────────────────
# Google Sheets
# ─────────────────────────────────────────────────────────────────

def get_service():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SA_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def load_transactions(service):
    """تحميل كل العمليات. تُرجع قائمة فارغة عند الخطأ."""
    try:
        res = (
            service.spreadsheets().values()
            .get(spreadsheetId=SPREADSHEET_ID, range="Transactions!A2:E")
            .execute()
        )
        rows = res.get("values", [])
    except Exception as e:
        log.error(f"load_transactions failed: {e}")
        return []

    data = []
    for r in rows:
        if len(r) < 4:
            continue
        data.append({
            "date":   r[0],
            "type":   r[1],
            "item":   r[2],
            "amount": r[3],
            "user":   r[4] if len(r) > 4 else "",
        })
    return data


def append_transaction(service, kind, item, amount, user):
    """حفظ عملية جديدة. تُرجع True عند النجاح."""
    try:
        ts = datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Transactions!A1:E1",
            valueInputOption="USER_ENTERED",
            body={"values": [[ts, kind, item, amount, user]]},
        ).execute()
        log.info(f"Saved → {kind} | {item} | {amount} | {user}")
        return True
    except Exception as e:
        log.error(f"append_transaction failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────
# Aggregation — الحسابات تتم محلياً، لا نثق بأرقام الـ AI
# ─────────────────────────────────────────────────────────────────

def now_uae():
    return datetime.now(UAE_TZ)


def parse_amount(val):
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _parse_date(date_str):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str[:16], fmt).date()
        except ValueError:
            continue
    return datetime.min.date()


def filter_by_period(transactions, period):
    today = now_uae().date()

    if period == "all":
        return transactions

    if period == "today":
        key = today.isoformat()[:10]
        return [t for t in transactions if t["date"][:10] == key]

    if period == "this_week":
        week_start = today - timedelta(days=today.weekday())
        return [t for t in transactions if _parse_date(t["date"]) >= week_start]

    if period == "this_month":
        prefix = today.strftime("%Y-%m")
        return [t for t in transactions if t["date"].startswith(prefix)]

    if period == "last_month":
        first_this    = today.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        prefix        = last_month_end.strftime("%Y-%m")
        return [t for t in transactions if t["date"].startswith(prefix)]

    return transactions


def compute_totals(rows):
    income  = sum(parse_amount(r["amount"]) for r in rows if r["type"] == "دخل")
    expense = sum(parse_amount(r["amount"]) for r in rows if r["type"] == "صرف")
    return {"income": income, "expense": expense, "net": income - expense}


def fmt_amount(val):
    f = float(val)
    return f"{int(f):,}" if f.is_integer() else f"{f:,.2f}"


# ─────────────────────────────────────────────────────────────────
# Report builders
# ─────────────────────────────────────────────────────────────────

PERIOD_LABELS = {
    "today":      "اليوم",
    "this_week":  "هذا الأسبوع",
    "this_month": "هذا الشهر",
    "last_month": "الشهر الماضي",
    "all":        "الكل",
}


def build_report(transactions, period, label, show="all"):
    """
    show = "all"     → الدخل + المصروف + الصافي
    show = "income"  → الدخل فقط
    show = "expense" → المصروف فقط
    show = "net"     → الصافي فقط
    """
    rows = filter_by_period(transactions, period)
    tots = compute_totals(rows)
    sign = "+" if tots["net"] >= 0 else ""

    if show == "income":
        return (
            f"{DIVIDER}\n"
            f"إجمالي الدخل — {label}\n"
            f"{DIVIDER}\n"
            f"الدخل: {fmt_amount(tots['income'])} د.إ\n"
            f"{DIVIDER}"
        )

    if show == "expense":
        return (
            f"{DIVIDER}\n"
            f"إجمالي المصروف — {label}\n"
            f"{DIVIDER}\n"
            f"المصروف: {fmt_amount(tots['expense'])} د.إ\n"
            f"{DIVIDER}"
        )

    if show == "net":
        status = "✅ ربح" if tots["net"] >= 0 else "🔴 خسارة"
        return (
            f"{DIVIDER}\n"
            f"الصافي — {label}\n"
            f"{DIVIDER}\n"
            f"الدخل:    {fmt_amount(tots['income'])} د.إ\n"
            f"المصروف:  {fmt_amount(tots['expense'])} د.إ\n"
            f"الصافي:   {sign}{fmt_amount(tots['net'])} د.إ  {status}\n"
            f"{DIVIDER}"
        )

    # show == "all" → التقرير الكامل
    sign = "+" if tots["net"] >= 0 else ""
    return (
        f"{DIVIDER}\n"
        f"تقرير {label}\n"
        f"{DIVIDER}\n"
        f"الدخل:     {fmt_amount(tots['income'])} د.إ\n"
        f"المصروف:   {fmt_amount(tots['expense'])} د.إ\n"
        f"الصافي:    {sign}{fmt_amount(tots['net'])} د.إ\n"
        f"{DIVIDER}"
    )


def build_details(transactions, period, label, tx_filter="all", limit=10):
    rows = filter_by_period(transactions, period)
    if tx_filter == "دخل":
        rows = [r for r in rows if r["type"] == "دخل"]
    elif tx_filter == "صرف":
        rows = [r for r in rows if r["type"] == "صرف"]

    rows = list(reversed(rows))[:limit]

    if not rows:
        return f"{DIVIDER}\nلا توجد عمليات مسجلة في هذه الفترة.\n{DIVIDER}"

    lines = [DIVIDER, f"آخر {len(rows)} عملية — {label}", DIVIDER]
    for i, r in enumerate(rows, 1):
        t_label = "✅ دخل" if r["type"] == "دخل" else "🔴 صرف"
        lines.append(
            f"{i}. {r['date'][:10]} | {t_label} | {r['item']} | {fmt_amount(r['amount'])} د.إ"
        )
    lines.append(DIVIDER)
    return "\n".join(lines)


def build_comparison(transactions, pa, la, pb, lb):
    t_a  = compute_totals(filter_by_period(transactions, pa))
    t_b  = compute_totals(filter_by_period(transactions, pb))
    diff = t_a["net"] - t_b["net"]
    sign = "+" if diff >= 0 else ""

    def block(label, t):
        return (
            f"الفترة: {label}\n"
            f"  الدخل:    {fmt_amount(t['income'])} د.إ\n"
            f"  المصروف:  {fmt_amount(t['expense'])} د.إ\n"
            f"  الصافي:   {fmt_amount(t['net'])} د.إ"
        )

    return (
        f"{DIVIDER}\n"
        f"مقارنة\n"
        f"{DIVIDER}\n"
        f"{block(la, t_a)}\n"
        f"{DIVIDER}\n"
        f"{block(lb, t_b)}\n"
        f"{DIVIDER}\n"
        f"فرق الصافي: {sign}{fmt_amount(diff)} د.إ\n"
        f"{DIVIDER}"
    )


# ─────────────────────────────────────────────────────────────────
# Alert engine
# ─────────────────────────────────────────────────────────────────

def check_expense_alert(transactions):
    monthly  = filter_by_period(transactions, "this_month")
    expense  = compute_totals(monthly)["expense"]
    if expense >= MONTHLY_EXPENSE_ALERT_THRESHOLD:
        broadcast_admins(
            f"⚠️ تنبيه: المصروف الشهري تجاوز الحد\n"
            f"{DIVIDER}\n"
            f"الإجمالي هذا الشهر: {fmt_amount(expense)} د.إ\n"
            f"الحد المحدد: {fmt_amount(MONTHLY_EXPENSE_ALERT_THRESHOLD)} د.إ\n"
            f"{DIVIDER}"
        )


# ─────────────────────────────────────────────────────────────────
# AI Engine — يحدد النية فقط، لا يحسب أرقاماً أبداً
# ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
أنت محلل نية (intent classifier) لبوت محاسبة عزبة في الإمارات.

مهمتك الوحيدة: تحليل رسالة المستخدم وإعادة JSON يصف نيته بدقة.
لا تحسب أرقاماً. لا تنشئ تقارير. الكود سيتولى ذلك.

قواعد صارمة:
- أعد JSON فقط. لا نص خارجه. لا Markdown. لا ```.
- لا تخترع أرقاماً أبداً.
- اللغة العربية بكل لهجاتها مدعومة (خليجي، مصري، فصحى، عامية).

──────────────────────────────────────────
الأنواع الممكنة:
──────────────────────────────────────────

1. عملية مالية:
{"intent":"transaction","type":"دخل|صرف","item":"اسم البند","amount":<رقم>,"date":"اليوم|أمس|<تاريخ>"}

2. تقرير مالي — حقل show مهم جداً:
{"intent":"report","period":"today|this_week|this_month|last_month|all","show":"income|expense|net|all"}

   show = "income"  → لما يسأل عن الدخل أو المبيعات أو الإيرادات فقط
   show = "expense" → لما يسأل عن المصروف أو الإنفاق أو المدفوعات فقط
   show = "net"     → لما يسأل عن الصافي أو الربح أو هل هو في خسارة أو ربح
   show = "all"     → لما يطلب تقرير كامل أو ملخص شامل

3. تفاصيل عمليات:
{"intent":"details","period":"today|this_week|this_month|last_month|all","filter":"all|دخل|صرف","limit":<عدد أو null>}

4. مقارنة:
{"intent":"comparison","period_a":"this_week|this_month|last_month|all","period_b":"this_week|this_month|last_month|all"}

5. ملخص أسبوعي:
{"intent":"weekly_summary"}

6. ملخص شهري:
{"intent":"monthly_summary"}

7. محادثة عادية:
{"intent":"conversation","reply":"<رد مختصر رسمي>"}

──────────────────────────────────────────
أمثلة دقيقة لحقل show — ادرسها جيداً:
──────────────────────────────────────────

"كم صرفنا هالشهر؟"              → report / this_month / show=expense
"قديش صرفنا؟"                   → report / all / show=expense
"كم مصروفنا هذا الأسبوع؟"       → report / this_week / show=expense
"شو مجموع المصروف؟"             → report / all / show=expense
"كم جبنا هالشهر؟"               → report / this_month / show=income
"قديش دخلنا اليوم؟"             → report / today / show=income
"كم الإيرادات هذا الشهر؟"       → report / this_month / show=income
"شو مجموع المبيعات؟"            → report / all / show=income
"هل نحن في ربح أو خسارة؟"       → report / all / show=net
"وين وصلنا؟"                    → report / this_month / show=net
"كم الصافي هالشهر؟"             → report / this_month / show=net
"شو وضعنا المالي؟"              → report / this_month / show=net
"عطني تقرير كامل"               → report / this_month / show=all
"ملخص هالشهر"                   → monthly_summary
"ملخص الأسبوع"                  → weekly_summary
"بعنا قمح بـ 3000"              → transaction / دخل
"دفعنا فاتورة كهرباء 500"       → transaction / صرف
"آخر 5 عمليات"                  → details / limit=5
"قارن هالأسبوع بالشهر الماضي"   → comparison
"صباح الخير"                    → conversation
""".strip()


def ask_ai(user_text):
    """يسأل الـ AI عن النية فقط. يُرجع dict آمن دائماً."""
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            max_tokens=250,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_text},
            ],
        )
        raw = resp.choices[0].message.content or ""
        log.info(f"AI raw: {raw[:200]}")
        return _parse(raw)
    except Exception as e:
        log.error(f"OpenAI error: {e}")
        return _fallback("حدث خطأ في الاتصال بالذكاء الاصطناعي.")


def _parse(raw):
    text = raw.strip()
    if "```" in text:
        text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("```")).strip()

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}") + 1
        if s != -1 and e > s:
            try:
                data = json.loads(text[s:e])
            except json.JSONDecodeError:
                pass

    if data is None:
        log.warning(f"JSON parse failed: {text[:150]}")
        return _fallback("لم أستطع فهم الرسالة. يرجى إعادة الصياغة.")
    return _validate(data)


def _validate(data):
    intent = data.get("intent")

    if intent == "transaction":
        if not data.get("type") or not data.get("item"):
            return _fallback("بيانات العملية غير مكتملة.")
        try:
            data["amount"] = abs(float(data["amount"]))
        except (ValueError, TypeError):
            return _fallback("المبلغ غير صالح. يرجى إدخال رقم.")
        if data["type"] not in ("دخل", "صرف"):
            return _fallback("نوع العملية غير معروف.")
        return data

    if intent in ("report", "details", "comparison",
                  "weekly_summary", "monthly_summary", "conversation"):
        return data

    return _fallback("لم أفهم المطلوب.")


def _fallback(msg):
    return {"intent": "conversation", "reply": msg}


# ─────────────────────────────────────────────────────────────────
# Reply builder
# ─────────────────────────────────────────────────────────────────

def build_reply(intent_data, transactions, user_name, service):
    intent = intent_data.get("intent")

    if intent == "transaction":
        kind   = intent_data["type"]
        item   = intent_data["item"]
        amount = intent_data["amount"]
        date   = intent_data.get("date", "اليوم")

        if not append_transaction(service, kind, item, amount, user_name):
            return "⚠️ حدث خطأ أثناء الحفظ. يرجى المحاولة مرة أخرى."

        # تحقق من التنبيهات بعد الحفظ مباشرة
        if kind == "صرف":
            check_expense_alert(load_transactions(service))

        type_label = "✅ دخل" if kind == "دخل" else "🔴 صرف"
        return (
            f"{DIVIDER}\n"
            f"تم التسجيل\n"
            f"{DIVIDER}\n"
            f"التاريخ:    {date}\n"
            f"النوع:      {type_label}\n"
            f"البند:      {item}\n"
            f"المبلغ:     {fmt_amount(amount)} د.إ\n"
            f"المستخدم:   {user_name}\n"
            f"{DIVIDER}"
        )

    if intent == "report":
        period = intent_data.get("period", "all")
        show   = intent_data.get("show", "all")
        return build_report(transactions, period, PERIOD_LABELS.get(period, period), show)

    if intent == "details":
        period = intent_data.get("period", "all")
        fltr   = intent_data.get("filter", "all")
        try:
            limit = int(intent_data.get("limit") or 10)
        except (ValueError, TypeError):
            limit = 10
        return build_details(transactions, period, PERIOD_LABELS.get(period, period), fltr, limit)

    if intent == "comparison":
        pa = intent_data.get("period_a", "this_month")
        pb = intent_data.get("period_b", "last_month")
        return build_comparison(
            transactions,
            pa, PERIOD_LABELS.get(pa, pa),
            pb, PERIOD_LABELS.get(pb, pb),
        )

    if intent == "weekly_summary":
        return build_report(transactions, "this_week", "الأسبوع الحالي", show="all")

    if intent == "monthly_summary":
        return build_report(transactions, "this_month", "الشهر الحالي", show="all")

    if intent == "conversation":
        return intent_data.get("reply", "أنا هنا للمساعدة.")

    return "لم أفهم المطلوب."


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
            body   = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            update = json.loads(body)
        except Exception as e:
            log.error(f"Bad request: {e}")
            self._ok()
            return

        msg = update.get("message")
        if not msg or "text" not in msg:
            self._ok()
            return

        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text    = msg["text"].strip()

        user_info = USERS.get(user_id)
        if not user_info:
            send(chat_id, "غير مصرح.")
            self._ok()
            return

        user_name = user_info["name"]
        user_role = user_info["role"]

        try:
            service      = get_service()
            transactions = load_transactions(service)
        except Exception as e:
            log.error(f"Sheets failed: {e}")
            send(chat_id, "⚠️ تعذّر الاتصال بقاعدة البيانات.")
            self._ok()
            return

        intent_data = ask_ai(text)

        # viewer لا يستطيع إضافة عمليات
        if intent_data.get("intent") == "transaction" and user_role != "admin":
            send(chat_id, "⛔ ليس لديك صلاحية إضافة عمليات.")
            self._ok()
            return

        reply = build_reply(intent_data, transactions, user_name, service)
        send(chat_id, reply)
        self._ok()

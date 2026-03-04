"""
Ezba (Farm) Telegram Bot  –  Vercel Python Serverless
======================================================
Google Sheets layout (3 tabs):
  Transactions : A=التاريخ | B=النوع(دخل/صرف) | C=البند | D=التصنيف | E=المبلغ | F=المستخدم
  Inventory    : A=Item | B=Type | C=Quantity | D=Notes
  Pending      : A=UserId | B=Timestamp | C=OperationType | D=Action | E=Item | F=Amount | G=Quantity | H=Person | I=NotesOrSnapshotJson
"""

from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta
import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ── ENV ────────────────────────────────────────────────────────────────────────
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

# ── SHEET NAMES & RANGES ───────────────────────────────────────────────────────
S_TRANSACTIONS = "Transactions"   # A=date B=type C=item D=category E=amount F=user
S_INVENTORY    = "Inventory"      # A=Item B=Type C=Quantity D=Notes
S_PENDING      = "Pending"        # A=UserId B=Timestamp C=OperationType D=Action E=Item F=Amount G=Quantity H=Person I=NotesOrSnapshotJson

# ── TELEGRAM ───────────────────────────────────────────────────────────────────
def send(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception:
        pass

# ── GOOGLE SHEETS ──────────────────────────────────────────────────────────────
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

def update_inventory(svc, item_name: str, qty_delta: int, item_type: str = "", notes: str = ""):
    rows = read_sheet(svc, S_INVENTORY)
    for i, r in enumerate(rows):
        if r and r[0].strip() == item_name.strip():
            old_qty = int(r[2]) if len(r) > 2 and r[2] else 0
            new_qty = max(0, old_qty + qty_delta)
            row_num = i + 2
            svc.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{S_INVENTORY}!C{row_num}",
                valueInputOption="USER_ENTERED",
                body={"values": [[new_qty]]},
            ).execute()
            return
    append_row(svc, S_INVENTORY, [item_name, item_type, max(0, qty_delta), notes])

# ── UTILS ──────────────────────────────────────────────────────────────────────
def now_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")

def today_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d")

def cur_month():
    return datetime.now(UAE_TZ).strftime("%Y-%m")

def fmt(x):
    try:
        f = float(x)
        return int(f) if f.is_integer() else round(f, 2)
    except Exception:
        return x

D = "──────────────"   # divider

def _norm_ar(s: str) -> str:
    if not isinstance(s, str):
        return ""
    t = s.strip()
    t = (t.replace("أ", "ا")
           .replace("إ", "ا")
           .replace("آ", "ا")
           .replace("ة", "ه")
           .replace("ى", "ي"))
    return t

def _norm_token(s: str) -> str:
    """Normalize and remove common filler words so 'بيع البيض' و 'بيض' ينحسبون نفس الشي."""
    t = _norm_ar(s)
    for w in ["ال", " ", "بيع", "شراء", "دخل", "صرف", "من", "على"]:
        t = t.replace(w, "")
    return t

def period_label(p: str) -> str:
    return {
        "today": "اليوم",
        "week": "هذا الاسبوع",
        "month": "هذا الشهر",
        "all": "لكل الفترة المسجلة",
    }.get(p, "هذه الفترة")

def filter_by_period(data, period: str):
    if period == "all":
        return data
    today = datetime.now(UAE_TZ).date()
    if period == "today":
        s = today.strftime("%Y-%m-%d")
        return [x for x in data if x["date"].startswith(s)]
    if period == "week":
        start = today - timedelta(days=6)
        out = []
        for x in data:
            try:
                d = datetime.strptime(x["date"][:10], "%Y-%m-%d").date()
            except Exception:
                continue
            if start <= d <= today:
                out.append(x)
        return out
    # default: month
    pref = cur_month()
    return [x for x in data if x["date"].startswith(pref)]

def match_category(tx, category: str) -> bool:
    """رجّع True لو العملية تخص التصنيف (بيض، أعلاف، ...)."""
    term = _norm_token(category)
    if not term:
        return False
    in_cat = term in _norm_token(tx.get("category", ""))
    in_item = term in _norm_token(tx.get("item", ""))
    return in_cat or in_item

# ── TRANSACTIONS HELPERS ───────────────────────────────────────────────────────
def load_transactions(svc):
    rows = read_sheet(svc, S_TRANSACTIONS)
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        try:
            out.append({
                "date":     r[0],
                "type":     r[1],          # دخل | صرف
                "item":     r[2],
                "category": r[3] if len(r) > 3 else "",
                "amount":   float(r[4]),
                "user":     r[5] if len(r) > 5 else "",
            })
        except (ValueError, IndexError):
            continue
    return out

def add_transaction(svc, kind, item, category, amount, user):
    append_row(svc, S_TRANSACTIONS, [now_str(), kind, item, category, amount, user])

def totals_all(data):
    inc = sum(x["amount"] for x in data if x["type"] == "دخل")
    exp = sum(x["amount"] for x in data if x["type"] == "صرف")
    return inc, exp

def totals_month(data):
    m = cur_month()
    filtered = [x for x in data if x["date"].startswith(m)]
    return totals_all(filtered)

# ── INVENTORY HELPERS ──────────────────────────────────────────────────────────
def load_inventory(svc):
    rows = read_sheet(svc, S_INVENTORY)
    out = []
    for r in rows:
        if r and r[0]:
            out.append({
                "item":  r[0],
                "type":  r[1] if len(r) > 1 else "",
                "qty":   int(r[2]) if len(r) > 2 and r[2] else 0,
                "notes": r[3] if len(r) > 3 else "",
            })
    return out

# ── PENDING HELPERS ────────────────────────────────────────────────────────────
def add_pending(svc, user_id, op_type, action, item, amount, qty, person, notes=""):
    append_row(svc, S_PENDING, [
        str(user_id), now_str(), op_type, action, item,
        amount, qty, person, notes
    ])

# ── AI INTENT ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
أنت مساعد ذكي لإدارة عزبة (مزرعة) في الإمارات.
المستخدم يرسل رسائل باللهجة الإماراتية أو العربية.
أرجع JSON فقط بدون أي نص إضافي:

{
  "intent": "<intent>",
  "direction": "in | out | none",
  "item": "",
  "category": "",
  "amount": 0,
  "quantity": 0,
  "animal_type": "",
  "gender": "ذكر | أنثى | مختلط | ",
  "worker_name": "",
  "role": "",
  "month": "",
  "period": "today | week | month | all"
}

الـ intents المتاحة:
- add_income        : تسجيل دخل / إيراد عام (مثل بعت بيض)
- add_expense       : تسجيل صرف / مصروف (مثل صرفنا على الأعلاف)
- add_livestock     : شراء مواشي (غنم، بقر، إبل...)
- sell_livestock    : بيع أو ذبح مواشي
- add_poultry       : شراء دواجن (دجاج، بط، حمام...)
- sell_poultry      : بيع دواجن
- pay_salary        : صرف راتب عامل
- income_total      : سؤال عن إجمالي الدخل (عام أو من بند محدد)
- expense_total     : سؤال عن إجمالي المصروف (عام أو من بند محدد)
- profit            : صافي الربح (الدخل - المصروف)
- inventory         : جرد المواشي / المخزون
- last_transactions : آخر العمليات
- category_total    : إجمالي حسب تصنيف معين (مثلاً الأعلاف)
- income_by_category: تقسيم الدخل على البنود (بيض، غنم...)
- daily_report      : تقرير مختصر عن اليوم والشهر
- smalltalk         : دردشة عامة لا علاقة لها بالحسابات
- clarify           : الرسالة غير واضحة

قواعد:
- أي بيع أو دخل → direction = "in".
- أي شراء أو دفع أو صرف أو فاتورة أو راتب → direction = "out".
- إذا كان السؤال مثل: "كم دخل البيض؟" أو "كم الدخل من البيض بس؟"
  → intent = "income_total",  category = "بيض".
- إذا كان السؤال مثل: "كم صرفنا على الأعلاف؟"
  → intent = "expense_total", category = "أعلاف".
- إذا قال: "قسم لي الدخل حسب البند" أو "قسم لي الدخل حسب الغرض"
  → intent = "income_by_category"  (واترك category فارغ).
- period:
  - افتراضي = "month" إلا إذا المستخدم ذكر اليوم أو الأسبوع أو كل الفترة.
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
    except Exception as e:
        return {"intent": "clarify", "_error": str(e)}

# ── HANDLERS ───────────────────────────────────────────────────────────────────

def h_add_income(svc, d, chat_id, user_name, user_id):
    item     = d.get("item", "")
    amount   = d.get("amount", 0)
    category = d.get("category") or item
    if not item or not amount:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: بعت بيض بـ 200")
        return
    add_transaction(svc, "دخل", item, category, amount, user_name)
    add_pending(svc, user_id, "income", "add", item, amount, 0, user_name)
    send(chat_id,
         f"{D}\n✅ دخل مسجل\n"
         f"البند: {item}\n"
         f"التصنيف: {category}\n"
         f"المبلغ: {fmt(amount)} د.إ\n"
         f"بواسطة: {user_name}\n"
         f"{D}")

def h_add_expense(svc, d, chat_id, user_name, user_id):
    item     = d.get("item", "")
    amount   = d.get("amount", 0)
    category = d.get("category") or item
    if not item or not amount:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: صرفنا على الأعلاف 800")
        return
    add_transaction(svc, "صرف", item, category, amount, user_name)
    add_pending(svc, user_id, "expense", "add", item, amount, 0, user_name)
    send(chat_id,
         f"{D}\n✅ صرف مسجل\n"
         f"البند: {item}\n"
         f"التصنيف: {category}\n"
         f"المبلغ: {fmt(amount)} د.إ\n"
         f"بواسطة: {user_name}\n"
         f"{D}")

def h_add_livestock(svc, d, chat_id, user_name, user_id):
    animal = d.get("animal_type") or d.get("item", "")
    qty    = int(d.get("quantity") or 1)
    cost   = d.get("amount", 0)
    gender = d.get("gender", "")
    if not animal:
        send(chat_id, "❌ حدد نوع الحيوان والعدد.")
        return

    update_inventory(svc, animal, qty, "مواشي", gender)

    if cost:
        add_transaction(svc, "صرف", f"شراء {qty} {animal}", "مواشي", cost, user_name)

    notes = json.dumps({"gender": gender, "cost_per_head": round(cost/qty, 1) if qty else 0}, ensure_ascii=False)
    add_pending(svc, user_id, "inventory", "buy_livestock", animal, cost, qty, user_name, notes)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == animal), qty)
    send(chat_id,
         f"{D}\n✅ تم إضافة المواشي\n"
         f"النوع: {animal}\n"
         f"العدد المضاف: {qty}\n"
         f"الجنس: {gender or '-'}\n"
         f"التكلفة الإجمالية: {fmt(cost)} د.إ\n"
         f"{D}\n"
         f"🐄 رصيد {animal} الحالي: {current_qty}")

def h_sell_livestock(svc, d, chat_id, user_name, user_id):
    animal  = d.get("animal_type") or d.get("item", "")
    qty     = int(d.get("quantity") or 1)
    price   = d.get("amount", 0)
    is_slaughter = any(w in (d.get("item") or "") for w in ["ذبح", "ذبيحه", "ذبيحة"])
    action_label = "ذبح" if is_slaughter else "بيع"

    if not animal:
        send(chat_id, "❌ حدد نوع الحيوان.")
        return

    update_inventory(svc, animal, -qty, "مواشي")

    if price and not is_slaughter:
        add_transaction(svc, "دخل", f"بيع {qty} {animal}", "مواشي", price, user_name)

    add_pending(svc, user_id, "inventory", f"{action_label}_livestock",
                animal, price, qty, user_name)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == animal), 0)
    send(chat_id,
         f"{D}\n✅ تم تسجيل {action_label}\n"
         f"الحيوان: {animal} × {qty}\n"
         f"السعر: {fmt(price)} د.إ\n"
         f"بواسطة: {user_name}\n"
         f"{D}\n"
         f"🐄 رصيد {animal} الحالي: {current_qty}")

def h_add_poultry(svc, d, chat_id, user_name, user_id):
    bird  = d.get("animal_type") or d.get("item", "دجاج")
    qty   = int(d.get("quantity") or 1)
    cost  = d.get("amount", 0)

    update_inventory(svc, bird, qty, "دواجن")
    if cost:
        add_transaction(svc, "صرف", f"شراء {qty} {bird}", "دواجن", cost, user_name)
    add_pending(svc, user_id, "inventory", "buy_poultry", bird, cost, qty, user_name)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == bird), qty)
    send(chat_id,
         f"{D}\n✅ تم إضافة الدواجن\n"
         f"النوع: {bird} × {qty}\n"
         f"التكلفة: {fmt(cost)} د.إ\n"
         f"{D}\n"
         f"🐔 رصيد {bird} الحالي: {current_qty}")

def h_sell_poultry(svc, d, chat_id, user_name, user_id):
    bird  = d.get("animal_type") or d.get("item", "دجاج")
    qty   = int(d.get("quantity") or 1)
    price = d.get("amount", 0)

    update_inventory(svc, bird, -qty, "دواجن")
    if price:
        add_transaction(svc, "دخل", f"بيع {qty} {bird}", "دواجن", price, user_name)
    add_pending(svc, user_id, "inventory", "sell_poultry", bird, price, qty, user_name)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == bird), 0)
    send(chat_id,
         f"{D}\n✅ تم تسجيل البيع\n"
         f"الطير: {bird} × {qty}\n"
         f"السعر: {fmt(price)} د.إ\n"
         f"{D}\n"
         f"🐔 رصيد {bird} الحالي: {current_qty}")

def h_pay_salary(svc, d, chat_id, user_name, user_id):
    worker = d.get("worker_name") or d.get("item", "")
    amount = d.get("amount", 0)
    month  = d.get("month", "") or cur_month()
    if not worker or not amount:
        send(chat_id, "❌ حدد اسم العامل والمبلغ.\nمثال: راتب محمد 1500 شهر يناير")
        return
    add_transaction(svc, "صرف", f"راتب {worker}", "رواتب", amount, user_name)
    add_pending(svc, user_id, "labor", "pay_salary", worker, amount, 0, user_name,
                json.dumps({"month": month}, ensure_ascii=False))
    send(chat_id,
         f"{D}\n✅ تم صرف الراتب\n"
         f"العامل: {worker}\n"
         f"المبلغ: {fmt(amount)} د.إ\n"
         f"الشهر: {month}\n"
         f"بواسطة: {user_name}\n"
         f"{D}")

def h_profit(data, period, chat_id):
    rows = filter_by_period(data, period)
    inc = sum(x["amount"] for x in rows if x["type"] == "دخل")
    exp = sum(x["amount"] for x in rows if x["type"] == "صرف")
    net = inc - exp
    emoji = "📈" if net >= 0 else "📉"
    send(chat_id,
         f"{D}\n💰 ملخص {period_label(period)}\n"
         f"الدخل:   {fmt(inc)} د.إ\n"
         f"الصرف:   {fmt(exp)} د.إ\n"
         f"{emoji} الصافي: {fmt(net)} د.إ\n"
         f"{D}")

def h_inventory(svc, chat_id):
    inv = load_inventory(svc)
    if not inv:
        send(chat_id, "📋 الجرد فارغ حالياً.")
        return
    lines = [D, "📦 الجرد الحالي"]
    for x in inv:
        lines.append(f"  {x['item']} ({x['type'] or '-'}): {x['qty']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_last(data, chat_id):
    recent = sorted(data, key=lambda x: x["date"], reverse=True)[:7]
    if not recent:
        send(chat_id, "لا توجد عمليات مسجلة.")
        return
    lines = [D, "🕐 آخر العمليات"]
    for t in recent:
        sign = "+" if t["type"] == "دخل" else "-"
        lines.append(f"{t['date']}  {sign}{fmt(t['amount'])} د.إ  {t['item']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_category_total(data, d, chat_id):
    cat    = d.get("category", "")
    period = d.get("period", "month")
    if not cat:
        send(chat_id, "❌ حدد التصنيف أو البند.\nمثال: كم الدخل من البيض؟ أو كم صرفنا على الأعلاف؟")
        return
    rows = filter_by_period(data, period)
    total_inc = sum(x["amount"] for x in rows
                    if x["type"] == "دخل" and match_category(x, cat))
    total_exp = sum(x["amount"] for x in rows
                    if x["type"] == "صرف" and match_category(x, cat))
    if total_inc == 0 and total_exp == 0:
        send(chat_id, f"لا توجد عمليات مرتبطة بـ {cat}.")
        return
    label = period_label(period)
    lines = [D, f"📊 ملخص {cat} ({label})"]
    if total_inc:
        lines.append(f"الدخل: {fmt(total_inc)} د.إ")
    if total_exp:
        lines.append(f"المصروف: {fmt(total_exp)} د.إ")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_income_by_category(data, period, chat_id):
    rows = filter_by_period(data, period)
    groups = {}
    for tx in rows:
        if tx["type"] != "دخل":
            continue
        key = tx["category"] or tx["item"]
        groups[key] = groups.get(key, 0) + tx["amount"]
    if not groups:
        send(chat_id, "لا يوجد دخل في هذه الفترة.")
        return
    label = period_label(period)
    lines = [D, f"📊 الدخل حسب البند ({label})"]
    for key, total in sorted(groups.items(), key=lambda kv: -kv[1]):
        lines.append(f"{key}: {fmt(total)} د.إ")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_daily_report(svc, data, chat_id):
    today = today_str()
    t_data = [x for x in data if x["date"].startswith(today)]
    t_inc, t_exp = totals_all(t_data)

    m_inc, m_exp = totals_month(data)

    inv = load_inventory(svc)
    inv_lines = "  " + " | ".join(f"{x['item']}: {x['qty']}" for x in inv) if inv else "  لا يوجد"

    send(chat_id,
         f"{D}\n📋 التقرير اليومي — {today}\n{D}\n"
         f"📅 اليوم\n"
         f"  دخل: {fmt(t_inc)} | صرف: {fmt(t_exp)} | صافي: {fmt(t_inc-t_exp)}\n"
         f"{D}\n"
         f"📆 هذا الشهر\n"
         f"  دخل: {fmt(m_inc)} | صرف: {fmt(m_exp)} | صافي: {fmt(m_inc-m_exp)}\n"
         f"{D}\n"
         f"📦 الجرد الحالي\n{inv_lines}\n"
         f"{D}")

# ── HELP ───────────────────────────────────────────────────────────────────────
HELP = """
🌾 بوت العزبة – مثال على الأشياء اللي يقدر يسويها:

💰 تسجيل دخل:
  • بعت بيض بـ 200
  • وردة غنم 4699

📤 تسجيل صرف:
  • صرفنا على الأعلاف 800
  • دفعنا فاتورة كهرباء 350

🐄 مواشي:
  • اشترينا 10 عنم بـ 15000
  • بعنا 2 ثور بـ 8000
  • ذبحنا خروف

🐔 دواجن:
  • اشترينا 50 فروج بـ 1000
  • بعنا دجاج بـ 500

💵 رواتب:
  • راتب العامل 1400
  • راتب محمد 2000 شهر فبراير

📊 استعلامات:
  • كم دخل البيض؟
  • كم صرفنا على الأعلاف؟
  • كم الربح هذا الشهر؟
  • قسم لي الدخل حسب البند
  • آخر العمليات
  • تقرير اليوم
"""

# ── MAIN HANDLER ───────────────────────────────────────────────────────────────
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

        chat_id  = msg["chat"]["id"]
        user_id  = msg["from"]["id"]
        text     = msg["text"].strip()

        if user_id not in ALLOWED_USERS:
            send(chat_id, "⛔ غير مصرح.")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]

        if text in ("/start", "/help", "مساعدة", "help", "وش تسوي", "شو تسوي"):
            send(chat_id, HELP)
            self._ok()
            return

        try:
            svc  = sheets_svc()
            data = load_transactions(svc)
        except Exception as e:
            send(chat_id, f"❌ خطأ في Google Sheets:\n{e}")
            self._ok()
            return

        d      = detect_intent(text)
        intent = d.get("intent", "clarify")
        period = d.get("period", "month") or "month"

        # تسجيل العمليات
        if intent == "add_income":
            h_add_income(svc, d, chat_id, user_name, user_id)

        elif intent == "add_expense":
            h_add_expense(svc, d, chat_id, user_name, user_id)

        elif intent == "add_livestock":
            h_add_livestock(svc, d, chat_id, user_name, user_id)

        elif intent == "sell_livestock":
            h_sell_livestock(svc, d, chat_id, user_name, user_id)

        elif intent == "add_poultry":
            h_add_poultry(svc, d, chat_id, user_name, user_id)

        elif intent == "sell_poultry":
            h_sell_poultry(svc, d, chat_id, user_name, user_id)

        elif intent == "pay_salary":
            h_pay_salary(svc, d, chat_id, user_name, user_id)

        # إجمالي الدخل
        elif intent == "income_total":
            cat = (d.get("category") or "").strip()
            rows = filter_by_period(data, period if period != "all" else "all")
            if cat:
                total = sum(x["amount"] for x in rows
                            if x["type"] == "دخل" and match_category(x, cat))
                if total == 0:
                    send(chat_id, f"لا توجد عمليات دخل مرتبطة بـ {cat}.")
                else:
                    send(chat_id,
                         f"💰 الدخل من {cat} ({period_label(period)}): {fmt(total)} د.إ")
            else:
                inc = sum(x["amount"] for x in rows if x["type"] == "دخل")
                send(chat_id,
                     f"💰 الدخل ({period_label(period)}): {fmt(inc)} د.إ")

        # إجمالي المصروف
        elif intent == "expense_total":
            cat = (d.get("category") or "").strip()
            rows = filter_by_period(data, period if period != "all" else "all")
            if cat:
                total = sum(x["amount"] for x in rows
                            if x["type"] == "صرف" and match_category(x, cat))
                if total == 0:
                    send(chat_id, f"لا توجد مصروفات مرتبطة بـ {cat}.")
                else:
                    send(chat_id,
                         f"📤 المصروف على {cat} ({period_label(period)}): {fmt(total)} د.إ")
            else:
                exp = sum(x["amount"] for x in rows if x["type"] == "صرف")
                send(chat_id,
                     f"📤 المصروف ({period_label(period)}): {fmt(exp)} د.إ")

        elif intent == "profit":
            h_profit(data, period, chat_id)

        elif intent == "inventory":
            h_inventory(svc, chat_id)

        elif intent == "last_transactions":
            h_last(data, chat_id)

        elif intent == "income_by_category":
            h_income_by_category(data, period, chat_id)

        elif intent == "category_total":
            h_category_total(data, d, chat_id)

        elif intent == "daily_report":
            h_daily_report(svc, data, chat_id)

        elif intent == "smalltalk":
            # دردشة بسيطة – رد قصير بس
            send(chat_id, "👌 تمام، إذا حاب تسأل عن الدخل أو الصرف قول لي مثل: كم دخل البيض؟")
        else:
            send(chat_id,
                 "❓ ما فهمت. جرب:\n"
                 "• \"اشترينا 5 عنم بـ 5000\"\n"
                 "• \"كم دخل البيض؟\"\n"
                 "• \"قسم لي الدخل حسب البند\"\n"
                 "أو اكتب /help")

        self._ok()

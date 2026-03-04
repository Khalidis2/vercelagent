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
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)

def read_sheet(svc, sheet, rng="A2:Z"):
    res = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{rng}",
    ).execute()
    return res.get("values", [])

def append_row(svc, sheet, row):
    svc.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()

def update_inventory(svc, item_name, qty_delta, item_type="", notes=""):
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

D = "──────────────"

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
- add_income         : تسجيل دخل / إيراد
- add_expense        : تسجيل صرف / مصروف عام
- add_livestock      : شراء/إضافة مواشي (غنم، بقر، إبل، ماعز...)
- sell_livestock     : بيع أو ذبح مواشي
- add_poultry        : شراء دواجن (دجاج، بط، حمام...)
- sell_poultry       : بيع دواجن (وليس بيع البيض)
- pay_salary         : صرف راتب عامل
- income_total       : استعلام إجمالي الدخل
- expense_total      : استعلام إجمالي المصروف
- profit             : صافي الربح
- inventory          : جرد المواشي / المخزون الحالي
- last_transactions  : آخر العمليات
- category_total     : إجمالي تصنيف أو بند معيّن (مثل البيض، الأعلاف)
- income_breakdown   : تقسيم الدخل حسب البند/التصنيف
- daily_report       : تقرير يومي شامل
- smalltalk          : كلام عام (تحية، شكر، شرح عن البوت)
- clarify            : الرسالة غير واضحة

قواعد مهمة:
- بيع / باع / وردة / دخل / إيراد → direction: in
- شراء / اشترى / دفع / صرف / راتب / أعلاف → direction: out
- "عنم" أو "غنم" أو "خروف" → animal_type: "غنم" ، category: "مواشي"
- "بقر" أو "ثور" أو "عجل" → animal_type: "بقر" ، category: "مواشي"
- "إبل" أو "بعير" أو "ناقة" → animal_type: "إبل" ، category: "مواشي"
- "دجاج" أو "فروج" → animal_type: "دجاج" ، category: "دواجن"
- بيع البيض (كلمة "بيض") ليس بيع دواجن → اعتبره دخل عادي وليس sell_poultry.
- سؤال مثل "كم دخل البيض؟" أو "كم صرفنا على الأعلاف؟" → intent = "category_total" مع category = اسم الشيء.
- عبارات مثل "قسم لي الدخل حسب التصنيف" أو "قسم الدخل حسب البند" → intent = "income_breakdown".
- إذا كان الكلام مجرد تحية أو سؤال عام عن البوت → intent = "smalltalk".
- period افتراضي = "month" إذا لم يحدد المستخدم.
"""

def detect_intent(text):
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

    data = load_transactions(svc)
    inc, exp = totals_month(data)

    send(chat_id,
         f"{D}\n✅ دخل مسجل\n"
         f"البند: {item}\n"
         f"التصنيف: {category}\n"
         f"المبلغ: {fmt(amount)} د.إ\n"
         f"بواسطة: {user_name}\n"
         f"{D}\n"
         f"📊 هذا الشهر:\n"
         f"  دخل: {fmt(inc)} د.إ | صرف: {fmt(exp)} د.إ")


def h_add_expense(svc, d, chat_id, user_name, user_id):
    item     = d.get("item", "")
    amount   = d.get("amount", 0)
    category = d.get("category") or item
    if not item or not amount:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: صرفنا على الأعلاف 800")
        return

    add_transaction(svc, "صرف", item, category, amount, user_name)
    add_pending(svc, user_id, "expense", "add", item, amount, 0, user_name)

    data = load_transactions(svc)
    inc, exp = totals_month(data)

    warn = ""
    if exp > inc:
        warn = "\n⚠️ المصروفات أكثر من الدخل في هذا الشهر."

    send(chat_id,
         f"{D}\n✅ صرف مسجل\n"
         f"البند: {item}\n"
         f"التصنيف: {category}\n"
         f"المبلغ: {fmt(amount)} د.إ\n"
         f"بواسطة: {user_name}\n"
         f"{D}\n"
         f"📊 هذا الشهر:\n"
         f"  دخل: {fmt(inc)} د.إ | صرف: {fmt(exp)} د.إ{warn}")


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

    notes = json.dumps(
        {"gender": gender, "cost_per_head": round(cost/qty, 1) if qty else 0},
        ensure_ascii=False,
    )
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
    is_slaughter = any(w in (d.get("item") or "") for w in ["ذبح", "ذبيحة", "ذبحنا"])
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

    # حالة خاصة: بيع البيض → دخل عادي، بدون جرد
    if "بيض" in str(bird):
        if not price:
            send(chat_id, "❌ حدد مبلغ بيع البيض.")
            return

        item_name = "بيض"
        category  = "بيع البيض"
        add_transaction(svc, "دخل", item_name, category, price, user_name)
        add_pending(svc, user_id, "income", "add", item_name, price, 0, user_name)

        data = load_transactions(svc)
        inc, exp = totals_month(data)

        send(chat_id,
             f"{D}\n✅ دخل مسجل\n"
             f"البند: {item_name}\n"
             f"التصنيف: {category}\n"
             f"المبلغ: {fmt(price)} د.إ\n"
             f"بواسطة: {user_name}\n"
             f"{D}\n"
             f"📊 هذا الشهر:\n"
             f"  دخل: {fmt(inc)} د.إ | صرف: {fmt(exp)} د.إ")
        return

    # باقي الدواجن العادية
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
    add_pending(
        svc, user_id, "labor", "pay_salary", worker, amount, 0, user_name,
        json.dumps({"month": month}, ensure_ascii=False),
    )
    send(chat_id,
         f"{D}\n✅ تم صرف الراتب\n"
         f"العامل: {worker}\n"
         f"المبلغ: {fmt(amount)} د.إ\n"
         f"الشهر: {month}\n"
         f"بواسطة: {user_name}\n"
         f"{D}")


def h_profit(data, period, chat_id):
    if period in ("month", "today", "week"):
        inc, exp = totals_month(data)
        label = "هذا الشهر"
    else:
        inc, exp = totals_all(data)
        label = "الإجمالي"
    net = inc - exp
    emoji = "📈" if net >= 0 else "📉"
    send(chat_id,
         f"{D}\n💰 ملخص {label}\n"
         f"الدخل:   {fmt(inc)} د.إ\n"
         f"الصرف:   {fmt(exp)} د.إ\n"
         f"{emoji} الصافي: {fmt(net)} د.إ\n"
         f"{D}")


def h_inventory(svc, chat_id):
    inv = load_inventory(svc)
    if not inv:
        send(chat_id, "📦 الجرد فارغ حالياً.")
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
        lines.append(
            f"{t['date'][:10]}  {sign}{fmt(t['amount'])} د.إ  {t['item']} ({t['type']})"
        )
    lines.append(D)
    send(chat_id, "\n".join(lines))


def h_category_total(data, d, chat_id):
    cat    = d.get("category", "")
    period = d.get("period", "month")
    if not cat:
        send(chat_id, "❌ حدد التصنيف أو البند.\nمثال: كم الدخل من البيض؟ أو كم صرفنا على الأعلاف؟")
        return

    if period == "month":
        m = cur_month()
        rows = [x for x in data if x["date"].startswith(m) and x["category"] == cat]
        label = "هذا الشهر"
    else:
        rows = [x for x in data if x["category"] == cat]
        label = "لكل الفترة"

    inc = sum(x["amount"] for x in rows if x["type"] == "دخل")
    exp = sum(x["amount"] for x in rows if x["type"] == "صرف")

    if inc and not exp:
        send(chat_id, f"📊 الدخل من {cat} ({label}): {fmt(inc)} د.إ")
    elif exp and not inc:
        send(chat_id, f"📊 المصروف على {cat} ({label}): {fmt(exp)} د.إ")
    elif inc or exp:
        send(chat_id,
             f"{D}\n📊 {cat} ({label})\n"
             f"دخل: {fmt(inc)} د.إ\n"
             f"صرف: {fmt(exp)} د.إ\n"
             f"{D}")
    else:
        send(chat_id, f"لا توجد عمليات مرتبطة بـ {cat}.")


def h_income_breakdown(data, period, chat_id):
    if period == "month":
        m = cur_month()
        rows = [x for x in data if x["date"].startswith(m) and x["type"] == "دخل"]
        label = "هذا الشهر"
    else:
        rows = [x for x in data if x["type"] == "دخل"]
        label = "لكل الفترة"

    if not rows:
        send(chat_id, "لا يوجد دخل مسجل في الفترة المطلوبة.")
        return

    by_item = {}
    for r in rows:
        key = r["item"] or r["category"] or "غير محدد"
        by_item[key] = by_item.get(key, 0) + r["amount"]

    lines = [D, f"📊 الدخل حسب البند ({label})"]
    total = 0
    for k, v in by_item.items():
        lines.append(f"{k}: {fmt(v)} د.إ")
        total += v
    lines.append(f"{D}\nالإجمالي: {fmt(total)} د.إ")
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
🌾 بوت العزبة – أمثلة:

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
  • كم الدخل الكلي؟
  • كم صرفنا هالشهر؟
  • كم الربح هذا الشهر؟
  • كم الدخل من البيض؟
  • قسم لي الدخل حسب التصنيف
  • كم المواشي الحالية؟
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

        msg = update.get("message") or update.get("edited_message")
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

        if text in ("/start", "/help", "help", "مساعدة"):
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
        period = d.get("period", "month")

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

        elif intent == "income_total":
            inc, _ = totals_month(data) if period != "all" else totals_all(data)
            label  = "هذا الشهر" if period != "all" else "لكل الفترة"
            send(chat_id, f"{D}\n💰 الدخل ({label}): {fmt(inc)} د.إ\n{D}")

        elif intent == "expense_total":
            _, exp = totals_month(data) if period != "all" else totals_all(data)
            label  = "هذا الشهر" if period != "all" else "لكل الفترة"
            send(chat_id, f"{D}\n📤 المصروف ({label}): {fmt(exp)} د.إ\n{D}")

        elif intent == "profit":
            h_profit(data, period, chat_id)

        elif intent == "inventory":
            h_inventory(svc, chat_id)

        elif intent == "last_transactions":
            h_last(data, chat_id)

        elif intent == "category_total":
            h_category_total(data, d, chat_id)

        elif intent == "income_breakdown":
            h_income_breakdown(data, period, chat_id)

        elif intent == "daily_report":
            h_daily_report(svc, data, chat_id)

        elif intent == "smalltalk":
            send(chat_id, "أنا بوت العزبة أساعدك في تسجيل الدخل والمصروف والجرد.\nجرب مثلاً: \"بعت بيض بـ 200\" أو \"كم دخل البيض؟\" أو /help")
        else:
            send(chat_id,
                 "❓ ما فهمت.\n"
                 "جرب مثلاً:\n"
                 "• اشترينا 5 عنم بـ 5000\n"
                 "• كم الربح هذا الشهر؟\n"
                 "• كم الدخل من البيض؟\n"
                 "• قسم لي الدخل حسب التصنيف\n"
                 "أو اكتب /help")

        self._ok()

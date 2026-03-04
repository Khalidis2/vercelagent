"""
Ezba (Farm) Telegram Bot – Vercel Python Serverless

Google Sheets layout (3 tabs):

Transactions : A=التاريخ | B=النوع(دخل/صرف) | C=البند | D=التصنيف | E=المبلغ | F=المستخدم
Inventory    : A=Item | B=Type | C=Quantity | D=Notes
Pending      : A=UserId | B=Timestamp | C=OperationType | D=Action
               E=Item | F=Amount | G=Quantity | H=Person | I=NotesOrSnapshotJson
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

# ── SHEET NAMES ────────────────────────────────────────────────────────────────

S_TRANSACTIONS = "Transactions"   # A=date B=type C=item D=category E=amount F=user
S_INVENTORY    = "Inventory"      # A=Item B=Type C=Quantity D=Notes
S_PENDING      = "Pending"        # A=UserId B=Timestamp C=OperationType D=Action ...

# ── TELEGRAM ───────────────────────────────────────────────────────────────────

def send(chat_id, text: str):
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

def update_inventory(svc, item_name: str, qty_delta: int, item_type: str = "", notes: str = ""):
    """Add or update a row in Inventory sheet."""
    rows = read_sheet(svc, S_INVENTORY)
    found_index = None
    current_qty = 0
    for i, r in enumerate(rows):
        if r and r[0].strip() == item_name.strip():
            found_index = i + 2  # row number (data starts at row 2)
            try:
                current_qty = int(r[2]) if len(r) > 2 and r[2] else 0
            except Exception:
                current_qty = 0
            break

    new_qty = max(0, current_qty + qty_delta)

    if found_index:
        svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{S_INVENTORY}!A{found_index}:D{found_index}",
            valueInputOption="USER_ENTERED",
            body={"values": [[item_name, item_type, new_qty, notes]]},
        ).execute()
    else:
        append_row(svc, S_INVENTORY, [item_name, item_type, new_qty, notes])

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

D = "────────────────────"   # divider

# ── TRANSACTIONS HELPERS ───────────────────────────────────────────────────────

def load_transactions(svc):
    rows = read_sheet(svc, S_TRANSACTIONS)
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        try:
            out.append(
                {
                    "date":     r[0],
                    "type":     r[1],  # دخل | صرف
                    "item":     r[2],
                    "category": r[3] if len(r) > 3 else "",
                    "amount":   float(r[4]),
                    "user":     r[5] if len(r) > 5 else "",
                }
            )
        except Exception:
            continue
    return out

def add_transaction(svc, kind, item, category, amount, user):
    append_row(
        svc,
        S_TRANSACTIONS,
        [now_str(), kind, item, category, amount, user],
    )

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
        if not r or not r[0]:
            continue
        qty = 0
        try:
            qty = int(r[2]) if len(r) > 2 and r[2] else 0
        except Exception:
            qty = 0
        out.append(
            {
                "item":  r[0],
                "type":  r[1] if len(r) > 1 else "",
                "qty":   qty,
                "notes": r[3] if len(r) > 3 else "",
            }
        )
    return out

# ── PENDING (LOG) ──────────────────────────────────────────────────────────────

def add_pending(svc, user_id, op_type, action, item, amount, qty, person, notes=""):
    append_row(
        svc,
        S_PENDING,
        [str(user_id), now_str(), op_type, action, item, amount, qty, person, notes],
    )

# ── AI INTENT (OpenAI) ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
أنت مساعد ذكي لإدارة مصاريف العزبة في الإمارات.
اللغة: عربية ولهجة إماراتية.

أرجع دائماً JSON فقط بهذا الشكل (بدون أي شرح إضافي):

{
  "intent": "add_income | add_expense | add_livestock | sell_livestock | add_poultry | sell_poultry | pay_salary | income_total | expense_total | profit | inventory | last_transactions | category_total | daily_report | smalltalk | clarify",
  "direction": "in | out | none",
  "item": "",
  "category": "",
  "amount": 0,
  "quantity": 0,
  "animal_type": "",
  "gender": "ذكر | أنثى | مختلط | ",
  "worker_name": "",
  "month": "",
  "period": "today | week | month | all"
}

التوضيح:

- add_income  : جملة فيها بيع / دخل للصندوق (بعت، دخل لنا، وردة، استلمنا...)
- add_expense : جملة فيها دفع / صرف / فاتورة / سلفة / أعلاف / مصروف / راتب بدون ذكر اسم عامل.
- add_livestock / sell_livestock : شراء أو بيع/ذبح مواشي (عنم، غنم، بقر، إبل...)
- add_poultry / sell_poultry     : شراء أو بيع دواجن (بيض، دجاج، فروج، حمام...)
- pay_salary : جملة فيها "راتب" مع اسم عامل.
- income_total  : أسئلة مثل "كم الدخل؟", "كم دخلنا هذا الشهر؟".
- expense_total : أسئلة مثل "كم صرفنا؟", "كم المصروف؟".
- profit        : أسئلة مثل "كم الربح؟", "الصافي؟", "العجز؟".
- inventory     : "كم المواشي الحالية؟", "كم عندنا دجاج؟", "جرد".
- last_transactions : "آخر العمليات", "آخر عمليات البيع", "آخر المصاريف".
- category_total : أي سؤال عن دخل/صرف مرتبط ببند أو تصنيف معين:
    - مثال: "كم دخل البيض؟"  → intent="category_total", direction="in", category="بيض".
    - مثال: "كم صرفنا على الأعلاف؟" → intent="category_total", direction="out", category="أعلاف".
    - مثال: "قسم لي الدخل حسب التصنيف" → intent="category_total", direction="in", category="*".
- daily_report : "تقرير اليوم", "ملخص اليوم".

القواعد:
- direction:
    - للدخل: in
    - للمصروف: out
    - إذا السؤال عام بدون تحديد (مثل "قسم الدخل حسب التصنيف") → in.
- period:
    - إذا ذكر اليوم/اليوم/هاليوم → "today"
    - الأسبوع / آخر سبوع → "week"
    - الشهر / هالشهر / هذا الشهر → "month"
    - إذا قال "لكل الفترة", "من أول ما بدينا" → "all"
    - بدون تحديد → "month" في معظم الأسئلة المالية.

التصنيف:
- لو السؤال عن "البيض" → category = "بيض".
- لو عن "الأعلاف" → category = "أعلاف".
- لو قال "قسم الدخل حسب التصنيف" → category = "*".
- استخدم field "item" للبند النصي إذا احتجت، لكن field "category" هو الأهم للتجميع.

smalltalk:
- أي ترحيب أو كلام عام مثل "مرحبا", "شو تقدر تسوي؟" → intent="smalltalk".

إذا لم تستطع الفهم إطلاقاً → intent="clarify".
"""

def detect_intent(text: str) -> dict:
    try:
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        return json.loads(completion.choices[0].message.content)
    except Exception as e:
        return {"intent": "clarify", "_error": str(e)}

# ── HANDLERS ───────────────────────────────────────────────────────────────────

def h_add_income(svc, d, chat_id, user_name, user_id):
    item     = d.get("item") or d.get("category") or ""
    amount   = d.get("amount") or 0
    category = d.get("category") or item

    if not item or not amount:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: بعت بيض بـ 200")
        return

    add_transaction(svc, "دخل", item, category, amount, user_name)
    add_pending(svc, user_id, "income", "add", item, amount, 0, user_name)

    data = load_transactions(svc)
    inc, exp = totals_month(data)

    send(
        chat_id,
        f"{D}\n✅ دخل مسجل\n"
        f"البند: {item}\n"
        f"التصنيف: {category}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}\n"
        f"📊 هذا الشهر – دخل: {fmt(inc)} د.إ | صرف: {fmt(exp)} د.إ",
    )

def h_add_expense(svc, d, chat_id, user_name, user_id):
    item     = d.get("item") or d.get("category") or ""
    amount   = d.get("amount") or 0
    category = d.get("category") or item

    if not item or not amount:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: صرفنا على الأعلاف 800")
        return

    add_transaction(svc, "صرف", item, category, amount, user_name)
    add_pending(svc, user_id, "expense", "add", item, amount, 0, user_name)

    data = load_transactions(svc)
    inc, exp = totals_month(data)
    warn = "\n⚠️ المصروفات أعلى من الدخل هذا الشهر." if exp > inc else ""

    send(
        chat_id,
        f"{D}\n✅ صرف مسجل\n"
        f"البند: {item}\n"
        f"التصنيف: {category}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}\n"
        f"📊 هذا الشهر – دخل: {fmt(inc)} د.إ | صرف: {fmt(exp)} د.إ{warn}",
    )

def h_add_livestock(svc, d, chat_id, user_name, user_id):
    animal = d.get("animal_type") or d.get("item") or "غنم"
    qty    = int(d.get("quantity") or 1)
    cost   = d.get("amount") or 0
    gender = d.get("gender") or ""

    update_inventory(svc, animal, qty, "مواشي", gender)
    if cost:
        add_transaction(svc, "صرف", f"شراء {qty} {animal}", "مواشي", cost, user_name)

    per_head = round(cost / qty, 2) if qty and cost else 0
    notes = json.dumps({"gender": gender, "cost_per_head": per_head}, ensure_ascii=False)
    add_pending(svc, user_id, "inventory", "buy_livestock", animal, cost, qty, user_name, notes)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == animal), qty)

    send(
        chat_id,
        f"{D}\n✅ تم إضافة مواشي\n"
        f"النوع: {animal}\n"
        f"العدد: {qty}\n"
        f"الجنس: {gender or '-'}\n"
        f"التكلفة: {fmt(cost)} د.إ\n"
        f"{D}\n"
        f"🐄 رصيد {animal} الحالي: {current_qty}",
    )

def h_sell_livestock(svc, d, chat_id, user_name, user_id):
    animal  = d.get("animal_type") or d.get("item") or "غنم"
    qty     = int(d.get("quantity") or 1)
    price   = d.get("amount") or 0
    text    = (d.get("item") or "") + " " + (d.get("category") or "")
    is_slaughter = any(w in text for w in ["ذبح", "ذبيحة", "ذبحنا"])

    update_inventory(svc, animal, -qty, "مواشي", "")

    if price and not is_slaughter:
        add_transaction(svc, "دخل", f"بيع {qty} {animal}", "مواشي", price, user_name)

    action_label = "ذبح" if is_slaughter else "بيع"
    add_pending(svc, user_id, "inventory", f"{action_label}_livestock", animal, price, qty, user_name)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == animal), 0)

    send(
        chat_id,
        f"{D}\n✅ تم تسجيل {action_label}\n"
        f"الحيوان: {animal} × {qty}\n"
        f"السعر: {fmt(price)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}\n"
        f"🐄 رصيد {animal} الحالي: {current_qty}",
    )

def h_add_poultry(svc, d, chat_id, user_name, user_id):
    bird = d.get("animal_type") or d.get("item") or "دجاج"
    qty  = int(d.get("quantity") or 1)
    cost = d.get("amount") or 0

    update_inventory(svc, bird, qty, "دواجن", "")
    if cost:
        add_transaction(svc, "صرف", f"شراء {qty} {bird}", "دواجن", cost, user_name)

    add_pending(svc, user_id, "inventory", "buy_poultry", bird, cost, qty, user_name)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == bird), qty)

    send(
        chat_id,
        f"{D}\n✅ تم إضافة دواجن\n"
        f"النوع: {bird} × {qty}\n"
        f"التكلفة: {fmt(cost)} د.إ\n"
        f"{D}\n"
        f"🐔 رصيد {bird} الحالي: {current_qty}",
    )

def h_sell_poultry(svc, d, chat_id, user_name, user_id):
    bird  = d.get("animal_type") or d.get("item") or "دجاج"
    qty   = int(d.get("quantity") or 1)
    price = d.get("amount") or 0

    update_inventory(svc, bird, -qty, "دواجن", "")
    if price:
        add_transaction(svc, "دخل", f"بيع {qty} {bird}", "دواجن", price, user_name)

    add_pending(svc, user_id, "inventory", "sell_poultry", bird, price, qty, user_name)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == bird), 0)

    send(
        chat_id,
        f"{D}\n✅ تم تسجيل بيع دواجن\n"
        f"الطير: {bird} × {qty}\n"
        f"السعر: {fmt(price)} د.إ\n"
        f"{D}\n"
        f"🐔 رصيد {bird} الحالي: {current_qty}",
    )

def h_pay_salary(svc, d, chat_id, user_name, user_id):
    worker = d.get("worker_name") or d.get("item") or ""
    amount = d.get("amount") or 0
    month  = d.get("month") or cur_month()

    if not worker or not amount:
        send(chat_id, "❌ حدد اسم العامل والمبلغ.\nمثال: راتب محمد 1500 شهر فبراير")
        return

    add_transaction(svc, "صرف", f"راتب {worker}", "رواتب", amount, user_name)
    add_pending(
        svc,
        user_id,
        "labor",
        "pay_salary",
        worker,
        amount,
        0,
        user_name,
        json.dumps({"month": month}, ensure_ascii=False),
    )

    send(
        chat_id,
        f"{D}\n✅ تم صرف راتب\n"
        f"العامل: {worker}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"الشهر: {month}\n"
        f"بواسطة: {user_name}\n"
        f"{D}",
    )

def h_profit(data, period, chat_id):
    if period in ("today", "week", "month"):
        inc, exp = totals_month(data)
        label = "هذا الشهر"
    else:
        inc, exp = totals_all(data)
        label = "لكل الفترة"

    net = inc - exp
    emoji = "📈" if net >= 0 else "📉"

    send(
        chat_id,
        f"{D}\n💰 الصافي ({label})\n"
        f"الدخل: {fmt(inc)} د.إ\n"
        f"الصرف: {fmt(exp)} د.إ\n"
        f"{emoji} الصافي: {fmt(net)} د.إ\n"
        f"{D}",
    )

def h_inventory(svc, chat_id):
    inv = load_inventory(svc)
    if not inv:
        send(chat_id, "📦 الجرد الحالي فارغ.")
        return

    lines = [D, "📦 الجرد الحالي:"]
    for x in inv:
        lines.append(f"- {x['item']} ({x['type'] or '-'}): {x['qty']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_last(data, chat_id, direction=None, category=None):
    rows = list(data)

    if direction == "in":
        rows = [x for x in rows if x["type"] == "دخل"]
    elif direction == "out":
        rows = [x for x in rows if x["type"] == "صرف"]

    if category:
        cat = category
        rows = [
            x
            for x in rows
            if cat in (x["category"] or "") or cat in (x["item"] or "")
        ]

    rows = sorted(rows, key=lambda x: x["date"], reverse=True)[:7]

    if not rows:
        send(chat_id, "لا توجد عمليات مسجلة.")
        return

    lines = [D, "🕐 آخر العمليات:"]
    for t in rows:
        sign = "+" if t["type"] == "دخل" else "-"
        lines.append(
            f"{t['date']} | {t['type']} | {t['item']} | {sign}{fmt(t['amount'])} د.إ"
        )
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_category_total(data, d, chat_id):
    cat      = (d.get("category") or "").strip()
    period   = d.get("period", "month")
    direction = d.get("direction", "none")

    if not cat:
        send(chat_id, "❌ حدد التصنيف أو البند.\nمثال: كم الدخل من البيض؟ أو كم صرفنا على الأعلاف؟")
        return

    # فلترة الفترة
    if period == "month":
        m = cur_month()
        base_rows = [x for x in data if x["date"].startswith(m)]
        label_period = "هذا الشهر"
    elif period == "all":
        base_rows = list(data)
        label_period = "لكل الفترة المسجلة"
    else:
        base_rows = list(data)
        label_period = "للـفترة المسجلة"

    # فلترة حسب البند/التصنيف – نستخدم contains وليس تطابق ثابت
    def match_row(x):
        item = str(x.get("item") or "")
        catg = str(x.get("category") or "")

        if cat == "*":  # تجميع حسب التصنيف
            return True

        # حالات خاصة للبيض → أي شيء فيه كلمة "بيض"
        if "بيض" in cat:
            return ("بيض" in item) or ("بيض" in catg)

        # غير البيض: إذا التصنيف أو البند يحتوي الكلمة
        return (cat in item) or (cat in catg)

    rows = [x for x in base_rows if match_row(x)]

    if not rows:
        send(chat_id, f"لا توجد عمليات مرتبطة بـ {cat}.")
        return

    # تجميع
    def dir_ok(x):
        if direction == "in":
            return x["type"] == "دخل"
        if direction == "out":
            return x["type"] == "صرف"
        return True  # لو ما حددنا

    rows_dir = [x for x in rows if dir_ok(x)]

    # حالة "قسم لي الدخل حسب التصنيف"
    if cat == "*":
        buckets = {}
        for x in rows_dir:
            if x["type"] != "دخل":
                continue
            key = x["category"] or x["item"]
            buckets[key] = buckets.get(key, 0) + x["amount"]

        if not buckets:
            send(chat_id, "لا يوجد دخل مسجل في هذه الفترة.")
            return

        lines = [D, f"📊 الدخل حسب البند ({label_period}):"]
        total = 0
        for k, v in sorted(buckets.items(), key=lambda t: -t[1]):
            lines.append(f"{k}: {fmt(v)} د.إ")
            total += v
        lines.append(f"{D}\nالإجمالي: {fmt(total)} د.إ")
        send(chat_id, "\n".join(lines))
        return

    # غير النجمة: بند/تصنيف معيّن
    inc = sum(x["amount"] for x in rows_dir if x["type"] == "دخل")
    exp = sum(x["amount"] for x in rows_dir if x["type"] == "صرف")

    if direction == "in":
        send(chat_id, f"💰 الدخل من {cat} ({label_period}): {fmt(inc)} د.إ")
    elif direction == "out":
        send(chat_id, f"📤 المصروف على {cat} ({label_period}): {fmt(exp)} د.إ")
    else:
        send(
            chat_id,
            f"{D}\n📊 {cat} ({label_period})\n"
            f"الدخل: {fmt(inc)} د.إ\n"
            f"المصروف: {fmt(exp)} د.إ\n"
            f"{D}",
        )

def h_daily_report(svc, data, chat_id):
    today = today_str()
    day_rows = [x for x in data if x["date"].startswith(today)]
    d_inc, d_exp = totals_all(day_rows)
    m_inc, m_exp = totals_month(data)

    inv = load_inventory(svc)
    inv_txt = " | ".join(f"{x['item']}: {x['qty']}" for x in inv) if inv else "لا يوجد"

    send(
        chat_id,
        f"{D}\n📋 تقرير اليوم — {today}\n"
        f"اليوم: دخل {fmt(d_inc)} د.إ | صرف {fmt(d_exp)} د.إ\n"
        f"{D}\n"
        f"هذا الشهر: دخل {fmt(m_inc)} د.إ | صرف {fmt(m_exp)} د.إ\n"
        f"{D}\n"
        f"الجرد الحالي: {inv_txt}\n"
        f"{D}",
    )

# ── HELP TEXT ──────────────────────────────────────────────────────────────────

HELP = """
🌾 بوت العزبة – أمثلة:

• بعت بيض بـ 200
• صرفنا على الأعلاف 800
• اشترينا 10 عنم بـ 15000
• بعنا 2 ثور بـ 8000
• راتب العامل 1400
• كم الدخل؟
• كم صرفنا؟
• كم الربح هذا الشهر؟
• كم دخل البيض؟
• كم صرفنا على الأعلاف؟
• قسم لي الدخل حسب التصنيف
• آخر العمليات
• تقرير اليوم
"""

# ── MAIN HTTP HANDLER (Vercel) ────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # لا نطبع شيء في اللوق حق Vercel
        return

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self._ok()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode() if length else "{}"
            update = json.loads(body)
        except Exception:
            self._ok()
            return

        msg = update.get("message") or {}
        text = msg.get("text")
        if not text:
            self._ok()
            return

        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]

        if user_id not in ALLOWED_USERS:
            send(chat_id, "⛔ هذا البوت خاص بأهل العزبة فقط.")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]
        text = text.strip()

        # أوامر سريعة
        if text in ("/start", "/help", "help", "مساعدة", "شو تسوي", "شو تسوي؟", "وش تسوي"):
            send(chat_id, HELP)
            self._ok()
            return

        # اتصال بجوجل شيت
        try:
            svc = sheets_svc()
            data = load_transactions(svc)
        except Exception as e:
            send(chat_id, f"❌ خطأ في Google Sheets:\n{e}")
            self._ok()
            return

        # AI intent
        d = detect_intent(text)
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
            label = "هذا الشهر" if period != "all" else "لكل الفترة المسجلة"
            send(chat_id, f"💰 الدخل ({label}): {fmt(inc)} د.إ")

        elif intent == "expense_total":
            _, exp = totals_month(data) if period != "all" else totals_all(data)
            label = "هذا الشهر" if period != "all" else "لكل الفترة المسجلة"
            send(chat_id, f"📤 المصروف ({label}): {fmt(exp)} د.إ")

        elif intent == "profit":
            h_profit(data, period, chat_id)

        elif intent == "inventory":
            h_inventory(svc, chat_id)

        elif intent == "last_transactions":
            h_last(data, chat_id, d.get("direction"), d.get("category"))

        elif intent == "category_total":
            h_category_total(data, d, chat_id)

        elif intent == "daily_report":
            h_daily_report(svc, data, chat_id)

        elif intent == "smalltalk":
            send(chat_id, "أنا أسجّل لك الدخل والمصروف وأجاوبك عن التقارير.\nجرب مثلاً:\n- كم دخل البيض؟\n- كم صرفنا على الأعلاف؟\n- قسم لي الدخل حسب التصنيف\nأو اكتب /help")

        else:
            send(
                chat_id,
                "❓ ما قدرت أفهم الرسالة.\n"
                "جرب شيئ مثل:\n"
                "• بعت بيض بـ 200\n"
                "• كم دخل البيض؟\n"
                "• كم صرفنا على الأعلاف؟\n"
                "• قسم لي الدخل حسب التصنيف\n"
                "أو اكتب /help",
            )

        self._ok()

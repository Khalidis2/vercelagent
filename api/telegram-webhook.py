# api/telegram-webhook.py

from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta, date
import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ================== CONFIG ======================

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

# ================== TELEGRAM ====================

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

# ================== GOOGLE SHEETS ===============

def sheets_svc():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
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

def find_inventory_row(rows, item_name):
    """
    Find the best matching row index for item_name.
    1. Exact match
    2. item_name contains the row item (e.g. "غنم حري" found in "غنم حري، غنم نعيمي")
    3. Row item contains item_name
    Returns index in rows list (0-based), or -1 if not found.
    """
    name = item_name.strip()
    # Pass 1: exact
    for i, r in enumerate(rows):
        if r and r[0].strip() == name:
            return i
    # Pass 2: row cell contains our name as substring
    for i, r in enumerate(rows):
        if r and name in r[0]:
            return i
    # Pass 3: our name contains the row cell (e.g. searching "غنم حري" when cell is "غنم")
    for i, r in enumerate(rows):
        if r and r[0].strip() and r[0].strip() in name:
            return i
    return -1

def update_inventory(svc, item_name, qty_delta, item_type="", notes=""):
    rows = read_sheet(svc, S_INVENTORY)
    values_api = svc.spreadsheets().values()

    i = find_inventory_row(rows, item_name)
    if i >= 0:
        r = rows[i]
        old_qty = int(r[2]) if len(r) > 2 and r[2] else 0
        new_qty = max(0, old_qty + int(qty_delta))
        row_num = i + 2  # +1 for header, +1 for 1-based
        values_api.update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{S_INVENTORY}!A{row_num}:D{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [[r[0], r[1] if len(r) > 1 else item_type, new_qty, r[3] if len(r) > 3 else notes]]},
        ).execute()
        return

    if qty_delta > 0:
        values_api.append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{S_INVENTORY}!A1:D1",
            valueInputOption="USER_ENTERED",
            body={"values": [[item_name, item_type, int(qty_delta), notes]]},
        ).execute()

# ================== UTILS =======================

def now_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")

def today_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d")

def cur_month_key():
    return datetime.now(UAE_TZ).strftime("%Y-%m")

def fmt(x):
    try:
        f = float(x)
        return int(f) if f.is_integer() else round(f, 2)
    except Exception:
        return x

def filter_by_period(data, period):
    if not period:
        period = "month"

    now = datetime.now(UAE_TZ)

    if period == "all":
        return data, "لكل الفترة المسجلة"

    if period == "today":
        key = now.strftime("%Y-%m-%d")
        return [x for x in data if x["date"].startswith(key)], f"اليوم {key}"

    if period == "week":
        start = (now - timedelta(days=6)).date()
        out = []
        for x in data:
            try:
                d = datetime.strptime(x["date"][:10], "%Y-%m-%d").date()
            except Exception:
                continue
            if start <= d <= now.date():
                out.append(x)
        return out, f"آخر ٧ أيام"

    key = now.strftime("%Y-%m")
    return [x for x in data if x["date"].startswith(key)], "هذا الشهر"

def split_animals_for_inventory(animal_str, qty):
    s = (animal_str or "").strip()
    if not s:
        return [("غنم", qty)]

    s = s.replace(" و", "،").replace("، و", "،")
    parts = [p.strip() for p in s.split("،") if p.strip()]
    if not parts:
        return [("غنم", qty)]

    if len(parts) == 1:
        return [(parts[0], qty)]

    base = qty // len(parts)
    extra = qty % len(parts)
    out = []
    for i, p in enumerate(parts):
        q = base + (1 if i < extra else 0)
        out.append((p, q))
    return out

# ================== TRANSACTIONS ===============

def load_transactions(svc):
    rows = read_sheet(svc, S_TRANSACTIONS)
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        try:
            amount = float(r[4])
        except Exception:
            continue
        out.append({
            "date":     r[0],
            "type":     r[1],
            "item":     r[2],
            "category": r[3] if len(r) > 3 else "",
            "amount":   amount,
            "user":     r[5] if len(r) > 5 else "",
        })
    return out

def add_transaction(svc, ttype, item, category, amount, user):
    append_row(svc, S_TRANSACTIONS, [now_str(), ttype, item, category, amount, user])

def totals_all(data):
    inc = sum(x["amount"] for x in data if x["type"] == "دخل")
    exp = sum(x["amount"] for x in data if x["type"] == "صرف")
    return inc, exp

# ================== INVENTORY ==================

def load_inventory(svc):
    rows = read_sheet(svc, S_INVENTORY)
    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        out.append({
            "item":  r[0],
            "type":  r[1] if len(r) > 1 else "",
            "qty":   int(r[2]) if len(r) > 2 and r[2] else 0,
            "notes": r[3] if len(r) > 3 else "",
        })
    return out

# ================== PENDING ====================

def add_pending(svc, user_id, op_type, action, item, amount, qty, person, notes=""):
    append_row(svc, S_PENDING, [
        str(user_id), now_str(), op_type, action, item, amount, qty, person, notes,
    ])

# ================== AI INTENT ==================

SYSTEM_PROMPT = """
أنت مساعد لإدارة عزبة/مزرعة. مهمتك استخراج النية والبيانات من رسائل المستخدم.

أرجع JSON فقط بدون أي نص آخر:

{
  "intent": "add_income | add_expense | add_livestock | sell_livestock | add_poultry | sell_poultry | pay_salary | income_total | expense_total | profit | inventory | last_transactions | income_by_item | income_breakdown | smalltalk | clarify",
  "item": "",
  "category": "",
  "amount": 0,
  "quantity": 0,
  "animal_type": "",
  "worker_name": "",
  "period": "today | week | month | all",
  "inventory_item": ""
}

قواعد تحديد intent:

دخل (add_income):
- بيع أي منتج ليس حيوان حي: بيض، لبن، صوف، زبدة، جبن، خضار، محاصيل
- كلمات: "بعت"، "وردنا"، "دخل"، "إيراد"، "استلمنا"
- IMPORTANT: إذا كان المبيع غنم أو بقر أو إبل أو حيوان حي → ليس add_income بل sell_livestock
- item = المنتج المباع (مثل "بيض"، "لبن")
- amount = المبلغ

مواشي-شراء (add_livestock):
- شراء حيوانات: غنم، بقر، إبل، حمير، خيل
- كلمات: "اشترينا غنم"، "جبنا بقر"، "وصل لنا غنم"
- animal_type = نوع الحيوان، quantity = العدد، amount = التكلفة إن ذكرت

مواشي-بيع (sell_livestock):
- بيع حيوانات: غنم، بقر، إبل، ناقة
- كلمات: "بعنا غنم"، "بيع غنم"، "تم بيع غنم"، "بعت غنم"
- IMPORTANT: أي جملة فيها "غنم" أو "بقر" أو "إبل" مع "بيع" أو "بعت" أو "تم بيع" → sell_livestock دائماً وليس add_income
- animal_type = نوع الحيوان بالتفصيل (مثل "غنم حري،غنم نعيمي" إذا ذكر نوعين)
- مثال: "تم بيع غنم عدد 2 واحد حري وواحد نعيمي بمبلغ 1510" → intent=sell_livestock, animal_type="غنم حري،غنم نعيمي", quantity=2, amount=1510
- quantity = العدد الكلي، amount = السعر الكلي
- إذا ذكر أكثر من نوع افصل بـ ، في animal_type

دواجن-شراء (add_poultry):
- شراء دواجن للتربية: دجاج، حمام، بط، ديك رومي
- animal_type = النوع، quantity = العدد، amount = التكلفة

دواجن-بيع (sell_poultry):
- بيع دواجن أو بيض كمنتج للأكل/البيع
- "بعت دجاج"، "بعت طيور" → sell_poultry
- ملاحظة: "بعت بيض" → add_income وليس sell_poultry
- animal_type = النوع، quantity = العدد، amount = السعر

مصروف (add_expense):
- أي صرف ليس شراء حيوان ولا راتب
- أعلاف، دواء، كهرباء، وقود، صيانة، مستلزمات
- كلمات: "صرفنا"، "اشترينا أعلاف"، "دفعنا"، "فاتورة"
- item = ما تم الصرف عليه، amount = المبلغ

راتب (pay_salary):
- راتب أو معاش عامل
- worker_name = اسم العامل أو "العامل" إن لم يذكر اسم
- amount = المبلغ

استعلامات:
- "كم الدخل" / "إجمالي الدخل" → income_total
- "كم صرفنا" / "إجمالي المصاريف" → expense_total
- "كم الربح" / "الصافي" / "كم ربحنا" → profit
- "الجرد" / "كم عدد الغنم" / "كم الدواجن" → inventory، inventory_item = اسم الصنف
- "آخر العمليات" / "آخر المعاملات" → last_transactions
- "كم دخل البيض" / "كم دخل الغنم" → income_by_item، item = البند
- "قسم الدخل" / "توزيع الدخل" → income_breakdown
- حديث عام / سؤال عن البوت → smalltalk
- لا تفهم الرسالة → clarify

قواعد period:
- "اليوم" → "today"
- "هالأسبوع" / "آخر أسبوع" / "الأسبوع" → "week"
- "هالشهر" / "هذا الشهر" / "الشهر" → "month"
- "إجمالي" / "الكلي" / "كل الفترة" / "من الأول" → "all"
- إذا لم تذكر فترة → "month" افتراضي

تعليمات استخراج البيانات:
- amount: استخرج الرقم حتى لو مكتوب "بمبلغ 500" أو "بـ 500" أو "500 درهم"
- quantity: العدد المذكور صراحة، وإلا 1
- إذا قال "عدد 2 واحد حري وواحد نعيمي" → quantity=2, animal_type="غنم حري،غنم نعيمي"
- item: البند الرئيسي للمعاملة (مثل "بيض"، "أعلاف"، "كهرباء")
- category: نفس item إن لم يذكر تصنيف آخر
"""

def detect_intent(text):
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

# ================== HELPERS ====================

def resolve_item(d):
    """
    FIX: Extract item from any available field.
    GPT sometimes puts the value in animal_type or category instead of item.
    """
    return (
        (d.get("item") or "").strip()
        or (d.get("animal_type") or "").strip()
        or (d.get("category") or "").strip()
        or ""
    )

# ================== HANDLERS ====================

def h_add_income(svc, d, chat_id, user_name, user_id):
    # FIX: use resolve_item to avoid empty item when GPT puts value in animal_type
    item     = resolve_item(d)
    amount   = float(d.get("amount") or 0)
    category = (d.get("category") or item or "").strip()

    if not item or amount <= 0:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: بعت بيض بـ 200")
        return

    add_transaction(svc, "دخل", item, category, amount, user_name)
    add_pending(svc, user_id, "transaction", "add_income", item, amount, 0, user_name)

    data = load_transactions(svc)
    month_data, _ = filter_by_period(data, "month")
    all_inc, all_exp = totals_all(data)
    m_inc, m_exp     = totals_all(month_data)

    send(chat_id,
        f"{D}\n✅ دخل مسجل\n"
        f"البند: {item}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}\n"
        f"📊 هذا الشهر: دخل {fmt(m_inc)} | صرف {fmt(m_exp)}\n"
        f"📊 كل الفترة: دخل {fmt(all_inc)} | صرف {fmt(all_exp)}\n"
        f"{D}"
    )

def h_add_expense(svc, d, chat_id, user_name, user_id):
    # FIX: use resolve_item
    item     = resolve_item(d)
    amount   = float(d.get("amount") or 0)
    category = (d.get("category") or item or "").strip()

    if not item or amount <= 0:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: صرفنا على الأعلاف 800")
        return

    add_transaction(svc, "صرف", item, category, amount, user_name)
    add_pending(svc, user_id, "transaction", "add_expense", item, amount, 0, user_name)

    data = load_transactions(svc)
    month_data, _ = filter_by_period(data, "month")
    all_inc, all_exp = totals_all(data)
    m_inc, m_exp     = totals_all(month_data)

    send(chat_id,
        f"{D}\n✅ صرف مسجل\n"
        f"البند: {item}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}\n"
        f"📊 هذا الشهر: دخل {fmt(m_inc)} | صرف {fmt(m_exp)}\n"
        f"📊 كل الفترة: دخل {fmt(all_inc)} | صرف {fmt(all_exp)}\n"
        f"{D}"
    )

def h_add_livestock(svc, d, chat_id, user_name, user_id):
    animal = (d.get("animal_type") or d.get("item") or "غنم").strip()
    qty    = int(d.get("quantity") or 1)
    cost   = float(d.get("amount") or 0)

    update_inventory(svc, animal, qty, item_type="مواشي")
    if cost > 0:
        add_transaction(svc, "صرف", f"شراء {qty} {animal}", "مواشي", cost, user_name)

    add_pending(svc, user_id, "inventory", "add_livestock", animal, cost, qty, user_name)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == animal), qty)

    send(chat_id,
        f"{D}\n✅ تم إضافة المواشي\n"
        f"النوع: {animal} × {qty}\n"
        f"التكلفة: {fmt(cost)} د.إ\n"
        f"{D}\n"
        f"🐄 رصيد {animal} الحالي: {current_qty}\n"
        f"{D}"
    )

def h_sell_livestock(svc, d, chat_id, user_name, user_id):
    animal_raw = (d.get("animal_type") or d.get("item") or "غنم").strip()
    qty        = int(d.get("quantity") or 1)
    price      = float(d.get("amount") or 0)

    splits = split_animals_for_inventory(animal_raw, qty)
    for name, q in splits:
        update_inventory(svc, name, -q, item_type="مواشي")

    if price > 0:
        add_transaction(svc, "دخل", f"بيع {qty} {animal_raw}", "مواشي", price, user_name)

    add_pending(svc, user_id, "inventory", "sell_livestock", animal_raw, price, qty, user_name)

    inv = load_inventory(svc)
    lines = [D, f"✅ تم تسجيل بيع المواشي\nالحيوان: {animal_raw} × {qty}\nالسعر: {fmt(price)} د.إ\nالرصيد الحالي:"]
    for name, _q in splits:
        current = next((x["qty"] for x in inv if x["item"] == name), 0)
        lines.append(f"  {name}: {current}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_add_poultry(svc, d, chat_id, user_name, user_id):
    bird = (d.get("animal_type") or d.get("item") or "دجاج").strip()
    qty  = int(d.get("quantity") or 1)
    cost = float(d.get("amount") or 0)

    update_inventory(svc, bird, qty, item_type="دواجن")
    if cost > 0:
        add_transaction(svc, "صرف", f"شراء {qty} {bird}", "دواجن", cost, user_name)

    add_pending(svc, user_id, "inventory", "add_poultry", bird, cost, qty, user_name)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == bird), qty)

    send(chat_id,
        f"{D}\n✅ تم إضافة الدواجن\n"
        f"النوع: {bird} × {qty}\n"
        f"التكلفة: {fmt(cost)} د.إ\n"
        f"{D}\n"
        f"🐔 رصيد {bird} الحالي: {current_qty}\n"
        f"{D}"
    )

def h_sell_poultry(svc, d, chat_id, user_name, user_id):
    bird  = (d.get("animal_type") or d.get("item") or "دجاج").strip()
    qty   = int(d.get("quantity") or 1)
    price = float(d.get("amount") or 0)

    update_inventory(svc, bird, -qty, item_type="دواجن")
    if price > 0:
        add_transaction(svc, "دخل", f"بيع {qty} {bird}", "دواجن", price, user_name)

    add_pending(svc, user_id, "inventory", "sell_poultry", bird, price, qty, user_name)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == bird), 0)

    send(chat_id,
        f"{D}\n✅ تم تسجيل بيع\n"
        f"الطير: {bird} × {qty}\n"
        f"السعر: {fmt(price)} د.إ\n"
        f"{D}\n"
        f"🐔 رصيد {bird} الحالي: {current_qty}\n"
        f"{D}"
    )

def h_pay_salary(svc, d, chat_id, user_name, user_id):
    worker = (d.get("worker_name") or d.get("item") or "").strip()
    amount = float(d.get("amount") or 0)
    month  = d.get("month") or cur_month_key()

    if not worker or amount <= 0:
        send(chat_id, "❌ حدد اسم العامل والمبلغ.\nمثال: راتب العامل 1400")
        return

    add_transaction(svc, "صرف", f"راتب {worker}", "رواتب", amount, user_name)
    add_pending(svc, user_id, "labor", "pay_salary", worker, amount, 0, user_name,
                json.dumps({"month": month}, ensure_ascii=False))

    send(chat_id,
        f"{D}\n✅ تم صرف الراتب\n"
        f"العامل: {worker}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"الشهر: {month}\n"
        f"{D}"
    )

def h_profit(data, period, chat_id):
    period_data, label = filter_by_period(data, period)
    inc, exp = totals_all(period_data)
    net = inc - exp
    emo = "📈" if net >= 0 else "📉"
    send(chat_id,
        f"{D}\n💰 الصافي ({label}):\n"
        f"الدخل: {fmt(inc)} د.إ\n"
        f"المصروف: {fmt(exp)} د.إ\n"
        f"{emo} الصافي: {fmt(net)} د.إ\n"
        f"{D}"
    )

def h_inventory(svc, chat_id, item_kw=None):
    inv = load_inventory(svc)
    if not inv:
        send(chat_id, "📦 الجرد فارغ.")
        return

    lines = [D, "📦 الجرد الحالي"]
    filtered = inv
    if item_kw:
        item_kw = item_kw.strip()
        filtered = [x for x in inv if item_kw in x["item"]]

    if not filtered:
        # FIX: if keyword filter returns nothing, show all instead of empty
        filtered = inv

    for x in filtered:
        lines.append(f"{x['item']}: {x['qty']}")
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
        lines.append(f"{t['date'][:10]} | {sign}{fmt(t['amount'])} د.إ | {t['item']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_income_by_item(data, d, chat_id):
    # FIX: also check animal_type if item is empty
    kw = (d.get("item") or d.get("animal_type") or "").strip()
    period = d.get("period") or "month"

    if not kw:
        send(chat_id, "❌ حدد البند.\nمثال: كم دخل البيض؟")
        return

    period_data, label = filter_by_period(data, period)
    rows = [
        x for x in period_data
        if x["type"] == "دخل" and (kw in (x["item"] or "") or kw in (x["category"] or ""))
    ]
    total = sum(x["amount"] for x in rows)
    send(chat_id, f"{D}\nالدخل من {kw} ({label}): {fmt(total)} د.إ\n{D}")

def h_income_breakdown(data, d, chat_id):
    period = d.get("period") or "month"
    period_data, label = filter_by_period(data, period)
    inc_rows = [x for x in period_data if x["type"] == "دخل"]

    if not inc_rows:
        send(chat_id, f"لا يوجد دخل في الفترة ({label}).")
        return

    sums = {}
    for x in inc_rows:
        key = x["category"] or x["item"] or "غير محدد"
        sums[key] = sums.get(key, 0) + x["amount"]

    lines = [D, f"📊 الدخل حسب البند ({label})"]
    total = 0
    for k, v in sorted(sums.items(), key=lambda kv: -kv[1]):
        lines.append(f"{k}: {fmt(v)} د.إ")
        total += v
    lines.append(f"{D}\nالإجمالي: {fmt(total)} د.إ\n{D}")
    send(chat_id, "\n".join(lines))

def h_smalltalk(chat_id):
    send(chat_id,
        "أنا بوت العزبة 🤖 أساعدك في:\n"
        "- تسجيل الدخل والمصروف\n"
        "- حركة المواشي والدواجن في الجرد\n"
        "- حساب الإجمالي والربح\n"
        "- عرض آخر العمليات والجرد\n\n"
        "جرب:\n"
        "• بعت بيض بـ 200\n"
        "• صرفنا على الأعلاف 500\n"
        "• تم بيع غنم عدد 2 بمبلغ 1510\n"
        "• كم الربح هذا الشهر؟\n"
        "• كم عدد الغنم في الجرد؟\n"
        "أو اكتب /help"
    )

HELP = """
🌾 بوت مصاريف العزبة

أمثلة:
• بعت بيض بـ 200
• صرفنا على الأعلاف 500
• تم بيع غنم عدد 2 واحد حري وواحد نعيمي بمبلغ 1510
• راتب العامل 1400
• كم دخل البيض الكلي؟
• كم الربح هذا الشهر؟
• كم عدد الغنم في الجرد؟
• قسم لي الدخل حسب التصنيف
• آخر العمليات
"""

# ================== MAIN HTTP HANDLER ==========

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
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            update = json.loads(raw)
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
        text = text.strip()

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
            svc = sheets_svc()
            data = load_transactions(svc)
        except Exception as e:
            send(chat_id, f"❌ خطأ في Google Sheets:\n{e}")
            self._ok()
            return

        d = detect_intent(text)
        intent = d.get("intent") or "clarify"
        period = d.get("period") or "month"

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
            period_data, label = filter_by_period(data, period)
            inc, _ = totals_all(period_data)
            send(chat_id, f"{D}\n💰 الدخل ({label}): {fmt(inc)} د.إ\n{D}")
        elif intent == "expense_total":
            period_data, label = filter_by_period(data, period)
            _, exp = totals_all(period_data)
            send(chat_id, f"{D}\n📤 المصروف ({label}): {fmt(exp)} د.إ\n{D}")
        elif intent == "profit":
            h_profit(data, period, chat_id)
        elif intent == "inventory":
            h_inventory(svc, chat_id, d.get("inventory_item") or d.get("item"))
        elif intent == "last_transactions":
            h_last(data, chat_id)
        elif intent == "income_by_item":
            h_income_by_item(data, d, chat_id)
        elif intent == "income_breakdown":
            h_income_breakdown(data, d, chat_id)
        elif intent == "smalltalk":
            h_smalltalk(chat_id)
        else:
            send(chat_id,
                "❓ ما فهمت.\n"
                "جرب:\n"
                "• بعت بيض بـ 200\n"
                "• كم دخل البيض الكلي؟\n"
                "• كم الربح هذا الشهر؟\n"
                "أو اكتب /help"
            )

        self._ok()

# Ezba (Farm) Telegram Bot – Vercel Python Serverless
# ====================================================
# Google Sheets layout:
#   Transactions: A=التاريخ | B=النوع(دخل/صرف) | C=البند | D=التصنيف | E=المبلغ | F=المستخدم
#   Inventory   : A=Item | B=Type | C=Quantity | D=Notes
#   Pending     : A=UserId | B=Timestamp | C=OperationType | D=Action | E=Item | F=Amount | G=Quantity | H=Person | I=NotesJson

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

# Sheet names
S_TRANSACTIONS = "Transactions"
S_INVENTORY    = "Inventory"
S_PENDING      = "Pending"

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

def update_inventory_qty(svc, item_name, qty_delta, item_type="", notes=""):
    rows = read_sheet(svc, S_INVENTORY)
    values_api = svc.spreadsheets().values()

    for i, r in enumerate(rows, start=2):
        if r and r[0].strip() == item_name.strip():
            old_qty = int(r[2]) if len(r) > 2 and r[2] else 0
            new_qty = max(0, old_qty + qty_delta)
            values_api.update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{S_INVENTORY}!A{i}:D{i}",
                valueInputOption="USER_ENTERED",
                body={"values": [[item_name, r[1] if len(r) > 1 else item_type, new_qty, r[3] if len(r) > 3 else notes]]},
            ).execute()
            return

    # لم يوجد – نضيف صف جديد
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

def norm(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.strip()
    return (
        s.replace("أ", "ا")
         .replace("إ", "ا")
         .replace("آ", "ا")
         .replace("ة", "ه")
         .replace("ى", "ي")
    )

# ── TRANSACTIONS ───────────────────────────────────────────────────────────────
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
                    "type":     r[1],                   # دخل | صرف
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
    append_row(svc, S_TRANSACTIONS, [now_str(), kind, item, category, amount, user])

def totals_all(data):
    inc = sum(x["amount"] for x in data if x["type"] == "دخل")
    exp = sum(x["amount"] for x in data if x["type"] == "صرف")
    return inc, exp

def totals_month(data):
    m = cur_month()
    rows = [x for x in data if x["date"].startswith(m)]
    return totals_all(rows)

# ── INVENTORY ──────────────────────────────────────────────────────────────────
def load_inventory(svc):
    rows = read_sheet(svc, S_INVENTORY)
    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        out.append(
            {
                "item":  r[0],
                "type":  r[1] if len(r) > 1 else "",
                "qty":   int(r[2]) if len(r) > 2 and r[2] else 0,
                "notes": r[3] if len(r) > 3 else "",
            }
        )
    return out

# ── PENDING (بسيط) ────────────────────────────────────────────────────────────
def add_pending(svc, user_id, op_type, action, item, amount, qty, person, notes=""):
    append_row(
        svc,
        S_PENDING,
        [
            str(user_id),
            now_str(),
            op_type,
            action,
            item,
            amount,
            qty,
            person,
            notes,
        ],
    )

# ── AI SYSTEM PROMPT ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
أنت مساعد ذكي لإدارة عزبة (مزرعة) في الإمارات.
اللغة دائماً عربية. أرجع JSON فقط بدون أي شرح إضافي.

البنية:

{
  "intent": "add_income | add_expense | add_livestock | sell_livestock | add_poultry | sell_poultry | pay_salary | income_total | expense_total | profit | inventory | inventory_item | last_transactions | category_total | daily_report | smalltalk | clarify",
  "direction": "in | out | none",
  "item": "",
  "category": "",
  "amount": 0,
  "quantity": 0,
  "animal_type": "",
  "worker_name": "",
  "month": "",
  "period": "month | all",
  "notes": ""
}

التفسير:

- add_income  : المستخدم يصف بيع/دخل مثل "بعنا بيض 200" أو "ورد لنا مبلغ 500".
- add_expense : المستخدم يصف صرف/مشتريات مثل "صرفنا 300 على الاعلاف" أو "دفعنا فاتورة كهرباء 250".
- add_livestock / add_poultry : شراء مواشي/دواجن للجرد (يزيد المخزون).
- sell_livestock / sell_poultry : بيع من المخزون (ينقص المخزون) مع مبلغ دخل.
- pay_salary  : جملة فيها راتب أو معاش لعامل.
- income_total : سؤال عن مجموع الدخل بدون تحديد بند معين. مثال: "كم الدخل الكلي؟"
- expense_total: سؤال عن مجموع المصروف.
- profit      : سؤال عن الربح أو العجز (الدخل - المصروف).
- inventory   : سؤال عن الجرد بشكل عام. مثال: "كم المخزون الحالي؟"
- inventory_item : سؤال عن كمية نوع واحد في الجرد. مثال: "كم عدد الغنم الحري؟" → item = "غنم حري".
- last_transactions : سؤال عن آخر العمليات.
- category_total   :
    * إذا كان السؤال عن بند معيّن مثل "كم دخل البيض؟" أو "كم صرفنا على الاعلاف؟"
      → intent = "category_total", direction = "in" أو "out"، category = الكلمة المفتاحية مثل "بيض" أو "اعلاف".
    * إذا كان السؤال "قسم لي الدخل حسب التصنيف" أو "قسم لي الدخل" بدون ذكر بند
      → intent = "category_total" مع category = "".
- daily_report : سؤال عن تقرير اليوم.
- smalltalk   : دردشة عامة (سلام، شكر، مزاح).
- clarify     : إذا كانت الرسالة غير مفهومة.

قواعد:

- كلمات تدل على دخل (direction = "in"):
  "دخل", "ربح", "بيع", "بعنا", "وردة", "ورد", "استلمنا", "اجمالي المبيعات".
- كلمات تدل على صرف (direction = "out"):
  "صرف", "صرفنا", "دفعنا", "فاتورة", "اشترينا", "شراء", "راتب", "اجار", "ايجار", "ديزل", "بنزين".
- الأسئلة عن "كم دخل البيض؟" أو "كم دخل الغنم؟" → intent = "category_total", direction = "in", category = الكلمة الأساسية مثل "بيض" أو "غنم".
- الأسئلة عن "كم صرفنا على الاعلاف؟" → intent = "category_total", direction = "out", category = "اعلاف".
- "كم دخل البيض الكلي؟" → نفس السابق لكن period = "all".
- إذا لم يذكر فترة استخدم period = "month" (هذا الشهر).
- inventory_item:
  - مثال: "كم عدد البيض في الجرد؟" → item = "بيض".
  - مثال: "كم عدد الغنم السورمالي؟" → item = "غنم سورمالي".
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

# ── HANDLERS: ADD INCOME / EXPENSE ────────────────────────────────────────────
def _summary_lines_for_new_tx(data):
    inc_m, exp_m   = totals_month(data)
    inc_all, exp_all = totals_all(data)
    lines = [
        D,
        "هذا الشهر:",
        f"  دخل: {fmt(inc_m)} د.إ | صرف: {fmt(exp_m)} د.إ",
        "الإجمالي:",
        f"  دخل: {fmt(inc_all)} د.إ | صرف: {fmt(exp_all)} د.إ",
        D,
    ]
    return "\n".join(lines)

def h_add_income(svc, d, chat_id, user_name, user_id):
    item     = d.get("item") or ""
    amount   = float(d.get("amount") or 0)
    category = d.get("category") or item or "دخل عام"

    if not item or amount <= 0:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: بعت بيض بـ 200")
        return

    add_transaction(svc, "دخل", item, category, amount, user_name)
    add_pending(svc, user_id, "income", "add", item, amount, 0, user_name)

    data = load_transactions(svc)
    summary = _summary_lines_for_new_tx(data)

    send(
        chat_id,
        f"{D}\n✅ دخل مسجل\n"
        f"البند: {item}\n"
        f"التصنيف: {category}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{summary}"
    )

def h_add_expense(svc, d, chat_id, user_name, user_id):
    item     = d.get("item") or ""
    amount   = float(d.get("amount") or 0)
    category = d.get("category") or item or "مصروف عام"

    if not item or amount <= 0:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: صرفنا على الأعلاف 500")
        return

    add_transaction(svc, "صرف", item, category, amount, user_name)
    add_pending(svc, user_id, "expense", "add", item, amount, 0, user_name)

    data = load_transactions(svc)
    summary = _summary_lines_for_new_tx(data)

    send(
        chat_id,
        f"{D}\n✅ صرف مسجل\n"
        f"البند: {item}\n"
        f"التصنيف: {category}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{summary}"
    )

# ── LIVESTOCK & POULTRY (تحديث الجرد) ────────────────────────────────────────
def h_add_livestock(svc, d, chat_id, user_name, user_id):
    animal = d.get("animal_type") or d.get("item") or "غنم"
    qty    = int(d.get("quantity") or 1)
    cost   = float(d.get("amount") or 0)

    update_inventory_qty(svc, animal, qty, item_type="مواشي")
    if cost > 0:
        add_transaction(svc, "صرف", f"شراء {qty} {animal}", "مواشي", cost, user_name)

    add_pending(
        svc,
        user_id,
        "inventory",
        "buy_livestock",
        animal,
        cost,
        qty,
        user_name,
    )

    inv = load_inventory(svc)
    current = next((x["qty"] for x in inv if x["item"] == animal), qty)

    send(
        chat_id,
        f"{D}\n✅ تمت إضافة مواشي\n"
        f"النوع: {animal} × {qty}\n"
        f"التكلفة: {fmt(cost)} د.إ\n"
        f"الرصيد الحالي: {current}\n{D}"
    )

def h_sell_livestock(svc, d, chat_id, user_name, user_id):
    animal = d.get("animal_type") or d.get("item") or "غنم"
    qty    = int(d.get("quantity") or 1)
    price  = float(d.get("amount") or 0)

    update_inventory_qty(svc, animal, -qty, item_type="مواشي")
    if price > 0:
        add_transaction(svc, "دخل", f"بيع {qty} {animal}", "غنم", price, user_name)

    add_pending(
        svc,
        user_id,
        "inventory",
        "sell_livestock",
        animal,
        price,
        qty,
        user_name,
    )

    inv = load_inventory(svc)
    current = next((x["qty"] for x in inv if x["item"] == animal), 0)

    send(
        chat_id,
        f"{D}\n✅ تم تسجيل بيع\n"
        f"الحيوان: {animal} × {qty}\n"
        f"السعر: {fmt(price)} د.إ\n"
        f"الرصيد الحالي: {current}\n{D}"
    )

def h_add_poultry(svc, d, chat_id, user_name, user_id):
    bird = d.get("animal_type") or d.get("item") or "دجاج"
    qty  = int(d.get("quantity") or 1)
    cost = float(d.get("amount") or 0)

    update_inventory_qty(svc, bird, qty, item_type="دواجن")
    if cost > 0:
        add_transaction(svc, "صرف", f"شراء {qty} {bird}", "دواجن", cost, user_name)

    add_pending(svc, user_id, "inventory", "buy_poultry", bird, cost, qty, user_name)

    inv = load_inventory(svc)
    current = next((x["qty"] for x in inv if x["item"] == bird), qty)

    send(
        chat_id,
        f"{D}\n✅ تمت إضافة دواجن\n"
        f"النوع: {bird} × {qty}\n"
        f"التكلفة: {fmt(cost)} د.إ\n"
        f"الرصيد الحالي: {current}\n{D}"
    )

def h_sell_poultry(svc, d, chat_id, user_name, user_id):
    bird  = d.get("animal_type") or d.get("item") or "دجاج"
    qty   = int(d.get("quantity") or 1)
    price = float(d.get("amount") or 0)

    update_inventory_qty(svc, bird, -qty, item_type="دواجن")
    if price > 0:
        add_transaction(svc, "دخل", f"بيع {qty} {bird}", "بيض" if "بيض" in bird else "دواجن", price, user_name)

    add_pending(svc, user_id, "inventory", "sell_poultry", bird, price, qty, user_name)

    inv = load_inventory(svc)
    current = next((x["qty"] for x in inv if x["item"] == bird), 0)

    send(
        chat_id,
        f"{D}\n✅ تم تسجيل بيع\n"
        f"الطير: {bird} × {qty}\n"
        f"السعر: {fmt(price)} د.إ\n"
        f"الرصيد الحالي: {current}\n{D}"
    )

# ── PAY SALARY ────────────────────────────────────────────────────────────────
def h_pay_salary(svc, d, chat_id, user_name, user_id):
    worker = d.get("worker_name") or d.get("item") or "عامل"
    amount = float(d.get("amount") or 0)
    month  = d.get("month") or cur_month()

    if amount <= 0:
        send(chat_id, "❌ حدد مبلغ الراتب.\nمثال: راتب العامل 1400")
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
        f"{D}\n✅ تم تسجيل راتب\n"
        f"العامل: {worker}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"الشهر: {month}\n{D}"
    )

# ── PROFIT / TOTALS / CATEGORY ────────────────────────────────────────────────
def h_profit(data, period, chat_id):
    if period == "all":
        inc, exp = totals_all(data)
        label = "لكل الفترة المسجلة"
    else:
        inc, exp = totals_month(data)
        label = "هذا الشهر"

    net = inc - exp
    emoji = "📈" if net >= 0 else "📉"
    send(
        chat_id,
        f"{D}\n💰 الربح ({label})\n"
        f"الدخل: {fmt(inc)} د.إ\n"
        f"المصروف: {fmt(exp)} د.إ\n"
        f"{emoji} الصافي: {fmt(net)} د.إ\n{D}"
    )

def h_income_total(data, period, chat_id):
    if period == "all":
        inc, _ = totals_all(data)
        label = "لكل الفترة المسجلة"
    else:
        inc, _ = totals_month(data)
        label = "هذا الشهر"
    send(chat_id, f"{D}\n💰 الدخل ({label}): {fmt(inc)} د.إ\n{D}")

def h_expense_total(data, period, chat_id):
    if period == "all":
        _, exp = totals_all(data)
        label = "لكل الفترة المسجلة"
    else:
        _, exp = totals_month(data)
        label = "هذا الشهر"
    send(chat_id, f"{D}\n📤 المصروف ({label}): {fmt(exp)} د.إ\n{D}")

def _filter_by_period(rows, period):
    if period == "all":
        return rows
    m = cur_month()
    return [x for x in rows if x["date"].startswith(m)]

def h_category_total(data, d, chat_id):
    cat_raw = d.get("category") or d.get("item") or ""
    cat = norm(cat_raw)
    direction = d.get("direction") or "in"
    period = d.get("period") or "month"

    # إذا ما حدد بند → نعمل تقرير حسب البند
    if not cat:
        rows = _filter_by_period(data, period)
        rows = [r for r in rows if r["type"] == "دخل"]
        if not rows:
            send(chat_id, "لا يوجد دخل في هذه الفترة.")
            return

        sums = {}
        for r in rows:
            key = r["category"] or r["item"] or "غير محدد"
            sums[key] = sums.get(key, 0) + r["amount"]

        label = "هذا الشهر" if period != "all" else "لكل الفترة المسجلة"
        lines = [D, f"📊 الدخل حسب البند ({label})"]
        total = 0
        for k, v in sorted(sums.items(), key=lambda kv: -kv[1]):
            total += v
            lines.append(f"{k}: {fmt(v)} د.إ")
        lines.append(D)
        lines.append(f"الإجمالي: {fmt(total)} د.إ")
        lines.append(D)
        send(chat_id, "\n".join(lines))
        return

    # بند محدد (بيض، غنم، اعلاف...)
    rows = _filter_by_period(data, period)

    def match(r):
        text = norm((r["category"] or "") + " " + (r["item"] or ""))
        return cat in text

    rows = [r for r in rows if match(r)]
    if direction == "out":
        rows = [r for r in rows if r["type"] == "صرف"]
        title = "المصروف"
    else:
        rows = [r for r in rows if r["type"] == "دخل"]
        title = "الدخل"

    total = sum(r["amount"] for r in rows)
    label = "لكل الفترة المسجلة" if period == "all" else "هذا الشهر"

    send(
        chat_id,
        f"{D}\n{title} من {cat_raw} ({label}): {fmt(total)} د.إ\n{D}"
    )

# ── INVENTORY QUERIES ─────────────────────────────────────────────────────────
def h_inventory(svc, chat_id):
    inv = load_inventory(svc)
    if not inv:
        send(chat_id, "📦 الجرد فارغ.")
        return

    lines = [D, "📦 الجرد الحالي:"]
    for r in inv:
        lines.append(f"{r['item']}: {r['qty']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_inventory_item(svc, d, chat_id):
    name_raw = d.get("item") or d.get("animal_type") or ""
    name = norm(name_raw)
    if not name:
        send(chat_id, "❌ حدد اسم الصنف في الجرد.\nمثال: كم عدد الغنم الحري؟")
        return

    inv = load_inventory(svc)
    matches = []
    for r in inv:
        if name in norm(r["item"]):
            matches.append(r)

    if not matches:
        send(chat_id, f"ما لقيت {name_raw} في الجرد الحالي.")
        return

    lines = [D, f"📦 الجرد لـ {name_raw}:"]
    for r in matches:
        lines.append(f"{r['item']}: {r['qty']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

# ── OTHER REPORTS ─────────────────────────────────────────────────────────────
def h_last(data, chat_id):
    if not data:
        send(chat_id, "لا توجد عمليات مسجلة.")
        return
    recent = sorted(data, key=lambda x: x["date"], reverse=True)[:7]
    lines = [D, "🕒 آخر العمليات:"]
    for r in recent:
        sign = "+" if r["type"] == "دخل" else "-"
        lines.append(
            f"{r['date'][:10]} | {r['item']} | {sign}{fmt(r['amount'])} د.إ"
        )
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_daily_report(svc, data, chat_id):
    today = today_str()
    today_rows = [r for r in data if r["date"].startswith(today)]
    inc_t, exp_t = totals_all(today_rows)
    inc_m, exp_m = totals_month(data)
    inv = load_inventory(svc)

    inv_line = " | ".join(f"{r['item']}: {r['qty']}" for r in inv) if inv else "-"

    send(
        chat_id,
        f"{D}\n📋 تقرير اليوم {today}\n"
        f"{D}\nاليوم:\n"
        f"  دخل: {fmt(inc_t)} د.إ | صرف: {fmt(exp_t)} د.إ\n"
        f"{D}\nهذا الشهر:\n"
        f"  دخل: {fmt(inc_m)} د.إ | صرف: {fmt(exp_m)} د.إ\n"
        f"{D}\nالجرد: {inv_line}\n{D}"
    )

# ── HELP ───────────────────────────────────────────────────────────────────────
HELP = """
🌾 بوت العزبة – أوامر مختصرة:

• تسجيل دخل:
  بعت بيض بـ 200
  ورد لنا مبلغ 500

• تسجيل صرف:
  صرفنا على الاعلاف 800
  دفعنا فاتورة كهرباء 350

• مواشي ودواجن والجرد:
  اشترينا 10 غنم بـ 15000
  بعنا 2 غنم بـ 2000
  اشترينا 50 دجاج بـ 1000
  كم عدد الغنم الحري؟
  كم المخزون الحالي؟

• تقارير:
  كم الدخل الكلي؟
  كم دخل البيض الكلي؟
  كم دخل الغنم هذا الشهر؟
  قسم لي الدخل حسب التصنيف
  كم المصروف الكلي؟
  كم الربح هذا الشهر؟
  آخر العمليات
  تقرير اليوم
"""

# ── HTTP HANDLER (Vercel) ─────────────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # no stdout noise on vercel
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
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            update = json.loads(body)
        except Exception:
            self._ok()
            return

        msg = update.get("message") or {}
        if "text" not in msg:
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

        # أوامر مباشرة
        if text in ("/start", "/help", "help", "مساعدة", "شو تسوي", "وش تسوي"):
            send(chat_id, HELP)
            self._ok()
            return

        try:
            svc  = sheets_svc()
            data = load_transactions(svc)
        except Exception as e:
            send(chat_id, f"❌ خطأ في الاتصال بـ Google Sheets:\n{e}")
            self._ok()
            return

        d = detect_intent(text)
        intent = d.get("intent", "clarify")
        period = d.get("period", "month")

        # توجيه حسب الـ intent
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
            h_income_total(data, period, chat_id)

        elif intent == "expense_total":
            h_expense_total(data, period, chat_id)

        elif intent == "profit":
            h_profit(data, period, chat_id)

        elif intent == "inventory":
            h_inventory(svc, chat_id)

        elif intent == "inventory_item":
            h_inventory_item(svc, d, chat_id)

        elif intent == "last_transactions":
            h_last(data, chat_id)

        elif intent == "category_total":
            h_category_total(data, d, chat_id)

        elif intent == "daily_report":
            h_daily_report(svc, data, chat_id)

        elif intent == "smalltalk":
            # رد بسيط مختصر بدون تخريب الشيت
            send(chat_id, "👍 تمام، موجود. اسألني عن الدخل أو المصروف أو الجرد بأي طريقة تعجبك.")
        else:
            send(
                chat_id,
                "❓ ما فهمت الرسالة.\n"
                "جرب أمثلة مثل:\n"
                "• بعت بيض بـ 200\n"
                "• كم دخل البيض الكلي؟\n"
                "• كم دخل الغنم؟\n"
                "• قسم لي الدخل حسب التصنيف\n"
                "• كم عدد الغنم الحري؟\n"
                "أو اكتب /help",
            )

        self._ok()

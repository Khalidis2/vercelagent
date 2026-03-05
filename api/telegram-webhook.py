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

# المستخدمين المسموح لهم
ALLOWED_USERS = {
    47329648:   "Khaled",
    6894180427: "Hamad",
}

UAE_TZ = timezone(timedelta(hours=4))
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# أسماء الشيتات
S_TRANSACTIONS = "Transactions"   # A=التاريخ B=النوع C=البند D=التصنيف E=المبلغ F=المستخدم
S_INVENTORY    = "Inventory"      # A=Item B=Type C=Quantity D=Notes
S_PENDING      = "Pending"        # A=UserId B=Timestamp C=OperationType D=Action E=Item F=Amount G=Quantity H=Person I=NotesJson

D = "──────────────"   # فاصل

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

def update_inventory(svc, item_name, qty_delta, item_type="", notes=""):
    """زيادة أو إنقاص كمية الصنف في شيت Inventory."""
    rows = read_sheet(svc, S_INVENTORY)
    values_api = svc.spreadsheets().values()

    for i, r in enumerate(rows):
        if r and r[0].strip() == item_name.strip():
            old_qty = int(r[2]) if len(r) > 2 and r[2] else 0
            new_qty = max(0, old_qty + int(qty_delta))
            row_num = i + 2
            values_api.update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{S_INVENTORY}!A{row_num}:D{row_num}",
                valueInputOption="USER_ENTERED",
                body={"values": [[item_name, r[1] if len(r) > 1 else item_type, new_qty, r[3] if len(r) > 3 else notes]]},
            ).execute()
            return

    # إذا ما لقيناه نضيفه كصف جديد (إذا الكمية موجبة)
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
    """ترجع (data_filtered, label)."""
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
        return out, f"آخر ٧ أيام من {start} إلى {now.date()}"

    # الافتراضي: هذا الشهر
    key = now.strftime("%Y-%m")
    return [x for x in data if x["date"].startswith(key)], "هذا الشهر"

def split_animals_for_inventory(animal_str, qty):
    """
    إذا حصل أكثر من نوع في الجملة (مفصول بـ '،' أو 'و') نقسم الكمية عليهم.
    مثال: "غنم حري، غنم نعيمي" مع qty=2 → [("غنم حري", 1), ("غنم نعيمي", 1)]
    """
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
        out.append(
            {
                "date":     r[0],
                "type":     r[1],          # دخل | صرف
                "item":     r[2],
                "category": r[3] if len(r) > 3 else "",
                "amount":   amount,
                "user":     r[5] if len(r) > 5 else "",
            }
        )
    return out

def add_transaction(svc, ttype, item, category, amount, user):
    append_row(
        svc,
        S_TRANSACTIONS,
        [now_str(), ttype, item, category, amount, user],
    )

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
        out.append(
            {
                "item":  r[0],
                "type":  r[1] if len(r) > 1 else "",
                "qty":   int(r[2]) if len(r) > 2 and r[2] else 0,
                "notes": r[3] if len(r) > 3 else "",
            }
        )
    return out

# ================== PENDING (للتتبع فقط) =======

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

# ================== AI INTENT ==================

SYSTEM_PROMPT = """
أنت مساعد لإدارة عزبة/مزرعة، تتعامل مع:
- تسجيل الدخل (بيع منتجات مثل بيض، غنم، إلخ)
- تسجيل المصروفات (أعلاف، فواتير، رواتب)
- تسجيل حركة المواشي والدواجن في الجرد
- حساب إجمالي الدخل والمصروف والربح
- الاستعلام عن الجرد وآخر العمليات

أرجع دائماً JSON فقط بدون أي نص آخر بالشكل التالي:

{
  "intent": "add_income | add_expense | add_livestock | sell_livestock | add_poultry | sell_poultry | pay_salary | income_total | expense_total | profit | inventory | last_transactions | income_by_item | income_breakdown | smalltalk | clarify",
  "direction": "in | out | none",
  "item": "",
  "category": "",
  "amount": 0,
  "quantity": 0,
  "animal_type": "",
  "worker_name": "",
  "month": "",
  "period": "today | week | month | all",
  "inventory_item": ""
}

القواعد:

- أي جملة فيها "بعت" أو "وردة" أو "دخل" أو "أجرنا" → intent = "add_income", direction = "in".
- أي جملة فيها "اشترينا" أو "صرفنا" أو "دفعنا" أو "فاتورة" أو "راتب" → intent = "add_expense", direction = "out".
- إذا كان الكلام عن شراء مواشي (غنم، بقر، إبل...) → intent = "add_livestock", animal_type = نوع الحيوان, quantity = العدد, amount = المبلغ إن وجد.
- بيع مواشي → intent = "sell_livestock".
- شراء دواجن/طيور/بيض للتربية → intent = "add_poultry".
- بيع دواجن/بيض كمنتج → intent = "sell_poultry".
- جملة فيها "راتب" أو "معاش" لعامل → intent = "pay_salary", worker_name = اسم العامل, amount = المبلغ.
- أسئلة مثل "كم الدخل؟" أو "كم دخل العزبة؟" → intent = "income_total".
- "كم صرفنا؟" → "expense_total".
- "كم الربح؟" أو "كم الصافي؟" → "profit".
- "الجرد" أو "كم عدد الغنم" أو "كم البيض في الجرد؟" → intent = "inventory", inventory_item = الكلمة الرئيسة مثل "غنم" أو "بيض".
- "آخر العمليات" → intent = "last_transactions".
- "كم دخل البيض؟" أو "كم دخل الغنم الكلي؟" → intent = "income_by_item", item = "بيض" أو "غنم".
- "قسم لي الدخل حسب التصنيف" أو "قسم لي الدخل" → intent = "income_breakdown".
- الحديث العام مثل "شو تسوي؟" أو "منو انت؟" → intent = "smalltalk".
- إذا لم تفهم اطلاقاً → intent = "clarify".

period:
- إذا ذكر "اليوم" → "today"
- "هالأسبوع" أو "آخر أسبوع" → "week"
- "هالشهر" أو "هذا الشهر" → "month"
- "إجمالي" أو "الكلي" أو "لكل الفترة" → "all"
- الافتراضي إذا ما ذكر فترة = "month".
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

# ================== HANDLERS ====================

def h_add_income(svc, d, chat_id, user_name, user_id):
    item     = (d.get("item") or d.get("category") or "").strip()
    amount   = float(d.get("amount") or 0)
    category = (d.get("category") or item or "").strip()

    if not item or amount <= 0:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: بعت بيض بـ 200")
        return

    add_transaction(svc, "دخل", item, category, amount, user_name)
    add_pending(svc, user_id, "transaction", "add_income", item, amount, 0, user_name)

    data = load_transactions(svc)
    month_data, _label_m = filter_by_period(data, "month")
    all_inc, all_exp = totals_all(data)
    m_inc, m_exp     = totals_all(month_data)

    send(
        chat_id,
        f"{D}\n✅ دخل مسجل\n"
        f"البند: {item}\n"
        f"التصنيف: {category}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}\n"
        f"📊 هذا الشهر:\n"
        f"  دخل: {fmt(m_inc)} | صرف: {fmt(m_exp)}\n"
        f"📊 كل الفترة:\n"
        f"  دخل: {fmt(all_inc)} | صرف: {fmt(all_exp)}\n"
        f"{D}"
    )

def h_add_expense(svc, d, chat_id, user_name, user_id):
    item     = (d.get("item") or d.get("category") or "").strip()
    amount   = float(d.get("amount") or 0)
    category = (d.get("category") or item or "").strip()

    if not item or amount <= 0:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: صرفنا على الأعلاف 800")
        return

    add_transaction(svc, "صرف", item, category, amount, user_name)
    add_pending(svc, user_id, "transaction", "add_expense", item, amount, 0, user_name)

    data = load_transactions(svc)
    month_data, _label_m = filter_by_period(data, "month")
    all_inc, all_exp = totals_all(data)
    m_inc, m_exp     = totals_all(month_data)

    send(
        chat_id,
        f"{D}\n✅ صرف مسجل\n"
        f"البند: {item}\n"
        f"التصنيف: {category}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}\n"
        f"📊 هذا الشهر:\n"
        f"  دخل: {fmt(m_inc)} | صرف: {fmt(m_exp)}\n"
        f"📊 كل الفترة:\n"
        f"  دخل: {fmt(all_inc)} | صرف: {fmt(all_exp)}\n"
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

    send(
        chat_id,
        f"{D}\n✅ تم إضافة المواشي\n"
        f"النوع: {animal}\n"
        f"العدد المضاف: {qty}\n"
        f"التكلفة: {fmt(cost)} د.إ\n"
        f"{D}\n"
        f"🐄 رصيد {animal} الحالي: {current_qty}\n"
        f"{D}"
    )

def h_sell_livestock(svc, d, chat_id, user_name, user_id):
    animal_raw = (d.get("animal_type") or d.get("item") or "غنم").strip()
    qty        = int(d.get("quantity") or 1)
    price      = float(d.get("amount") or 0)

    # توزيع الكمية على الأنواع الموجودة في الجملة (غنم حري، غنم نعيمي...)
    splits = split_animals_for_inventory(animal_raw, qty)
    for name, q in splits:
        update_inventory(svc, name, -q, item_type="مواشي")

    if price > 0:
        add_transaction(svc, "دخل", f"بيع {qty} {animal_raw}", "مواشي", price, user_name)

    add_pending(svc, user_id, "inventory", "sell_livestock", animal_raw, price, qty, user_name)

    inv = load_inventory(svc)

    lines = [D, "✅ تم تسجيل بيع"]
    lines.append(f"الحيوان: {animal_raw} × {qty}")
    lines.append(f"السعر: {fmt(price)} د.إ")
    lines.append("الرصيد الحالي:")
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

    send(
        chat_id,
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

    send(
        chat_id,
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
    send(
        chat_id,
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
    if item_kw:
        item_kw = item_kw.strip()
        inv = [x for x in inv if item_kw in x["item"]]

    for x in inv:
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
        lines.append(
            f"{t['date'][:10]} | {sign}{fmt(t['amount'])} د.إ | {t['item']}"
        )
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_income_by_item(data, d, chat_id):
    kw = (d.get("item") or "").strip()
    period = d.get("period") or "month"

    if not kw:
        send(chat_id, "❌ حدد البند.\nمثال: كم دخل البيض؟")
        return

    period_data, label = filter_by_period(data, period)
    rows = [
        x
        for x in period_data
        if x["type"] == "دخل" and (kw in (x["item"] or "") or kw in (x["category"] or ""))
    ]
    total = sum(x["amount"] for x in rows)
    send(
        chat_id,
        f"{D}\nالدخل من {kw} ({label}): {fmt(total)} د.إ\n{D}"
    )

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
    send(
        chat_id,
        "أنا بوت العزبة 🤖 أساعدك في:\n"
        "- تسجيل الدخل والمصروف.\n"
        "- تسجيل حركة المواشي والدواجن في الجرد.\n"
        "- حساب إجمالي الدخل والمصروف والربح.\n"
        "- عرض آخر العمليات والجرد.\n\n"
        "جرب تكتب مثلاً:\n"
        "• بعت بيض بـ 200\n"
        "• كم دخل البيض الكلي؟\n"
        "• كم الربح هذا الشهر؟\n"
        "• كم عدد الغنم في الجرد؟"
    )

HELP = """
🌾 بوت مصاريف العزبة

أمثلة:
- بعت بيض بـ 200
- صرفنا على الأعلاف 500
- تم بيع غنم عدد 2 واحد حري وواحد نعيمي بمبلغ 1510
- راتب العامل 1400
- كم دخل البيض الكلي؟
- كم الربح هذا الشهر؟
- كم عدد الغنم في الجرد؟
- قسم لي الدخل حسب التصنيف
- آخر العمليات
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
            _inc, exp = totals_all(period_data)
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
            send(
                chat_id,
                "❓ ما فهمت.\n"
                "جرب أمثلة مثل:\n"
                "• بعت بيض بـ 200\n"
                "• كم دخل البيض الكلي؟\n"
                "• كم الربح هذا الشهر؟\n"
                "أو اكتب /help"
            )

        self._ok()

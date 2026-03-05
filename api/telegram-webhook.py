# api/telegram-webhook.py
# Telegram → Vercel (Python) → Google Sheets → OpenAI (intent only)

from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta, date

import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ── ENV / CONFIG ──────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY              = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID              = os.environ.get("SPREADSHEET_ID")

# فقط المستخدمين المصرح لهم
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

D = "────────────────"  # خط فاصل بسيط

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

# ── GOOGLE SHEETS HELPERS ─────────────────────────────────────────────────────

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

def update_inventory_quantity_delta(svc, item, delta_qty, item_type="", notes=""):
    """زيادة أو نقصان كمية صنف في ورقة Inventory."""
    values_api = svc.spreadsheets().values()
    rows = read_sheet(svc, S_INVENTORY, "A2:D")
    row_index = None
    current_qty = 0
    cur_type = ""
    cur_notes = ""
    for i, r in enumerate(rows, start=2):
        if r and r[0].strip() == item.strip():
            row_index = i
            cur_type = r[1] if len(r) > 1 else ""
            try:
                current_qty = int(float(r[2])) if len(r) > 2 and r[2] else 0
            except Exception:
                current_qty = 0
            cur_notes = r[3] if len(r) > 3 else ""
            break

    new_qty = max(0, current_qty + delta_qty)

    if row_index is not None:
        values_api.update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{S_INVENTORY}!A{row_index}:D{row_index}",
            valueInputOption="USER_ENTERED",
            body={"values": [[item, cur_type or item_type, new_qty, cur_notes or notes]]},
        ).execute()
    else:
        if new_qty <= 0:
            return
        append_row(svc, S_INVENTORY, [item, item_type, new_qty, notes])

# ── TIME / FORMAT ─────────────────────────────────────────────────────────────

def now_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")

def today_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d")

def current_month_prefix():
    return datetime.now(UAE_TZ).strftime("%Y-%m")

def fmt(x):
    try:
        f = float(x)
        if abs(f - int(f)) < 1e-9:
            return str(int(f))
        return f"{f:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(x)

# ── ARABIC NORMALIZATION / CATEGORIES ─────────────────────────────────────────

def normalize_ar(s: str) -> str:
    if not isinstance(s, str):
        return ""
    t = s.strip()
    repl = {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ى": "ي",
        "ة": "ه",
        "ئ": "ي",
        "ؤ": "و",
    }
    for k, v in repl.items():
        t = t.replace(k, v)
    return t

def text_has_any(text: str, keywords) -> bool:
    n = normalize_ar(text)
    return any(k in n for k in keywords)

def canonical_category_from_text(text: str) -> str:
    n = normalize_ar(text)
    if any(w in n for w in ["بيض", "بيوض"]):
        return "بيض"
    if any(w in n for w in ["غنم", "عنم", "خروف", "خرفان"]):
        return "غنم"
    if any(w in n for w in ["دجاج", "فروج", "دواجن"]):
        return "دواجن"
    if any(w in n for w in ["علف", "اعلاف", "كل", "كلأ"]):
        return "أعلاف"
    if any(w in n for w in ["راتب", "رواتب"]):
        return "رواتب"
    return text.strip() or "أخرى"

def match_category_or_item(cat: str, tx: dict) -> bool:
    """هل هذه العملية تخص التصنيف/البند المطلوب؟"""
    if not cat:
        return True
    base = canonical_category_from_text(cat)
    item = tx.get("item", "") or ""
    category = tx.get("category", "") or ""
    full = f"{item} {category}"

    if base == "بيض":
        return text_has_any(full, ["بيض", "بيوض"])
    if base == "غنم":
        return text_has_any(full, ["غنم", "عنم", "خروف", "خرفان"])
    if base == "دواجن":
        return text_has_any(full, ["دواجن", "دجاج", "فروج"])
    if base == "أعلاف":
        return text_has_any(full, ["علف", "اعلاف", "كل", "كلأ"])
    # fallback: تطابق نصي عام
    return base in normalize_ar(full)

# ── TRANSACTIONS HELPERS ──────────────────────────────────────────────────────

def load_transactions(svc):
    rows = read_sheet(svc, S_TRANSACTIONS, "A2:F")
    txs = []
    for r in rows:
        if len(r) < 5:
            continue
        try:
            dt_str = r[0]
            tx_type = r[1]          # دخل / صرف
            item = r[2]
            category = r[3] if len(r) > 3 else ""
            amount = float(r[4])
            user = r[5] if len(r) > 5 else ""
        except Exception:
            continue
        txs.append(
            {
                "date": dt_str,
                "type": tx_type,
                "item": item,
                "category": category,
                "amount": amount,
                "user": user,
            }
        )
    return txs

def add_transaction(svc, kind, item, category, amount, user):
    append_row(svc, S_TRANSACTIONS, [now_str(), kind, item, category, amount, user])

def filter_by_period(txs, period: str):
    if not txs:
        return []
    today = datetime.now(UAE_TZ).date()
    if period == "today":
        return [t for t in txs if t["date"][:10] == today_str()]
    if period == "week":
        start = today - timedelta(days=6)
        return [
            t for t in txs
            if start <= datetime.strptime(t["date"][:10], "%Y-%m-%d").date() <= today
        ]
    if period == "month":
        m = current_month_prefix()
        return [t for t in txs if t["date"].startswith(m)]
    # all
    return txs

def totals_all(txs):
    inc = sum(t["amount"] for t in txs if t["type"] == "دخل")
    exp = sum(t["amount"] for t in txs if t["type"] == "صرف")
    return inc, exp

# ── INVENTORY ─────────────────────────────────────────────────────────────────

def load_inventory(svc):
    rows = read_sheet(svc, S_INVENTORY, "A2:D")
    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        item = r[0]
        t = r[1] if len(r) > 1 else ""
        try:
            qty = int(float(r[2])) if len(r) > 2 and r[2] else 0
        except Exception:
            qty = 0
        notes = r[3] if len(r) > 3 else ""
        out.append({"item": item, "type": t, "qty": qty, "notes": notes})
    return out

# ── PENDING ───────────────────────────────────────────────────────────────────

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

# ── OPENAI INTENT DETECTION ───────────────────────────────────────────────────

SYSTEM_PROMPT = """
أنت مساعد ذكي لإدارة عزبة صغيرة (مزرعة) في الإمارات.
البيانات الفعلية موجودة في Google Sheets، ولا تراها أنت مباشرة.
دورك فقط فهم نية المستخدم (intent) وبعض الحقول المساعدة.

أرجع دائماً JSON فقط بالشكل التالي:

{
  "intent": "add_income | add_expense | add_livestock | sell_livestock | add_poultry | sell_poultry | pay_salary | income_total | expense_total | profit | inventory | last_transactions | category_total | category_breakdown | daily_report | smalltalk | clarify",
  "direction": "in | out | none",
  "item": "",
  "category": "",
  "amount": 0,
  "quantity": 0,
  "animal_type": "",
  "gender": "",
  "worker_name": "",
  "month": "",
  "period": "today | week | month | all",
  "answer": ""
}

تفسير الحقول:
- intent: نوع الطلب.
- direction: 
    in  = دخل (مال داخل للصندوق)
    out = صرف (مال خارج من الصندوق)
- item: اسم البند كما يفهمه الإنسان (بيض، علف، راتب العامل، غنم حري، ...).
- category: تصنيف مختصر مفيد للجمع (بيض، غنم، دواجن، أعلاف، رواتب...).
- amount: المبلغ المذكور (بدون عملة).
- quantity: عدد الرؤوس / الطيور إن وُجد.
- animal_type: نوع الحيوان أو الطير (غنم، بقر، إبل، دجاج...).
- month: نص مثل "2026-02" أو اسم شهر بالعربي إذا ذكر.
- period:
    - month = هذا الشهر (الافتراضي إذا سأل بدون تحديد).
    - all   = كل الفترة المسجلة.
    - today/week حسب النص.
- smalltalk: للأسئلة الاجتماعية أو العامة، ضع intent="smalltalk" واكتب الرد المناسب في الحقل answer.
- clarify: إذا كانت الرسالة غامضة جداً.

قواعد:
- أي بيع / دخل للصندوق → direction = "in"
- أي شراء / دفع / مصروف / راتب / فاتورة → direction = "out"
- إذا قال: "كم الربح" أو "كم الصافي" → intent = "profit".
- إذا قال: "كم الدخل" بدون تحديد بند → intent = "income_total".
- إذا قال: "كم الصرف" أو "كم المصروف" → intent = "expense_total".
- إذا قال: "آخر العمليات" أو "اخر العمليات" → intent = "last_transactions".
- إذا قال: "كم دخل البيض؟" أو "كم دخل البيض الكلي؟" → intent = "category_total", direction="in", category="بيض".
- إذا قال: "كم صرفنا على الأعلاف؟" → intent = "category_total", direction="out", category="أعلاف".
- إذا قال: "قسم لي الدخل حسب التصنيف" أو "قسم لي الدخل على حسب البند" → intent = "category_breakdown", direction="in".
- إذا قال: "تقرير اليوم" أو "شو وضع اليوم" → intent = "daily_report".
- إذا قال: "كم المواشي الحالية" أو "كم عندنا غنم/دجاج" → intent = "inventory".
- أي كلام ترحيب أو أسئلة عامة مثل "كيفك؟" أو "شو تسوي؟" → intent="smalltalk" مع نص الرد في "answer".

الفترة (period):
- إذا قال "هذا الشهر" → period="month".
- "هالاسبوع / الأسبوع" → period="week".
- "اليوم" → period="today".
- "الكلي / الكل / من البداية" → period="all".
- إذا لم يحدد → period="month".

أمثلة سريعة (فقط من أجل الفهم، لا تُرجعها للمستخدم):

س: "بعت بيض بـ 200"
→ {
  "intent": "add_income",
  "direction": "in",
  "item": "بيض",
  "category": "بيض",
  "amount": 200,
  "quantity": 0,
  "period": "month"
}

س: "صرفنا على الأعلاف 800"
→ {
  "intent": "add_expense",
  "direction": "out",
  "item": "أعلاف",
  "category": "أعلاف",
  "amount": 800,
  "period": "month"
}

س: "كم دخل البيض الكلي؟"
→ {
  "intent": "category_total",
  "direction": "in",
  "category": "بيض",
  "period": "all"
}

س: "قسم لي الدخل حسب التصنيف"
→ {
  "intent": "category_breakdown",
  "direction": "in",
  "period": "month"
}

س: "كم الربح هذا الشهر؟"
→ {
  "intent": "profit",
  "period": "month"
}

س: "كم صرفنا على الأعلاف الكلي؟"
→ {
  "intent": "category_total",
  "direction": "out",
  "category": "أعلاف",
  "period": "all"
}

س: "كيف حالك؟"
→ {
  "intent": "smalltalk",
  "answer": "تمام والحمدلله 🤍 كيف أمور العزبة وياك؟"
}
""".strip()

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
        raw = completion.choices[0].message.content
        return json.loads(raw)
    except Exception as e:
        return {"intent": "clarify", "_error": str(e)}

# ── HANDLERS: ADD INCOME / EXPENSE / LIVESTOCK / POULTRY / SALARY ────────────

def h_add_income(svc, d, chat_id, user_name, user_id, all_txs):
    item = d.get("item") or ""
    amount = d.get("amount") or 0
    category = d.get("category") or item
    if not item or not amount:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: بعت بيض بـ 200")
        return
    add_transaction(svc, "دخل", item, category, amount, user_name)
    add_pending(svc, user_id, "income", "add", item, amount, 0, user_name)

    # تحديث البيانات بعد الإضافة
    txs = load_transactions(svc)
    month_txs = filter_by_period(txs, "month")
    inc, exp = totals_all(month_txs)
    send(
        chat_id,
        f"{D}\n✅ دخل مسجل\n"
        f"البند: {item}\n"
        f"التصنيف: {category}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}\n"
        f"📊 هذا الشهر:\n"
        f"الدخل: {fmt(inc)} د.إ | الصرف: {fmt(exp)} د.إ",
    )

def h_add_expense(svc, d, chat_id, user_name, user_id, all_txs):
    item = d.get("item") or ""
    amount = d.get("amount") or 0
    category = d.get("category") or item
    if not item or not amount:
        send(chat_id, "❌ حدد البند والمبلغ.\nمثال: صرفنا على الأعلاف 800")
        return
    add_transaction(svc, "صرف", item, category, amount, user_name)
    add_pending(svc, user_id, "expense", "add", item, amount, 0, user_name)

    txs = load_transactions(svc)
    month_txs = filter_by_period(txs, "month")
    inc, exp = totals_all(month_txs)
    warn = "\n⚠️ المصروفات تجاوزت الدخل!" if exp > inc else ""
    send(
        chat_id,
        f"{D}\n✅ صرف مسجل\n"
        f"البند: {item}\n"
        f"التصنيف: {category}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}\n"
        f"📊 هذا الشهر:\n"
        f"الدخل: {fmt(inc)} د.إ | الصرف: {fmt(exp)} د.إ{warn}",
    )

def h_add_livestock(svc, d, chat_id, user_name, user_id):
    animal = d.get("animal_type") or d.get("item") or "غنم"
    qty = int(d.get("quantity") or 1)
    cost = d.get("amount") or 0
    gender = d.get("gender") or ""
    if not animal:
        send(chat_id, "❌ حدد نوع الحيوان والعدد.\nمثال: اشترينا 10 عنم بـ 15000")
        return

    update_inventory_quantity_delta(svc, animal, qty, "مواشي", gender)
    if cost:
        add_transaction(svc, "صرف", f"شراء {qty} {animal}", "مواشي", cost, user_name)
    notes = json.dumps(
        {"gender": gender, "cost_per_head": (cost / qty) if qty else 0},
        ensure_ascii=False,
    )
    add_pending(
        svc,
        user_id,
        "inventory",
        "buy_livestock",
        animal,
        cost,
        qty,
        user_name,
        notes,
    )

    inv = load_inventory(svc)
    cur = next((x["qty"] for x in inv if x["item"] == animal), qty)
    send(
        chat_id,
        f"{D}\n✅ تم إضافة المواشي\n"
        f"النوع: {animal}\n"
        f"العدد المضاف: {qty}\n"
        f"الجنس: {gender or '-'}\n"
        f"التكلفة الإجمالية: {fmt(cost)} د.إ\n"
        f"{D}\n"
        f"🐄 رصيد {animal} الحالي: {cur}",
    )

def h_sell_livestock(svc, d, chat_id, user_name, user_id):
    animal = d.get("animal_type") or d.get("item") or "غنم"
    qty = int(d.get("quantity") or 1)
    price = d.get("amount") or 0
    item_text = d.get("item") or ""
    is_slaughter = any(w in item_text for w in ["ذبح", "ذبيحه", "ذبيحة"])
    label = "ذبح" if is_slaughter else "بيع"

    update_inventory_quantity_delta(svc, animal, -qty, "مواشي")
    if price and not is_slaughter:
        add_transaction(svc, "دخل", f"بيع {qty} {animal}", "مواشي", price, user_name)

    add_pending(
        svc,
        user_id,
        "inventory",
        f"{label}_livestock",
        animal,
        price,
        qty,
        user_name,
    )

    inv = load_inventory(svc)
    cur = next((x["qty"] for x in inv if x["item"] == animal), 0)
    send(
        chat_id,
        f"{D}\n✅ تم تسجيل {label}\n"
        f"الحيوان: {animal} × {qty}\n"
        f"السعر: {fmt(price)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}\n"
        f"🐄 رصيد {animal} الحالي: {cur}",
    )

def h_add_poultry(svc, d, chat_id, user_name, user_id):
    bird = d.get("animal_type") or d.get("item") or "دجاج"
    qty = int(d.get("quantity") or 1)
    cost = d.get("amount") or 0

    update_inventory_quantity_delta(svc, bird, qty, "دواجن")
    if cost:
        add_transaction(svc, "صرف", f"شراء {qty} {bird}", "دواجن", cost, user_name)
    add_pending(
        svc, user_id, "inventory", "buy_poultry", bird, cost, qty, user_name
    )

    inv = load_inventory(svc)
    cur = next((x["qty"] for x in inv if x["item"] == bird), qty)
    send(
        chat_id,
        f"{D}\n✅ تم إضافة الدواجن\n"
        f"النوع: {bird} × {qty}\n"
        f"التكلفة: {fmt(cost)} د.إ\n"
        f"{D}\n"
        f"🐔 رصيد {bird} الحالي: {cur}",
    )

def h_sell_poultry(svc, d, chat_id, user_name, user_id):
    bird = d.get("animal_type") or d.get("item") or "دجاج"
    qty = int(d.get("quantity") or 1)
    price = d.get("amount") or 0

    update_inventory_quantity_delta(svc, bird, -qty, "دواجن")
    if price:
        add_transaction(svc, "دخل", f"بيع {qty} {bird}", "دواجن", price, user_name)
    add_pending(
        svc, user_id, "inventory", "sell_poultry", bird, price, qty, user_name
    )

    inv = load_inventory(svc)
    cur = next((x["qty"] for x in inv if x["item"] == bird), 0)
    send(
        chat_id,
        f"{D}\n✅ تم تسجيل البيع\n"
        f"الطير: {bird} × {qty}\n"
        f"السعر: {fmt(price)} د.إ\n"
        f"{D}\n"
        f"🐔 رصيد {bird} الحالي: {cur}",
    )

def h_pay_salary(svc, d, chat_id, user_name, user_id):
    worker = d.get("worker_name") or d.get("item") or "العامل"
    amount = d.get("amount") or 0
    month = d.get("month") or current_month_prefix()
    if not amount:
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
        f"{D}\n✅ تم صرف الراتب\n"
        f"العامل: {worker}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"الشهر: {month}\n"
        f"بواسطة: {user_name}\n"
        f"{D}",
    )

# ── SUMMARY / REPORT HANDLERS ─────────────────────────────────────────────────

def h_profit(txs, period, chat_id):
    filtered = filter_by_period(txs, period)
    inc, exp = totals_all(filtered)
    net = inc - exp
    label = {
        "today": "اليوم",
        "week": "هذا الأسبوع",
        "month": "هذا الشهر",
        "all": "لكل الفترة المسجلة",
    }.get(period, "هذا الشهر")
    emoji = "📈" if net >= 0 else "📉"
    send(
        chat_id,
        f"{D}\n💰 الصافي ({label})\n"
        f"الدخل: {fmt(inc)} د.إ\n"
        f"الصرف: {fmt(exp)} د.إ\n"
        f"{emoji} الصافي: {fmt(net)} د.إ\n"
        f"{D}",
    )

def h_inventory_report(svc, chat_id):
    inv = load_inventory(svc)
    if not inv:
        send(chat_id, "📦 الجرد الحالي فارغ.")
        return
    lines = [D, "📦 الجرد الحالي:"]
    for x in inv:
        t = x["type"] or "-"
        lines.append(f"- {x['item']} ({t}): {x['qty']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_last_transactions(txs, chat_id):
    if not txs:
        send(chat_id, "لا توجد عمليات مسجلة.")
        return
    recent = sorted(txs, key=lambda x: x["date"], reverse=True)[:7]
    lines = [D, "🕒 آخر العمليات"]
    for t in recent:
        sign = "+" if t["type"] == "دخل" else "-"
        lines.append(
            f"{t['date'][:10]} | {t['item']} | {sign}{fmt(t['amount'])} د.إ ({t['type']})"
        )
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_category_total(txs, d, chat_id):
    cat = d.get("category") or d.get("item") or ""
    period = d.get("period") or "month"
    direction = d.get("direction") or "in"

    if not cat:
        send(chat_id, "❌ حدد البند أو التصنيف.\nمثال: كم دخل البيض؟ أو كم صرفنا على الأعلاف؟")
        return

    filtered = filter_by_period(txs, period)
    if direction == "in":
        filtered = [t for t in filtered if t["type"] == "دخل"]
    elif direction == "out":
        filtered = [t for t in filtered if t["type"] == "صرف"]

    filtered = [t for t in filtered if match_category_or_item(cat, t)]
    total = sum(t["amount"] for t in filtered)

    label = {
        "today": "اليوم",
        "week": "هذا الأسبوع",
        "month": "هذا الشهر",
        "all": "لكل الفترة المسجلة",
    }.get(period, "هذا الشهر")

    dir_label = "الدخل" if direction == "in" else "المصروف"
    send(chat_id, f"{D}\n{dir_label} من {cat} ({label}): {fmt(total)} د.إ\n{D}")

def h_category_breakdown(txs, d, chat_id):
    period = d.get("period") or "month"
    direction = d.get("direction") or "in"

    filtered = filter_by_period(txs, period)
    if direction == "in":
        filtered = [t for t in filtered if t["type"] == "دخل"]
    elif direction == "out":
        filtered = [t for t in filtered if t["type"] == "صرف"]

    if not filtered:
        send(chat_id, "لا توجد عمليات في هذه الفترة.")
        return

    groups = {}
    for t in filtered:
        key = canonical_category_from_text(t["category"] or t["item"])
        groups.setdefault(key, 0)
        groups[key] += t["amount"]

    label = {
        "today": "اليوم",
        "week": "هذا الأسبوع",
        "month": "هذا الشهر",
        "all": "لكل الفترة المسجلة",
    }.get(period, "هذا الشهر")

    lines = [D, f"📊 {('الدخل' if direction=='in' else 'المصروف')} حسب البند ({label}):"]
    total = 0
    for key, val in sorted(groups.items(), key=lambda kv: -kv[1]):
        total += val
        lines.append(f"{key}: {fmt(val)} د.إ")
    lines.append(D)
    lines.append(f"الإجمالي: {fmt(total)} د.إ")
    send(chat_id, "\n".join(lines))

def h_daily_report(svc, txs, chat_id):
    today = today_str()
    today_txs = [t for t in txs if t["date"][:10] == today]
    t_inc, t_exp = totals_all(today_txs)

    month_txs = filter_by_period(txs, "month")
    m_inc, m_exp = totals_all(month_txs)

    inv = load_inventory(svc)
    inv_str = (
        " | ".join(f"{x['item']}: {x['qty']}" for x in inv) if inv else "لا يوجد جرد مسجل"
    )

    send(
        chat_id,
        f"{D}\n📋 تقرير اليوم — {today}\n{D}\n"
        f"📅 اليوم:\n"
        f"الدخل: {fmt(t_inc)} د.إ | الصرف: {fmt(t_exp)} د.إ | الصافي: {fmt(t_inc - t_exp)} د.إ\n"
        f"{D}\n"
        f"📆 هذا الشهر:\n"
        f"الدخل: {fmt(m_inc)} د.إ | الصرف: {fmt(m_exp)} د.إ | الصافي: {fmt(m_inc - m_exp)} د.إ\n"
        f"{D}\n"
        f"📦 الجرد الحالي:\n{inv_str}\n"
        f"{D}",
    )

# ── HELP TEXT ─────────────────────────────────────────────────────────────────

HELP_TEXT = """
🌾 بوت مصاريف العزبة

أمثلة للتسجيل:
• بعت بيض بـ 200
• وردة غنم 4699
• صرفنا على الأعلاف 800
• اشترينا 10 عنم بـ 15000
• بعنا 5 دجاج بـ 300
• راتب العامل 1400

أمثلة للاستفسار:
• كم دخلنا الكلي؟
• كم دخل البيض؟
• كم صرفنا على الأعلاف؟
• كم الربح هذا الشهر؟
• قسم لي الدخل حسب التصنيف
• آخر العمليات
• تقرير اليوم
• كم المواشي الحالية؟
""".strip()

# ── MAIN HTTP HANDLER (VERCEL) ────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        # منع لوجات إضافية في Vercel
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

        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            self._ok()
            return

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message["text"].strip()

        if user_id not in ALLOWED_USERS:
            send(chat_id, "⛔ هذا البوت خاص.")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]

        # أوامر بسيطة ثابتة
        if text in ("/start", "start", "/help", "help", "مساعدة", "شو تسوي"):
            send(chat_id, HELP_TEXT)
            self._ok()
            return

        # إعداد Google Sheets
        try:
            svc = sheets_svc()
            txs = load_transactions(svc)
        except Exception as e:
            send(chat_id, f"❌ خطأ في Google Sheets:\n{e}")
            self._ok()
            return

        # فهم intent عن طريق OpenAI
        d = detect_intent(text)
        intent = d.get("intent", "clarify")
        period = d.get("period") or "month"

        # الـ smalltalk
        if intent == "smalltalk":
            ans = d.get("answer") or "أنا بوت العزبة، أساعدك في تسجيل الدخل والمصروف والجرد 😊"
            send(chat_id, ans)
            self._ok()
            return

        # Routing على حسب intent
        if intent == "add_income":
            h_add_income(svc, d, chat_id, user_name, user_id, txs)

        elif intent == "add_expense":
            h_add_expense(svc, d, chat_id, user_name, user_id, txs)

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
            filtered = filter_by_period(txs, period)
            inc, _ = totals_all(filtered)
            label = {
                "today": "اليوم",
                "week": "هذا الأسبوع",
                "month": "هذا الشهر",
                "all": "لكل الفترة المسجلة",
            }.get(period, "هذا الشهر")
            send(chat_id, f"{D}\n💰 الدخل ({label}): {fmt(inc)} د.إ\n{D}")

        elif intent == "expense_total":
            filtered = filter_by_period(txs, period)
            _, exp = totals_all(filtered)
            label = {
                "today": "اليوم",
                "week": "هذا الأسبوع",
                "month": "هذا الشهر",
                "all": "لكل الفترة المسجلة",
            }.get(period, "هذا الشهر")
            send(chat_id, f"{D}\n📤 المصروف ({label}): {fmt(exp)} د.إ\n{D}")

        elif intent == "profit":
            h_profit(txs, period, chat_id)

        elif intent == "inventory":
            h_inventory_report(svc, chat_id)

        elif intent == "last_transactions":
            h_last_transactions(txs, chat_id)

        elif intent == "category_total":
            h_category_total(txs, d, chat_id)

        elif intent == "category_breakdown":
            h_category_breakdown(txs, d, chat_id)

        elif intent == "daily_report":
            h_daily_report(svc, txs, chat_id)

        else:
            # لو ما فهم الـ intent
            send(
                chat_id,
                "❓ ما فهمت.\n"
                "جرب شيء مثل:\n"
                "• بعت بيض بـ 200\n"
                "• كم دخل البيض الكلي؟\n"
                "• كم صرفنا على الأعلاف؟\n"
                "• قسم لي الدخل حسب التصنيف\n"
                "أو اكتب /help",
            )

        self._ok()

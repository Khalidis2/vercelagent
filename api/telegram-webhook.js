// telegram-webhook.js
import OpenAI from "openai";
import { google } from "googleapis";

/* ================= OpenAI ================= */

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

async function callAiToParse(text, fromName) {
  const completion = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    temperature: 0,
    messages: [
      {
        role: "system",
        content: `
أنت مساعد لتسجيل عمليات مزرعة (عزبة).

مهم جداً:
- أجب بصيغة JSON فقط
- لا تكتب أي نص خارج JSON

الصيغة المطلوبة بالضبط:

{
  "action": "expense | income | inventory",
  "item": "وصف مختصر",
  "amount": رقم أو null,
  "person": "اسم الشخص",
  "notes": "ملاحظات مختصرة"
}

تعليمات:
- افهم العربية الطبيعية
- حوّل المبالغ إلى أرقام
- إذا لم يُذكر الشخص استخدم "${fromName}"
        `.trim(),
      },
      { role: "user", content: text },
    ],
  });

  const raw = completion.choices[0].message.content;

  try {
    return JSON.parse(raw);
  } catch {
    console.error("AI returned invalid JSON:", raw);
    throw new Error("Invalid AI JSON");
  }
}

/* ================= Google Sheets ================= */

function getSheetsClient() {
  const raw = process.env.GOOGLE_SERVICE_ACCOUNT_JSON;
  if (!raw) throw new Error("Missing GOOGLE_SERVICE_ACCOUNT_JSON");

  const serviceAccount = JSON.parse(raw);

  const auth = new google.auth.GoogleAuth({
    credentials: {
      client_email: serviceAccount.client_email,
      private_key: serviceAccount.private_key,
    },
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
  });

  return google.sheets({ version: "v4", auth });
}

async function appendTransactionRow(parsed) {
  const spreadsheetId = process.env.SPREADSHEET_ID;
  if (!spreadsheetId) throw new Error("Missing SPREADSHEET_ID");

  const sheets = getSheetsClient();

  const values = [
    [
      new Date().toISOString(),
      parsed.action,
      parsed.item,
      parsed.amount ?? "",
      parsed.person,
      parsed.notes,
    ],
  ];

  await sheets.spreadsheets.values.append({
    spreadsheetId,
    range: "Transactions!A1",
    valueInputOption: "USER_ENTERED",
    requestBody: { values },
  });
}

/* ================= Telegram ================= */

async function sendTelegramMessage(chatId, text) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return;

  const url = `https://api.telegram.org/bot${token}/sendMessage`;

  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}

/* ================= Main Handler ================= */

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(200).send("OK");
    return;
  }

  const message = req.body.message || req.body.edited_message;
  if (!message || !message.text) {
    res.status(200).send("no message");
    return;
  }

  const chatId = message.chat.id;
  const text = message.text.trim();
  const fromName =
    message.from?.first_name || message.from?.username || "غير معروف";

  /* ---------- Commands ---------- */

  if (text === "/start") {
    await sendTelegramMessage(
      chatId,
      "مرحباً 👋\nأنا مساعد تسجيل عمليات العزبة.\nاكتب /help لمعرفة طريقة الاستخدام."
    );
    res.status(200).send("ok");
    return;
  }

  if (text === "/help") {
    await sendTelegramMessage(
      chatId,
      `
📌 *طريقة الاستخدام*

اكتب العملية بشكل طبيعي، أمثلة:

• اشتريت علف بـ 500
• بعت خروف بـ 1200
• دخل 300 من بيع حليب
• زاد عدد الغنم 5
• نقص عدد الغنم 2

لا تحتاج أوامر خاصة — فقط اكتب بالعربي 👍
      `.trim()
    );
    res.status(200).send("ok");
    return;
  }

  /* ---------- Normal Message ---------- */

  try {
    const parsed = await callAiToParse(text, fromName);

    let saved = true;
    try {
      await appendTransactionRow(parsed);
    } catch (e) {
      saved = false;
      console.error("Google Sheets error:", e);
    }

    const amountText =
      parsed.amount !== null ? `${parsed.amount} درهم` : "بدون مبلغ";

    const typeText =
      parsed.action === "expense"
        ? "مصروف"
        : parsed.action === "income"
        ? "دخل"
        : "تعديل مخزون";

    let reply = `
تم فهم العملية ✅
النوع: ${typeText}
البند: ${parsed.item}
المبلغ: ${amountText}
الشخص: ${parsed.person}
    `.trim();

    if (saved) {
      reply = reply.replace("تم فهم العملية", "تم تسجيل العملية");
    } else {
      reply += `\n\n⚠️ لم يتم الحفظ في Google Sheets (تحقق من الإعدادات)`;
    }

    await sendTelegramMessage(chatId, reply);
    res.status(200).json({ ok: true });
  } catch (err) {
    console.error("Fatal error:", err);
    await sendTelegramMessage(
      chatId,
      "صار خطأ في فهم الرسالة. حاول كتابتها بجملة واحدة واضحة."
    );
    res.status(500).json({ ok: false });
  }
}

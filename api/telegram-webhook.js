// telegram-webhook.js
import OpenAI from "openai";
import { google } from "googleapis";

/* ================= SECURITY ================= */

const ALLOWED_USERS = {
  47329648: "Khaled",
  6894180427: "Hamad",
};

/* ================= OpenAI ================= */

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

async function callAiToParse(text, personName) {
  const completion = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    temperature: 0,
    messages: [
      {
        role: "system",
        content: `
أنت مساعد لتسجيل عمليات مزرعة (عزبة).

أجب بصيغة JSON فقط بدون أي نص إضافي.

الصيغة المطلوبة:

{
  "action": "expense | income | inventory",
  "item": "وصف مختصر",
  "amount": رقم أو null,
  "person": "${personName}",
  "notes": "ملاحظات مختصرة"
}

تعليمات:
- افهم العربية
- حوّل المبالغ إلى أرقام
- لا تخمّن
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
  const serviceAccount = JSON.parse(
    process.env.GOOGLE_SERVICE_ACCOUNT_JSON
  );

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
  const sheets = getSheetsClient();

  await sheets.spreadsheets.values.append({
    spreadsheetId: process.env.SPREADSHEET_ID,
    range: "Transactions!A1",
    valueInputOption: "USER_ENTERED",
    requestBody: {
      values: [
        [
          new Date().toISOString(),
          parsed.action,
          parsed.item,
          parsed.amount ?? "",
          parsed.person,
          parsed.notes,
        ],
      ],
    },
  });
}

/* ================= Telegram ================= */

async function sendTelegramMessage(chatId, text) {
  await fetch(
    `https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text }),
    }
  );
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
  const userId = message.from.id;
  const text = message.text.trim();

  /* ---------- SECURITY CHECK ---------- */

  if (!ALLOWED_USERS[userId]) {
    await sendTelegramMessage(chatId, "⛔ هذا البوت خاص.");
    res.status(200).send("blocked");
    return;
  }

  const personName = ALLOWED_USERS[userId];

  /* ---------- COMMANDS (NO AI) ---------- */

  if (text === "/start") {
    await sendTelegramMessage(
      chatId,
      `مرحباً ${personName} 👋\nأنا بوت تسجيل عمليات العزبة.\nاكتب /help لمعرفة الاستخدام.`
    );
    res.status(200).send("ok");
    return;
  }

  if (text === "/help") {
    await sendTelegramMessage(
      chatId,
      `
📌 *طريقة الاستخدام*

✍️ اكتب العملية بشكل طبيعي، أمثلة:

• اشتريت علف بـ 500
• بعت خروف بـ 1200
• دخل 300 من بيع حليب
• زاد عدد الغنم 5
• نقص عدد الغنم 2

🔒 هذا البوت خاص بالعائلة فقط
      `.trim()
    );
    res.status(200).send("ok");
    return;
  }

  /* ---------- NORMAL TEXT → AI ---------- */

  try {
    const parsed = await callAiToParse(text, personName);

    let saved = true;
    try {
      await appendTransactionRow(parsed);
    } catch (e) {
      saved = false;
      console.error("Sheets error:", e);
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
تم تسجيل العملية ✅
النوع: ${typeText}
البند: ${parsed.item}
المبلغ: ${amountText}
الشخص: ${parsed.person}
    `.trim();

    if (!saved) {
      reply += "\n\n⚠️ لم يتم الحفظ في Google Sheets";
    }

    await sendTelegramMessage(chatId, reply);
    res.status(200).json({ ok: true });
  } catch (err) {
    console.error("Fatal error:", err);
    await sendTelegramMessage(
      chatId,
      "صار خطأ في فهم الرسالة. حاول كتابتها بشكل أوضح."
    );
    res.status(500).json({ ok: false });
  }
}

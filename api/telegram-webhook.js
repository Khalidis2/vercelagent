// api/telegram-webhook.js
import OpenAI from "openai";
import { google } from "googleapis";

const ALLOWED_USERS = {
  47329648: "Khaled",
  6894180427: "Hamad",
};

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

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

async function sendTelegramMessage(chatId, text) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) {
    console.error("Missing TELEGRAM_BOT_TOKEN");
    return;
  }
  const url = `https://api.telegram.org/bot${token}/sendMessage`;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}

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
  "person": "اسم الشخص",
  "notes": "ملاحظات مختصرة"
}

تعليمات:
- افهم العربية
- حوّل المبالغ إلى أرقام
- لا تخمّن
- استخدم اسم الشخص التالي في الحقل person: "${personName}"
        `.trim(),
      },
      { role: "user", content: text },
    ],
  });

  const raw = completion.choices[0].message.content;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    console.error("AI returned invalid JSON:", raw);
    throw new Error("Invalid AI JSON");
  }
  return parsed;
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(200).send("OK");
    return;
  }

  const update = req.body || {};
  const message = update.message || update.edited_message;
  if (!message || !message.text) {
    res.status(200).send("no message");
    return;
  }

  const chatId = message.chat.id;
  const userId = message.from.id;
  const text = message.text.trim();

  if (!ALLOWED_USERS[userId]) {
    await sendTelegramMessage(chatId, "⛔ هذا البوت خاص.");
    res.status(200).send("blocked");
    return;
  }

  const personName = ALLOWED_USERS[userId];

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
📌 طريقة الاستخدام

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

  try {
    const parsed = await callAiToParse(text, personName);

    if (!parsed.action) {
      await sendTelegramMessage(
        chatId,
        "ما فهمت العملية 🤔\nحاول تكتبها مثل:\nاشتريت علف بـ 500"
      );
      res.status(200).send("ok");
      return;
    }

    let saved = true;
    try {
      await appendTransactionRow(parsed);
    } catch (e) {
      saved = false;
      console.error("Sheets error:", e);
    }

    const amountText =
      parsed.amount !== null && parsed.amount !== undefined
        ? `${parsed.amount} درهم`
        : "بدون مبلغ";

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
      "حدث خطأ أثناء معالجة الرسالة. حاول تكتبها بشكل أوضح."
    );
    res.status(500).json({ ok: false });
  }
}

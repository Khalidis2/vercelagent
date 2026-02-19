// api/data.js
// GET  /api/data  → returns Transactions + Inventory as JSON for the HTML app
// POST /api/data  → adds a new transaction from the HTML app

import { google } from "googleapis";

const SPREADSHEET_ID = process.env.SPREADSHEET_ID;
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ALLOWED_CHAT_IDS = [47329648, 6894180427];

// ── CORS headers ──────────────────────────────────────────────────────────────
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Content-Type": "application/json",
};

// ── Sheets client (same pattern as telegram-webhook.js) ───────────────────────
function getSheetsClient() {
  const serviceAccount = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_JSON);
  const auth = new google.auth.GoogleAuth({
    credentials: {
      client_email: serviceAccount.client_email,
      private_key: serviceAccount.private_key,
    },
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
  });
  return google.sheets({ version: "v4", auth });
}

// ── Read a sheet range ────────────────────────────────────────────────────────
async function readSheet(sheets, sheetName, range = "A1:Z") {
  const res = await sheets.spreadsheets.values.get({
    spreadsheetId: SPREADSHEET_ID,
    range: `${sheetName}!${range}`,
  });
  return res.data.values || [];
}

// ── Append a row ──────────────────────────────────────────────────────────────
async function appendRow(sheets, sheetName, row) {
  await sheets.spreadsheets.values.append({
    spreadsheetId: SPREADSHEET_ID,
    range: `${sheetName}!A1`,
    valueInputOption: "USER_ENTERED",
    requestBody: { values: [row] },
  });
}

// ── Parse Transactions sheet ──────────────────────────────────────────────────
// Existing columns: date | action(expense/income) | item | amount | person | notes
function parseTransactions(rows) {
  const out = [];
  for (const r of rows) {
    if (!r || r.length < 3) continue;
    if (r[0] === "التاريخ" || r[0] === "date") continue; // skip header

    const action = (r[1] || "").toLowerCase();
    const amount = parseFloat(r[3]) || 0;

    out.push({
      date:     r[0] || "",
      type:     action === "income" ? "دخل" : "صرف",
      item:     r[2] || "",
      category: r[2] || "",           // item used as category
      amount,
      user:     r[4] || "",
      notes:    r[5] || "",
    });
  }
  return out;
}

// ── Parse Inventory sheet ─────────────────────────────────────────────────────
// Existing columns: Item | Type | Quantity | Notes
function parseInventory(rows) {
  const out = [];
  for (const r of rows) {
    if (!r || !r[0] || r[0] === "Item") continue;
    out.push({
      item:  r[0],
      type:  r[1] || "",
      qty:   parseInt(r[2]) || 0,
      notes: r[3] || "",
    });
  }
  return out;
}

// ── Telegram notify ───────────────────────────────────────────────────────────
async function notifyTelegram(kind, item, amount, user) {
  if (!TELEGRAM_BOT_TOKEN) return;
  const emoji = kind === "income" ? "💰" : "📤";
  const typeLabel = kind === "income" ? "دخل" : "صرف";
  const text = `${emoji} [من التطبيق]\n${typeLabel}: ${item}\nالمبلغ: ${amount} د.إ\nبواسطة: ${user}`;
  for (const chatId of ALLOWED_CHAT_IDS) {
    try {
      await fetch(
        `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: chatId, text }),
        }
      );
    } catch (_) {}
  }
}

// ── Main handler ──────────────────────────────────────────────────────────────
export default async function handler(req, res) {
  // Set CORS headers on every response
  Object.entries(CORS).forEach(([k, v]) => res.setHeader(k, v));

  // Preflight
  if (req.method === "OPTIONS") {
    return res.status(204).end();
  }

  // ── GET: return all data ──────────────────────────────────────────────────
  if (req.method === "GET") {
    try {
      const sheets = getSheetsClient();

      const [tRows, iRows] = await Promise.all([
        readSheet(sheets, "Transactions", "A1:F"),
        readSheet(sheets, "Inventory",    "A1:D"),
      ]);

      const transactions = parseTransactions(tRows);
      const inventory    = parseInventory(iRows);

      const income  = transactions.filter(x => x.type === "دخل").reduce((s, x) => s + x.amount, 0);
      const expense = transactions.filter(x => x.type === "صرف").reduce((s, x) => s + x.amount, 0);

      return res.status(200).json({
        ok: true,
        transactions,
        inventory,
        summary: { income, expense, profit: income - expense },
      });
    } catch (e) {
      return res.status(500).json({ ok: false, error: e.message });
    }
  }

  // ── POST: add a transaction from HTML app ─────────────────────────────────
  if (req.method === "POST") {
    try {
      const { type, item, category, amount, user = "App" } = req.body;

      if (!type || !item || !amount) {
        return res.status(400).json({ ok: false, error: "type, item, amount required" });
      }

      // Map Arabic type to English (matching existing bot format)
      const action = type === "دخل" ? "income" : "expense";
      const now    = new Date().toLocaleString("ar-AE", { timeZone: "Asia/Dubai" });

      const sheets = getSheetsClient();
      await appendRow(sheets, "Transactions", [now, action, item, amount, user, category || ""]);

      // Notify Telegram
      await notifyTelegram(action, item, amount, user);

      return res.status(200).json({ ok: true, message: "تم التسجيل" });
    } catch (e) {
      return res.status(500).json({ ok: false, error: e.message });
    }
  }

  return res.status(405).json({ ok: false, error: "Method not allowed" });
}

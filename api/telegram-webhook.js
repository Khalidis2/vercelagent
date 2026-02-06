// api/telegram-webhook.js
export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(200).send("OK");
    return;
  }

  try {
    const update = req.body;
    const message = update.message || update.edited_message;

    if (!message || !message.text) {
      res.status(200).send("no message");
      return;
    }

    const chatId = message.chat.id;
    const text = message.text;

    const url = `https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`;

    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: `استلمت رسالتك: ${text}`,
      }),
    });

    res.status(200).json({ ok: true });
  } catch (err) {
    console.error("Error in handler:", err);
    res.status(500).json({ ok: false });
  }
}

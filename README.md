# WhatsApp Lead Bot

Turns WhatsApp enquiries into qualified leads. A customer messages the business, the bot answers and
asks for what the team needs, the lead is saved and scored, and the owner gets an alert.

The sample business, Demo Dental Clinic, is fictional. Change `business_profile.json` for another business.

## Features
- Replies in the customer's language, including Hinglish, using Claude
- Collects service, urgency, preferred time and name, one question at a time
- Scores leads as hot, warm or cold in code, not by the AI
- Alerts the owner when a lead is complete or asks for a person, then the bot stays quiet
- Saves leads to SQLite, exports CSV, and can POST them to n8n, Zapier, a CRM or Apps Script
- Handles WhatsApp webhook signatures, repeated deliveries, STOP requests and non-text messages
- Hands off to a person if the AI fails or a conversation runs too long

## Run the demo
No WhatsApp account or API key is needed.

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/uvicorn --factory leadbot.main:create_app --host 127.0.0.1 --port 8010
```

Open http://127.0.0.1:8010. Keep demo mode on localhost, because it has no login.

Without `ANTHROPIC_API_KEY`, a scripted brain with fixed rules writes the replies. To use Claude, copy
`.env.example` to `.env` and add a key. The default model is `claude-opus-5-5`; a bot like this may run
well on a cheaper model, which you can set with `LEAD_BOT_MODEL` after testing it.

## Connect real WhatsApp
1. Create a Meta app with WhatsApp and get a phone number ID and access token.
2. Fill in the live settings from `.env.example` and set `DEMO_MODE=false`. The server will not start if one is missing.
3. Deploy behind HTTPS and set the webhook URL to `https://your-domain/webhook`, subscribed to `messages`.
4. Create an approved template with one body variable for owner alerts, and set `OWNER_ALERT_TEMPLATE`.
   Without it, alerts are plain text, which WhatsApp only delivers inside a 24-hour customer service window.
5. Check `WHATSAPP_API_VERSION` against Meta's current version.

Get customers' permission as WhatsApp's policies and local privacy law require. Leads are personal data.

## Status
Tested with automated tests and a simulated chat. It has not yet run against a live WhatsApp number.
The Meta and Claude calls are tested against fakes unless you run them with your own credentials.

## Tests
```bash
.venv/bin/python -m pytest -q
```

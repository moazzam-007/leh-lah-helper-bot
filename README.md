# LehLah Helper Bot

Telegram bot for:
- Generating LehLah affiliate links from product URLs
- Extracting original product URLs from LehLah links
- Bulk URL processing
- Token health/status checks

## Features

- Smart link detection:
  - LehLah URL -> extract original URL
  - Product URL -> generate affiliate URL
- Admin-only access support via `ADMIN_ID`
- Bulk mode (`/bulk`) for up to 20 URLs
- Token expiry check (`/check_token`)
- Flask webhook endpoint for Telegram updates

## Tech Stack

- Python
- Flask
- python-telegram-bot
- requests

## Required Environment Variables

- `BOT_TOKEN`: Telegram bot token
- `WEBHOOK_URL`: Public base URL of your app (without trailing slash)
- `LEHLAH_COOKIE`: Full LehLah cookie containing `authToken`
- `ADMIN_ID` (optional but recommended): Telegram user ID for admin lock
- `PORT` (optional): Defaults to `10000`

## Local Run

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Set environment variables.

3. Start app:

```bash
python main.py
```

## Bot Commands

- `/start` - Welcome and usage info
- `/bulk` - Enable bulk URL processing mode
- `/check_token` - Check LehLah token status
- `/status` - Bot and token health summary

## Render Deployment

This repo includes `render.yaml`.

1. Create a new Web Service on Render.
2. Connect this GitHub repository.
3. Render uses:
   - Build command: `pip install -r requirements.txt`
   - Start command: `python main.py`
4. Add required environment variables in Render dashboard.
5. Ensure `WEBHOOK_URL` matches your Render service URL.

## Notes

- Keep `LEHLAH_COOKIE` private.
- Set `ADMIN_ID` in production to prevent unauthorized access.
- If token is exposed, revoke and regenerate immediately.
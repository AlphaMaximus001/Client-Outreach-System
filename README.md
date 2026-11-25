# Client Outreach System

A dashboard for storing client details and automatically sending **birthday** and **festival** offers over **WhatsApp** and **email**, one message at a time with a 1–2 second pause between messages on each channel.

## Features
- **Clients:** add them one at a time on the dashboard or bulk-import a CSV. A client has a name, date of birth, address, phone and email. **Age is calculated from the date of birth**, so it never goes out of date. Re-importing a client with the same email or phone updates their record instead of creating a duplicate. Bad rows are listed by row number with the reason.
- **Overview:** upcoming birthdays (date, how many days away, the age they're turning) and upcoming festivals.
- **Automatic sending:** runs once a day at `SEND_HOUR`. You can also use **Send due messages now** or **Preview**.
  - Birthday offers go out `birthday_days_before` days before the birthday (default `0`, on the day).
  - Festival offers go to every client `festival_days_before` days before the festival (default `1`).
- **Rate-limit safe:** WhatsApp and email each have their own queue, sent **strictly one by one** with a random `MIN_DELAY_SECONDS`–`MAX_DELAY_SECONDS` (1–2 s) pause between messages. If a provider rate-limits us (HTTP 429, or SMTP 421/45x), sending backs off exponentially and retries.
- **No double-sends:** each message is recorded per client, channel and campaign (e.g. `birthday:2026`). A rerun or a server restart only retries failed messages, up to 3 attempts.
- **Opt-out** per client, a **message log**, and editable **message templates**. The templates support the placeholders `{first_name} {name} {age} {offer} {festival} {date} {business}`.
- **Festivals:** fixed-date festivals repeat every year. Festivals whose date moves (Eid, Diwali, Easter…) are added once per year with that year's date.
- Optional dashboard login (HTTP Basic).

## Stack
Python 3.11+ · FastAPI · SQLite (stdlib) · plain HTML/JS dashboard. The WhatsApp Cloud API is called over HTTPS and email goes over SMTP, both using the standard library.

## Run it
```bash
pip install -r requirements.txt
cp .env.example .env      # fill in credentials
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Open http://localhost:8000. While `DRY_RUN=true` (the default), messages are logged but nothing is sent. Set it to `false` once you've added your credentials.

Run the tests:
```bash
pip install -r requirements-dev.txt && pytest
```

## CSV format
```
name,dob,address,phone,email
Ada Obi,1990-12-01,"12 Allen Ave, Ikeja",08031234567,ada@example.com
```
- Headers are matched loosely, so `Date of Birth`, `birthday`, `Phone Number`, `mobile` and so on all work.
- Dates can be `YYYY-MM-DD` or `DD/MM/YYYY` (set `CSV_DATE_ORDER=MDY` for US-style dates).
- Local numbers such as `0803…` are converted using `DEFAULT_COUNTRY_CODE`.
- A downloadable template is linked on the Clients tab.

## WhatsApp setup (important)
This uses the official **Meta WhatsApp Cloud API**. WhatsApp only lets a business message a customer first using a **pre-approved message template**; free-text messages only reach people who messaged you in the last 24 hours. So:
1. In Meta Business Manager, create two templates (for example `birthday_offer` and `festival_offer`), each with two body variables: `{{1}}` = first name and `{{2}}` = offer text.
2. Put their names in `WHATSAPP_TEMPLATE_BIRTHDAY` / `WHATSAPP_TEMPLATE_FESTIVAL`, and set `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID`.

When a template is configured, the WhatsApp text set on the dashboard isn't used, because Meta sends the approved template wording. The email text is always used.

## Email setup
Any SMTP provider works: Gmail with an app password, SendGrid, Mailgun, Amazon SES and so on. Set `SMTP_HOST/PORT/USER/PASSWORD` and `EMAIL_FROM`.

## Deploy on Render
The repo includes a `render.yaml` Blueprint. It sets the build and start commands, a `/healthz` health check, and a 1 GB disk mounted at `/data` for the database.

1. In Render, choose **New → Blueprint** and pick this repo.
2. Render asks for the values marked `sync: false`. Fill in at least `DASHBOARD_PASSWORD`, `TIMEZONE`, `BUSINESS_NAME` and `DEFAULT_COUNTRY_CODE`. You can leave the WhatsApp and SMTP fields blank for now and add them later under **Environment**.
3. Deploy, then open the `onrender.com` URL. You can add your own domain under **Settings → Custom Domains**.
4. Test with `DRY_RUN=true`. When everything looks right, change it to `false` under **Environment**.

Notes:
- **It needs a paid instance** (Starter, about $7/month). Free instances sleep after 15 minutes without visitors, so the daily send would never fire. They also can't have a disk, so the database would be wiped on every deploy.
- **Deploys cause a short outage.** With a disk attached, Render stops the old instance before starting the new one, so expect a few seconds of downtime per deploy. It also runs only one instance, which is what this app needs.
- **Back up the database.** Render takes daily disk snapshots, and you can also download `/data/outreach.db` from the service **Shell**.

## Deploy on Railway
The repo includes `railway.json`. It sets the start command, a `/healthz` health check, restart-on-crash and a single instance.

1. **New project:** in Railway, choose **New Project → Deploy from GitHub repo** and pick this repo.
2. **Add a volume** so the database survives redeploys: right-click the service → **Attach Volume** → mount path `/data`.
3. **Variables:** on the service's **Variables** tab, add at least:
   ```
   DATABASE_PATH=/data/outreach.db
   DASHBOARD_USER=admin
   DASHBOARD_PASSWORD=<a strong password>
   TIMEZONE=Africa/Lagos
   BUSINESS_NAME=Your Business
   DEFAULT_COUNTRY_CODE=234
   SEND_HOUR=9
   DRY_RUN=true
   ```
   Add the WhatsApp and SMTP variables from `.env.example` too. Don't upload a `.env` file to Railway.
4. **Public URL:** go to **Settings → Networking → Generate Domain** to get an HTTPS address for the dashboard. You can add your own domain there too.
5. **Test, then go live:** add a test client with your own number and email, then click **Preview** and **Send due messages now**. When the dry-run looks right, set `DRY_RUN=false`.

Notes:
- **Keep it at 1 replica.** The daily scheduler runs inside the app, so a second copy would send every message twice. A volume also forces a single instance anyway.
- **Missed sends catch up.** If the service restarts or redeploys after `SEND_HOUR`, it still runs that day's sends once. Anything already sent is never sent again.
- **Back up regularly.** `outreach.db` holds personal data, so download a copy from the volume now and then.
- The app also runs on any host that keeps a server running (Render with a disk, Fly.io, a VPS): `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.

## API
`GET /api/overview` · `GET/POST /api/clients` · `PUT/DELETE /api/clients/{id}` · `POST /api/clients/{id}/opt-out` · `POST /api/clients/upload` · `GET/POST/PUT/DELETE /api/festivals` · `GET/PUT /api/settings` · `GET /api/campaigns/preview` · `POST /api/campaigns/run` · `GET /api/messages`. Interactive docs are at `/docs`.

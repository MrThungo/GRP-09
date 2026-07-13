# NMB-HLabSys — Nelson Mandela Bay Haematology Lab System

Pure **Flask + Jinja + Tailwind CSS** (Tailwind via the official CDN — no React,
no Vite, no Node build step). SQLite is available for local demo mode. For ONP400 deployment, use SQL Server via `DATABASE_URL` after running `database/sql_server_schema.sql`.

## What's new in this build

- **React + Vite removed.** UI is server-rendered Jinja with Tailwind utilities
  and a small vanilla-JS notification bell.
- **Public landing page** at `/` — transparent top bar, full-viewport hero with
  a background video (graceful fallback to image) and a soft box-shadow fade
  into the page below.
- **Generic registration flow.** Anyone who signs up is created as a *pending
  user* with no role. Every admin receives a notification, then assigns the
  user a role from **Admin → Users** (doctor / lab technician / lab manager /
  admin / patient).
- **Pending users** see a friendly waiting screen until an admin grants access.
- Patient self-service (book a request directly from the patient dashboard once
  the role is granted), doctor request release flow, technician capture/verify,
  manager catalog/inventory/suppliers/reports, admin user management & audit
  log — all unchanged behavior, polished UI.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
flask --app wsgi:app seed       # creates the demo accounts
flask --app wsgi:app run        # http://localhost:5000
```

### Demo accounts (after `seed`)

| Role            | Email                       | Password    |
| --------------- | --------------------------- | ----------- |
| Admin           | admin@nmb.example.com             | admin123    |
| Lab manager     | manager@nmb.example.com           | manager123  |
| Doctor          | doctor@nmb.example.com            | doctor123   |
| Lab technician  | technician@nmb.example.com        | tech123     |
| Patient         | patient@nmb.example.com           | patient123  |

## Background media on the landing page

Drop your own clip at `app/static/media/hero.mp4` (and an optional poster at
`app/static/media/hero.jpg`) — the landing page picks them up automatically.
Until then, a Pexels-hosted lab clip is used as the source.

## WhatsApp welcome messages

Newly created accounts can receive an automated WhatsApp welcome through
GreenAPI. Add these values to `.env` or `env.txt`:

```bash
GREENAPI_ENABLED=true
GREENAPI_API_URL=https://api.green-api.com
GREENAPI_ID_INSTANCE=your-instance-id
GREENAPI_API_TOKEN_INSTANCE=your-instance-token
GREENAPI_DEFAULT_COUNTRY_CODE=27
GREENAPI_TIMEOUT_SECONDS=10
GREENAPI_INCLUDE_TEMP_PASSWORD=false
```

The app sends the WhatsApp message after public patient signup, doctor-created
patient accounts, and manager-created doctor or technician accounts. By default
the temporary password is sent only by email; set
`GREENAPI_INCLUDE_TEMP_PASSWORD=true` if you explicitly want it included in the
WhatsApp message too.

## Twilio Conversations patient chatbot

The patient assistant uses Twilio Conversations for browser-based chat. It does
not use SMS or WhatsApp. Configure these values in `.env` or `env.txt`:

```bash
TWILIO_ACCOUNT_SID=your-account-sid
TWILIO_AUTH_TOKEN=your-auth-token
TWILIO_API_KEY_SID=your-api-key-sid
TWILIO_API_KEY_SECRET=your-api-key-secret
TWILIO_CONVERSATIONS_SERVICE_SID=your-conversations-service-sid
TWILIO_BOT_IDENTITY=nmb-hlab-bot
TWILIO_WEBHOOK_PUBLIC_URL=https://your-domain.example.com/chatbot/twilio/conversations/webhook
TWILIO_VALIDATE_REQUESTS=true
```

In Twilio Conversations, configure the post-action webhook for
`onMessageAdded` to:

```text
https://your-domain.example.com/chatbot/twilio/conversations/webhook
```

Patients can open **Assistant** in the portal sidebar. The assistant can answer
patient-scoped questions about profile details, request status, latest released
results, secure report links, access requests, consent and notifications.

## Project layout

```
app/
  __init__.py          app factory
  extensions.py        SQLAlchemy
  models.py            DB models (User, UserRole, Patient, TestRequest, …)
  auth_utils.py        @role_required decorator
  services.py          notify(), audit, release_request(), verify_item()
  seed.py              demo data
  blueprints/
    auth.py            login / signup / logout / pending
    public.py          landing page
    admin.py           users + role assignment + audit log
    doctor.py          requests, capture preview, release
    patient.py         my results, book test, PDF download
    technician.py      capture + verify
    manager.py         catalog / inventory / suppliers / reports
    api.py             /api/notifications JSON
  templates/           Jinja (Tailwind via CDN)
  static/
    css/app.css        small extras on top of Tailwind
    js/app.js          notification bell + tiny UX helpers
    media/             optional hero.mp4 / hero.jpg
```

## SQL Server deployment

For ONP400 deployment, create the SQL Server database manually with
`database/sql_server_schema.sql`, then set `DATABASE_URL` in `.env` to a
`mssql+pyodbc://...` connection string. The app only auto-creates tables for
local SQLite demo mode; it does not call `db.create_all()` for SQL Server.


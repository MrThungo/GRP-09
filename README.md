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

### Seeded local accounts

Seeded account passwords are not stored in source code. For local testing, set
`DEFAULT_USER_PASSWORD` to use one shared password, or set role-specific values
such as `DEFAULT_ADMIN_PASSWORD`, `DEFAULT_DOCTOR_PASSWORD`,
`DEFAULT_TECHNICIAN_PASSWORD`, `DEFAULT_MANAGER_PASSWORD` and
`DEFAULT_PATIENT_PASSWORD`. If these are blank, the app generates temporary
passwords during local seeding/startup and prints them to the server console.
Set `ENABLE_QUICK_LOGIN=true` only in local/testing env files to show quick
login buttons. Keep it `false` for the IIS production/publish environment.

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

## Online consultation video

The live consultation room uses WebRTC in the browser. Flask/PythonAnywhere only
handles invite-only room access and signaling; the actual camera/microphone media
must connect browser-to-browser or through a TURN relay. For reliable calls on
PythonAnywhere, configure a TURN service in `env.txt`:

```bash
WEBRTC_STUN_URLS=stun:stun.l.google.com:19302
WEBRTC_TURN_URLS=turn:your-turn-host:3478,turns:your-turn-host:5349
WEBRTC_TURN_USERNAME=your-turn-username
WEBRTC_TURN_CREDENTIAL=your-turn-password
WEBRTC_FORCE_RELAY=false
```

Set `WEBRTC_FORCE_RELAY=true` temporarily when testing TURN, then switch it back
to `false` after confirming the live room connects.

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

## IIS publish target

The app will be published to this IIS network share:

```text
\\SOIT-IIS.MANDELA.AC.ZA\GRP-04-09$
```

Keep environment-specific values such as `SECRET_KEY`, `DATABASE_URL`, SMTP,
GreenAPI and Twilio credentials in the server-side environment or `env.txt`;
do not hard-code secrets into the source tree before publishing.

### Publish without changing the database

While the database is still being tested, publish only the application files.
Do not run `database/sql_server_schema.sql` against the testing database unless
you explicitly decide to reset or rebuild that database.

1. Confirm the IIS site has HttpPlatformHandler enabled.
2. Publish the source to the IIS share:

   ```powershell
   .\tools\publish-iis.ps1
   ```

   To preview the copy without writing files:

   ```powershell
   .\tools\publish-iis.ps1 -Preview
   ```

3. On the IIS server/share, create a virtual environment in the published folder:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. Create the server `env.txt` from `env.production.example`, using the existing
   testing `DATABASE_URL`. The publish script excludes local `env.txt` by
   default so local testing secrets are not copied accidentally.

5. Restart the IIS app pool/site. `web.config` runs `wsgi.py` through the local
   `.venv\Scripts\python.exe` and uses IIS' dynamic `HTTP_PLATFORM_PORT`.


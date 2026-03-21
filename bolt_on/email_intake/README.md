# Email Intake Bolt-On

Optional email connector for Home Document Ops Hub.

This bolt-on runs as a separate process and is not required by the core scanner filing system.
If disabled or not installed, scanner-based filing continues to work unchanged.

## What It Does

- Polls one or more IMAP inboxes.
- Extracts PDF attachments into the scanner inbox for normal OCR/classification/filing.
- Supports per-mailbox routing into scanner user inboxes and dedicated subfolders (for example: `inbox/<user>/email/`).
- Extracts structured entities from email text for later integrations:
  - shipment tracking references
  - carrier hints (UPS, Royal Mail, DHL, Evri, DPD, FedEx, USPS, Parcelforce, Yodel)
  - policy numbers
  - status hints (for example: in_transit, delivered, renewed)
- Stores entity current state and event history in SQLite.
- Writes JSON snapshot exports for downstream projects.

## Separation Guarantee

- Separate folder: `bolt_on/email_intake/`
- Separate config: `bolt_on/email_intake/config.yaml`
- Separate runtime state: `state/email_intake/`
- Separate systemd unit: `scanner-email-intake.service`
- No imports from the core `scanner_filer` runtime path are required.

## Quick Start

1. Copy config example.

```bash
cd /home/pi/scanner/bolt_on/email_intake
cp config.example.yaml config.yaml
```

2. Edit account details in `config.yaml`.

3. Run once in foreground.

```bash
cd /home/pi/scanner
python3 bolt_on/email_intake/email_intake_service.py --config bolt_on/email_intake/config.yaml --once
```

4. Run continuously.

```bash
cd /home/pi/scanner
python3 bolt_on/email_intake/email_intake_service.py --config bolt_on/email_intake/config.yaml
```

5. Test a specific account connection safely.

```bash
python3 bolt_on/email_intake/email_intake_service.py --config bolt_on/email_intake/config.yaml --test-account main_mailbox
```

6. Optional: run the bolt-on UI.

```bash
python3 bolt_on/email_intake/web_ui.py --config bolt_on/email_intake/config.yaml --host 0.0.0.0 --port 8091
```

## Data Outputs

- SQLite DB: `state/email_intake/entities.db`
- Current entities JSON: `state/email_intake/current_entities.json`
- Recent events JSON: `state/email_intake/recent_events.json`
- Poll/account cursor state: `state/email_intake/cursors.json`
- Optional metadata logs: `state/email_intake/messages/`

## Read-Only API (for downstream projects)

The UI process exposes read-only endpoints:

- `GET /api/health`
- `GET /api/entities/current?type=shipment&limit=200`
- `GET /api/entities/events?since=2026-03-16T00:00:00Z&type=policy&limit=500`
- `GET /api/config` (passwords masked)

These are intended for your later integration project to consume current state and change timeline.

## Security Notes

- Preferred: OAuth2 (refresh token + XOAUTH2) for modern mailbox auth.
- Fallback: app-specific IMAP passwords if provider policy allows it.
- Restrict mailbox scope to required folders only.
- Keep `config.yaml` permissions tight (`chmod 600`).

### Shared Login Session (Scanner UI + Email Intake UI)

The bolt-on can share the same browser login session as the main scanner UI.

- Set the same `SCANNER_WEB_SECRET` value for both web services.
- Set the same `SCANNER_WEB_SESSION_COOKIE` value for both services (default: `scanner_session`).
- Keep both UIs on the same host/domain so the browser sends the same cookie.

With that in place:

- If you log in on scanner UI first, email UI opens without another login.
- If you log in on email UI first, scanner UI opens without another login.
- Logging out from either UI clears the shared session.

## Modern OAuth2 Mailbox Setup

Each account supports both modes:

- `auth_mode: password` uses `username` + `password`.
- `auth_mode: oauth2` uses XOAUTH2 with refresh-token flow.

UI sign-in is available in Mailbox Editor:

- Save mailbox with `oauth2_client_id` first.
- Click `Sign in with Microsoft` or `Sign in with Google`.
- After consent, tokens are stored back into that mailbox entry.

One-click Outlook connect is also available:

- Use `Quick Connect Outlook` in the UI.
- It opens Microsoft sign-in and auto-creates a mailbox with IMAP defaults.
- Requires server env vars on the web UI process:
  - `SCANNER_EMAIL_MS_CLIENT_ID`
  - `SCANNER_EMAIL_MS_CLIENT_SECRET` (optional, depending on app type)
  - `SCANNER_EMAIL_MS_TENANT` (set `consumers` for personal Outlook/Hotmail accounts)

If Microsoft returns a userAudience/common mismatch error, set:

- `SCANNER_EMAIL_MS_TENANT=consumers`

Then restart `scanner-email-intake-web`.

Multiple Outlook accounts are supported:

- Each mailbox account has independent OAuth2 settings/tokens.
- Repeat sign-in per mailbox account.

For OAuth2 accounts, configure:

- `oauth2_provider`: `microsoft`, `google`, or `custom`
- `oauth2_client_id`
- `oauth2_client_secret` (if your provider requires it)
- `oauth2_refresh_token`
- Optional overrides: `oauth2_token_url`, `oauth2_scope`

Provider defaults when overrides are blank:

- Microsoft token URL: `https://login.microsoftonline.com/common/oauth2/v2.0/token`
- Microsoft scope: `https://outlook.office.com/IMAP.AccessAsUser.All offline_access`
- Google token URL: `https://oauth2.googleapis.com/token`
- Google scope: `https://mail.google.com/`

Token behavior:

- The service refreshes access tokens automatically before expiry.
- Refreshed tokens are written back into `config.yaml` for reuse.
- Optional env override per account access token:
  - `EMAIL_INTAKE_OAUTH2_ACCESS_TOKEN_<ACCOUNT_NAME_UPPER>`
  - Example account `main_mailbox` -> `EMAIL_INTAKE_OAUTH2_ACCESS_TOKEN_MAIN_MAILBOX`

## Duplicate Processing Behavior

The intake service avoids reprocessing old emails in two ways:

- Per-account mailbox UID cursor state in `state/email_intake/cursors.json`
- Seen-message table in SQLite (`seen_messages`) keyed by account/mailbox/UID

So even when emails are left unread on the IMAP server, previously processed messages are remembered and skipped.

### Outlook / Microsoft 365 Quick Path

If you see `LOGIN failed` for Outlook accounts, switch account auth mode to OAuth2.

1. Register an app in Azure (Microsoft Entra):
  - Supported account type: as needed (personal Outlook.com or org accounts)
  - Redirect URI: `http://localhost` (public client/native app flow is fine)
  - API permissions: IMAP.AccessAsUser.All
2. Generate a client secret if your app type requires one.
3. Use the helper to get an authorization URL:

```bash
cd /home/pi/scanner
python3 bolt_on/email_intake/oauth2_bootstrap.py --provider microsoft --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET
```

4. Sign in, grant consent, then copy the `code` query parameter from the redirect URL.
5. Exchange the code for tokens:

```bash
cd /home/pi/scanner
python3 bolt_on/email_intake/oauth2_bootstrap.py --provider microsoft --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET --code YOUR_AUTH_CODE
```

6. Copy the JSON output values into the mailbox fields:
  - `auth_mode: oauth2`
  - `oauth2_provider: microsoft`
  - `oauth2_client_id`
  - `oauth2_client_secret` (if used)
  - `oauth2_refresh_token`
  - optional initial `oauth2_access_token`
7. Run mailbox test again.

## Installing systemd unit

```bash
sudo cp bolt_on/email_intake/systemd/scanner-email-intake.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scanner-email-intake
sudo systemctl status scanner-email-intake
```

Optional UI service:

```bash
sudo cp bolt_on/email_intake/systemd/scanner-email-intake-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scanner-email-intake-web
sudo systemctl status scanner-email-intake-web
```

Adjust paths/user in the unit file if your install differs.

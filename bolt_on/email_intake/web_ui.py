#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash
import yaml

from email_intake_service import AccountConfig, DEFAULT_TRACKING_SERVICES, EmailIntakeService, test_account_connection
from entity_store import EntityStore


def _scanner_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _scanner_config_path() -> Path:
    return _scanner_root() / "config.yaml"


def _scanner_state_dir() -> Path:
    path = _scanner_config_path()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                paths = data.get("paths", {})
                if isinstance(paths, dict):
                    raw = str(paths.get("state", "")).strip()
                    if raw:
                        return Path(raw).expanduser().resolve()
        except Exception:
            pass
    return (_scanner_root() / "state").resolve()


def _security_config_path() -> Path:
    return _scanner_state_dir() / "web_security.json"


def _load_auth_accounts() -> list[dict[str, str]]:
    path = _security_config_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return []

    if isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
        rows = payload.get("accounts")
    elif isinstance(payload, list):
        rows = payload
    else:
        return []

    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        user = str(row.get("username", "")).strip()
        pw_hash = str(row.get("password_hash", "")).strip()
        if user and pw_hash:
            out.append({"username": user, "password_hash": pw_hash})
    return out


def _load_scanner_user_inboxes() -> list[str]:
    path = _scanner_state_dir() / "user_inboxes.json"
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        data = payload.get("inboxes", {}) if isinstance(payload, dict) else {}
        if isinstance(data, dict):
            out = [str(k).strip() for k in data.keys() if str(k).strip()]
            return sorted(out)
    except Exception:
        pass
    return []


def _load_or_create_web_secret() -> str:
    # Prefer the scanner web secret so both apps can validate one shared session cookie.
    env_secret = os.environ.get("SCANNER_WEB_SECRET", "").strip() or os.environ.get("SCANNER_EMAIL_WEB_SECRET", "").strip()
    if env_secret:
        return env_secret

    shared_secret_path = _scanner_state_dir() / "web_secret.txt"
    try:
        if shared_secret_path.exists():
            existing_shared = shared_secret_path.read_text(encoding="utf-8").strip()
            if existing_shared:
                return existing_shared
    except Exception:
        pass

    state_dir = _scanner_state_dir()
    secret_path = state_dir / "email_intake_web_secret.txt"
    try:
        if secret_path.exists():
            existing = secret_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing

        secret_path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(32)
        secret_path.write_text(generated + "\n", encoding="utf-8")
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
        return generated
    except Exception:
        return secrets.token_hex(32)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def save_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


def _bool_from_form(name: str) -> bool:
    return request.form.get(name, "off") == "on"


def _int_from_form(name: str, default: int) -> int:
    raw = request.form.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _str_from_form(name: str, default: str = "") -> str:
    raw = request.form.get(name, "")
    if raw is None:
        return default
    return str(raw).strip()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _to_sort_epoch(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).timestamp()
    except Exception:
        return 0.0


def _tracking_templates() -> dict[str, str]:
    out: dict[str, str] = {}
    for row in DEFAULT_TRACKING_SERVICES:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip().lower()
        template = str(row.get("tracking_url", "")).strip()
        if name and template:
            out[name] = template
    return out


def _tracking_url_for_payload(payload: dict[str, Any], entity_key: str) -> str:
    direct = str(payload.get("tracking_url", "")).strip()
    if direct:
        return direct

    carrier = str(payload.get("carrier", "")).strip().lower()
    tracking_number = str(payload.get("tracking_number", "")).strip() or str(entity_key).strip()
    if not carrier or not tracking_number:
        return ""

    template = _tracking_templates().get(carrier, "")
    if not template:
        return ""

    postcode = str(payload.get("tracking_postcode", "")).strip()
    return template.format(
        tracking_number=urllib.parse.quote(tracking_number, safe=""),
        postcode=urllib.parse.quote(postcode, safe=""),
    )


def _oauth2_defaults(provider: str) -> dict[str, str]:
    p = str(provider or "").strip().lower()
    if p == "microsoft":
        tenant = str(os.environ.get("SCANNER_EMAIL_MS_TENANT", "common")).strip() or "common"
        return {
            "authorize_url": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
            "token_url": f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            "scope": "offline_access https://outlook.office.com/IMAP.AccessAsUser.All",
            "response_mode": "query",
        }
    if p == "google":
        return {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scope": "https://mail.google.com/",
            "response_mode": "query",
        }
    return {
        "authorize_url": "",
        "token_url": "",
        "scope": "",
        "response_mode": "query",
    }


def _quick_ms_scope() -> str:
    return "openid profile email offline_access https://outlook.office.com/IMAP.AccessAsUser.All"


def _safe_mailbox_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip().lower())
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean or "outlook_mailbox"


def _unique_mailbox_name(accounts: list[Any], base: str) -> str:
    existing = {str(row.get("name", "")).strip() for row in accounts if isinstance(row, dict)}
    if base not in existing:
        return base
    n = 2
    while True:
        candidate = f"{base}_{n}"
        if candidate not in existing:
            return candidate
        n += 1


def _decode_id_token_claims(id_token: str) -> dict[str, Any]:
    token = str(id_token or "").strip()
    if token.count(".") != 2:
        return {}
    try:
        _head, payload, _sig = token.split(".", 2)
        pad = "=" * ((4 - (len(payload) % 4)) % 4)
        raw = base64.urlsafe_b64decode((payload + pad).encode("ascii"))
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def _email_from_claims(claims: dict[str, Any]) -> str:
    for key in ("preferred_username", "email", "upn"):
        val = str(claims.get(key, "")).strip()
        if val and "@" in val:
            return val
    return ""


def _oauth2_redirect_uri() -> str:
    env_uri = str(os.environ.get("SCANNER_EMAIL_OAUTH_REDIRECT_URI", "")).strip()
    if env_uri:
        return env_uri
    return url_for("oauth2_callback", _external=True)


def _oauth2_exchange_code(
    token_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    scope: str,
) -> dict[str, Any]:
    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        form["client_secret"] = client_secret
    if scope:
        form["scope"] = scope

    req = urllib.request.Request(
        token_url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError("OAuth token endpoint returned invalid JSON")
    return data


def make_app(config_path: Path) -> Flask:
    app = Flask(__name__, template_folder="templates")
    app.secret_key = _load_or_create_web_secret()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = str(os.environ.get("SCANNER_WEB_SECURE_COOKIE", "")).strip().lower() in {"1", "true", "yes", "on"}
    app.config["SESSION_COOKIE_NAME"] = str(os.environ.get("SCANNER_WEB_SESSION_COOKIE", "scanner_session")).strip() or "scanner_session"
    app.config["AUTH_REQUIRED"] = str(os.environ.get("SCANNER_WEB_DISABLE_AUTH", "")).strip().lower() not in {"1", "true", "yes", "on"}
    app.config["CONFIG_PATH"] = str(config_path)

    def _auth_enabled() -> bool:
        return bool(app.config.get("AUTH_REQUIRED", True))

    def _csrf_token() -> str:
        token = session.get("csrf_token", "")
        if not token:
            token = uuid4().hex
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = _csrf_token

    @app.before_request
    def _security_gate() -> Any:
        endpoint = request.endpoint or ""
        if endpoint not in {"static", "login"}:
            accounts = _load_auth_accounts()
            if _auth_enabled() and not accounts:
                abort(503, description="No scanner web accounts found. Configure accounts in main scanner UI first.")
            if _auth_enabled() and not session.get("auth_ok", False):
                return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

        if request.method == "POST" and endpoint != "login":
            token = str(session.get("csrf_token", ""))
            supplied = request.form.get("_csrf", "")
            if not token or not supplied or not secrets.compare_digest(token, supplied):
                abort(400, description="Invalid CSRF token")

        return None

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        next_url = request.values.get("next", "")
        if not isinstance(next_url, str) or not next_url.startswith("/"):
            next_url = url_for("index")

        if not _auth_enabled():
            session["auth_ok"] = True
            _csrf_token()
            return redirect(next_url)

        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            for acct in _load_auth_accounts():
                acct_user = str(acct.get("username", ""))
                acct_hash = str(acct.get("password_hash", ""))
                if secrets.compare_digest(username, acct_user) and acct_hash and check_password_hash(acct_hash, password):
                    session["auth_ok"] = True
                    session["auth_user"] = acct_user
                    _csrf_token()
                    return redirect(next_url)
            return render_template("login.html", msg="Invalid username or password", next_url=next_url)

        return render_template("login.html", msg="", next_url=next_url)

    @app.post("/logout")
    def logout() -> Any:
        session.clear()
        return redirect(url_for("login"))

    @app.get("/oauth2/start")
    def oauth2_start() -> Any:
        provider = request.args.get("provider", "").strip().lower()
        account_name = request.args.get("account", "").strip()
        if provider not in {"microsoft", "google"}:
            return redirect(url_for("index", msg="OAuth provider must be microsoft or google", edit=account_name))
        if not account_name:
            return redirect(url_for("index", msg="Save mailbox first, then start OAuth sign-in"))

        cfg = load_config(config_path)
        accounts = cfg.get("accounts", []) if isinstance(cfg.get("accounts"), list) else []
        row = next((x for x in accounts if isinstance(x, dict) and str(x.get("name", "")).strip() == account_name), None)
        if row is None:
            return redirect(url_for("index", msg=f"Mailbox not found: {account_name}"))

        client_id = str(row.get("oauth2_client_id", "")).strip()
        if not client_id:
            return redirect(url_for("index", msg="oauth2_client_id is required before OAuth sign-in", edit=account_name))

        defaults = _oauth2_defaults(provider)
        auth_url = defaults["authorize_url"]
        scope = str(row.get("oauth2_scope", "")).strip() or defaults["scope"]
        redirect_uri = _oauth2_redirect_uri()
        state = secrets.token_urlsafe(24)

        session["oauth2_pending"] = {
            "state": state,
            "provider": provider,
            "account": account_name,
            "redirect_uri": redirect_uri,
        }

        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "prompt": "consent",
            "access_type": "offline",
            "response_mode": defaults.get("response_mode", "query"),
        }
        return redirect(f"{auth_url}?{urllib.parse.urlencode(params)}")

    @app.post("/oauth2/quick/microsoft")
    def oauth2_quick_microsoft() -> Any:
        # Uses server-side Microsoft app credentials so users can connect with one click.
        client_id = str(os.environ.get("SCANNER_EMAIL_MS_CLIENT_ID", "")).strip()
        client_secret = str(os.environ.get("SCANNER_EMAIL_MS_CLIENT_SECRET", "")).strip()
        if not client_id:
            return redirect(url_for("index", msg="Server missing SCANNER_EMAIL_MS_CLIENT_ID for quick Outlook connect"))

        desired_name = _str_from_form("quick_name", "")
        login_hint = _str_from_form("quick_email", "")
        redirect_uri = _oauth2_redirect_uri()
        state = secrets.token_urlsafe(24)

        session["oauth2_pending"] = {
            "state": state,
            "provider": "microsoft",
            "mode": "quick_microsoft",
            "redirect_uri": redirect_uri,
            "desired_name": desired_name,
            "login_hint": login_hint,
            "target_inbox_user": _str_from_form("quick_target_inbox_user", ""),
            "target_subfolder": _str_from_form("quick_target_subfolder", "email") or "email",
        }

        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": _quick_ms_scope(),
            "state": state,
            "prompt": "select_account",
            "access_type": "offline",
            "response_mode": "query",
        }
        if login_hint:
            params["login_hint"] = login_hint

        defaults = _oauth2_defaults("microsoft")
        return redirect(f"{defaults['authorize_url']}?{urllib.parse.urlencode(params)}")

    @app.get("/oauth2/callback")
    def oauth2_callback() -> Any:
        pending = session.get("oauth2_pending", {}) if isinstance(session.get("oauth2_pending"), dict) else {}
        if not pending:
            return redirect(url_for("index", msg="No OAuth sign-in is pending"))

        state = request.args.get("state", "").strip()
        expected_state = str(pending.get("state", "")).strip()
        account_name = str(pending.get("account", "")).strip()
        provider = str(pending.get("provider", "")).strip().lower()
        mode = str(pending.get("mode", "account")).strip().lower() or "account"
        if not state or not expected_state or not secrets.compare_digest(state, expected_state):
            session.pop("oauth2_pending", None)
            return redirect(url_for("index", msg="OAuth state mismatch", edit=account_name))

        oauth_error = request.args.get("error", "").strip()
        if oauth_error:
            desc = request.args.get("error_description", "").strip() or oauth_error
            session.pop("oauth2_pending", None)
            return redirect(url_for("index", msg=f"OAuth sign-in failed: {desc}", edit=account_name))

        code = request.args.get("code", "").strip()
        if not code:
            session.pop("oauth2_pending", None)
            return redirect(url_for("index", msg="OAuth callback missing code", edit=account_name))

        if mode == "quick_microsoft":
            client_id = str(os.environ.get("SCANNER_EMAIL_MS_CLIENT_ID", "")).strip()
            client_secret = str(os.environ.get("SCANNER_EMAIL_MS_CLIENT_SECRET", "")).strip()
            if not client_id:
                session.pop("oauth2_pending", None)
                return redirect(url_for("index", msg="Server missing SCANNER_EMAIL_MS_CLIENT_ID for quick Outlook connect"))

            defaults = _oauth2_defaults("microsoft")
            token_url = defaults["token_url"]
            scope = _quick_ms_scope()
            redirect_uri = str(pending.get("redirect_uri", "")).strip() or _oauth2_redirect_uri()

            try:
                token_payload = _oauth2_exchange_code(
                    token_url=token_url,
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=redirect_uri,
                    code=code,
                    scope=scope,
                )
            except Exception as exc:
                session.pop("oauth2_pending", None)
                return redirect(url_for("index", msg=f"OAuth token exchange failed: {exc}"))

            access_token = str(token_payload.get("access_token", "")).strip()
            refresh_token = str(token_payload.get("refresh_token", "")).strip()
            id_token = str(token_payload.get("id_token", "")).strip()
            expires_in = int(token_payload.get("expires_in", 3600) or 3600)
            if not access_token or not refresh_token:
                desc = str(token_payload.get("error_description", token_payload.get("error", "missing OAuth tokens")))
                session.pop("oauth2_pending", None)
                return redirect(url_for("index", msg=f"OAuth sign-in failed: {desc}"))

            claims = _decode_id_token_claims(id_token)
            username = _email_from_claims(claims) or str(pending.get("login_hint", "")).strip()
            if not username or "@" not in username:
                session.pop("oauth2_pending", None)
                return redirect(url_for("index", msg="Could not determine mailbox email from Microsoft sign-in"))

            cfg = load_config(config_path)
            accounts = cfg.get("accounts", []) if isinstance(cfg.get("accounts"), list) else []
            desired_name = str(pending.get("desired_name", "")).strip()
            base_name = desired_name or _safe_mailbox_name(username.split("@", 1)[0])
            account_name_final = _unique_mailbox_name(accounts, _safe_mailbox_name(base_name))

            row = {
                "name": account_name_final,
                "enabled": True,
                "host": "imap-mail.outlook.com",
                "port": 993,
                "use_ssl": True,
                "auth_mode": "oauth2",
                "username": username,
                "password": "",
                "oauth2_provider": "microsoft",
                "oauth2_token_url": token_url,
                "oauth2_scope": "offline_access https://outlook.office.com/IMAP.AccessAsUser.All",
                "oauth2_client_id": client_id,
                "oauth2_client_secret": client_secret,
                "oauth2_refresh_token": refresh_token,
                "oauth2_access_token": access_token,
                "oauth2_access_token_expires_at": int(time.time()) + max(60, expires_in),
                "target_inbox_user": str(pending.get("target_inbox_user", "")).strip(),
                "target_subfolder": str(pending.get("target_subfolder", "email")).strip() or "email",
                "mailbox": "INBOX",
                "max_messages_per_poll": 50,
                "mark_seen": False,
            }
            accounts.append(row)
            cfg["accounts"] = accounts
            cfg["enabled"] = True
            save_config(config_path, cfg)
            session.pop("oauth2_pending", None)
            return redirect(url_for("index", msg=f"Outlook mailbox connected: {account_name_final}", edit=account_name_final))

        cfg = load_config(config_path)
        accounts = cfg.get("accounts", []) if isinstance(cfg.get("accounts"), list) else []
        idx = -1
        row: dict[str, Any] = {}
        for i, item in enumerate(accounts):
            if isinstance(item, dict) and str(item.get("name", "")).strip() == account_name:
                idx = i
                row = item
                break
        if idx < 0:
            session.pop("oauth2_pending", None)
            return redirect(url_for("index", msg=f"Mailbox not found: {account_name}"))

        defaults = _oauth2_defaults(provider)
        token_url = str(row.get("oauth2_token_url", "")).strip() or defaults["token_url"]
        scope = str(row.get("oauth2_scope", "")).strip() or defaults["scope"]
        client_id = str(row.get("oauth2_client_id", "")).strip()
        client_secret = str(row.get("oauth2_client_secret", "")).strip()
        redirect_uri = str(pending.get("redirect_uri", "")).strip() or _oauth2_redirect_uri()

        try:
            token_payload = _oauth2_exchange_code(
                token_url=token_url,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                code=code,
                scope=scope,
            )
        except Exception as exc:
            session.pop("oauth2_pending", None)
            return redirect(url_for("index", msg=f"OAuth token exchange failed: {exc}", edit=account_name))

        access_token = str(token_payload.get("access_token", "")).strip()
        refresh_token = str(token_payload.get("refresh_token", "")).strip() or str(row.get("oauth2_refresh_token", "")).strip()
        expires_in = int(token_payload.get("expires_in", 3600) or 3600)
        if not access_token:
            desc = str(token_payload.get("error_description", token_payload.get("error", "missing access_token")))
            session.pop("oauth2_pending", None)
            return redirect(url_for("index", msg=f"OAuth token exchange failed: {desc}", edit=account_name))

        row["auth_mode"] = "oauth2"
        row["oauth2_provider"] = provider
        row["oauth2_token_url"] = token_url
        row["oauth2_scope"] = scope
        row["oauth2_access_token"] = access_token
        row["oauth2_refresh_token"] = refresh_token
        row["oauth2_access_token_expires_at"] = int(time.time()) + max(60, expires_in)
        accounts[idx] = row
        cfg["accounts"] = accounts
        save_config(config_path, cfg)
        session.pop("oauth2_pending", None)
        return redirect(url_for("index", msg=f"OAuth sign-in successful for {account_name}", edit=account_name))

    def _svc() -> EmailIntakeService:
        cfg = load_config(config_path)
        return EmailIntakeService(cfg, config_path)

    @app.get("/")
    def index() -> str:
        cfg = load_config(config_path)
        svc = _svc()
        output_cfg = cfg.get("output", {}) if isinstance(cfg.get("output"), dict) else {}
        attach_cfg = cfg.get("attachment_handling", {}) if isinstance(cfg.get("attachment_handling"), dict) else {}
        entity_cfg = cfg.get("entity_extraction", {}) if isinstance(cfg.get("entity_extraction"), dict) else {}
        preview_accounts = [
            {
                "name": a.name,
                "enabled": a.enabled,
                "auth_mode": a.auth_mode,
                "host": a.host,
                "port": a.port,
                "use_ssl": a.use_ssl,
                "mailbox": a.mailbox,
                "username": a.username,
                "mark_seen": a.mark_seen,
                "max_messages_per_poll": a.max_messages_per_poll,
                "oauth2_provider": a.oauth2_provider,
                "oauth2_token_url": a.oauth2_token_url,
                "oauth2_scope": a.oauth2_scope,
                "oauth2_client_id": a.oauth2_client_id,
                "oauth2_client_secret": a.oauth2_client_secret,
                "oauth2_refresh_token": a.oauth2_refresh_token,
                "oauth2_access_token": a.oauth2_access_token,
                "oauth2_access_token_expires_at": a.oauth2_access_token_expires_at,
                "target_inbox_user": getattr(a, "target_inbox_user", ""),
                "target_subfolder": getattr(a, "target_subfolder", "email"),
            }
            for a in svc.accounts
        ]
        edit_name = request.args.get("edit", "").strip()
        edit_account = next((a for a in preview_accounts if str(a.get("name", "")) == edit_name), None)
        if edit_account is None and preview_accounts:
            edit_account = preview_accounts[0]
        config_text = yaml.safe_dump(cfg, sort_keys=False)
        return render_template(
            "index.html",
            config_path=str(config_path),
            config_text=config_text,
            accounts=preview_accounts,
            edit_account=edit_account,
            settings={
                "enabled": bool(cfg.get("enabled", False)),
                "poll_seconds": int(cfg.get("poll_seconds", 300) or 300),
                "log_level": str(cfg.get("log_level", "INFO") or "INFO").upper(),
                "scanner_inbox_dir": str(output_cfg.get("scanner_inbox_dir", "/home/pi/scanner/inbox") or "/home/pi/scanner/inbox"),
                "state_dir": str(output_cfg.get("state_dir", "/home/pi/scanner/state/email_intake") or "/home/pi/scanner/state/email_intake"),
                "entity_db_path": str(output_cfg.get("entity_db_path", "/home/pi/scanner/state/email_intake/entities.db") or "/home/pi/scanner/state/email_intake/entities.db"),
                "write_message_metadata": bool(output_cfg.get("write_message_metadata", True)),
                "max_recent_events_export": int(output_cfg.get("max_recent_events_export", 500) or 500),
                "save_pdf_to_scanner_inbox": bool(attach_cfg.get("save_pdf_to_scanner_inbox", True)),
                "save_non_pdf_to_state": bool(attach_cfg.get("save_non_pdf_to_state", False)),
                "allowed_extensions_csv": ", ".join([str(x) for x in (attach_cfg.get("allowed_extensions", [".pdf"]) or []) if str(x).strip()]),
                "entity_enabled": bool(entity_cfg.get("enabled", True)),
                "detect_tracking": bool(entity_cfg.get("detect_tracking", True)),
                "detect_policy_numbers": bool(entity_cfg.get("detect_policy_numbers", True)),
                "detect_status_keywords": bool(entity_cfg.get("detect_status_keywords", True)),
                "known_shippers_only": bool(entity_cfg.get("known_shippers_only", True)),
                "policy_expiry_alert_days": int(entity_cfg.get("policy_expiry_alert_days", 30) or 30),
                "auto_remove_delivered_enabled": bool(entity_cfg.get("auto_remove_delivered_enabled", True)),
                "auto_remove_delivered_after_days": int(entity_cfg.get("auto_remove_delivered_after_days", 7) or 7),
                "tracking_terminal_statuses_csv": ", ".join([str(x) for x in (entity_cfg.get("tracking_terminal_statuses", ["delivered", "cancelled", "expired", "invalid_tracking"]) or []) if str(x).strip()]),
                "tracking_postcodes_json": json.dumps(entity_cfg.get("tracking_postcodes", {}), ensure_ascii=True, indent=2),
                "additional_tracking_services_json": json.dumps(entity_cfg.get("additional_tracking_services", []), ensure_ascii=True, indent=2),
            },
            state_dir=str(svc.state_dir),
            db_path=str(svc.entity_db_path),
            auth_user=str(session.get("auth_user", "")),
            quick_ms_available=bool(str(os.environ.get("SCANNER_EMAIL_MS_CLIENT_ID", "")).strip()),
            scanner_inbox_users=_load_scanner_user_inboxes(),
            run_once_last_at=str(session.get("run_once_last_at", "")),
            run_once_last_status=str(session.get("run_once_last_status", "")),
            run_once_last_detail=str(session.get("run_once_last_detail", "")),
            message=request.args.get("msg", "").strip(),
        )

    @app.get("/info")
    def info() -> Any:
        cfg = load_config(config_path)
        svc = _svc()
        entity_cfg = cfg.get("entity_extraction", {}) if isinstance(cfg.get("entity_extraction"), dict) else {}
        attach_cfg = cfg.get("attachment_handling", {}) if isinstance(cfg.get("attachment_handling"), dict) else {}
        return render_template(
            "info.html",
            auth_user=str(session.get("auth_user", "")),
            service_enabled=bool(cfg.get("enabled", False)),
            poll_seconds=int(cfg.get("poll_seconds", 300) or 300),
            accounts_total=len(svc.accounts),
            accounts_enabled=len([a for a in svc.accounts if a.enabled]),
            scanner_inbox_dir=str(svc.scanner_inbox_dir),
            state_dir=str(svc.state_dir),
            db_path=str(svc.entity_db_path),
            messages_dir=str(svc.messages_dir),
            attachments_dir=str(svc.attachments_dir),
            current_export_path=str(svc.current_export_path),
            events_export_path=str(svc.events_export_path),
            cursors_path=str(svc.cursors_path),
            save_pdf_to_scanner_inbox=bool(attach_cfg.get("save_pdf_to_scanner_inbox", True)),
            save_non_pdf_to_state=bool(attach_cfg.get("save_non_pdf_to_state", False)),
            allowed_extensions=[str(x) for x in (attach_cfg.get("allowed_extensions", [".pdf"]) or []) if str(x).strip()],
            entity_enabled=bool(entity_cfg.get("enabled", True)),
            detect_tracking=bool(entity_cfg.get("detect_tracking", True)),
            detect_policy_numbers=bool(entity_cfg.get("detect_policy_numbers", True)),
            detect_status_keywords=bool(entity_cfg.get("detect_status_keywords", True)),
            known_shippers_only=bool(entity_cfg.get("known_shippers_only", True)),
            policy_expiry_alert_days=int(entity_cfg.get("policy_expiry_alert_days", 30) or 30),
        )

    @app.get("/monitoring")
    def monitoring_page() -> Any:
        svc = _svc()
        store = EntityStore(svc.entity_db_path)
        shipments = [r for r in store.query_current(entity_type="shipment", limit=2000) if str((r.get("payload") or {}).get("carrier", "unknown")) != "unknown"]
        policies = store.query_current(entity_type="policy", limit=2000)

        for row in shipments:
            payload = row.get("payload", {}) if isinstance(row.get("payload"), dict) else {}
            payload["tracking_url_ui"] = _tracking_url_for_payload(payload, str(row.get("entity_key", "")))
            row["payload"] = payload

        show_hidden = request.args.get("show_hidden", "0").strip().lower() in {"1", "true", "yes", "on"}
        hide_statuses_csv = request.args.get("hide_statuses", "in_transit,expired").strip()
        hidden_statuses = {x.strip().lower() for x in hide_statuses_csv.split(",") if x.strip()}

        if not show_hidden and hidden_statuses:
            shipments = [
                r
                for r in shipments
                if str((r.get("payload") or {}).get("status", "")).strip().lower() not in hidden_statuses
            ]
            policies = [
                r
                for r in policies
                if str((r.get("payload") or {}).get("status", "")).strip().lower() not in hidden_statuses
            ]

        ship_sort = request.args.get("ship_sort", "last_seen_at").strip().lower() or "last_seen_at"
        ship_dir = request.args.get("ship_dir", "desc").strip().lower() or "desc"
        pol_sort = request.args.get("pol_sort", "last_seen_at").strip().lower() or "last_seen_at"
        pol_dir = request.args.get("pol_dir", "desc").strip().lower() or "desc"

        allowed_ship_sort = {
            "carrier",
            "tracking_number",
            "status",
            "eta_date",
            "last_location",
            "tracking_postcode",
            "last_seen_at",
        }
        allowed_pol_sort = {
            "policy_number",
            "policy_type",
            "provider",
            "live",
            "expires_on",
            "expiry_alert",
            "status",
            "last_seen_at",
        }
        if ship_sort not in allowed_ship_sort:
            ship_sort = "last_seen_at"
        if pol_sort not in allowed_pol_sort:
            pol_sort = "last_seen_at"
        if ship_dir not in {"asc", "desc"}:
            ship_dir = "desc"
        if pol_dir not in {"asc", "desc"}:
            pol_dir = "desc"

        def _ship_key(row: dict[str, Any], field: str):
            payload = row.get("payload", {}) if isinstance(row.get("payload"), dict) else {}
            if field == "carrier":
                return str(payload.get("carrier", "")).lower()
            if field == "tracking_number":
                return str(row.get("entity_key", "")).lower()
            if field == "status":
                return str(payload.get("status", "")).lower()
            if field == "eta_date":
                return _to_sort_epoch(payload.get("eta_date", ""))
            if field == "last_location":
                return str(payload.get("last_location", "")).lower()
            if field == "tracking_postcode":
                return str(payload.get("tracking_postcode", "")).lower()
            return _to_sort_epoch(row.get("last_seen_at", ""))

        def _pol_key(row: dict[str, Any], field: str):
            payload = row.get("payload", {}) if isinstance(row.get("payload"), dict) else {}
            if field == "policy_number":
                return str(row.get("entity_key", "")).lower()
            if field == "policy_type":
                return str(payload.get("policy_type", "")).lower()
            if field == "provider":
                return str(payload.get("provider", "")).lower()
            if field == "live":
                return 1 if bool(payload.get("live", False)) else 0
            if field == "expires_on":
                return _to_sort_epoch(payload.get("expires_on", ""))
            if field == "expiry_alert":
                return str(payload.get("expiry_alert", "")).lower()
            if field == "status":
                return str(payload.get("status", "")).lower()
            return _to_sort_epoch(row.get("last_seen_at", ""))

        shipments = sorted(shipments, key=lambda r: _ship_key(r, ship_sort), reverse=(ship_dir == "desc"))
        policies = sorted(policies, key=lambda r: _pol_key(r, pol_sort), reverse=(pol_dir == "desc"))

        def _next_dir(active_sort: str, active_dir: str, field: str) -> str:
            if active_sort != field:
                return "asc"
            return "desc" if active_dir == "asc" else "asc"

        def _monitoring_url(next_ship_sort: str, next_ship_dir: str, next_pol_sort: str, next_pol_dir: str) -> str:
            params = {
                "ship_sort": next_ship_sort,
                "ship_dir": next_ship_dir,
                "pol_sort": next_pol_sort,
                "pol_dir": next_pol_dir,
                "hide_statuses": hide_statuses_csv,
                "show_hidden": "1" if show_hidden else "0",
            }
            return f"/monitoring?{urllib.parse.urlencode(params)}"

        ship_sort_urls = {
            field: _monitoring_url(
                field,
                _next_dir(ship_sort, ship_dir, field),
                pol_sort,
                pol_dir,
            )
            for field in sorted(allowed_ship_sort)
        }
        pol_sort_urls = {
            field: _monitoring_url(
                ship_sort,
                ship_dir,
                field,
                _next_dir(pol_sort, pol_dir, field),
            )
            for field in sorted(allowed_pol_sort)
        }

        active_shipments = [r for r in shipments if bool((r.get("payload") or {}).get("active_monitoring", True))]
        retired_shipments = [r for r in shipments if not bool((r.get("payload") or {}).get("active_monitoring", True))]
        live_policies = [r for r in policies if bool((r.get("payload") or {}).get("live", False))]
        expiring_policies = [
            r
            for r in policies
            if str((r.get("payload") or {}).get("expiry_alert", "")).strip().lower() in {"expiring_soon", "expired"}
        ]

        return render_template(
            "monitoring.html",
            auth_user=str(session.get("auth_user", "")),
            shipments=shipments,
            policies=policies,
            active_shipments=active_shipments,
            retired_shipments=retired_shipments,
            live_policies=live_policies,
            expiring_policies=expiring_policies,
            ship_sort=ship_sort,
            ship_dir=ship_dir,
            pol_sort=pol_sort,
            pol_dir=pol_dir,
            ship_sort_urls=ship_sort_urls,
            pol_sort_urls=pol_sort_urls,
            show_hidden=show_hidden,
            hide_statuses_csv=hide_statuses_csv,
        )

    @app.post("/monitoring/untrack")
    def monitoring_untrack() -> Any:
        entity_type = request.form.get("entity_type", "").strip().lower()
        entity_key = request.form.get("entity_key", "").strip()
        ship_sort = request.form.get("ship_sort", "last_seen_at").strip().lower() or "last_seen_at"
        ship_dir = request.form.get("ship_dir", "desc").strip().lower() or "desc"
        pol_sort = request.form.get("pol_sort", "last_seen_at").strip().lower() or "last_seen_at"
        pol_dir = request.form.get("pol_dir", "desc").strip().lower() or "desc"
        hide_statuses = request.form.get("hide_statuses", "in_transit,expired").strip() or "in_transit,expired"
        show_hidden = request.form.get("show_hidden", "0").strip().lower() in {"1", "true", "yes", "on"}

        if entity_type and entity_key:
            svc = _svc()
            store = EntityStore(svc.entity_db_path)
            actor = str(session.get("auth_user", "")).strip() or "unknown"
            store.untrack_entity(entity_type=entity_type, entity_key=entity_key, source_ref=f"ui:{actor}")

        return redirect(
            url_for(
                "monitoring_page",
                ship_sort=ship_sort,
                ship_dir=ship_dir,
                pol_sort=pol_sort,
                pol_dir=pol_dir,
                hide_statuses=hide_statuses,
                show_hidden="1" if show_hidden else "0",
            )
        )

    @app.get("/entities")
    def entities_page() -> Any:
        svc = _svc()
        store = EntityStore(svc.entity_db_path)
        entity_type = request.args.get("type", "").strip().lower()
        try:
            limit = int(request.args.get("limit", "200") or "200")
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 2000))

        current_sort = request.args.get("current_sort", "last_seen_at").strip().lower() or "last_seen_at"
        current_dir = request.args.get("current_dir", "desc").strip().lower() or "desc"
        events_sort = request.args.get("events_sort", "event_time").strip().lower() or "event_time"
        events_dir = request.args.get("events_dir", "desc").strip().lower() or "desc"

        allowed_current_sort = {"entity_type", "entity_key", "sender", "last_seen_at", "version", "source_last"}
        allowed_events_sort = {"event_time", "entity_type", "entity_key", "sender", "event_type", "source_ref"}
        if current_sort not in allowed_current_sort:
            current_sort = "last_seen_at"
        if events_sort not in allowed_events_sort:
            events_sort = "event_time"
        if current_dir not in {"asc", "desc"}:
            current_dir = "desc"
        if events_dir not in {"asc", "desc"}:
            events_dir = "desc"

        current_rows = store.query_current(entity_type=entity_type, limit=limit)
        event_rows = store.query_events(entity_type=entity_type, limit=limit)

        for row in current_rows:
            if str(row.get("entity_type", "")).strip().lower() != "shipment":
                continue
            payload = row.get("payload", {}) if isinstance(row.get("payload"), dict) else {}
            payload["tracking_url_ui"] = _tracking_url_for_payload(payload, str(row.get("entity_key", "")))
            row["payload"] = payload

        def _current_key(row: dict[str, Any], field: str):
            if field == "version":
                try:
                    return int(row.get("version", 0))
                except Exception:
                    return 0
            if field == "last_seen_at":
                return _to_sort_epoch(row.get("last_seen_at", ""))
            if field == "sender":
                payload = row.get("payload", {}) if isinstance(row.get("payload"), dict) else {}
                return str(payload.get("sender", "")).lower()
            return str(row.get(field, "")).lower()

        def _event_key(row: dict[str, Any], field: str):
            if field == "event_time":
                return _to_sort_epoch(row.get("event_time", ""))
            if field == "sender":
                payload = row.get("payload", {}) if isinstance(row.get("payload"), dict) else {}
                return str(payload.get("sender", "")).lower()
            return str(row.get(field, "")).lower()

        current_rows = sorted(current_rows, key=lambda r: _current_key(r, current_sort), reverse=(current_dir == "desc"))
        event_rows = sorted(event_rows, key=lambda r: _event_key(r, events_sort), reverse=(events_dir == "desc"))

        def _next_dir(active_sort: str, active_dir: str, field: str) -> str:
            if active_sort != field:
                return "asc"
            return "desc" if active_dir == "asc" else "asc"

        def _entities_url(
            cur_sort: str,
            cur_dir: str,
            ev_sort: str,
            ev_dir: str,
        ) -> str:
            params = {
                "type": entity_type,
                "limit": str(limit),
                "current_sort": cur_sort,
                "current_dir": cur_dir,
                "events_sort": ev_sort,
                "events_dir": ev_dir,
            }
            return f"/entities?{urllib.parse.urlencode(params)}"

        current_sort_urls = {
            field: _entities_url(
                field,
                _next_dir(current_sort, current_dir, field),
                events_sort,
                events_dir,
            )
            for field in sorted(allowed_current_sort)
        }
        events_sort_urls = {
            field: _entities_url(
                current_sort,
                current_dir,
                field,
                _next_dir(events_sort, events_dir, field),
            )
            for field in sorted(allowed_events_sort)
        }
        return render_template(
            "entities.html",
            auth_user=str(session.get("auth_user", "")),
            entity_type=entity_type,
            limit=limit,
            current_rows=current_rows,
            event_rows=event_rows,
            current_count=len(current_rows),
            events_count=len(event_rows),
            current_sort=current_sort,
            current_dir=current_dir,
            events_sort=events_sort,
            events_dir=events_dir,
            current_sort_urls=current_sort_urls,
            events_sort_urls=events_sort_urls,
        )

    @app.post("/entities/untrack")
    def entities_untrack() -> Any:
        entity_type = request.form.get("entity_type", "").strip().lower()
        entity_key = request.form.get("entity_key", "").strip()
        filter_type = request.form.get("filter_type", "").strip().lower()
        limit_raw = request.form.get("limit", "200").strip() or "200"
        current_sort = request.form.get("current_sort", "last_seen_at").strip().lower() or "last_seen_at"
        current_dir = request.form.get("current_dir", "desc").strip().lower() or "desc"
        events_sort = request.form.get("events_sort", "event_time").strip().lower() or "event_time"
        events_dir = request.form.get("events_dir", "desc").strip().lower() or "desc"

        try:
            limit = max(1, min(int(limit_raw), 2000))
        except ValueError:
            limit = 200

        if not entity_type or not entity_key:
            return redirect(
                url_for(
                    "entities_page",
                    type=filter_type,
                    limit=limit,
                    current_sort=current_sort,
                    current_dir=current_dir,
                    events_sort=events_sort,
                    events_dir=events_dir,
                )
            )

        svc = _svc()
        store = EntityStore(svc.entity_db_path)
        actor = str(session.get("auth_user", "")).strip() or "unknown"
        removed = store.untrack_entity(entity_type=entity_type, entity_key=entity_key, source_ref=f"ui:{actor}")
        msg = f"Untracked {entity_type}:{entity_key}" if removed else f"Entity not found: {entity_type}:{entity_key}"
        return redirect(
            url_for(
                "entities_page",
                type=filter_type,
                limit=limit,
                current_sort=current_sort,
                current_dir=current_dir,
                events_sort=events_sort,
                events_dir=events_dir,
                msg=msg,
            )
        )

    @app.post("/settings/save")
    def settings_save() -> Any:
        cfg = load_config(config_path)
        try:
            poll_seconds = _int_from_form("poll_seconds", 300)
            max_recent_events_export = _int_from_form("max_recent_events_export", 500)
            if poll_seconds < 10 or poll_seconds > 3600:
                raise ValueError("poll_seconds must be between 10 and 3600")
            if max_recent_events_export < 50 or max_recent_events_export > 5000:
                raise ValueError("max_recent_events_export must be between 50 and 5000")

            allowed_ext_raw = _str_from_form("allowed_extensions_csv", ".pdf")
            allowed_extensions = []
            for token in allowed_ext_raw.split(","):
                ext = token.strip().lower()
                if not ext:
                    continue
                if not ext.startswith("."):
                    ext = f".{ext}"
                allowed_extensions.append(ext)
            if not allowed_extensions:
                allowed_extensions = [".pdf"]

            cfg["enabled"] = _bool_from_form("enabled")
            cfg["poll_seconds"] = int(poll_seconds)
            cfg["log_level"] = _str_from_form("log_level", "INFO").upper() or "INFO"

            output_cfg = cfg.setdefault("output", {})
            if not isinstance(output_cfg, dict):
                output_cfg = {}
                cfg["output"] = output_cfg
            output_cfg["scanner_inbox_dir"] = _str_from_form("scanner_inbox_dir", "/home/pi/scanner/inbox")
            output_cfg["state_dir"] = _str_from_form("state_dir", "/home/pi/scanner/state/email_intake")
            output_cfg["entity_db_path"] = _str_from_form("entity_db_path", "/home/pi/scanner/state/email_intake/entities.db")
            output_cfg["write_message_metadata"] = _bool_from_form("write_message_metadata")
            output_cfg["max_recent_events_export"] = int(max_recent_events_export)

            attach_cfg = cfg.setdefault("attachment_handling", {})
            if not isinstance(attach_cfg, dict):
                attach_cfg = {}
                cfg["attachment_handling"] = attach_cfg
            attach_cfg["save_pdf_to_scanner_inbox"] = _bool_from_form("save_pdf_to_scanner_inbox")
            attach_cfg["save_non_pdf_to_state"] = _bool_from_form("save_non_pdf_to_state")
            attach_cfg["allowed_extensions"] = allowed_extensions

            entity_cfg = cfg.setdefault("entity_extraction", {})
            if not isinstance(entity_cfg, dict):
                entity_cfg = {}
                cfg["entity_extraction"] = entity_cfg
            entity_cfg["enabled"] = _bool_from_form("entity_enabled")
            entity_cfg["detect_tracking"] = _bool_from_form("detect_tracking")
            entity_cfg["detect_policy_numbers"] = _bool_from_form("detect_policy_numbers")
            entity_cfg["detect_status_keywords"] = _bool_from_form("detect_status_keywords")
            entity_cfg["known_shippers_only"] = _bool_from_form("known_shippers_only")
            entity_cfg["auto_remove_delivered_enabled"] = _bool_from_form("auto_remove_delivered_enabled")

            policy_expiry_alert_days = _int_from_form("policy_expiry_alert_days", 30)
            entity_cfg["policy_expiry_alert_days"] = max(1, min(policy_expiry_alert_days, 365))
            auto_remove_days = _int_from_form("auto_remove_delivered_after_days", 7)
            entity_cfg["auto_remove_delivered_after_days"] = max(1, min(auto_remove_days, 180))

            terminal_csv = _str_from_form("tracking_terminal_statuses_csv", "delivered,cancelled,expired,invalid_tracking")
            entity_cfg["tracking_terminal_statuses"] = [
                x.strip().lower()
                for x in terminal_csv.split(",")
                if x.strip()
            ]

            postcodes_raw = _str_from_form("tracking_postcodes_json", "{}")
            services_raw = _str_from_form("additional_tracking_services_json", "[]")
            try:
                postcodes_obj = json.loads(postcodes_raw or "{}")
            except Exception:
                raise ValueError("tracking_postcodes_json must be valid JSON")
            try:
                services_obj = json.loads(services_raw or "[]")
            except Exception:
                raise ValueError("additional_tracking_services_json must be valid JSON")

            if not isinstance(postcodes_obj, dict):
                raise ValueError("tracking_postcodes_json must be a JSON object")
            if not isinstance(services_obj, list):
                raise ValueError("additional_tracking_services_json must be a JSON array")

            entity_cfg["tracking_postcodes"] = {
                str(k).strip().lower(): str(v).strip()
                for k, v in postcodes_obj.items()
                if str(k).strip()
            }
            entity_cfg["additional_tracking_services"] = [x for x in services_obj if isinstance(x, dict)]

            save_config(config_path, cfg)
            return redirect(url_for("index", msg="Settings saved"))
        except Exception as exc:
            return redirect(url_for("index", msg=f"Settings save failed: {exc}"))

    @app.post("/account/save")
    def account_save() -> Any:
        cfg = load_config(config_path)
        accounts = cfg.get("accounts", [])
        if not isinstance(accounts, list):
            accounts = []
        submit_action = _str_from_form("submit_action", "save")

        current_name = _str_from_form("current_name", "")
        name = _str_from_form("name", "")
        enabled = _bool_from_form("enabled")
        host = _str_from_form("host", "")
        port = _int_from_form("port", 993)
        use_ssl = _bool_from_form("use_ssl")
        username = _str_from_form("username", "")
        mailbox = _str_from_form("mailbox", "INBOX") or "INBOX"
        max_messages_per_poll = _int_from_form("max_messages_per_poll", 50)
        mark_seen = _bool_from_form("mark_seen")

        auth_mode = _str_from_form("auth_mode", "password").lower() or "password"
        password = _str_from_form("password", "")
        oauth2_provider = _str_from_form("oauth2_provider", "")
        oauth2_token_url = _str_from_form("oauth2_token_url", "")
        oauth2_scope = _str_from_form("oauth2_scope", "")
        oauth2_client_id = _str_from_form("oauth2_client_id", "")
        oauth2_client_secret = _str_from_form("oauth2_client_secret", "")
        oauth2_refresh_token = _str_from_form("oauth2_refresh_token", "")
        oauth2_access_token = _str_from_form("oauth2_access_token", "")
        oauth2_access_token_expires_at = _int_from_form("oauth2_access_token_expires_at", 0)
        target_inbox_user = _str_from_form("target_inbox_user", "")
        target_subfolder = _str_from_form("target_subfolder", "email") or "email"

        if not name:
            return redirect(url_for("index", msg="Account name is required"))
        if not host or not username:
            return redirect(url_for("index", msg="Host and username are required"))
        if port < 1 or port > 65535:
            return redirect(url_for("index", msg="Port must be between 1 and 65535"))
        if max_messages_per_poll < 1 or max_messages_per_poll > 500:
            return redirect(url_for("index", msg="max_messages_per_poll must be between 1 and 500"))
        if auth_mode not in {"password", "oauth2"}:
            return redirect(url_for("index", msg="auth_mode must be password or oauth2"))

        target_index = -1
        existing_row: dict[str, Any] = {}
        for idx, row in enumerate(accounts):
            if not isinstance(row, dict):
                continue
            row_name = str(row.get("name", "")).strip()
            if current_name and row_name == current_name:
                target_index = idx
                existing_row = dict(row)
                break
            if not current_name and row_name == name:
                target_index = idx
                existing_row = dict(row)
                break

        if auth_mode == "password" and not password:
            password = str(existing_row.get("password", "")).strip()
            if not password:
                return redirect(url_for("index", msg="Password is required for password auth mode", edit=name or current_name))

        if auth_mode == "oauth2":
            if not oauth2_client_secret:
                oauth2_client_secret = str(existing_row.get("oauth2_client_secret", "")).strip()
            if not oauth2_refresh_token:
                oauth2_refresh_token = str(existing_row.get("oauth2_refresh_token", "")).strip()
            if not oauth2_access_token:
                oauth2_access_token = str(existing_row.get("oauth2_access_token", "")).strip()
            if not oauth2_client_id:
                oauth2_client_id = str(existing_row.get("oauth2_client_id", "")).strip()
            if not oauth2_client_id or not oauth2_refresh_token:
                return redirect(url_for("index", msg="oauth2_client_id and oauth2_refresh_token are required for oauth2 mode", edit=name or current_name))

        new_row = {
            "name": name,
            "enabled": bool(enabled),
            "host": host,
            "port": int(port),
            "use_ssl": bool(use_ssl),
            "auth_mode": auth_mode,
            "username": username,
            "password": password,
            "oauth2_provider": oauth2_provider,
            "oauth2_token_url": oauth2_token_url,
            "oauth2_scope": oauth2_scope,
            "oauth2_client_id": oauth2_client_id,
            "oauth2_client_secret": oauth2_client_secret,
            "oauth2_refresh_token": oauth2_refresh_token,
            "oauth2_access_token": oauth2_access_token,
            "oauth2_access_token_expires_at": int(oauth2_access_token_expires_at),
            "target_inbox_user": target_inbox_user,
            "target_subfolder": target_subfolder,
            "mailbox": mailbox,
            "max_messages_per_poll": int(max_messages_per_poll),
            "mark_seen": bool(mark_seen),
        }

        if target_index >= 0:
            accounts[target_index] = new_row
        else:
            accounts.append(new_row)

        cfg["accounts"] = accounts
        save_config(config_path, cfg)

        if submit_action == "oauth_microsoft":
            return redirect(url_for("oauth2_start", provider="microsoft", account=name))
        if submit_action == "oauth_google":
            return redirect(url_for("oauth2_start", provider="google", account=name))

        return redirect(url_for("index", msg=f"Account saved: {name}", edit=name))

    @app.post("/account/delete")
    def account_delete() -> Any:
        cfg = load_config(config_path)
        accounts = cfg.get("accounts", [])
        if not isinstance(accounts, list):
            accounts = []
        name = _str_from_form("name", "")
        if not name:
            return redirect(url_for("index", msg="Account name is required"))

        kept = []
        removed = False
        for row in accounts:
            if not isinstance(row, dict):
                continue
            if str(row.get("name", "")).strip() == name:
                removed = True
                continue
            kept.append(row)

        if not removed:
            return redirect(url_for("index", msg=f"Account not found: {name}"))

        cfg["accounts"] = kept
        save_config(config_path, cfg)
        return redirect(url_for("index", msg=f"Account deleted: {name}"))

    @app.post("/config/save")
    def config_save() -> Any:
        raw = request.form.get("config_text", "")
        try:
            parsed = yaml.safe_load(raw) or {}
            if not isinstance(parsed, dict):
                raise ValueError("Config root must be a mapping")
            save_config(config_path, parsed)
            return redirect(url_for("index", msg="Configuration saved"))
        except Exception as exc:
            return redirect(url_for("index", msg=f"Config save failed: {exc}"))

    @app.post("/account/test")
    def account_test() -> Any:
        account_name = request.form.get("account_name", "").strip()
        svc = _svc()
        account = next((a for a in svc.accounts if a.name == account_name), None)
        if account is None:
            return redirect(url_for("index", msg=f"Account not found: {account_name}"))
        ok, message = test_account_connection(account, service=svc)
        prefix = "OK" if ok else "FAILED"
        return redirect(url_for("index", msg=f"Account test {prefix}: {message}"))

    @app.post("/run-once")
    def run_once() -> Any:
        cmd = [
            "python3",
            str(Path(__file__).resolve().parent / "email_intake_service.py"),
            "--config",
            str(config_path),
            "--once",
        ]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if cp.returncode == 0:
                session["run_once_last_at"] = _utc_now_iso()
                session["run_once_last_status"] = "ok"
                session["run_once_last_detail"] = "Run-once completed"
                return redirect(url_for("index", msg="Run-once completed"))
            tail = (cp.stderr or cp.stdout or "").strip().splitlines()[-1:]
            msg = tail[0] if tail else f"exit={cp.returncode}"
            session["run_once_last_at"] = _utc_now_iso()
            session["run_once_last_status"] = "failed"
            session["run_once_last_detail"] = msg
            return redirect(url_for("index", msg=f"Run-once failed: {msg}"))
        except Exception as exc:
            session["run_once_last_at"] = _utc_now_iso()
            session["run_once_last_status"] = "failed"
            session["run_once_last_detail"] = str(exc)
            return redirect(url_for("index", msg=f"Run-once failed: {exc}"))

    @app.get("/api/health")
    def api_health() -> Any:
        svc = _svc()
        return jsonify(
            {
                "enabled": bool(svc.enabled),
                "poll_seconds": int(svc.poll_seconds),
                "accounts_total": len(svc.accounts),
                "accounts_enabled": len([a for a in svc.accounts if a.enabled]),
                "state_dir": str(svc.state_dir),
                "db_path": str(svc.entity_db_path),
            }
        )

    @app.get("/api/entities/current")
    def api_entities_current() -> Any:
        svc = _svc()
        store = EntityStore(svc.entity_db_path)
        entity_type = request.args.get("type", "").strip().lower()
        limit = int(request.args.get("limit", "500") or "500")
        return jsonify({"rows": store.query_current(entity_type=entity_type, limit=limit)})

    @app.get("/api/entities/events")
    def api_entities_events() -> Any:
        svc = _svc()
        store = EntityStore(svc.entity_db_path)
        entity_type = request.args.get("type", "").strip().lower()
        since = request.args.get("since", "").strip()
        limit = int(request.args.get("limit", "500") or "500")
        return jsonify({"rows": store.query_events(since_iso=since, entity_type=entity_type, limit=limit)})

    @app.get("/api/config")
    def api_config() -> Any:
        cfg = load_config(config_path)
        safe = json.loads(json.dumps(cfg))
        if isinstance(safe.get("accounts"), list):
            for row in safe["accounts"]:
                if isinstance(row, dict):
                    if row.get("password"):
                        row["password"] = "***"
                    if row.get("oauth2_client_secret"):
                        row["oauth2_client_secret"] = "***"
                    if row.get("oauth2_refresh_token"):
                        row["oauth2_refresh_token"] = "***"
                    if row.get("oauth2_access_token"):
                        row["oauth2_access_token"] = "***"
        return jsonify(safe)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Email intake bolt-on web UI")
    parser.add_argument("--config", required=True, help="Path to email intake YAML config")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8091, help="Bind port")
    args = parser.parse_args()

    app = make_app(Path(args.config).expanduser().resolve())
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import email
import imaplib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.policy import default
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import yaml

from entity_store import EntityObservation, EntityStore, utc_now_iso


LOG = logging.getLogger("email_intake")

TRACKING_PATTERNS = [
    re.compile(r"\b1Z[0-9A-Z]{16}\b", re.IGNORECASE),
    re.compile(r"\b[A-Z]{2}[0-9]{9}[A-Z]{2}\b"),
    re.compile(r"\b[0-9]{10,22}\b"),
]

DEFAULT_TRACKING_SERVICES: list[dict[str, Any]] = [
    {
        "name": "ups",
        "hints": ["ups", "1z"],
        "patterns": [r"\b1Z[0-9A-Z]{16}\b"],
        "tracking_url": "https://www.ups.com/track?tracknum={tracking_number}",
        "requires_postcode": False,
    },
    {
        "name": "fedex",
        "hints": ["fedex", "federal express"],
        "patterns": [r"\b([0-9]{12}|[0-9]{15}|[0-9]{20}|[0-9]{22})\b"],
        "tracking_url": "https://www.fedex.com/fedextrack/?trknbr={tracking_number}",
        "requires_postcode": False,
    },
    {
        "name": "usps",
        "hints": ["usps", "united states postal service"],
        "patterns": [r"\b(94[0-9]{20}|93[0-9]{20}|92[0-9]{20}|95[0-9]{20}|[A-Z]{2}[0-9]{9}[A-Z]{2})\b"],
        "tracking_url": "https://tools.usps.com/go/TrackConfirmAction?tLabels={tracking_number}",
        "requires_postcode": False,
    },
    {
        "name": "dhl",
        "hints": ["dhl", "deutsche post dhl"],
        "patterns": [r"\b([0-9]{10}|[0-9]{11}|JD[0-9]{18})\b"],
        "tracking_url": "https://www.dhl.com/global-en/home/tracking/tracking-express.html?submit=1&tracking-id={tracking_number}",
        "requires_postcode": False,
    },
    {
        "name": "royal_mail",
        "hints": ["royal mail", "tracked 24", "tracked 48"],
        "patterns": [r"\b[A-Z]{2}[0-9]{9}GB\b"],
        "tracking_url": "https://www.royalmail.com/track-your-item#/tracking-results/{tracking_number}",
        "requires_postcode": True,
    },
    {
        "name": "evri",
        "hints": ["evri", "hermes"],
        "patterns": [r"\bH00[0-9A-Z]{12,18}\b", r"\b[0-9]{16}\b"],
        "tracking_url": "https://www.evri.com/track-a-parcel?tracking-number={tracking_number}",
        "requires_postcode": True,
    },
    {
        "name": "dpd",
        "hints": ["dpd", "dpd local"],
        "patterns": [r"\b[0-9]{14}\b"],
        "tracking_url": "https://track.dpd.co.uk/?parcel={tracking_number}",
        "requires_postcode": True,
    },
    {
        "name": "parcelforce",
        "hints": ["parcelforce"],
        "patterns": [r"\b[A-Z]{2}[0-9]{9}GB\b"],
        "tracking_url": "https://www.parcelforce.com/track-trace?trackNumber={tracking_number}",
        "requires_postcode": True,
    },
    {
        "name": "yodel",
        "hints": ["yodel"],
        "patterns": [r"\bJD[0-9]{14}\b", r"\b[0-9]{16}\b"],
        "tracking_url": "https://www.yodel.co.uk/track/{tracking_number}",
        "requires_postcode": True,
    },
    {
        "name": "amazon_logistics",
        "hints": ["amazon logistics", "amazon shipping", "tba"],
        "patterns": [r"\bTBA[0-9]{12}\b"],
        "tracking_url": "https://track.amazon.co.uk/tracking/{tracking_number}",
        "requires_postcode": False,
    },
    {
        "name": "gls",
        "hints": ["gls"],
        "patterns": [r"\b[0-9]{8,12}\b"],
        "tracking_url": "https://gls-group.com/track-detail?match={tracking_number}",
        "requires_postcode": False,
    },
]

POLICY_PATTERNS = [
    re.compile(r"\bpolicy(?:\s*(?:no|number|#))?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{5,40})\b", re.IGNORECASE),
    re.compile(r"\bpolicy\s+reference\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{5,40})\b", re.IGNORECASE),
    re.compile(r"\bpolicy\s+id\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{5,40})\b", re.IGNORECASE),
]

POLICY_PROVIDER_PATTERNS = [
    re.compile(r"\binsurer(?:\s*name)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9 &.'\-/]{2,80})\b", re.IGNORECASE),
    re.compile(r"\binsurance\s+provider\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9 &.'\-/]{2,80})\b", re.IGNORECASE),
    re.compile(r"\bunderwriter\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9 &.'\-/]{2,80})\b", re.IGNORECASE),
]

POLICY_EXPIRY_PATTERNS = [
    re.compile(r"\b(?:policy\s+)?(?:expiry|expires|expiration|valid\s+until|renewal\s+date|end\s+date)\s*[:\-]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4}|[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})\b", re.IGNORECASE),
]

POLICY_TYPE_KEYWORDS: dict[str, list[str]] = {
    "car": ["car insurance", "vehicle insurance", "motor insurance", "auto insurance", "fleet insurance"],
    "house": ["home insurance", "house insurance", "property insurance", "buildings insurance", "contents insurance"],
    "business": ["business insurance", "commercial insurance", "public liability", "professional indemnity"],
}

STATUS_KEYWORDS = {
    "delivered": ["delivered", "delivery complete", "left with"],
    "in_transit": ["in transit", "on the way", "moving through network"],
    "out_for_delivery": ["out for delivery", "due today"],
    "shipped": ["shipped", "dispatch confirmed", "despatched"],
    "renewed": ["renewed", "renewal confirmed"],
    "expired": ["expired", "lapsed"],
    "cancelled": ["cancelled", "canceled", "terminated"],
    "invalid_tracking": [
        "tracking number is not valid",
        "tracking number cannot be found",
        "tracking number not found",
        "no longer valid",
        "invalid tracking",
        "tracking id is invalid",
    ],
}

TRACKING_CONTEXT_KEYWORDS = [
    "tracking",
    "track",
    "shipment",
    "shipped",
    "parcel",
    "consignment",
    "waybill",
    "awb",
    "delivery",
    "courier",
]

POLICY_CONTEXT_KEYWORDS = [
    "policy",
    "insurance",
    "insurer",
    "underwriter",
    "premium",
    "renewal",
    "cover",
    "coverage",
]


@dataclass
class AccountConfig:
    name: str
    enabled: bool
    host: str
    port: int
    use_ssl: bool
    username: str
    password: str
    auth_mode: str
    oauth2_provider: str
    oauth2_token_url: str
    oauth2_scope: str
    oauth2_client_id: str
    oauth2_client_secret: str
    oauth2_refresh_token: str
    oauth2_access_token: str
    oauth2_access_token_expires_at: int
    target_inbox_user: str
    target_subfolder: str
    mailbox: str
    max_messages_per_poll: int
    mark_seen: bool


class EmailIntakeService:
    def __init__(self, cfg: dict[str, Any], cfg_path: Path) -> None:
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.enabled = bool(cfg.get("enabled", False))
        self.poll_seconds = int(cfg.get("poll_seconds", 300))

        output = cfg.get("output", {}) if isinstance(cfg.get("output"), dict) else {}
        self.scanner_inbox_dir = Path(str(output.get("scanner_inbox_dir", "/home/pi/scanner/inbox")))
        self.state_dir = Path(str(output.get("state_dir", "/home/pi/scanner/state/email_intake")))
        self.entity_db_path = Path(str(output.get("entity_db_path", self.state_dir / "entities.db")))
        self.write_message_metadata = bool(output.get("write_message_metadata", True))
        self.max_recent_events_export = int(output.get("max_recent_events_export", 500))

        attach = cfg.get("attachment_handling", {}) if isinstance(cfg.get("attachment_handling"), dict) else {}
        self.save_pdf_to_scanner_inbox = bool(attach.get("save_pdf_to_scanner_inbox", True))
        self.save_non_pdf_to_state = bool(attach.get("save_non_pdf_to_state", False))
        self.allowed_extensions = {
            str(x).strip().lower() for x in (attach.get("allowed_extensions", [".pdf"]) or []) if str(x).strip()
        }

        entity_cfg = cfg.get("entity_extraction", {}) if isinstance(cfg.get("entity_extraction"), dict) else {}
        self.entity_enabled = bool(entity_cfg.get("enabled", True))
        self.detect_tracking = bool(entity_cfg.get("detect_tracking", True))
        self.detect_policy_numbers = bool(entity_cfg.get("detect_policy_numbers", True))
        self.detect_status_keywords = bool(entity_cfg.get("detect_status_keywords", True))
        self.known_shippers_only = bool(entity_cfg.get("known_shippers_only", True))
        self.policy_expiry_alert_days = max(1, int(entity_cfg.get("policy_expiry_alert_days", 30) or 30))
        self.auto_remove_delivered_after_days = max(1, int(entity_cfg.get("auto_remove_delivered_after_days", 7) or 7))
        self.auto_remove_delivered_enabled = bool(entity_cfg.get("auto_remove_delivered_enabled", True))
        self.tracking_terminal_statuses = {
            str(x).strip().lower()
            for x in (entity_cfg.get("tracking_terminal_statuses", ["delivered", "cancelled", "expired", "invalid_tracking"]) or [])
            if str(x).strip()
        }
        postcode_cfg = entity_cfg.get("tracking_postcodes", {}) if isinstance(entity_cfg.get("tracking_postcodes"), dict) else {}
        self.tracking_postcodes = {str(k).strip().lower(): str(v).strip() for k, v in postcode_cfg.items() if str(k).strip() and str(v).strip()}
        self.additional_tracking_services = entity_cfg.get("additional_tracking_services", []) if isinstance(entity_cfg.get("additional_tracking_services"), list) else []
        self.tracking_services = self._build_tracking_services(self.additional_tracking_services)

        self.messages_dir = self.state_dir / "messages"
        self.attachments_dir = self.state_dir / "attachments"
        self.cursors_path = self.state_dir / "cursors.json"
        self.current_export_path = self.state_dir / "current_entities.json"
        self.events_export_path = self.state_dir / "recent_events.json"

        self.scanner_inbox_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.messages_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)

        self.store = EntityStore(self.entity_db_path)
        self.cursors = self._load_cursors()

        self.accounts = self._parse_accounts(cfg.get("accounts", []))

    def _parse_accounts(self, raw_accounts: Any) -> list[AccountConfig]:
        rows: list[AccountConfig] = []
        if not isinstance(raw_accounts, list):
            return rows
        for item in raw_accounts:
            if not isinstance(item, dict):
                continue
            rows.append(
                AccountConfig(
                    name=str(item.get("name", "account")).strip() or "account",
                    enabled=bool(item.get("enabled", False)),
                    host=str(item.get("host", "")).strip(),
                    port=int(item.get("port", 993)),
                    use_ssl=bool(item.get("use_ssl", True)),
                    username=str(item.get("username", "")).strip(),
                    password=str(item.get("password", "")).strip(),
                    auth_mode=str(item.get("auth_mode", "password")).strip().lower() or "password",
                    oauth2_provider=str(item.get("oauth2_provider", "")).strip().lower(),
                    oauth2_token_url=str(item.get("oauth2_token_url", "")).strip(),
                    oauth2_scope=str(item.get("oauth2_scope", "")).strip(),
                    oauth2_client_id=str(item.get("oauth2_client_id", "")).strip(),
                    oauth2_client_secret=str(item.get("oauth2_client_secret", "")).strip(),
                    oauth2_refresh_token=str(item.get("oauth2_refresh_token", "")).strip(),
                    oauth2_access_token=str(item.get("oauth2_access_token", "")).strip(),
                    oauth2_access_token_expires_at=self._to_int(item.get("oauth2_access_token_expires_at", 0), default=0),
                    target_inbox_user=str(item.get("target_inbox_user", "")).strip(),
                    target_subfolder=str(item.get("target_subfolder", "")).strip() or "email",
                    mailbox=str(item.get("mailbox", "INBOX")).strip() or "INBOX",
                    max_messages_per_poll=max(1, int(item.get("max_messages_per_poll", 50))),
                    mark_seen=bool(item.get("mark_seen", False)),
                )
            )
        return rows

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _oauth2_defaults(provider: str) -> tuple[str, str]:
        p = (provider or "").strip().lower()
        if p == "microsoft":
            return "https://login.microsoftonline.com/common/oauth2/v2.0/token", "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
        if p == "google":
            return "https://oauth2.googleapis.com/token", "https://mail.google.com/"
        return "", ""

    def _token_url_for_account(self, account: AccountConfig) -> str:
        if account.oauth2_token_url:
            return account.oauth2_token_url
        token_url, _ = self._oauth2_defaults(account.oauth2_provider)
        return token_url

    def _scope_for_account(self, account: AccountConfig) -> str:
        if account.oauth2_scope:
            return account.oauth2_scope
        _, scope = self._oauth2_defaults(account.oauth2_provider)
        return scope

    @staticmethod
    def _oauth2_valid_for_seconds(account: AccountConfig, skew_seconds: int = 120) -> int:
        now = int(time.time())
        exp = int(account.oauth2_access_token_expires_at or 0)
        if exp <= 0:
            return 0
        return max(0, exp - now - max(0, skew_seconds))

    def _persist_account_tokens(self, account_name: str, access_token: str, refresh_token: str, expires_at: int) -> None:
        if not isinstance(self.cfg.get("accounts"), list):
            return
        updated = False
        for row in self.cfg["accounts"]:
            if not isinstance(row, dict):
                continue
            if str(row.get("name", "")).strip() != account_name:
                continue
            row["oauth2_access_token"] = access_token
            if refresh_token:
                row["oauth2_refresh_token"] = refresh_token
            row["oauth2_access_token_expires_at"] = int(expires_at)
            updated = True
            break
        if not updated:
            return
        try:
            with self.cfg_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(self.cfg, f, sort_keys=False)
        except Exception as exc:
            LOG.warning("Failed to persist OAuth2 token for account %s: %s", account_name, exc)

    def _refresh_oauth2_access_token(self, account: AccountConfig) -> tuple[str, int, str]:
        token_url = self._token_url_for_account(account)
        scope = self._scope_for_account(account)
        if not token_url:
            raise ValueError("oauth2_token_url is required (or set oauth2_provider to microsoft/google)")
        if not account.oauth2_client_id:
            raise ValueError("oauth2_client_id is required for OAuth2")
        if not account.oauth2_refresh_token:
            raise ValueError("oauth2_refresh_token is required for OAuth2")

        form: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": account.oauth2_refresh_token,
            "client_id": account.oauth2_client_id,
        }
        if account.oauth2_client_secret:
            form["client_secret"] = account.oauth2_client_secret
        if scope:
            form["scope"] = scope

        body = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            token_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))

        if not isinstance(payload, dict):
            raise ValueError("OAuth2 token endpoint returned non-JSON payload")

        access_token = str(payload.get("access_token", "")).strip()
        if not access_token:
            err = str(payload.get("error_description", payload.get("error", "missing access_token")))
            raise ValueError(f"OAuth2 token refresh failed: {err}")

        expires_in = self._to_int(payload.get("expires_in", 3600), default=3600)
        expires_at = int(time.time()) + max(60, expires_in)
        refresh_token = str(payload.get("refresh_token", "")).strip() or account.oauth2_refresh_token
        return access_token, expires_at, refresh_token

    def _resolve_oauth2_access_token(self, account: AccountConfig) -> str:
        # Environment override allows secret injection without writing tokens to disk.
        env_key = f"EMAIL_INTAKE_OAUTH2_ACCESS_TOKEN_{self._safe_name(account.name).upper()}"
        env_token = os.environ.get(env_key, "").strip()
        if env_token:
            return env_token

        if account.oauth2_access_token and self._oauth2_valid_for_seconds(account) > 0:
            return account.oauth2_access_token

        access_token, expires_at, refresh_token = self._refresh_oauth2_access_token(account)
        account.oauth2_access_token = access_token
        account.oauth2_access_token_expires_at = expires_at
        account.oauth2_refresh_token = refresh_token
        self._persist_account_tokens(account.name, access_token, refresh_token, expires_at)
        return access_token

    def _imap_authenticate(self, client: imaplib.IMAP4, account: AccountConfig) -> None:
        auth_mode = (account.auth_mode or "password").strip().lower()
        if auth_mode == "oauth2":
            token = self._resolve_oauth2_access_token(account)
            xoauth = f"user={account.username}\x01auth=Bearer {token}\x01\x01"
            typ, _ = client.authenticate("XOAUTH2", lambda _arg: xoauth.encode("utf-8"))
            if typ != "OK":
                raise RuntimeError("XOAUTH2 authentication failed")
            return

        if not account.password:
            raise ValueError("Missing password for password auth_mode")
        client.login(account.username, account.password)

    def _load_cursors(self) -> dict[str, int]:
        if not self.cursors_path.exists():
            return {}
        try:
            data = json.loads(self.cursors_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out: dict[str, int] = {}
                for k, v in data.items():
                    try:
                        out[str(k)] = int(v)
                    except (TypeError, ValueError):
                        continue
                return out
        except Exception:
            pass
        return {}

    def _save_cursors(self) -> None:
        self.cursors_path.write_text(json.dumps(self.cursors, ensure_ascii=True, indent=2), encoding="utf-8")

    def _cursor_key(self, account: AccountConfig) -> str:
        return f"{account.name}:{account.mailbox}"

    def _set_cursor(self, account: AccountConfig, uid: int) -> None:
        key = self._cursor_key(account)
        current = int(self.cursors.get(key, 0))
        if uid > current:
            self.cursors[key] = int(uid)

    def _get_cursor(self, account: AccountConfig) -> int:
        return int(self.cursors.get(self._cursor_key(account), 0))

    def run_forever(self) -> None:
        if not self.enabled:
            LOG.warning("Email intake is disabled in config; exiting")
            return

        LOG.info("Email intake started with %d account(s)", len(self.accounts))
        while True:
            self.run_once()
            time.sleep(max(10, self.poll_seconds))

    def run_once(self) -> None:
        if not self.enabled:
            LOG.info("Email intake disabled; run_once skipped")
            return

        processed_total = 0
        for account in self.accounts:
            if not account.enabled:
                continue
            processed_total += self._poll_account(account)

        if self.entity_enabled and self.auto_remove_delivered_enabled:
            self._prune_delivered_shipments()

        self._save_cursors()
        self._write_exports()
        LOG.info("Poll cycle complete; processed_messages=%d", processed_total)

    def _prune_delivered_shipments(self) -> None:
        cutoff_seconds = max(1, int(self.auto_remove_delivered_after_days)) * 86400
        now = datetime.now(timezone.utc)
        rows = self.store.query_current(entity_type="shipment", limit=20000)
        removed = 0

        for row in rows:
            payload = row.get("payload", {}) if isinstance(row.get("payload"), dict) else {}
            status = str(payload.get("status", "")).strip().lower()
            if status != "delivered":
                continue

            last_seen_at = str(row.get("last_seen_at", "")).strip()
            dt = self._parse_iso_utc(last_seen_at)
            if dt is None:
                continue

            age_seconds = (now - dt).total_seconds()
            if age_seconds < cutoff_seconds:
                continue

            entity_key = str(row.get("entity_key", "")).strip()
            if not entity_key:
                continue

            if self.store.suppress_entity(
                "shipment",
                entity_key,
                reason=f"delivered_older_than_{self.auto_remove_delivered_after_days}d",
                source_ref="retention:auto",
            ):
                removed += 1

        if removed:
            LOG.info("Pruned delivered shipments older than %d day(s): removed=%d", self.auto_remove_delivered_after_days, removed)

    @staticmethod
    def _parse_iso_utc(raw: str) -> datetime | None:
        value = str(raw or "").strip()
        if not value:
            return None
        try:
            if value.endswith("Z"):
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _poll_account(self, account: AccountConfig) -> int:
        if not account.host or not account.username:
            LOG.warning("Account %s missing required host/username; skipped", account.name)
            return 0
        if (account.auth_mode or "password").strip().lower() == "oauth2":
            if not account.oauth2_refresh_token:
                LOG.warning("Account %s missing oauth2_refresh_token; skipped", account.name)
                return 0
        elif not account.password:
            LOG.warning("Account %s missing password; skipped", account.name)
            return 0

        try:
            client = imaplib.IMAP4_SSL(account.host, account.port) if account.use_ssl else imaplib.IMAP4(account.host, account.port)
            self._imap_authenticate(client, account)
            client.select(account.mailbox)

            cursor = self._get_cursor(account)
            criteria = f"UID {cursor + 1}:*"
            status, data = client.uid("SEARCH", None, criteria)
            if status != "OK":
                LOG.warning("IMAP search failed for %s", account.name)
                client.logout()
                return 0

            uid_tokens = [tok for tok in (data[0].split() if data and data[0] else [])]
            uids: list[int] = []
            for tok in uid_tokens:
                try:
                    uids.append(int(tok))
                except (TypeError, ValueError):
                    continue

            if not uids:
                client.logout()
                return 0

            processed = 0
            for uid in sorted(uids)[: account.max_messages_per_poll]:
                processed += self._process_uid(client, account, uid)

            client.logout()
            return processed
        except Exception as exc:
            LOG.exception("Account poll failed for %s: %s", account.name, exc)
            return 0

    def _process_uid(self, client: imaplib.IMAP4, account: AccountConfig, uid: int) -> int:
        fetch_status, payload = client.uid("FETCH", str(uid), "(RFC822)")
        if fetch_status != "OK" or not payload:
            return 0

        msg_bytes = b""
        for part in payload:
            if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
                msg_bytes = bytes(part[1])
                break
        if not msg_bytes:
            return 0

        msg = email.message_from_bytes(msg_bytes, policy=default)
        message_id = str(msg.get("Message-ID", "")).strip() or f"uid:{uid}"
        source_ref = f"{account.name}:{account.mailbox}:{uid}:{message_id}"

        if self.store.is_seen(account.name, account.mailbox, uid):
            self._set_cursor(account, uid)
            return 0

        subject = str(msg.get("Subject", "")).strip()
        sender = str(msg.get("From", "")).strip()
        received_at = self._normalized_received_at(str(msg.get("Date", "")).strip())

        body_text = self._extract_body_text(msg)
        attachments = self._extract_attachments(msg, account.name, uid)

        if self.entity_enabled:
            self._extract_and_store_entities(source_ref, subject, sender, body_text, received_at)

        if self.write_message_metadata:
            meta_path = self.messages_dir / f"{self._safe_name(account.name)}_{uid}.json"
            metadata = {
                "account": account.name,
                "mailbox": account.mailbox,
                "uid": uid,
                "message_id": message_id,
                "subject": subject,
                "from": sender,
                "received_at": received_at,
                "saved_attachments": attachments,
            }
            meta_path.write_text(json.dumps(metadata, ensure_ascii=True, indent=2), encoding="utf-8")

        if account.mark_seen:
            try:
                client.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
            except Exception:
                LOG.debug("Unable to mark uid=%s as seen for account=%s", uid, account.name)

        self.store.mark_seen(account.name, account.mailbox, uid, message_id)
        self._set_cursor(account, uid)
        return 1

    def _normalized_received_at(self, raw_date: str) -> str:
        # Store mail times as UTC ISO8601 so both DB and UI sorting are stable.
        raw = str(raw_date or "").strip()
        if not raw:
            return utc_now_iso()
        try:
            dt = parsedate_to_datetime(raw)
            if dt is None:
                return utc_now_iso()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except Exception:
            return utc_now_iso()

    def _extract_body_text(self, msg: Message) -> str:
        parts: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                content_type = str(part.get_content_type() or "").lower()
                if content_type != "text/plain":
                    continue
                disp = str(part.get_content_disposition() or "").lower()
                if disp == "attachment":
                    continue
                try:
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="ignore"))
                except Exception:
                    continue
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="ignore"))
            except Exception:
                pass
        joined = "\n".join(parts)
        return re.sub(r"\s+", " ", joined).strip()

    def _extract_attachments(self, msg: Message, account_name: str, uid: int) -> list[str]:
        saved_paths: list[str] = []
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

        for part in msg.walk():
            filename = part.get_filename()
            if not filename:
                continue
            suffix = Path(filename).suffix.lower()
            if self.allowed_extensions and suffix not in self.allowed_extensions:
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            safe_name = self._safe_name(Path(filename).name)
            out_name = f"email_{self._safe_name(account_name)}_{uid}_{ts}_{safe_name}"

            if suffix == ".pdf" and self.save_pdf_to_scanner_inbox:
                out_path = self._account_scanner_drop_dir(account) / out_name
            elif self.save_non_pdf_to_state:
                out_path = self.attachments_dir / out_name
            else:
                continue

            out_path.write_bytes(payload)
            saved_paths.append(str(out_path))

        return saved_paths

    def _extract_and_store_entities(
        self,
        source_ref: str,
        subject: str,
        sender: str,
        body_text: str,
        observed_at: str,
    ) -> None:
        hay = f"{subject}\n{body_text}"

        statuses = self._detect_statuses(hay) if self.detect_status_keywords else []

        if self.detect_tracking:
            seen_track: set[str] = set()
            for key, carrier, service in self._find_tracking_candidates(hay):
                if not key or key in seen_track:
                    continue
                seen_track.add(key)
                if self.store.is_suppressed("shipment", key):
                    continue

                tracking_status = statuses[0] if statuses else "unknown"
                terminal = tracking_status in self.tracking_terminal_statuses
                details = self._extract_tracking_details(hay)
                postcode = self.tracking_postcodes.get(carrier, self.tracking_postcodes.get("default", ""))
                tracking_url = self._build_tracking_url(service, key, postcode)

                payload = {
                    "tracking_number": key,
                    "carrier": carrier,
                    "status": tracking_status,
                    "status_signals": statuses,
                    "sender": sender,
                    "subject": subject,
                    "tracking_url": tracking_url,
                    "tracking_postcode": postcode,
                    "tracking_requires_postcode": bool(service.get("requires_postcode", False)),
                    "eta_date": details.get("eta_date", ""),
                    "last_location": details.get("last_location", ""),
                    "active_monitoring": not terminal,
                    "known_service": bool(service.get("known_service", True)),
                }

                if self._is_invalid_tracking(hay, statuses):
                    self.store.suppress_entity("shipment", key, reason="invalid_tracking_number", source_ref=source_ref)
                    continue

                self.store.upsert_observation(
                    EntityObservation(
                        entity_type="shipment",
                        entity_key=key,
                        payload=payload,
                        source_ref=source_ref,
                        observed_at=observed_at,
                    )
                )

        if self.detect_policy_numbers:
            seen_policy: set[str] = set()
            for patt in POLICY_PATTERNS:
                for match in patt.finditer(hay):
                    raw = match.group(1) if match.lastindex else match.group(0)
                    key = str(raw).strip().upper()
                    if not key or key in seen_policy:
                        continue
                    if not self._is_likely_policy_number(hay, match.start(), match.end(), key):
                        continue
                    seen_policy.add(key)
                    provider = self._extract_policy_provider(hay, sender)
                    expires_on = self._extract_policy_expiry(hay)
                    policy_type = self._detect_policy_type(hay)
                    policy_live = self._is_policy_live(statuses, expires_on)
                    expiry_alert = self._policy_expiry_alert(expires_on)
                    payload = {
                        "policy_number": key,
                        "status": statuses[0] if statuses else "active_or_unknown",
                        "status_signals": statuses,
                        "sender": sender,
                        "subject": subject,
                        "provider": provider,
                        "policy_type": policy_type,
                        "expires_on": expires_on,
                        "live": policy_live,
                        "expiry_alert": expiry_alert,
                    }
                    self.store.upsert_observation(
                        EntityObservation(
                            entity_type="policy",
                            entity_key=key,
                            payload=payload,
                            source_ref=source_ref,
                            observed_at=observed_at,
                        )
                    )

    def _detect_statuses(self, text: str) -> list[str]:
        low = text.lower()
        found: list[str] = []
        for label, keywords in STATUS_KEYWORDS.items():
            for kw in keywords:
                if kw in low:
                    found.append(label)
                    break
        return found

    def _build_tracking_services(self, additional_services: list[Any]) -> dict[str, dict[str, Any]]:
        services: dict[str, dict[str, Any]] = {}
        for row in DEFAULT_TRACKING_SERVICES:
            name = str(row.get("name", "")).strip().lower()
            if not name:
                continue
            services[name] = {
                "name": name,
                "hints": [str(x).strip().lower() for x in (row.get("hints", []) or []) if str(x).strip()],
                "patterns": [re.compile(str(p), re.IGNORECASE) for p in (row.get("patterns", []) or []) if str(p).strip()],
                "tracking_url": str(row.get("tracking_url", "")).strip(),
                "requires_postcode": bool(row.get("requires_postcode", False)),
                "known_service": True,
            }

        for raw in additional_services:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip().lower()
            if not name:
                continue
            patterns: list[re.Pattern[str]] = []
            for item in (raw.get("patterns", []) or []):
                txt = str(item).strip()
                if not txt:
                    continue
                try:
                    patterns.append(re.compile(txt, re.IGNORECASE))
                except re.error:
                    continue

            services[name] = {
                "name": name,
                "hints": [str(x).strip().lower() for x in (raw.get("hints", []) or []) if str(x).strip()],
                "patterns": patterns,
                "tracking_url": str(raw.get("tracking_url", "")).strip(),
                "requires_postcode": bool(raw.get("requires_postcode", False)),
                "known_service": bool(raw.get("known_service", False)),
            }
        return services

    def _find_tracking_candidates(self, text: str) -> list[tuple[str, str, dict[str, Any]]]:
        hay = str(text or "")
        low = hay.lower()
        out: list[tuple[str, str, dict[str, Any]]] = []

        for name, service in self.tracking_services.items():
            has_hint = False
            hints = service.get("hints", []) if isinstance(service.get("hints"), list) else []
            if hints:
                has_hint = any(h in low for h in hints)
            for patt in (service.get("patterns", []) or []):
                for match in patt.finditer(hay):
                    value = match.group(1) if match.lastindex else match.group(0)
                    key = str(value).strip().upper()
                    if not key:
                        continue
                    if not self._is_likely_tracking_number(hay, match.start(), match.end(), key, hints, has_hint):
                        continue
                    out.append((key, name, service))

        if out or self.known_shippers_only:
            return out

        fallback_carrier = self._detect_carrier_from_hints(low)
        fallback_service = {
            "name": fallback_carrier,
            "tracking_url": "",
            "requires_postcode": False,
            "known_service": False,
        }
        for patt in TRACKING_PATTERNS:
            for match in patt.finditer(hay):
                key = str(match.group(0)).strip().upper()
                if not key:
                    continue
                if not self._is_likely_tracking_number(hay, match.start(), match.end(), key, [], False):
                    continue
                out.append((key, fallback_carrier, fallback_service))
        return out

    @staticmethod
    def _context_window(text: str, start: int, end: int, pad: int = 64) -> str:
        s = max(0, int(start) - max(0, pad))
        e = min(len(text), int(end) + max(0, pad))
        return text[s:e]

    def _is_likely_tracking_number(
        self,
        text: str,
        start: int,
        end: int,
        key: str,
        hints: list[str],
        has_global_hint: bool,
    ) -> bool:
        candidate = str(key).strip().upper()
        if len(candidate) < 8:
            return False
        if candidate.isalpha():
            return False
        if candidate.isdigit() and len(candidate) < 10:
            return False
        if candidate.isdigit() and len(set(candidate)) <= 2:
            return False
        if re.fullmatch(r"20[0-9]{6}", candidate):
            return False

        around = self._context_window(text, start, end).lower()
        has_local_hint = any(str(h).strip().lower() in around for h in hints if str(h).strip())
        has_context = any(k in around for k in TRACKING_CONTEXT_KEYWORDS)

        # Numeric references are noisy; keep them only with nearby shipping context.
        if candidate.isdigit() and not (has_context or has_local_hint or has_global_hint):
            return False

        # For alphanumeric IDs, still require some signal when no carrier hints exist.
        if not candidate.isdigit() and not (has_context or has_local_hint or has_global_hint):
            return False

        return True

    def _is_likely_policy_number(self, text: str, start: int, end: int, key: str) -> bool:
        candidate = str(key).strip().upper()
        if len(candidate) < 6:
            return False
        if re.fullmatch(r"[A-Z]{6,}", candidate):
            return False
        if candidate.isdigit() and len(candidate) < 8:
            return False
        if candidate.isdigit() and len(set(candidate)) <= 2:
            return False
        if not any(ch.isdigit() for ch in candidate):
            return False

        around = self._context_window(text, start, end).lower()
        if not any(k in around for k in POLICY_CONTEXT_KEYWORDS):
            return False

        return True

    def _detect_carrier_from_hints(self, low_text: str) -> str:
        for name, service in self.tracking_services.items():
            hints = service.get("hints", []) if isinstance(service.get("hints"), list) else []
            for hint in hints:
                if hint and hint in low_text:
                    return name
        return "unknown"

    def _build_tracking_url(self, service: dict[str, Any], tracking_number: str, postcode: str) -> str:
        template = str(service.get("tracking_url", "")).strip()
        if not template:
            return ""
        return template.format(
            tracking_number=urllib.parse.quote(str(tracking_number), safe=""),
            postcode=urllib.parse.quote(str(postcode), safe=""),
        )

    def _extract_tracking_details(self, text: str) -> dict[str, str]:
        details = {
            "eta_date": self._extract_date_value(text, [
                r"\b(?:expected delivery|estimated delivery|due delivery|arrives by)\s*[:\-]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4}|[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})\b",
            ]),
            "last_location": "",
        }
        loc_patterns = [
            re.compile(r"\b(?:arrived at|departed from|processed at|at depot|in)\s+([A-Za-z0-9][A-Za-z0-9 .,'\-/]{2,60})\b", re.IGNORECASE),
        ]
        for patt in loc_patterns:
            m = patt.search(text)
            if m:
                details["last_location"] = str(m.group(1)).strip()
                break
        return details

    def _is_invalid_tracking(self, text: str, statuses: list[str]) -> bool:
        if "invalid_tracking" in statuses:
            return True
        low = str(text or "").lower()
        return "no longer valid" in low and "tracking" in low

    def _extract_policy_provider(self, text: str, sender: str) -> str:
        for patt in POLICY_PROVIDER_PATTERNS:
            m = patt.search(text)
            if m:
                val = str(m.group(1)).strip(" .-_")
                if val:
                    return val
        sender_email = sender
        if "<" in sender and ">" in sender:
            sender_email = sender.split("<", 1)[1].split(">", 1)[0]
        if "@" in sender_email:
            domain = sender_email.split("@", 1)[1].strip().lower()
            parts = [p for p in domain.split(".") if p and p not in {"com", "co", "uk", "org", "net", "io"}]
            if parts:
                return parts[0].replace("-", " ").title()
        return "unknown"

    def _extract_policy_expiry(self, text: str) -> str:
        return self._extract_date_value(text, [p.pattern for p in POLICY_EXPIRY_PATTERNS])

    def _extract_date_value(self, text: str, patterns: list[str]) -> str:
        for raw in patterns:
            patt = re.compile(raw, re.IGNORECASE)
            m = patt.search(text)
            if not m:
                continue
            val = str(m.group(1)).strip()
            parsed = self._parse_date_text(val)
            if parsed:
                return parsed
        return ""

    def _parse_date_text(self, raw: str) -> str:
        value = str(raw or "").strip()
        fmts = ["%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d %b %Y", "%d %B %Y"]
        for fmt in fmts:
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                continue
        return ""

    def _detect_policy_type(self, text: str) -> str:
        low = text.lower()
        for policy_type, keywords in POLICY_TYPE_KEYWORDS.items():
            for kw in keywords:
                if kw in low:
                    return policy_type
        return "unknown"

    def _is_policy_live(self, statuses: list[str], expires_on: str) -> bool:
        if "cancelled" in statuses or "expired" in statuses:
            return False
        if expires_on:
            try:
                return datetime.utcnow().date() <= datetime.strptime(expires_on, "%Y-%m-%d").date()
            except ValueError:
                return True
        return True

    def _policy_expiry_alert(self, expires_on: str) -> str:
        if not expires_on:
            return "unknown"
        try:
            exp = datetime.strptime(expires_on, "%Y-%m-%d").date()
            today = datetime.utcnow().date()
            days = (exp - today).days
            if days < 0:
                return "expired"
            if days <= self.policy_expiry_alert_days:
                return "expiring_soon"
            return "ok"
        except ValueError:
            return "unknown"

    def _write_exports(self) -> None:
        current = self.store.export_current()
        recent_events = self.store.export_recent_events(limit=self.max_recent_events_export)
        self.current_export_path.write_text(json.dumps(current, ensure_ascii=True, indent=2), encoding="utf-8")
        self.events_export_path.write_text(json.dumps(recent_events, ensure_ascii=True, indent=2), encoding="utf-8")

    @staticmethod
    def _safe_name(value: str) -> str:
        clean = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
        clean = re.sub(r"_+", "_", clean).strip("_")
        return clean or "item"

    @staticmethod
    def _safe_rel_subfolder(value: str) -> str:
        raw = str(value or "").replace("\\", "/")
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        safe_parts = [re.sub(r"[^A-Za-z0-9._-]+", "_", p).strip("_") for p in parts]
        safe_parts = [p for p in safe_parts if p and p not in {".", ".."}]
        return "/".join(safe_parts)

    def _account_scanner_drop_dir(self, account: AccountConfig) -> Path:
        root = self.scanner_inbox_dir
        user = self._safe_name(account.target_inbox_user) if account.target_inbox_user else ""
        subfolder = self._safe_rel_subfolder(account.target_subfolder)

        if user:
            root = root / user
        if subfolder:
            root = root / subfolder
        root.mkdir(parents=True, exist_ok=True)
        return root


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return data


def setup_logging(level_name: str) -> None:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def test_account_connection(account: AccountConfig, service: EmailIntakeService | None = None) -> tuple[bool, str]:
    if not account.host or not account.username:
        return False, "Missing host/username"

    try:
        client = imaplib.IMAP4_SSL(account.host, account.port) if account.use_ssl else imaplib.IMAP4(account.host, account.port)
        auth_mode = (account.auth_mode or "password").strip().lower()
        if auth_mode == "oauth2":
            if service is not None:
                token = service._resolve_oauth2_access_token(account)
            else:
                token = account.oauth2_access_token
            if not token:
                return False, "Missing OAuth2 access token"
            xoauth = f"user={account.username}\x01auth=Bearer {token}\x01\x01"
            typ, _ = client.authenticate("XOAUTH2", lambda _arg: xoauth.encode("utf-8"))
            if typ != "OK":
                return False, "XOAUTH2 authentication failed"
        else:
            if not account.password:
                return False, "Missing password"
            client.login(account.username, account.password)
        status, _ = client.select(account.mailbox)
        client.logout()
        if status != "OK":
            return False, f"Connected but mailbox select failed: {account.mailbox}"
        return True, f"Connected via {auth_mode} and selected mailbox {account.mailbox}"
    except Exception as exc:
        return False, f"Connection failed: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Email intake bolt-on service")
    parser.add_argument("--config", required=True, help="Path to bolt-on config YAML")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    parser.add_argument("--test-account", default="", help="Test a configured account by name and exit")
    args = parser.parse_args()

    cfg_path = Path(args.config).expanduser().resolve()
    cfg = load_config(cfg_path)
    setup_logging(str(cfg.get("log_level", "INFO")))

    svc = EmailIntakeService(cfg, cfg_path)

    if args.test_account:
        selected = [acc for acc in svc.accounts if acc.name == args.test_account]
        if not selected:
            LOG.error("Account not found in config: %s", args.test_account)
            return 2
        ok, message = test_account_connection(selected[0], service=svc)
        if ok:
            LOG.info("Account test OK: %s", message)
            return 0
        LOG.error("Account test failed: %s", message)
        return 1

    if args.once:
        svc.run_once()
        return 0

    svc.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

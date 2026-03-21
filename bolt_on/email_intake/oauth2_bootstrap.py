#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.parse
import urllib.request


def _microsoft_defaults(tenant: str) -> tuple[str, str]:
    token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    scope = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
    return token_url, scope


def _google_defaults() -> tuple[str, str]:
    return "https://oauth2.googleapis.com/token", "https://mail.google.com/"


def _authorize_url(provider: str, tenant: str, client_id: str, redirect_uri: str, scope: str, state: str) -> str:
    if provider == "microsoft":
        base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
    elif provider == "google":
        base = "https://accounts.google.com/o/oauth2/v2/auth"
    else:
        raise ValueError("Unsupported provider")

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": scope,
        "state": state,
        "prompt": "consent",
        "access_type": "offline",
    }
    return f"{base}?{urllib.parse.urlencode(params)}"


def _exchange_code(token_url: str, client_id: str, client_secret: str, redirect_uri: str, code: str, scope: str) -> dict:
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    if scope:
        payload["scope"] = scope

    req = urllib.request.Request(
        token_url,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Token endpoint did not return a JSON object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="OAuth2 bootstrap helper for email intake")
    parser.add_argument("--provider", choices=["microsoft", "google"], required=True)
    parser.add_argument("--tenant", default="common", help="Microsoft tenant (default: common)")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", default="")
    parser.add_argument("--redirect-uri", default="http://localhost")
    parser.add_argument("--scope", default="", help="Override default scope")
    parser.add_argument("--code", default="", help="Authorization code to exchange")
    args = parser.parse_args()

    if args.provider == "microsoft":
        token_url, default_scope = _microsoft_defaults(args.tenant)
    else:
        token_url, default_scope = _google_defaults()

    scope = args.scope.strip() or default_scope
    state = secrets.token_urlsafe(20)

    if not args.code:
        auth_url = _authorize_url(
            provider=args.provider,
            tenant=args.tenant,
            client_id=args.client_id,
            redirect_uri=args.redirect_uri,
            scope=scope,
            state=state,
        )
        print("Open this URL in a browser, sign in, then copy the 'code' query parameter:")
        print(auth_url)
        print()
        print("Then run this tool again with --code <value> to exchange it for tokens.")
        return 0

    try:
        data = _exchange_code(
            token_url=token_url,
            client_id=args.client_id,
            client_secret=args.client_secret,
            redirect_uri=args.redirect_uri,
            code=args.code,
            scope=scope,
        )
    except Exception as exc:
        print(f"Token exchange failed: {exc}", file=sys.stderr)
        return 1

    access_token = str(data.get("access_token", "")).strip()
    refresh_token = str(data.get("refresh_token", "")).strip()
    expires_in = int(data.get("expires_in", 0) or 0)

    if not access_token:
        err = str(data.get("error_description", data.get("error", "missing access_token")))
        print(f"Token exchange failed: {err}", file=sys.stderr)
        return 1

    out = {
        "oauth2_provider": args.provider,
        "oauth2_token_url": token_url,
        "oauth2_scope": scope,
        "oauth2_client_id": args.client_id,
        "oauth2_client_secret": args.client_secret,
        "oauth2_refresh_token": refresh_token,
        "oauth2_access_token": access_token,
        "oauth2_access_token_expires_at": 0,
        "expires_in": expires_in,
    }
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

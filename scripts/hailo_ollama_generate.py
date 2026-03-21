#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_HOST = str(os.getenv("OLLAMA_HOST", "http://127.0.0.1:8000")).strip().rstrip("/")
DEFAULT_TIMEOUT = float(os.getenv("HAILO_OLLAMA_TIMEOUT", "600"))


def _usage() -> int:
    prog = os.path.basename(sys.argv[0] or "hailo_ollama_generate.py")
    print(f"Usage: {prog} MODEL", file=sys.stderr)
    return 2


def main() -> int:
    if len(sys.argv) != 2:
        return _usage()

    model = sys.argv[1].strip()
    if not model:
        return _usage()

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Prompt is required on stdin.", file=sys.stderr)
        return 2

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }

    request = Request(
        f"{DEFAULT_HOST}/api/generate",
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        print(f"Hailo-Ollama HTTP {exc.code}: {error_body or exc.reason}", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Cannot reach Hailo-Ollama at {DEFAULT_HOST}: {exc.reason}", file=sys.stderr)
        return 1

    try:
        response_payload = json.loads(raw_body)
    except json.JSONDecodeError:
        print(raw_body, file=sys.stderr)
        return 1

    response_text = response_payload.get("response")
    if not isinstance(response_text, str):
        print(raw_body, file=sys.stderr)
        return 1

    sys.stdout.write(response_text)
    if response_text and not response_text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
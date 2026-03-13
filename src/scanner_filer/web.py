from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
import yaml
from werkzeug.security import check_password_hash, generate_password_hash

from .config import AppConfig, ensure_directories, load_config
from .ocr import extract_text
from .progress_state import read_progress
from .rules import unique_path
from .splitter import split_pdf_at_starts, split_pdf_if_needed


@dataclass
class DocumentRecord:
    rel_path: str
    abs_path: Path
    bucket: str
    filename: str
    doc_type: str
    sender: str
    year: str
    size_bytes: int
    modified_iso: str
    modified_ts: float
    scanned_iso: str
    sort_ts: float
    keywords: list[str]
    suggested_type: str
    target_default_type: str
    auto_filed_recent: bool
    auto_filed_iso: str


@dataclass
class QueueRecord:
    filename: str
    location: str
    stage: str
    percent: int
    size_kb: float
    modified_iso: str


STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "your", "you", "are", "was", "were", "have", "has",
    "will", "can", "not", "all", "any", "our", "out", "per", "www", "http", "https", "com", "ltd", "pdf",
    "statement", "document", "page", "of", "to", "in", "on", "at", "by", "or", "is", "as", "be", "it", "an", "a",
}


CATEGORY_HINTS: dict[str, list[str]] = {
    "council_tax": ["council tax", "local authority", "council", "rates"],
    "bank_statement": ["bank statement", "sort code", "account number", "balance", "statement"],
    "pension": ["pension", "retirement", "annuity", "aegon"],
    "utility_bill": ["electricity", "gas", "water", "broadband", "utility"],
    "insurance": ["insurance", "policy", "premium", "claim"],
    "tax": ["tax", "hmrc", "vat", "self assessment"],
    "medical": ["nhs", "clinic", "patient", "medical", "prescription", "hospital"],
    "payslip": ["payslip", "pay period", "gross pay", "net pay"],
    "invoice": ["invoice", "amount due", "payment terms", "bill to"],
    "receipt": ["receipt", "paid", "transaction", "card payment"],
    "legal": ["agreement", "contract", "terms", "legal"],
    "personal": ["letter", "correspondence", "personal"],
}


def _extract_scan_info(filename: str, fallback_ts: float) -> tuple[float, str]:
    # Match scanner-style stamp like YYYYMMDDHHMMSS embedded in filename.
    m = re.search(r"(20\d{12})", filename)
    if not m:
        return fallback_ts, datetime.fromtimestamp(fallback_ts).strftime("%Y-%m-%d %H:%M")
    raw = m.group(1)
    try:
        dt = datetime.strptime(raw, "%Y%m%d%H%M%S")
        ts = dt.timestamp()
        return ts, dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return fallback_ts, datetime.fromtimestamp(fallback_ts).strftime("%Y-%m-%d %H:%M")


def _suggest_type(doc: DocumentRecord, allowed_types: list[str], unknown_type: str) -> str:
    if doc.bucket != "review":
        return ""

    hay = " ".join([doc.filename, doc.rel_path, " ".join(doc.keywords)]).lower()
    best_type = ""
    best_score = 0

    for t in allowed_types:
        if t == unknown_type:
            continue
        hints = CATEGORY_HINTS.get(t, []) + [t.replace("_", " ")]
        score = sum(1 for hint in hints if hint and hint in hay)
        if score > best_score:
            best_score = score
            best_type = t

    return best_type if best_score > 0 else ""


def _safe_rel(base: Path, candidate: Path) -> str:
    return str(candidate.resolve().relative_to(base.resolve()))


def _safe_join(base: Path, rel_path: str) -> Path:
    candidate = (base / rel_path).resolve()
    if base.resolve() not in candidate.parents and candidate != base.resolve():
        raise ValueError("Path traversal blocked")
    return candidate


def _scan_bucket(cfg: AppConfig, root: Path, bucket: str, rel_prefix: str = "") -> list[DocumentRecord]:
    rows: list[DocumentRecord] = []
    if not root.exists():
        return rows

    known_users = set(_load_user_inboxes(cfg).keys()) if bucket == "archive" else set()

    for p in sorted(root.rglob("*.pdf")):
        if not p.is_file():
            continue
        rel = _safe_rel(root, p)
        if rel_prefix:
            rel = str(Path(rel_prefix) / rel)
        parts = Path(rel).parts

        doc_type = "unknown"
        year = ""
        sender = ""
        if bucket == "archive":
            if len(parts) >= 5 and parts[0] in known_users:
                doc_type = parts[1]
                year = parts[2]
                sender = parts[3]
            elif len(parts) >= 4:
                doc_type = parts[0]
                year = parts[1]
                sender = parts[2]

        stat = p.stat()
        modified_ts = stat.st_mtime
        sort_ts, scanned_iso = _extract_scan_info(p.name, modified_ts)
        rows.append(
            DocumentRecord(
                rel_path=rel,
                abs_path=p,
                bucket=bucket,
                filename=p.name,
                doc_type=doc_type,
                sender=sender,
                year=year,
                size_bytes=stat.st_size,
                modified_iso=datetime.fromtimestamp(modified_ts).strftime("%Y-%m-%d %H:%M"),
                modified_ts=modified_ts,
                scanned_iso=scanned_iso,
                sort_ts=sort_ts,
                keywords=[],
                suggested_type="",
                target_default_type=doc_type,
                auto_filed_recent=False,
                auto_filed_iso="",
            )
        )

    return rows


def collect_documents(cfg: AppConfig) -> list[DocumentRecord]:
    docs: list[DocumentRecord] = []
    docs.extend(_scan_bucket(cfg, cfg.paths.archive, "archive"))
    docs.extend(_scan_bucket(cfg, cfg.paths.review, "review"))
    docs.extend(_scan_bucket(cfg, cfg.paths.rejected, "rejected"))

    inboxes = _load_user_inboxes(cfg)
    seen_roots = {
        ("archive", cfg.paths.archive.resolve()),
        ("review", cfg.paths.review.resolve()),
        ("rejected", cfg.paths.rejected.resolve()),
    }
    for username, meta in inboxes.items():
        if not _valid_username(username) or not isinstance(meta, dict):
            continue
        for bucket in ("archive", "review", "rejected"):
            raw = str(meta.get(f"{bucket}_path", "")).strip()
            if not raw:
                continue
            root = Path(raw).expanduser().resolve()
            root_key = (bucket, root)
            if root_key in seen_roots:
                continue
            seen_roots.add(root_key)
            docs.extend(_scan_bucket(cfg, root, bucket, rel_prefix=username))

    docs = [d for d in docs if d.abs_path.exists()]
    deduped: list[DocumentRecord] = []
    seen_docs: set[str] = set()
    for doc in docs:
        key = str(doc.abs_path.resolve())
        if key in seen_docs:
            continue
        seen_docs.add(key)
        deduped.append(doc)
    docs = deduped
    _attach_keywords(cfg, docs)
    _attach_recent_autofile_status(cfg, docs)
    allowed = cfg.rules.allowed_doc_types
    for d in docs:
        d.suggested_type = _suggest_type(d, allowed, cfg.rules.unknown_doc_type)
        if d.doc_type in allowed and d.doc_type != cfg.rules.unknown_doc_type:
            d.target_default_type = d.doc_type
        elif d.suggested_type:
            d.target_default_type = d.suggested_type
        else:
            first_allowed = [t for t in allowed if t != cfg.rules.unknown_doc_type]
            d.target_default_type = first_allowed[0] if first_allowed else cfg.rules.unknown_doc_type
    docs.sort(key=lambda d: d.sort_ts, reverse=True)
    return docs


def collect_queue(cfg: AppConfig) -> list[QueueRecord]:
    now_ts = datetime.now().timestamp()
    progress = read_progress(cfg).get("active", {})
    active_name = ""
    active_percent = 0
    active_stage = "processing"
    if isinstance(progress, dict):
        working = str(progress.get("working", ""))
        if working:
            active_name = Path(working).name
        active_percent = int(float(progress.get("percent", 0)))
        active_stage = str(progress.get("stage", "processing"))

    rows: list[QueueRecord] = []

    for p in sorted(cfg.paths.inbox.rglob("*.pdf")):
        stat = p.stat()
        age = max(0.0, now_ts - stat.st_mtime)
        settle_pct = 0.0
        if cfg.inbox_settle_seconds > 0:
            settle_pct = min(1.0, age / float(cfg.inbox_settle_seconds))
        pct = int(5 + settle_pct * 45)
        try:
            rel = p.relative_to(cfg.paths.inbox)
            rel_parent = str(rel.parent)
        except Exception:
            rel_parent = ""
        inbox_loc = "inbox" if rel_parent in {"", "."} else f"inbox/{rel_parent}"
        rows.append(
            QueueRecord(
                filename=p.name,
                location=inbox_loc,
                stage="waiting_for_settle",
                percent=pct,
                size_kb=round(stat.st_size / 1024.0, 1),
                modified_iso=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            )
        )

    for p in sorted(cfg.paths.processing.glob("*.pdf")):
        stat = p.stat()
        if p.name == active_name:
            pct = active_percent
            stage = active_stage
        else:
            pct = 55
            stage = "orphaned_processing"
        rows.append(
            QueueRecord(
                filename=p.name,
                location="processing",
                stage=stage,
                percent=max(1, min(99, pct)),
                size_kb=round(stat.st_size / 1024.0, 1),
                modified_iso=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            )
        )

    rows.sort(key=lambda r: r.modified_iso, reverse=True)
    return rows


def _index_path(cfg: AppConfig) -> Path:
    return cfg.paths.state / "keyword_index.json"


def _load_keyword_index(cfg: AppConfig) -> dict:
    path = _index_path(cfg)
    if not path.exists():
        return {"records": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("records", {}), dict):
                return data
    except Exception:
        pass
    return {"records": {}}


def _save_keyword_index(cfg: AppConfig, index: dict) -> None:
    path = _index_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=True)


def _manual_actions_path(cfg: AppConfig) -> Path:
    return cfg.paths.state / "manual_actions.json"


def _load_manual_actions(cfg: AppConfig) -> dict[str, float]:
    path = _manual_actions_path(cfg)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            out: dict[str, float] = {}
            for k, v in data.items():
                try:
                    out[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            return out
    except Exception:
        pass
    return {}


def _save_manual_actions(cfg: AppConfig, actions: dict[str, float]) -> None:
    path = _manual_actions_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(actions, f, ensure_ascii=True)


def _record_manual_action(cfg: AppConfig, paths: list[Path]) -> None:
    now = datetime.utcnow().timestamp()
    actions = _load_manual_actions(cfg)
    for p in paths:
        actions[str(p.resolve())] = now

    # Keep this file small and relevant (last ~5k records).
    if len(actions) > 5000:
        trimmed = sorted(actions.items(), key=lambda kv: kv[1], reverse=True)[:5000]
        actions = dict(trimmed)
    _save_manual_actions(cfg, actions)


def _load_recent_autofile_events(cfg: AppConfig, window_seconds: int = 3600) -> dict[str, float]:
    path = cfg.paths.state / "events.jsonl"
    if not path.exists():
        return {}
    now = datetime.utcnow().timestamp()
    out: dict[str, float] = {}
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                destination = str(row.get("destination", "")).strip()
                ts_raw = str(row.get("timestamp", "")).strip()
                if not destination or not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if now - ts > window_seconds:
                    continue
                parts = Path(destination).parts
                if "archive" not in parts:
                    continue
                current = out.get(destination)
                if current is None or ts > current:
                    out[destination] = ts
    except Exception:
        return {}
    return out


def _attach_recent_autofile_status(cfg: AppConfig, docs: list[DocumentRecord]) -> None:
    events = _load_recent_autofile_events(cfg, window_seconds=3600)
    if not events:
        return
    manual = _load_manual_actions(cfg)
    for doc in docs:
        key = str(doc.abs_path.resolve())
        event_ts = events.get(key)
        if event_ts is None:
            continue
        manual_ts = float(manual.get(key, 0.0))
        if manual_ts > event_ts:
            continue
        doc.auto_filed_recent = True
        doc.auto_filed_iso = datetime.fromtimestamp(event_ts).strftime("%Y-%m-%d %H:%M")


def _learn_keyword_overrides_from_manual_move(
    cfg: AppConfig,
    config_path: Path,
    src_bucket: str,
    rel_path: str,
    target_type: str,
) -> int:
    if target_type == cfg.rules.unknown_doc_type:
        return 0
    index = _load_keyword_index(cfg)
    rec = index.get("records", {}).get(f"{src_bucket}:{rel_path}", {})
    if not isinstance(rec, dict):
        return 0

    source_words = rec.get("manual_keywords") or rec.get("keywords") or []
    if not isinstance(source_words, list):
        return 0

    clean_words: list[str] = []
    for word in source_words:
        w = str(word).strip().lower()
        if len(w) < 3 or w in STOPWORDS:
            continue
        if not re.fullmatch(r"[a-z0-9._-]{3,40}", w):
            continue
        clean_words.append(w)
    if not clean_words:
        return 0

    raw = _load_raw_config(config_path)
    rules_raw = raw.setdefault("rules", {})
    overrides = rules_raw.setdefault("keyword_overrides", {})
    if not isinstance(overrides, dict):
        overrides = {}
        rules_raw["keyword_overrides"] = overrides

    added = 0
    for w in clean_words[:5]:
        if w in overrides:
            continue
        overrides[w] = target_type
        added += 1

    if added > 0:
        _save_raw_config(config_path, raw)
        cfg.rules.keyword_overrides = {
            str(k).strip().lower(): str(v).strip().lower()
            for k, v in overrides.items()
            if str(k).strip() and str(v).strip()
        }

    return added


def _derive_keywords(text: str, limit: int = 15) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9._-]{2,}", text.lower())
    freq: dict[str, int] = {}
    for token in tokens:
        if token in STOPWORDS or token.isdigit() or len(token) < 3:
            continue
        freq[token] = freq.get(token, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [k for k, _ in ranked[:limit]]


def _parse_keyword_input(raw: str) -> list[str]:
    parts = [p.strip().lower() for p in raw.split(",")]
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        token = re.sub(r"\s+", " ", part).strip(" ,")
        if not token or token in seen:
            continue
        seen.add(token)
        cleaned.append(token)
    return cleaned


def _attach_keywords(cfg: AppConfig, docs: list[DocumentRecord]) -> None:
    index = _load_keyword_index(cfg)
    records: dict = index.get("records", {})
    dirty = False

    for doc in docs:
        if not doc.abs_path.exists():
            continue
        key = f"{doc.bucket}:{doc.rel_path}"
        stat = doc.abs_path.stat()
        sig = f"{stat.st_mtime_ns}:{stat.st_size}"
        cached = records.get(key)

        if isinstance(cached, dict) and isinstance(cached.get("manual_keywords"), list):
            doc.keywords = [str(k) for k in cached.get("manual_keywords", [])]
            continue

        if isinstance(cached, dict) and cached.get("sig") == sig and isinstance(cached.get("keywords"), list):
            doc.keywords = [str(k) for k in cached.get("keywords", [])]
            continue

        text = extract_text(doc.abs_path, max_chars=2200)
        keywords = _derive_keywords(text)
        doc.keywords = keywords
        records[key] = {"sig": sig, "keywords": keywords}
        dirty = True

    if dirty:
        index["records"] = records
        _save_keyword_index(cfg, index)


def _matches(doc: DocumentRecord, query: str, bucket: str) -> bool:
    if bucket != "all" and doc.bucket != bucket:
        return False
    if not query:
        return True
    q = query.lower().strip()
    hay = " ".join(
        [
            doc.filename,
            doc.rel_path,
            doc.bucket,
            doc.doc_type,
            doc.sender,
            doc.year,
            " ".join(doc.keywords),
        ]
    ).lower()
    return q in hay


def _roots(cfg: AppConfig) -> dict[str, Path]:
    return {
        "archive": cfg.paths.archive,
        "review": cfg.paths.review,
        "rejected": cfg.paths.rejected,
    }


def _bucket_root_for_user(cfg: AppConfig, bucket: str, username: str | None = None) -> Path:
    roots = _roots(cfg)
    base = roots[bucket]
    if not username:
        return base
    inboxes = _load_user_inboxes(cfg)
    entry = inboxes.get(username, {}) if isinstance(inboxes, dict) else {}
    if not isinstance(entry, dict):
        return base
    raw = str(entry.get(f"{bucket}_path", "")).strip()
    if not raw:
        return base
    return Path(raw).expanduser().resolve()


def _resolve_doc_source(cfg: AppConfig, src_bucket: str, rel_path: str) -> tuple[Path, str, str | None]:
    roots = _roots(cfg)
    src_root = roots[src_bucket]
    rel_local = rel_path
    src_user: str | None = None

    parts = Path(rel_path).parts
    if parts:
        candidate = parts[0]
        inboxes = _load_user_inboxes(cfg)
        if candidate in inboxes:
            src_user = candidate
            src_root = _bucket_root_for_user(cfg, src_bucket, src_user)
            rel_local = str(Path(*parts[1:])) if len(parts) > 1 else ""

    return src_root, rel_local, src_user


def _ensure_unique_destination(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stem = dst.stem
    suffix = dst.suffix
    counter = 1
    while True:
        candidate = dst.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _sort_documents(docs: list[DocumentRecord], sort_by: str, sort_dir: str) -> list[DocumentRecord]:
    key_map = {
        "received": lambda d: d.sort_ts,
        "processed": lambda d: d.modified_ts,
        "location": lambda d: f"{d.bucket}/{d.rel_path}".lower(),
        "type": lambda d: d.doc_type.lower(),
        "sender": lambda d: d.sender.lower(),
        "size": lambda d: d.size_bytes,
        "name": lambda d: d.filename.lower(),
    }
    key_func = key_map.get(sort_by, key_map["received"])
    reverse = sort_dir != "asc"
    return sorted(docs, key=key_func, reverse=reverse)


def _build_tree_roots(docs: list[DocumentRecord], cfg: AppConfig) -> list[dict]:
    roots: dict[str, dict] = {}
    known_users = {str(name).strip().lower() for name in _load_user_inboxes(cfg).keys() if str(name).strip()}

    for doc in docs:
        root = roots.setdefault(
            doc.bucket,
            {
                "name": doc.bucket,
                "bucket": doc.bucket,
                "is_file": False,
                "rel_path": "",
                "children": [],
                "_map": {},
            },
        )

        parts = list(Path(doc.rel_path).parts)
        cur = root
        for idx, part in enumerate(parts):
            is_last = idx == len(parts) - 1
            node_key = ("f" if is_last else "d", part)
            child_map = cur.setdefault("_map", {})
            if node_key not in child_map:
                rel_path = str(Path(*parts[: idx + 1]))
                child_map[node_key] = {
                    "name": part,
                    "bucket": doc.bucket,
                    "is_file": is_last,
                    "rel_path": rel_path,
                    "children": [],
                    "_map": {},
                }
            cur = child_map[node_key]

    def finalize(node: dict) -> dict:
        child_map = node.pop("_map", {})
        children = [finalize(child) for child in child_map.values()]

        def _child_key(c: dict) -> tuple[int, int, str]:
            # Files always come after directories.
            is_file_rank = 1 if c.get("is_file", False) else 0

            # For direct children under bucket roots, put configured user folders first.
            user_rank = 1
            if not c.get("is_file", False) and node.get("rel_path", "") == "":
                if str(c.get("name", "")).strip().lower() in known_users:
                    user_rank = 0

            return (is_file_rank, user_rank, str(c.get("name", "")).lower())

        children.sort(key=_child_key)
        node["children"] = children
        return node

    ordered_buckets = ["archive", "review", "rejected"]
    result: list[dict] = []
    for bucket in ordered_buckets:
        if bucket in roots:
            result.append(finalize(roots[bucket]))
    for bucket, node in roots.items():
        if bucket not in ordered_buckets:
            result.append(finalize(node))
    return result


def _infer_inbox_user_from_rel(cfg: AppConfig, rel_path: str) -> str | None:
    parts = Path(rel_path).parts
    if not parts:
        return None
    candidate = parts[0]
    inboxes = _load_user_inboxes(cfg)
    if candidate in inboxes:
        return candidate
    return None


def _requeue_parts_to_inbox(cfg: AppConfig, parts: list[Path], inbox_user: str | None = None) -> list[Path]:
    moved: list[Path] = []
    inbox_root = cfg.paths.inbox
    if inbox_user:
        inbox_root = inbox_root / inbox_user
    inbox_root.mkdir(parents=True, exist_ok=True)
    for part in parts:
        dst = unique_path(inbox_root / part.name)
        shutil.move(str(part), str(dst))
        moved.append(dst)
    return moved


def _load_raw_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError("Invalid config")
    return raw


def _save_raw_config(config_path: Path, raw: dict) -> None:
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False)


def _redirect_index_with_context(msg: str, next_query: str = ""):
    params: dict[str, str] = {"msg": msg}
    if next_query:
        parsed = parse_qs(next_query, keep_blank_values=False)
        for key in ("q", "bucket", "sort_by", "sort_dir"):
            val = parsed.get(key, [""])[0].strip()
            if val:
                params[key] = val
    return redirect(url_for("index", **params))


def _run_setup_command(command: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    proc = subprocess.run(command, capture_output=True, text=True, env=merged_env)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    details = "\n".join(x for x in [out, err] if x).strip()
    if proc.returncode == 0:
        return True, details or "Completed"
    return False, details or f"Failed with exit code {proc.returncode}"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_networks(raw: str) -> list[ipaddress._BaseNetwork]:
    networks: list[ipaddress._BaseNetwork] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return networks


def _valid_username(username: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username))


def _security_config_path(cfg: AppConfig) -> Path:
    return cfg.paths.state / "web_security.json"


def _load_security_config(cfg: AppConfig) -> dict:
    path = _security_config_path(cfg)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_security_config(cfg: AppConfig, payload: dict) -> None:
    path = _security_config_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _get_auth_accounts(data: dict) -> list[dict[str, str]]:
    accounts: list[dict[str, str]] = []
    raw_accounts = data.get("accounts", [])
    if isinstance(raw_accounts, list):
        for item in raw_accounts:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username", "")).strip()
            password_hash = str(item.get("password_hash", "")).strip()
            if _valid_username(username) and password_hash:
                accounts.append({"username": username, "password_hash": password_hash})

    if accounts:
        return accounts

    legacy_user = str(data.get("username", "")).strip()
    legacy_hash = str(data.get("password_hash", "")).strip()
    if _valid_username(legacy_user) and legacy_hash:
        return [{"username": legacy_user, "password_hash": legacy_hash}]

    return []


def _save_auth_accounts(cfg: AppConfig, accounts: list[dict[str, str]]) -> None:
    existing = _load_security_config(cfg)
    created_utc = str(existing.get("created_utc", "")).strip() or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "accounts": accounts,
        "created_utc": created_utc,
        "updated_utc": now,
    }
    if accounts:
        payload["username"] = accounts[0]["username"]
        payload["password_hash"] = accounts[0]["password_hash"]
    _save_security_config(cfg, payload)


def _user_inboxes_path(cfg: AppConfig) -> Path:
    return cfg.paths.state / "user_inboxes.json"


def _load_user_inboxes(cfg: AppConfig) -> dict:
    path = _user_inboxes_path(cfg)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "inboxes" in data:
            return data.get("inboxes", {})
    except Exception:
        pass
    return {}


def _save_user_inboxes(cfg: AppConfig, inboxes: dict) -> None:
    path = _user_inboxes_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {"inboxes": inboxes}
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _default_user_mount_point(username: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", username).strip("_") or "user"
    return f"/mnt/nas-users/{safe}"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _backup_path_map(cfg: AppConfig, include_documents: bool) -> list[tuple[Path, str]]:
    root = _project_root()
    mappings: list[tuple[Path, str]] = []

    for rel in [
        "config.yaml",
        "config.example.yaml",
        "README.md",
        "requirements.txt",
        "install.sh",
        "setup_smb_mount.sh",
        "setup_scanner_drop_share.sh",
        "src",
        "systemd",
    ]:
        src = root / rel
        if src.exists():
            mappings.append((src, f"project/{rel}"))

    if cfg.paths.state.exists():
        mappings.append((cfg.paths.state, "runtime/state"))
    if cfg.paths.inbox.exists():
        mappings.append((cfg.paths.inbox, "runtime/inbox"))
    if cfg.paths.processing.exists():
        mappings.append((cfg.paths.processing, "runtime/processing"))

    if include_documents:
        if cfg.paths.archive.exists():
            mappings.append((cfg.paths.archive, "documents/archive"))
        if cfg.paths.review.exists():
            mappings.append((cfg.paths.review, "documents/review"))
        if cfg.paths.rejected.exists():
            mappings.append((cfg.paths.rejected, "documents/rejected"))

        # Include user-specific document roots so multi-user setups are fully captured.
        inboxes = _load_user_inboxes(cfg)
        for username, meta in inboxes.items():
            if not _valid_username(username) or not isinstance(meta, dict):
                continue
            for bucket in ["archive", "review", "rejected"]:
                raw_path = str(meta.get(f"{bucket}_path", "")).strip()
                if not raw_path:
                    continue
                src = Path(raw_path).expanduser().resolve()
                if not src.exists():
                    continue
                # Avoid duplicate exports when user path is same as global bucket root.
                if bucket == "archive" and src == cfg.paths.archive:
                    continue
                if bucket == "review" and src == cfg.paths.review:
                    continue
                if bucket == "rejected" and src == cfg.paths.rejected:
                    continue
                mappings.append((src, f"documents/users/{username}/{bucket}"))

    return mappings


def _add_path_to_tar(tar: tarfile.TarFile, src: Path, arcname: str) -> None:
    if src.is_dir():
        tar.add(src, arcname=arcname, recursive=True)
    elif src.is_file():
        tar.add(src, arcname=arcname, recursive=False)


def _safe_extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo, destination: Path) -> None:
    name = Path(member.name)
    if name.is_absolute() or ".." in name.parts:
        raise ValueError(f"Unsafe archive member: {member.name}")

    target = (destination / name).resolve()
    base = destination.resolve()
    if base not in target.parents and target != base:
        raise ValueError(f"Archive member escapes destination: {member.name}")

    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    extracted = tar.extractfile(member)
    if extracted is None:
        return
    with extracted, target.open("wb") as f:
        shutil.copyfileobj(extracted, f)


def _apply_restored_config(cfg: AppConfig, config_path: Path) -> None:
    new_cfg = load_config(config_path)
    cfg.paths = new_cfg.paths
    cfg.ocr = new_cfg.ocr
    cfg.llm = new_cfg.llm
    cfg.splitter = new_cfg.splitter
    cfg.rules = new_cfg.rules
    cfg.poll_seconds = new_cfg.poll_seconds
    cfg.inbox_settle_seconds = new_cfg.inbox_settle_seconds
    cfg.require_size_stability = new_cfg.require_size_stability
    cfg.stable_cycles_required = new_cfg.stable_cycles_required
    cfg.log_level = new_cfg.log_level
    ensure_directories(cfg)


def create_app(cfg: AppConfig, config_path: Path) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    secret = os.environ.get("SCANNER_WEB_SECRET", "")
    if not secret:
        secret = os.urandom(32).hex()
        app.logger.warning("SCANNER_WEB_SECRET is not set; using an ephemeral secret")
    app.secret_key = secret
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _env_bool("SCANNER_WEB_SECURE_COOKIE", False)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 512  # 512 MiB upload cap for backup restore

    app.config["AUTH_USER"] = os.environ.get("SCANNER_WEB_USER", "admin")
    app.config["AUTH_PASSWORD"] = os.environ.get("SCANNER_WEB_PASSWORD", "")
    app.config["AUTH_REQUIRED"] = not _env_bool("SCANNER_WEB_DISABLE_AUTH", False)
    app.config["SETUP_ENABLED"] = _env_bool("SCANNER_WEB_ENABLE_SETUP", False)
    app.config["ALLOWED_NETWORKS"] = _parse_networks(
        os.environ.get(
            "SCANNER_WEB_ALLOWED_NETS",
            "127.0.0.1/32,::1/128,192.168.1.0/24",
        )
    )

    app.config["CFG"] = cfg
    app.config["CONFIG_PATH"] = config_path

    security_file = _load_security_config(cfg)
    accounts = _get_auth_accounts(security_file)

    if not accounts:
        env_user = str(app.config.get("AUTH_USER", "admin")).strip() or "admin"
        env_pass = str(app.config.get("AUTH_PASSWORD", ""))
        if env_pass:
            accounts = [{"username": env_user, "password_hash": generate_password_hash(env_pass)}]

    app.config["AUTH_ACCOUNTS"] = accounts
    app.config["BOOTSTRAP_REQUIRED"] = bool(app.config.get("AUTH_REQUIRED", True) and not accounts)

    def _auth_enabled() -> bool:
        return bool(app.config.get("AUTH_REQUIRED", True))

    def _auth_ready() -> bool:
        if not _auth_enabled():
            return True
        return bool(app.config.get("AUTH_ACCOUNTS", []))

    def _client_allowed() -> bool:
        nets = app.config.get("ALLOWED_NETWORKS", [])
        if not nets:
            return True
        remote = request.remote_addr or ""
        try:
            addr = ipaddress.ip_address(remote)
        except ValueError:
            return False
        return any(addr in net for net in nets)

    def _csrf_token() -> str:
        token = session.get("csrf_token", "")
        if not token:
            token = uuid4().hex
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = _csrf_token

    @app.before_request
    def _security_gate():
        if not _client_allowed():
            abort(403, description="Client IP is not allowed")

        endpoint = request.endpoint or ""

        if app.config.get("BOOTSTRAP_REQUIRED", False):
            if endpoint not in {"static", "first_setup"}:
                return redirect(url_for("first_setup"))

        if not _auth_ready() and not app.config.get("BOOTSTRAP_REQUIRED", False):
            abort(503, description="SCANNER_WEB_PASSWORD is not configured")
        if endpoint not in {"static", "login", "first_setup"}:
            if _auth_enabled() and not session.get("auth_ok", False):
                return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

        if request.method == "POST":
            token = str(session.get("csrf_token", ""))
            supplied = request.form.get("_csrf", "")
            if not token or not supplied or not secrets.compare_digest(token, supplied):
                abort(400, description="Invalid CSRF token")

        if request.path.startswith("/setup/") and not app.config.get("SETUP_ENABLED", False):
            return _redirect_index_with_context(
                "Setup routes are disabled. Set SCANNER_WEB_ENABLE_SETUP=true to enable.",
                request.form.get("next", "") if request.method == "POST" else "",
            )

        return None

    @app.after_request
    def _security_headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return resp

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if app.config.get("BOOTSTRAP_REQUIRED", False):
            return redirect(url_for("first_setup"))

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
            for acct in list(app.config.get("AUTH_ACCOUNTS", [])):
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
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/first-setup", methods=["GET", "POST"])
    def first_setup():
        if not app.config.get("BOOTSTRAP_REQUIRED", False):
            return redirect(url_for("index"))

        msg = ""
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")

            if not _valid_username(username):
                msg = "Username must be 3-64 chars and contain only letters, numbers, ., _, -"
            elif len(password) < 12:
                msg = "Password must be at least 12 characters"
            elif password != confirm:
                msg = "Passwords do not match"
            else:
                accounts = [{"username": username, "password_hash": generate_password_hash(password)}]
                _save_auth_accounts(cfg, accounts)
                app.config["AUTH_ACCOUNTS"] = accounts
                app.config["BOOTSTRAP_REQUIRED"] = False
                session["auth_ok"] = True
                session["auth_user"] = username
                _csrf_token()
                return redirect(url_for("index", msg="Initial security setup complete"))

        return render_template("first_setup.html", msg=msg)

    @app.get("/")
    def index() -> str:
        q = request.args.get("q", "").strip()
        bucket = request.args.get("bucket", "all").strip() or "all"
        sort_by = request.args.get("sort_by", "received").strip() or "received"
        sort_dir = request.args.get("sort_dir", "desc").strip() or "desc"
        msg = request.args.get("msg", "").strip()

        docs = collect_documents(cfg)
        filtered = [d for d in docs if _matches(d, q, bucket)]
        filtered = _sort_documents(filtered, sort_by, sort_dir)
        tree_roots = _build_tree_roots(filtered, cfg)
        queue_rows = collect_queue(cfg)
        return_query = urlencode(
            {
                "q": q,
                "bucket": bucket,
                "sort_by": sort_by,
                "sort_dir": sort_dir,
            }
        )

        return render_template(
            "index.html",
            documents=filtered,
            inbox_users=sorted([str(name) for name in _load_user_inboxes(cfg).keys() if str(name).strip()]),
            q=q,
            bucket=bucket,
            sort_by=sort_by,
            sort_dir=sort_dir,
            return_query=return_query,
            msg=msg,
            allowed_types=[t for t in cfg.rules.allowed_doc_types if t != cfg.rules.unknown_doc_type],
            setup_defaults={
                "archive": str(cfg.paths.archive),
                "review": str(cfg.paths.review),
                "rejected": str(cfg.paths.rejected),
                "inbox": str(cfg.paths.inbox),
                "mount_point": "/mnt/nas",
                "subdir": "Home Filing",
                "nas_host": "",
                "nas_share": "Public",
                "nas_user": "",
                "security_user": str(session.get("auth_user", "admin")),
                "ai_confidence": float(cfg.llm.min_confidence_autofile),
                "ai_guidance": str(cfg.llm.classification_guidance),
                "ai_per_type_conf": json.dumps(cfg.rules.per_type_min_confidence, ensure_ascii=True, indent=2),
                "ai_keyword_overrides": json.dumps(cfg.rules.keyword_overrides, ensure_ascii=True, indent=2),
                "ai_negative_keywords": json.dumps(cfg.rules.negative_type_keywords, ensure_ascii=True, indent=2),
                "ocr_language": str(cfg.ocr.language),
                "ocr_skip_text": bool(cfg.ocr.skip_text),
                "ocr_rotate_pages": bool(cfg.ocr.rotate_pages),
                "ocr_deskew": bool(cfg.ocr.deskew),
                "ocr_clean": bool(cfg.ocr.clean),
                "ocr_extra_args": str(cfg.ocr.extra_args),
                "scanner_user": "scannerdrop",
                "scanner_share": "scanner_inbox",
            },
            setup_enabled=bool(app.config.get("SETUP_ENABLED", False)),
            tree_roots=tree_roots,
            queue_rows=queue_rows,
        )

    @app.get("/api/queue")
    def queue_api():
        rows = collect_queue(cfg)
        return jsonify(
            {
                "rows": [
                    {
                        "filename": r.filename,
                        "location": r.location,
                        "stage": r.stage,
                        "percent": r.percent,
                        "size_kb": r.size_kb,
                        "modified_iso": r.modified_iso,
                    }
                    for r in rows
                ]
            }
        )

    @app.post("/add-category")
    def add_category():
        next_query = request.form.get("next", "").strip()
        new_category = request.form.get("new_category", "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{2,40}", new_category):
            return _redirect_index_with_context("Category must match [a-z0-9_] and be 2-40 chars", next_query)

        raw = _load_raw_config(config_path)
        rules = raw.setdefault("rules", {})
        existing = list(rules.get("allowed_doc_types", []))
        if new_category in existing:
            return _redirect_index_with_context(f"Category already exists: {new_category}", next_query)

        unknown = str(rules.get("unknown_doc_type", "unknown"))
        if unknown in existing:
            insert_at = existing.index(unknown)
            existing.insert(insert_at, new_category)
        else:
            existing.append(new_category)
        rules["allowed_doc_types"] = existing
        _save_raw_config(config_path, raw)

        cfg.rules.allowed_doc_types = existing
        return _redirect_index_with_context(
            f"Added category: {new_category}. Restart scanner-filer to apply for classification.",
            next_query,
        )

    @app.post("/keywords")
    def update_keywords():
        next_query = request.form.get("next", "").strip()
        src_bucket = request.form.get("src_bucket", "")
        rel_path = request.form.get("rel_path", "")
        keywords_raw = request.form.get("keywords", "")
        roots = _roots(cfg)

        if src_bucket not in roots:
            return _redirect_index_with_context("Invalid source bucket for keywords", next_query)

        try:
            src = _safe_join(roots[src_bucket], rel_path)
        except ValueError:
            return _redirect_index_with_context("Invalid source path", next_query)

        if not src.exists() or not src.is_file():
            return _redirect_index_with_context("File not found for keyword update", next_query)

        key = f"{src_bucket}:{rel_path}"
        stat = src.stat()
        sig = f"{stat.st_mtime_ns}:{stat.st_size}"
        index = _load_keyword_index(cfg)
        records = index.setdefault("records", {})
        entry = records.get(key)
        if not isinstance(entry, dict):
            entry = {"sig": sig, "keywords": []}

        manual_keywords = _parse_keyword_input(keywords_raw)
        if manual_keywords:
            entry["manual_keywords"] = manual_keywords
            msg = f"Updated keywords for {src.name}"
        else:
            entry.pop("manual_keywords", None)
            msg = f"Cleared manual keywords for {src.name}"

        entry["sig"] = sig
        records[key] = entry
        index["records"] = records
        _save_keyword_index(cfg, index)

        return _redirect_index_with_context(msg, next_query)

    @app.post("/setup/nas")
    def setup_nas():
        next_query = request.form.get("next", "").strip()
        nas_host = request.form.get("nas_host", "").strip()
        nas_share = request.form.get("nas_share", "").strip()
        nas_user = request.form.get("nas_user", "").strip()
        nas_pass = request.form.get("nas_pass", "")
        mount_point = request.form.get("mount_point", "").strip() or "/mnt/nas"
        subdir = request.form.get("subdir", "").strip() or "Home Filing"

        if not nas_host or not nas_share or not nas_user or not nas_pass:
            return _redirect_index_with_context("NAS setup needs host/share/user/password", next_query)

        script = Path(__file__).resolve().parents[2] / "setup_smb_mount.sh"
        ok, details = _run_setup_command(
            [
                str(script),
                "--non-interactive",
                "--nas-host",
                nas_host,
                "--nas-share",
                nas_share,
                "--nas-user",
                nas_user,
                "--mount-point",
                mount_point,
                "--subdir",
                subdir,
            ],
            env={"NAS_PASS": nas_pass},
        )
        if not ok:
            return _redirect_index_with_context(f"NAS setup failed: {details[:220]}", next_query)

        root = Path(mount_point) / subdir
        raw = _load_raw_config(config_path)
        raw.setdefault("paths", {})
        raw["paths"]["archive"] = str(root / "archive")
        raw["paths"]["review"] = str(root / "review")
        raw["paths"]["rejected"] = str(root / "rejected")
        _save_raw_config(config_path, raw)

        cfg.paths.archive = (root / "archive").resolve()
        cfg.paths.review = (root / "review").resolve()
        cfg.paths.rejected = (root / "rejected").resolve()

        return _redirect_index_with_context("NAS setup applied. Archive/review/rejected paths updated.", next_query)

    @app.post("/setup/scanner-share")
    def setup_scanner_share():
        next_query = request.form.get("next", "").strip()
        scanner_user = request.form.get("scanner_user", "").strip() or "scannerdrop"
        scanner_pass = request.form.get("scanner_pass", "")
        scanner_share = request.form.get("scanner_share", "").strip() or "scanner_inbox"
        inbox_dir = request.form.get("inbox_dir", "").strip() or str(cfg.paths.inbox)

        if not scanner_pass:
            return _redirect_index_with_context("Scanner share setup needs a password", next_query)

        script = Path(__file__).resolve().parents[2] / "setup_scanner_drop_share.sh"
        ok, details = _run_setup_command(
            [
                str(script),
                "--non-interactive",
                "--scanner-user",
                scanner_user,
                "--share-name",
                scanner_share,
                "--inbox-dir",
                inbox_dir,
            ],
            env={"SMB_PASS": scanner_pass},
        )
        if not ok:
            return _redirect_index_with_context(f"Scanner share setup failed: {details[:220]}", next_query)

        raw = _load_raw_config(config_path)
        raw.setdefault("paths", {})
        raw["paths"]["inbox"] = inbox_dir
        _save_raw_config(config_path, raw)
        cfg.paths.inbox = Path(inbox_dir).expanduser().resolve()

        return _redirect_index_with_context("Scanner share setup applied.", next_query)

    @app.post("/setup/backup/export")
    def export_backup():
        include_documents = request.form.get("include_documents", "off") == "on"
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        filename = f"scanner-backup-{stamp}.tar.gz"

        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            manifest = {
                "created_utc": stamp,
                "project": "scanner",
                "include_documents": include_documents,
                "paths": {
                    "archive": str(cfg.paths.archive),
                    "review": str(cfg.paths.review),
                    "rejected": str(cfg.paths.rejected),
                    "inbox": str(cfg.paths.inbox),
                    "processing": str(cfg.paths.processing),
                    "state": str(cfg.paths.state),
                },
                "user_inboxes": _load_user_inboxes(cfg),
            }
            payload = json.dumps(manifest, ensure_ascii=True, indent=2).encode("utf-8")
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(payload)
            tar.addfile(info, BytesIO(payload))

            for src, arcname in _backup_path_map(cfg, include_documents):
                _add_path_to_tar(tar, src, arcname)

        buffer.seek(0)
        return send_file(
            buffer,
            mimetype="application/gzip",
            as_attachment=True,
            download_name=filename,
        )

    @app.post("/setup/backup/restore")
    def restore_backup():
        next_query = request.form.get("next", "").strip()
        upload = request.files.get("backup_file")
        if upload is None or not upload.filename:
            return _redirect_index_with_context("Select a backup archive to restore", next_query)

        data = upload.read()
        if not data:
            return _redirect_index_with_context("Uploaded backup archive is empty", next_query)

        try:
            with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as tar:
                root = _project_root()
                for member in tar.getmembers():
                    if member.name == "manifest.json":
                        continue
                    if member.name.startswith("project/"):
                        member.name = member.name[len("project/"):]
                        _safe_extract_member(tar, member, root)
                    elif member.name.startswith("runtime/state/"):
                        member.name = member.name[len("runtime/state/"):]
                        _safe_extract_member(tar, member, cfg.paths.state)
                    elif member.name.startswith("runtime/inbox/"):
                        member.name = member.name[len("runtime/inbox/"):]
                        _safe_extract_member(tar, member, cfg.paths.inbox)
                    elif member.name.startswith("runtime/processing/"):
                        member.name = member.name[len("runtime/processing/"):]
                        _safe_extract_member(tar, member, cfg.paths.processing)
                    elif member.name.startswith("documents/archive/"):
                        member.name = member.name[len("documents/archive/"):]
                        _safe_extract_member(tar, member, cfg.paths.archive)
                    elif member.name.startswith("documents/review/"):
                        member.name = member.name[len("documents/review/"):]
                        _safe_extract_member(tar, member, cfg.paths.review)
                    elif member.name.startswith("documents/rejected/"):
                        member.name = member.name[len("documents/rejected/"):]
                        _safe_extract_member(tar, member, cfg.paths.rejected)
                    elif member.name.startswith("documents/users/"):
                        # Format: documents/users/<username>/<bucket>/...
                        suffix = member.name[len("documents/users/"):]
                        parts = Path(suffix).parts
                        if len(parts) >= 2:
                            username = parts[0]
                            bucket = parts[1]
                            if _valid_username(username) and bucket in {"archive", "review", "rejected"}:
                                inboxes = _load_user_inboxes(cfg)
                                entry = inboxes.get(username, {}) if isinstance(inboxes, dict) else {}
                                if isinstance(entry, dict):
                                    target_raw = str(entry.get(f"{bucket}_path", "")).strip()
                                    if target_raw:
                                        target = Path(target_raw).expanduser().resolve()
                                        member.name = str(Path(*parts[2:])) if len(parts) > 2 else ""
                                        if member.name:
                                            _safe_extract_member(tar, member, target)
        except Exception as exc:
            return _redirect_index_with_context(f"Backup restore failed: {exc}", next_query)

        _apply_restored_config(cfg, config_path)
        return _redirect_index_with_context("Backup restore completed. Service restart is recommended.", next_query)

    @app.get("/api/security-accounts")
    def api_security_accounts():
        accounts = list(app.config.get("AUTH_ACCOUNTS", []))
        return jsonify({"accounts": [{"username": a.get("username", "")} for a in accounts]})

    @app.post("/setup/security")
    def setup_security():
        next_query = request.form.get("next", "").strip()
        new_user = request.form.get("security_user", "").strip()
        new_pass = request.form.get("security_password", "")
        confirm = request.form.get("security_password_confirm", "")

        if not _valid_username(new_user):
            return _redirect_index_with_context(
                "Username must be 3-64 chars and contain only letters, numbers, ., _, -",
                next_query,
            )
        if len(new_pass) < 12:
            return _redirect_index_with_context("Password must be at least 12 characters", next_query)
        if new_pass != confirm:
            return _redirect_index_with_context("Security passwords do not match", next_query)

        accounts = list(app.config.get("AUTH_ACCOUNTS", []))
        replaced = False
        for acct in accounts:
            if str(acct.get("username", "")) == new_user:
                acct["password_hash"] = generate_password_hash(new_pass)
                replaced = True
                break
        if not replaced:
            accounts.append({"username": new_user, "password_hash": generate_password_hash(new_pass)})

        _save_auth_accounts(cfg, accounts)
        app.config["AUTH_ACCOUNTS"] = accounts
        session["auth_ok"] = True
        session["auth_user"] = new_user
        if replaced:
            return _redirect_index_with_context(f"Login account '{new_user}' updated", next_query)
        return _redirect_index_with_context(f"Login account '{new_user}' added", next_query)

    @app.post("/setup/security/delete")
    def setup_security_delete():
        next_query = request.form.get("next", "").strip()
        username = request.form.get("security_user_delete", "").strip()
        if not username:
            return _redirect_index_with_context("Select an account to delete", next_query)

        accounts = list(app.config.get("AUTH_ACCOUNTS", []))
        remaining = [a for a in accounts if str(a.get("username", "")) != username]
        if len(remaining) == len(accounts):
            return _redirect_index_with_context(f"Login account '{username}' not found", next_query)
        if not remaining:
            return _redirect_index_with_context("At least one login account is required", next_query)

        _save_auth_accounts(cfg, remaining)
        app.config["AUTH_ACCOUNTS"] = remaining
        if str(session.get("auth_user", "")) == username:
            session["auth_user"] = str(remaining[0].get("username", "admin"))
        return _redirect_index_with_context(f"Login account '{username}' deleted", next_query)

    @app.post("/setup/ai")
    def setup_ai():
        next_query = request.form.get("next", "").strip()
        confidence_raw = request.form.get("min_confidence_autofile", "").strip()
        guidance = request.form.get("classification_guidance", "").strip()
        per_type_conf_raw = request.form.get("per_type_min_confidence", "").strip() or "{}"
        keyword_overrides_raw = request.form.get("keyword_overrides", "").strip() or "{}"
        negative_keywords_raw = request.form.get("negative_type_keywords", "").strip() or "{}"
        ocr_language = request.form.get("ocr_language", "").strip() or "eng"
        ocr_skip_text = request.form.get("ocr_skip_text", "off") == "on"
        ocr_rotate_pages = request.form.get("ocr_rotate_pages", "off") == "on"
        ocr_deskew = request.form.get("ocr_deskew", "off") == "on"
        ocr_clean = request.form.get("ocr_clean", "off") == "on"
        ocr_extra_args = request.form.get("ocr_extra_args", "").strip()

        try:
            confidence = float(confidence_raw)
        except ValueError:
            return _redirect_index_with_context("AI confidence threshold must be a number between 0 and 1", next_query)

        if confidence < 0.0 or confidence > 1.0:
            return _redirect_index_with_context("AI confidence threshold must be between 0 and 1", next_query)
        if len(guidance) > 2000:
            return _redirect_index_with_context("AI guidance is too long (max 2000 characters)", next_query)

        try:
            per_type_data = json.loads(per_type_conf_raw)
            keyword_overrides_data = json.loads(keyword_overrides_raw)
            negative_keywords_data = json.loads(negative_keywords_raw)
        except json.JSONDecodeError:
            return _redirect_index_with_context("Advanced AI settings must be valid JSON", next_query)

        if not isinstance(per_type_data, dict):
            return _redirect_index_with_context("Per-type confidence must be a JSON object", next_query)
        if not isinstance(keyword_overrides_data, dict):
            return _redirect_index_with_context("Keyword overrides must be a JSON object", next_query)
        if not isinstance(negative_keywords_data, dict):
            return _redirect_index_with_context("Negative type keywords must be a JSON object", next_query)

        per_type_clean: dict[str, float] = {}
        for k, v in per_type_data.items():
            key = str(k).strip().lower()
            if not key:
                continue
            try:
                score = float(v)
            except (TypeError, ValueError):
                return _redirect_index_with_context(f"Invalid confidence for type '{key}'", next_query)
            if score < 0.0 or score > 1.0:
                return _redirect_index_with_context(f"Confidence for type '{key}' must be between 0 and 1", next_query)
            per_type_clean[key] = round(score, 3)

        keyword_overrides_clean: dict[str, str] = {}
        for k, v in keyword_overrides_data.items():
            key = str(k).strip().lower()
            val = str(v).strip().lower()
            if key and val:
                keyword_overrides_clean[key] = val

        negative_keywords_clean: dict[str, list[str]] = {}
        for dtype, values in negative_keywords_data.items():
            dt = str(dtype).strip().lower()
            if not dt:
                continue
            if not isinstance(values, list):
                return _redirect_index_with_context(f"Negative keywords for '{dt}' must be a list", next_query)
            cleaned = [str(x).strip().lower() for x in values if str(x).strip()]
            if cleaned:
                negative_keywords_clean[dt] = cleaned

        raw = _load_raw_config(config_path)
        raw.setdefault("llm", {})
        raw["llm"]["min_confidence_autofile"] = round(confidence, 3)
        raw["llm"]["classification_guidance"] = guidance
        raw.setdefault("rules", {})
        raw["rules"]["per_type_min_confidence"] = per_type_clean
        raw["rules"]["keyword_overrides"] = keyword_overrides_clean
        raw["rules"]["negative_type_keywords"] = negative_keywords_clean
        raw.setdefault("ocr", {})
        raw["ocr"]["language"] = ocr_language
        raw["ocr"]["skip_text"] = bool(ocr_skip_text)
        raw["ocr"]["rotate_pages"] = bool(ocr_rotate_pages)
        raw["ocr"]["deskew"] = bool(ocr_deskew)
        raw["ocr"]["clean"] = bool(ocr_clean)
        raw["ocr"]["extra_args"] = ocr_extra_args
        _save_raw_config(config_path, raw)

        cfg.llm.min_confidence_autofile = round(confidence, 3)
        cfg.llm.classification_guidance = guidance
        cfg.rules.per_type_min_confidence = per_type_clean
        cfg.rules.keyword_overrides = keyword_overrides_clean
        cfg.rules.negative_type_keywords = negative_keywords_clean
        cfg.ocr.language = ocr_language
        cfg.ocr.skip_text = bool(ocr_skip_text)
        cfg.ocr.rotate_pages = bool(ocr_rotate_pages)
        cfg.ocr.deskew = bool(ocr_deskew)
        cfg.ocr.clean = bool(ocr_clean)
        cfg.ocr.extra_args = ocr_extra_args

        return _redirect_index_with_context("AI/OCR tuning updated", next_query)

    @app.get("/api/user-inboxes")
    def api_user_inboxes():
        inboxes = _load_user_inboxes(cfg)
        return jsonify({"inboxes": inboxes})

    @app.post("/setup/user-inbox/add")
    def setup_user_inbox_add():
        next_query = request.form.get("next", "").strip()
        name = request.form.get("name", "").strip()
        nas_host = request.form.get("nas_host", "").strip()
        nas_share = request.form.get("nas_share", "").strip() or "Public"
        nas_user = request.form.get("nas_user", "").strip()
        nas_pass = request.form.get("nas_pass", "")
        nas_subdir = request.form.get("nas_subdir", "").strip() or "Home Filing"

        if not _valid_username(name):
            return _redirect_index_with_context(
                "Inbox name must be 3-64 chars and contain only letters, numbers, ., _, -",
                next_query,
            )
        if not nas_host or not nas_user or not nas_pass:
            return _redirect_index_with_context("NAS host, user, and password are required", next_query)

        inboxes = _load_user_inboxes(cfg)
        if name in inboxes:
            return _redirect_index_with_context(f"User inbox '{name}' already exists", next_query)

        share_name = f"scanner_{name}"
        mount_point = _default_user_mount_point(name)
        cred_file = f"/home/pi/.smb-nas-{name}"

        mount_script = Path(__file__).resolve().parents[2] / "setup_smb_mount.sh"
        ok_mount, details_mount = _run_setup_command(
            [
                str(mount_script),
                "--non-interactive",
                "--nas-host",
                nas_host,
                "--nas-share",
                nas_share,
                "--nas-user",
                nas_user,
                "--mount-point",
                mount_point,
                "--cred-file",
                cred_file,
                "--subdir",
                nas_subdir,
            ],
            env={"NAS_PASS": nas_pass},
        )
        if not ok_mount:
            return _redirect_index_with_context(f"NAS setup failed for '{name}': {details_mount[:220]}", next_query)

        base = (Path(mount_point) / nas_subdir).resolve()
        inboxes[name] = {
            "samba_share": share_name,
            "nas_host": nas_host,
            "nas_share": nas_share,
            "nas_user": nas_user,
            "nas_subdir": nas_subdir,
            "mount_point": mount_point,
            "archive_path": str(base / "archive"),
            "review_path": str(base / "review"),
            "rejected_path": str(base / "rejected"),
            "enabled": True,
        }
        _save_user_inboxes(cfg, inboxes)
        return _redirect_index_with_context(
            f"User inbox '{name}' added with NAS credentials and mounted. Run 'Apply User Inboxes' to create Samba shares.",
            next_query,
        )

    @app.post("/setup/user-inbox/update")
    def setup_user_inbox_update():
        next_query = request.form.get("next", "").strip()
        current_name = request.form.get("current_name", "").strip()
        new_name = request.form.get("name", "").strip()
        nas_host = request.form.get("nas_host", "").strip()
        nas_share = request.form.get("nas_share", "").strip() or "Public"
        nas_user = request.form.get("nas_user", "").strip()
        nas_pass = request.form.get("nas_pass", "")
        nas_subdir = request.form.get("nas_subdir", "").strip() or "Home Filing"
        samba_share = request.form.get("samba_share", "").strip()
        enabled = request.form.get("enabled", "") == "on"

        if not current_name:
            return _redirect_index_with_context("Select a user inbox to edit", next_query)
        if not _valid_username(new_name):
            return _redirect_index_with_context(
                "Username must be 3-64 chars and contain only letters, numbers, ., _, -",
                next_query,
            )
        if not nas_host or not nas_user:
            return _redirect_index_with_context("NAS host and NAS user are required", next_query)

        inboxes = _load_user_inboxes(cfg)
        if current_name not in inboxes:
            return _redirect_index_with_context(f"User inbox '{current_name}' not found", next_query)
        if new_name != current_name and new_name in inboxes:
            return _redirect_index_with_context(f"User inbox '{new_name}' already exists", next_query)

        existing = inboxes[current_name]
        old_host = str(existing.get("nas_host", "")).strip()
        old_share = str(existing.get("nas_share", "")).strip()
        old_user = str(existing.get("nas_user", "")).strip()
        old_subdir = str(existing.get("nas_subdir", "")).strip()
        mount_point = str(existing.get("mount_point", "")).strip() or _default_user_mount_point(new_name)
        cred_file = f"/home/pi/.smb-nas-{new_name}"

        nas_changed = any(
            [
                nas_host != old_host,
                nas_share != old_share,
                nas_user != old_user,
                nas_subdir != old_subdir,
            ]
        )

        if nas_changed:
            if not nas_pass:
                return _redirect_index_with_context(
                    "NAS password is required when NAS host/share/user/subdirectory is changed",
                    next_query,
                )
            mount_script = Path(__file__).resolve().parents[2] / "setup_smb_mount.sh"
            ok_mount, details_mount = _run_setup_command(
                [
                    str(mount_script),
                    "--non-interactive",
                    "--nas-host",
                    nas_host,
                    "--nas-share",
                    nas_share,
                    "--nas-user",
                    nas_user,
                    "--mount-point",
                    mount_point,
                    "--cred-file",
                    cred_file,
                    "--subdir",
                    nas_subdir,
                ],
                env={"NAS_PASS": nas_pass},
            )
            if not ok_mount:
                return _redirect_index_with_context(f"NAS update failed for '{current_name}': {details_mount[:220]}", next_query)

        new_archive = str(existing.get("archive_path", "")).strip()
        new_review = str(existing.get("review_path", "")).strip()
        new_rejected = str(existing.get("rejected_path", "")).strip()
        if nas_changed:
            base = (Path(mount_point) / nas_subdir).resolve()
            new_archive = str(base / "archive")
            new_review = str(base / "review")
            new_rejected = str(base / "rejected")

        if new_name != current_name:
            old_inbox = cfg.paths.inbox / current_name
            new_inbox = cfg.paths.inbox / new_name
            if old_inbox.exists() and not new_inbox.exists():
                old_inbox.rename(new_inbox)

        if not samba_share:
            samba_share = str(existing.get("samba_share", "")).strip() or f"scanner_{new_name}"

        updated = {
            "samba_share": samba_share,
            "nas_host": nas_host,
            "nas_share": nas_share,
            "nas_user": nas_user,
            "nas_subdir": nas_subdir,
            "mount_point": mount_point,
            "archive_path": new_archive,
            "review_path": new_review,
            "rejected_path": new_rejected,
            "enabled": enabled,
        }

        if new_name != current_name:
            del inboxes[current_name]
        inboxes[new_name] = updated
        _save_user_inboxes(cfg, inboxes)

        return _redirect_index_with_context(f"User inbox '{current_name}' updated", next_query)

    @app.post("/setup/user-inbox/delete")
    def setup_user_inbox_delete():
        next_query = request.form.get("next", "").strip()
        name = request.form.get("name", "").strip()

        inboxes = _load_user_inboxes(cfg)
        if name not in inboxes:
            return _redirect_index_with_context(f"User inbox '{name}' not found", next_query)

        del inboxes[name]
        _save_user_inboxes(cfg, inboxes)
        return _redirect_index_with_context(f"User inbox '{name}' deleted.", next_query)

    @app.post("/setup/user-inbox/apply")
    def setup_user_inbox_apply():
        next_query = request.form.get("next", "").strip()
        samba_password = request.form.get("samba_password", "")

        if not samba_password:
            return _redirect_index_with_context("Samba password is required", next_query)

        inboxes = _load_user_inboxes(cfg)
        if not inboxes:
            return _redirect_index_with_context("No user inboxes configured", next_query)

        script = Path(__file__).resolve().parents[2] / "setup_scanner_drop_share.sh"
        for username, inbox_config in inboxes.items():
            if not inbox_config.get("enabled", True):
                continue

            inbox_path = cfg.paths.inbox / username
            inbox_path.mkdir(parents=True, exist_ok=True)

            ok, details = _run_setup_command(
                [
                    str(script),
                    "--non-interactive",
                    "--scanner-user",
                    username,
                    "--share-name",
                    inbox_config["samba_share"],
                    "--inbox-dir",
                    str(inbox_path),
                ],
                env={"SMB_PASS": samba_password},
            )
            if not ok:
                return _redirect_index_with_context(
                    f"Setup failed for inbox '{username}': {details[:200]}",
                    next_query,
                )

        return _redirect_index_with_context("User inboxes Samba shares applied successfully.", next_query)

    @app.post("/move")
    def move_document():
        next_query = request.form.get("next", "").strip()
        src_bucket = request.form.get("src_bucket", "")
        rel_path = request.form.get("rel_path", "")
        target_bucket = request.form.get("target_bucket", "")
        target_type = request.form.get("target_type", "").strip() or cfg.rules.unknown_doc_type
        target_year = request.form.get("target_year", "").strip() or str(datetime.now().year)
        target_sender = request.form.get("target_sender", "").strip() or "unknown"

        roots = _roots(cfg)

        if src_bucket not in roots or target_bucket not in roots:
            return _redirect_index_with_context("Invalid source or destination bucket", next_query)

        src_root, rel_local, src_user = _resolve_doc_source(cfg, src_bucket, rel_path)
        if not rel_local:
            return _redirect_index_with_context("Invalid source path", next_query)

        dst_root = _bucket_root_for_user(cfg, target_bucket, src_user)

        try:
            src = _safe_join(src_root, rel_local)
        except ValueError:
            return _redirect_index_with_context("Invalid source path", next_query)

        if not src.exists():
            return _redirect_index_with_context("Source file not found", next_query)

        if target_bucket == "archive":
            safe_sender = "".join(c if c.isalnum() or c in "._-" else "_" for c in target_sender).strip("_") or "unknown"
            safe_type = "".join(c if c.isalnum() or c in "._-" else "_" for c in target_type).strip("_") or cfg.rules.unknown_doc_type
            safe_year = "".join(c for c in target_year if c.isdigit()) or str(datetime.now().year)
            dst_dir = dst_root / safe_type / safe_year / safe_sender
        else:
            dst_dir = dst_root

        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = _ensure_unique_destination(dst_dir / src.name)

        os.replace(src, dst)
        _record_manual_action(cfg, [src, dst])
        learned = 0
        if target_bucket == "archive" and src_bucket in {"review", "rejected"}:
            learned = _learn_keyword_overrides_from_manual_move(cfg, config_path, src_bucket, rel_path, safe_type)
        if learned:
            return _redirect_index_with_context(
                f"Moved to {target_bucket}: {dst.name}. Learned {learned} keyword override(s).",
                next_query,
            )
        return _redirect_index_with_context(f"Moved to {target_bucket}: {dst.name}", next_query)

    @app.post("/move-inbox-bulk")
    def move_inbox_bulk():
        next_query = request.form.get("next", "").strip()
        target_user = request.form.get("target_user", "").strip()
        selected = request.form.getlist("selected")

        if not selected:
            return _redirect_index_with_context("No documents selected", next_query)

        if target_user == "generic":
            target_user = ""
        elif target_user and not _valid_username(target_user):
            return _redirect_index_with_context("Invalid target inbox", next_query)

        roots = _roots(cfg)
        moved = 0
        skipped = 0

        for item in selected:
            if "|" not in item:
                skipped += 1
                continue
            src_bucket, rel_path = item.split("|", 1)
            src_bucket = src_bucket.strip()
            rel_path = rel_path.strip()
            if src_bucket not in roots or not rel_path:
                skipped += 1
                continue

            try:
                src_root, rel_local, src_user = _resolve_doc_source(cfg, src_bucket, rel_path)
                if not rel_local:
                    skipped += 1
                    continue
                src = _safe_join(src_root, rel_local)
            except Exception:
                skipped += 1
                continue

            if not src.exists() or not src.is_file():
                skipped += 1
                continue

            effective_target_user = target_user if target_user else None
            if effective_target_user is None and src_user is None:
                # Generic -> generic is a no-op.
                skipped += 1
                continue

            dst_root = _bucket_root_for_user(cfg, src_bucket, effective_target_user)
            dst_root.mkdir(parents=True, exist_ok=True)
            try:
                dst_candidate = _safe_join(dst_root, rel_local)
            except ValueError:
                skipped += 1
                continue

            dst_candidate.parent.mkdir(parents=True, exist_ok=True)
            dst = _ensure_unique_destination(dst_candidate)
            if src.resolve() == dst.resolve():
                skipped += 1
                continue

            os.replace(src, dst)
            _record_manual_action(cfg, [src, dst])
            moved += 1

        if moved:
            msg = f"Transferred {moved} document(s) to {'generic inbox' if not target_user else target_user}"
            if skipped:
                msg += f" ({skipped} skipped)"
            return _redirect_index_with_context(msg, next_query)

        return _redirect_index_with_context("No documents were transferred", next_query)

    @app.get("/open")
    def open_document():
        src_bucket = request.args.get("bucket", "")
        rel_path = request.args.get("rel_path", "")
        roots = _roots(cfg)

        if src_bucket not in roots:
            abort(404)

        try:
            src_root, rel_local, _ = _resolve_doc_source(cfg, src_bucket, rel_path)
            if not rel_local:
                abort(404)
            src = _safe_join(src_root, rel_local)
        except ValueError:
            abort(404)

        if not src.exists() or not src.is_file():
            abort(404)

        return send_file(src, mimetype="application/pdf", as_attachment=False, download_name=src.name)

    @app.post("/split")
    def split_document():
        next_query = request.form.get("next", "").strip()
        src_bucket = request.form.get("src_bucket", "")
        rel_path = request.form.get("rel_path", "")
        roots = _roots(cfg)

        if src_bucket not in roots:
            return _redirect_index_with_context("Invalid source bucket for split", next_query)

        try:
            src_root, rel_local, _ = _resolve_doc_source(cfg, src_bucket, rel_path)
            if not rel_local:
                return _redirect_index_with_context("Invalid source path", next_query)
            src = _safe_join(src_root, rel_local)
        except ValueError:
            return _redirect_index_with_context("Invalid source path", next_query)

        if not src.exists():
            return _redirect_index_with_context("File not found for split", next_query)

        parts = split_pdf_if_needed(src, cfg.splitter, delete_original=True)
        if len(parts) <= 1:
            return _redirect_index_with_context(f"No split points found for {src.name}", next_query)

        inbox_user = _infer_inbox_user_from_rel(cfg, rel_path)
        moved = _requeue_parts_to_inbox(cfg, parts, inbox_user=inbox_user)

        return _redirect_index_with_context(
            f"Created {len(parts)} split part(s) and queued {len(moved)} to inbox for reprocessing",
            next_query,
        )

    @app.post("/split-manual")
    def split_document_manual():
        next_query = request.form.get("next", "").strip()
        src_bucket = request.form.get("src_bucket", "")
        rel_path = request.form.get("rel_path", "")
        pages_raw = request.form.get("split_pages", "").strip()
        roots = _roots(cfg)

        if src_bucket not in roots:
            return _redirect_index_with_context("Invalid source bucket for manual split", next_query)

        try:
            src_root, rel_local, _ = _resolve_doc_source(cfg, src_bucket, rel_path)
            if not rel_local:
                return _redirect_index_with_context("Invalid source path", next_query)
            src = _safe_join(src_root, rel_local)
        except ValueError:
            return _redirect_index_with_context("Invalid source path", next_query)

        if not src.exists():
            return _redirect_index_with_context("File not found for manual split", next_query)

        try:
            pages = [int(x.strip()) for x in pages_raw.split(",") if x.strip()]
        except ValueError:
            return _redirect_index_with_context("Manual split pages must be comma-separated integers", next_query)

        parts = split_pdf_at_starts(src, pages, delete_original=True)
        if len(parts) <= 1:
            return _redirect_index_with_context(f"Manual split produced no additional parts for {src.name}", next_query)

        inbox_user = _infer_inbox_user_from_rel(cfg, rel_path)
        moved = _requeue_parts_to_inbox(cfg, parts, inbox_user=inbox_user)

        return _redirect_index_with_context(
            f"Manual split created {len(parts)} part(s) and queued {len(moved)} to inbox for reprocessing",
            next_query,
        )

    @app.post("/delete")
    def delete_document():
        next_query = request.form.get("next", "").strip()
        src_bucket = request.form.get("src_bucket", "")
        rel_path = request.form.get("rel_path", "")
        roots = _roots(cfg)

        if src_bucket not in roots:
            return _redirect_index_with_context("Invalid source bucket for delete", next_query)

        try:
            src_root, rel_local, _ = _resolve_doc_source(cfg, src_bucket, rel_path)
            if not rel_local:
                return _redirect_index_with_context("Invalid source path", next_query)
            src = _safe_join(src_root, rel_local)
        except ValueError:
            return _redirect_index_with_context("Invalid source path", next_query)

        if not src.exists() or not src.is_file():
            return _redirect_index_with_context("File not found for delete", next_query)

        _record_manual_action(cfg, [src])
        src.unlink(missing_ok=True)
        return _redirect_index_with_context(f"Deleted {src.name}", next_query)

    @app.post("/splitter-settings")
    def update_splitter_settings():
        enabled = request.form.get("splitter_enabled", "off") == "on"
        min_pages_raw = request.form.get("min_pages_to_split", "").strip() or "3"
        max_chars_raw = request.form.get("max_first_page_chars", "").strip() or "700"
        keywords_raw = request.form.get("boundary_keywords", "").strip()

        try:
            min_pages = max(2, int(min_pages_raw))
            max_chars = max(200, int(max_chars_raw))
        except ValueError:
            return redirect(url_for("index", msg="Splitter settings must be numeric"))

        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
        if not keywords:
            keywords = cfg.splitter.boundary_keywords

        raw = _load_raw_config(config_path)
        raw["splitter"] = {
            "enabled": enabled,
            "min_pages_to_split": min_pages,
            "max_first_page_chars": max_chars,
            "boundary_keywords": keywords,
        }
        _save_raw_config(config_path, raw)

        cfg.splitter.enabled = enabled
        cfg.splitter.min_pages_to_split = min_pages
        cfg.splitter.max_first_page_chars = max_chars
        cfg.splitter.boundary_keywords = keywords

        return redirect(url_for("index", msg="Updated splitter settings"))

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scanner filer web interface")
    parser.add_argument("--config", required=True, help="Path to config YAML")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", default=8090, type=int, help="Bind port")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    ensure_directories(cfg)

    app = create_app(cfg, Path(args.config))
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

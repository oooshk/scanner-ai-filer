from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
import yaml

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


def _scan_bucket(cfg: AppConfig, root: Path, bucket: str) -> list[DocumentRecord]:
    rows: list[DocumentRecord] = []
    if not root.exists():
        return rows

    for p in sorted(root.rglob("*.pdf")):
        rel = _safe_rel(root, p)
        parts = Path(rel).parts

        doc_type = "unknown"
        year = ""
        sender = ""
        if bucket == "archive" and len(parts) >= 4:
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
            )
        )

    return rows


def collect_documents(cfg: AppConfig) -> list[DocumentRecord]:
    docs: list[DocumentRecord] = []
    docs.extend(_scan_bucket(cfg, cfg.paths.archive, "archive"))
    docs.extend(_scan_bucket(cfg, cfg.paths.review, "review"))
    docs.extend(_scan_bucket(cfg, cfg.paths.rejected, "rejected"))
    docs = [d for d in docs if d.abs_path.exists()]
    _attach_keywords(cfg, docs)
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

    for p in sorted(cfg.paths.inbox.glob("*.pdf")):
        stat = p.stat()
        age = max(0.0, now_ts - stat.st_mtime)
        settle_pct = 0.0
        if cfg.inbox_settle_seconds > 0:
            settle_pct = min(1.0, age / float(cfg.inbox_settle_seconds))
        pct = int(5 + settle_pct * 45)
        rows.append(
            QueueRecord(
                filename=p.name,
                location="inbox",
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


def _build_tree_roots(docs: list[DocumentRecord]) -> list[dict]:
    roots: dict[str, dict] = {}

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
        children.sort(key=lambda c: (c["is_file"], c["name"].lower()))
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


def _requeue_parts_to_inbox(cfg: AppConfig, parts: list[Path]) -> list[Path]:
    moved: list[Path] = []
    cfg.paths.inbox.mkdir(parents=True, exist_ok=True)
    for part in parts:
        dst = unique_path(cfg.paths.inbox / part.name)
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
    app.config["CFG"] = cfg
    app.config["CONFIG_PATH"] = config_path

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
        tree_roots = _build_tree_roots(filtered)
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
                "scanner_user": "scannerdrop",
                "scanner_share": "scanner_inbox",
            },
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
        except Exception as exc:
            return _redirect_index_with_context(f"Backup restore failed: {exc}", next_query)

        _apply_restored_config(cfg, config_path)
        return _redirect_index_with_context("Backup restore completed. Service restart is recommended.", next_query)

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

        src_root = roots[src_bucket]
        dst_root = roots[target_bucket]

        try:
            src = _safe_join(src_root, rel_path)
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
        dst = dst_dir / src.name
        if dst.exists():
            stem = dst.stem
            suffix = dst.suffix
            counter = 1
            while True:
                candidate = dst.with_name(f"{stem}_{counter}{suffix}")
                if not candidate.exists():
                    dst = candidate
                    break
                counter += 1

        os.replace(src, dst)
        return _redirect_index_with_context(f"Moved to {target_bucket}: {dst.name}", next_query)

    @app.get("/open")
    def open_document():
        src_bucket = request.args.get("bucket", "")
        rel_path = request.args.get("rel_path", "")
        roots = _roots(cfg)

        if src_bucket not in roots:
            abort(404)

        try:
            src = _safe_join(roots[src_bucket], rel_path)
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
            src = _safe_join(roots[src_bucket], rel_path)
        except ValueError:
            return _redirect_index_with_context("Invalid source path", next_query)

        if not src.exists():
            return _redirect_index_with_context("File not found for split", next_query)

        parts = split_pdf_if_needed(src, cfg.splitter, delete_original=True)
        if len(parts) <= 1:
            return _redirect_index_with_context(f"No split points found for {src.name}", next_query)

        moved = _requeue_parts_to_inbox(cfg, parts)

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
            src = _safe_join(roots[src_bucket], rel_path)
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

        moved = _requeue_parts_to_inbox(cfg, parts)

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
            src = _safe_join(roots[src_bucket], rel_path)
        except ValueError:
            return _redirect_index_with_context("Invalid source path", next_query)

        if not src.exists() or not src.is_file():
            return _redirect_index_with_context("File not found for delete", next_query)

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

# Home Document Ops Hub

An AI-powered document filing system for Raspberry Pi. Drop scanned PDFs into an inbox folder; the system OCRs them, classifies them with a local LLM, and automatically files them into a folder hierarchy — with a clean web UI for review, manual overrides, and administration.

This code is provided as-is, without warranty or guarantee of fitness, security, or reliability; you are responsible for your own deployment, access controls, backups, and overall security integrity.

---

## Features

- **Watch & auto-file** — monitors `inbox/` for new PDFs, OCRs them, classifies with LLM, files high-confidence results automatically
- **Intelligent PDF splitting** — detects multi-document batch scans and splits before filing
- **Web management UI** — review, search, reclassify, and move documents without touching the filesystem
- **Live queue** — scanner upload queue polled every 5 seconds (no page reload)
- **Directory tree browser** — expandable tree view of your archive with client-side filter
- **Editable keywords** — override the auto-extracted OCR keywords per document
- **Setup modal** — configure NAS Samba mount and scanner share directly from the browser
- **Backup & restore** — export a tar.gz of all config and runtime state; restore on a new instance
- **LLM-agnostic** — works with Ollama, llama.cpp, or any CLI tool
- **Keyword fallback** — if LLM fails, a keyword classifier provides a best-guess category

---

## Folder Layout

```
scanner/
├── inbox/          scanner drop target
├── processing/     temporary work area (auto-cleared)
├── archive/        final filed documents
├── review/         low-confidence files for manual triage
├── rejected/       files that failed processing
├── state/          keyword index, event log, progress state
├── src/            Python package (scanner_filer)
├── systemd/        service unit files
├── install.sh      one-shot installer
├── config.example.yaml
├── setup_smb_mount.sh          mount a NAS share
└── setup_scanner_drop_share.sh expose inbox as SMB share for scanner
```

---

## Requirements

**System packages** (installed by `install.sh`):

```
python3-venv  python3-pip  tesseract-ocr  ocrmypdf  poppler-utils
cifs-utils    samba        samba-common-bin  acl
```

**Python** ≥ 3.10

**LLM** — one of:
- [Ollama](https://ollama.com/) with a model such as `qwen2.5:3b-instruct` (recommended)
- [llama.cpp](https://github.com/ggerganov/llama.cpp) binary (`llama-completion`)

---

## Install

```bash
git clone https://github.com/oooshk/scanner-ai-filer.git <SCANNER_DIR>
cd <SCANNER_DIR>
chmod +x install.sh
./install.sh
```

The installer creates the virtualenv, installs Python dependencies, copies `config.example.yaml` → `config.yaml`, and creates the runtime directories.

---

## Configure

Edit `config.yaml`:

```yaml
paths:
  inbox:      <SCANNER_DIR>/inbox
  processing: <SCANNER_DIR>/processing
  # Point archive/review/rejected at a NAS mount for network-attached storage:
  archive:    /mnt/nas/Home Filing/archive
  review:     /mnt/nas/Home Filing/review
  rejected:   /mnt/nas/Home Filing/rejected
  state:      <SCANNER_DIR>/state

llm:
  enabled: true
  # Ollama (recommended):
  command_template: ollama run qwen2.5:3b-instruct
```

Key options:

| Field | Purpose |
|---|---|
| `llm.command_template` | LLM command; use `{prompt}` placeholder for llama.cpp-style CLI |
| `llm.min_confidence_autofile` | Confidence threshold for auto-filing (e.g. `0.60`) |
| `rules.allowed_doc_types` | List of accepted category names |
| `inbox_settle_seconds` | Wait before processing (prevents truncated network uploads) |
| `require_size_stability` | Skip files until size is stable across watcher cycles |
| `splitter.*` | Controls intelligent batch-scan splitting |

See `config.example.yaml` for the full annotated reference.

---

## NAS Setup (optional)

Mount a Samba/CIFS NAS share to store the archive on network storage:

```bash
chmod +x setup_smb_mount.sh
./setup_smb_mount.sh
# or non-interactively (e.g. from the web UI Setup modal):
./setup_smb_mount.sh --non-interactive \
  --nas-host 192.168.1.10 --nas-share Public \
  --nas-user <nas_user> --mount-point /mnt/nas \
  --subdir "Filing" --password "<nas_password>"
```

---

## Scanner Drop Share (optional)

Expose `inbox/` as a Samba share so the scanner can write directly to the Pi:

```bash
chmod +x setup_scanner_drop_share.sh
./setup_scanner_drop_share.sh
# or non-interactively:
./setup_scanner_drop_share.sh --non-interactive \
  --scanner-user scanner_user --password <scanner_share_password> \
  --inbox-dir <SCANNER_DIR>/inbox \
  --share-name scanner_inbox
```

Point your scanner at `\\<PI_IP>\scanner_inbox`.

---

## Run

### One-shot test

```bash
cd <SCANNER_DIR>
source .venv/bin/activate
export PYTHONPATH=<SCANNER_DIR>/src
python -m scanner_filer.cli --config config.yaml run-once
```

### Continuous watcher

```bash
python -m scanner_filer.cli --config config.yaml watch
```

### Web UI

```bash
python -m scanner_filer.web --config config.yaml --host 0.0.0.0 --port 8090
```

Open `http://<PI_IP>:8090` from any device on your LAN.

---

## systemd Services

```bash
chmod +x scripts/install_systemd_services.sh
./scripts/install_systemd_services.sh

# Optional overrides (examples):
# SCANNER_DIR=/opt/scanner-filer SCANNER_USER=scanner ./scripts/install_systemd_services.sh
# WEB_HOST=0.0.0.0 WEB_PORT=8090 WEB_ALLOWED_NETS="127.0.0.1/32,::1/128,192.168.0.0/16" ./scripts/install_systemd_services.sh
```

Both services restart automatically on failure.

---

## Web UI Overview

| Section | What it does |
|---|---|
| **Queue** | Live view of files currently being processed (auto-refreshes every 5 s) |
| **Documents** | Searchable table of archive/review/rejected files; click a row to expand actions |
| **Per-row actions** | Move to bucket, add category, edit keywords, manual split, delete |
| **Directory tree** | Expandable tree of all archived files with real-time filter |
| **Search** | Filter by filename, sender, category, path, or OCR keywords |
| **Setup (modal)** | Configure NAS mount and scanner share; run scripts from the browser |
| **Backup / Restore** | Download a tar.gz snapshot of config + state; restore on a new Pi |

---

## Backup & Restore

From the Setup modal in the web UI:

- **Download Full Backup** — creates `scanner-backup-<timestamp>.tar.gz` containing `config.yaml`, the Python source, setup scripts, and all runtime state (keyword index, event log). Optionally includes the document archives.
- **Restore Backup** — upload the tar.gz on a fresh install to reinstate config and state.

---

## Intelligent PDF Splitting

When a scanner creates one PDF per scan session (multiple physical documents), the splitter can break it into parts before classification.

Boundary detection uses:
- `Page 1 of N` restarts
- Header keywords (`statement`, `invoice`, `council tax`, etc.)
- Date/account-style first-page patterns

Tune in `config.yaml`:

```yaml
splitter:
  enabled: true
  min_pages_to_split: 3
  max_first_page_chars: 700
  boundary_keywords:
    - statement
    - invoice
    - receipt
    - council tax
    - pension
```

Manual split is also available per-document from the web UI.

---

## LLM Command Examples

### Ollama

```yaml
llm:
  command_template: ollama run qwen2.5:3b-instruct
```

### llama.cpp

```yaml
llm:
  command_template: >-
    /opt/llama.cpp/build/bin/llama-completion
    -m /opt/models/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf
    -n 128 --temp 0 --ctx-size 2048
    --json-schema '{"type":"object","required":["doc_type","vendor_or_sender","date","tags","confidence","reason"],...}'
    -p "{prompt}"
```

---

## Notes

- If `ocrmypdf` is not installed, OCR is skipped and classification falls back to existing text layer.
- If the LLM fails or times out, a keyword-based fallback classifier is used.
- Duplicate filenames are resolved automatically (`file_1.pdf`, `file_2.pdf`, …).
- Only PDF files are processed; other formats are ignored.

---

## License

MIT — see [LICENSE](LICENSE).

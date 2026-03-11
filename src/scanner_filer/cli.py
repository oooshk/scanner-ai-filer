from __future__ import annotations

import argparse
from pathlib import Path

from .config import ensure_directories, load_config
from .logging_utils import configure_logging
from .watcher import process_pending, run_polling_watcher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scanner AI filing pipeline")
    parser.add_argument("--config", required=True, help="Path to config YAML")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-once", help="Process all pending files once")
    sub.add_parser("watch", help="Run continuous watcher loop")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    ensure_directories(cfg)
    configure_logging(cfg.log_level)

    if args.command == "run-once":
        process_pending(cfg)
        return 0

    run_polling_watcher(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

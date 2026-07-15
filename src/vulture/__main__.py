"""Entry point: python -m vulture {scan|cramer}  (or `vulture ...` if installed)."""

import argparse
import logging
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="vulture",
        description="Vulture: options-focused trending-stock scanner.",
    )
    parser.add_argument(
        "command", choices=["scan", "cramer", "daemon"],
        help="scan: one pipeline run. cramer: one Mad Money digest run. "
             "daemon: long-running loop (scan every SCAN_INTERVAL_MIN, Cramer daily).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    from . import config

    try:
        config.validate_env(args.command)
    except ValueError as e:
        logging.getLogger(__name__).error("%s", e)
        return 2

    if args.command == "scan":
        from .pipeline import run_scan
        run_scan()
    elif args.command == "cramer":
        from .trackers.cramer import run_cramer_tracker
        run_cramer_tracker()
    else:
        from .daemon import run_daemon
        run_daemon()
    return 0


if __name__ == "__main__":
    sys.exit(main())

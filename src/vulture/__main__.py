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
        "command", choices=["scan", "cramer"],
        help="scan: Reddit/Stocktwits -> score -> Discord. cramer: Mad Money recap digest.",
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
    else:
        from .trackers.cramer import run_cramer_tracker
        run_cramer_tracker()
    return 0


if __name__ == "__main__":
    sys.exit(main())

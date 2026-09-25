"""Initialize a local SQLite database from the checked-in migration."""

from __future__ import annotations

import argparse
from pathlib import Path

from backend.app.services.storage import PokerStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the poker database")
    parser.add_argument(
        "--path", type=Path, default=Path("database/texas_holdem.sqlite3"),
        help="SQLite file path (default: database/texas_holdem.sqlite3)",
    )
    args = parser.parse_args()
    store = PokerStore(args.path)
    store.initialize()
    print(f"Database ready: {store.path.resolve()}")


if __name__ == "__main__":
    main()

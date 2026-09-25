"""Create verified online SQLite backups and restore them to an empty path."""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _verified_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"Database file does not exist: {path}")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError(f"SQLite integrity check failed: {path}")
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rooms'"
        ).fetchone() is None:
            raise ValueError(f"File is not a Texas Hold'em database: {path}")
    except Exception:
        connection.close()
        raise
    return connection


def _temporary_file(directory: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        prefix=".texas_holdem-", suffix=".partial", dir=directory, delete=False
    ) as temporary:
        return Path(temporary.name)


def backup_database(source: Path, backup_dir: Path) -> Path:
    """Take a consistent snapshot while the poker service may be running."""
    source = source.expanduser().resolve()
    backup_dir = backup_dir.expanduser().resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    filename = ("texas_holdem-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + "-" + uuid4().hex[:8] + ".sqlite3")
    destination = backup_dir / filename
    temporary = _temporary_file(backup_dir)
    try:
        with closing(_verified_connection(source)) as original:
            with closing(sqlite3.connect(temporary)) as snapshot:
                original.backup(snapshot, pages=100, sleep=0.1)
        with closing(_verified_connection(temporary)):
            pass
        os.link(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def restore_database(backup: Path, target: Path) -> Path:
    """Restore a checked snapshot without ever overwriting an existing DB."""
    backup = backup.expanduser().resolve()
    target = target.expanduser().absolute()
    if backup == target:
        raise ValueError("Backup and restore target must differ")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Restore target already exists: {target}")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            raise FileExistsError(f"Remove stale SQLite sidecar only after inspection: {sidecar}")
    if not target.parent.is_dir():
        raise FileNotFoundError(f"Restore directory does not exist: {target.parent}")
    temporary = _temporary_file(target.parent)
    try:
        with closing(_verified_connection(backup)) as original:
            with closing(sqlite3.connect(temporary)) as restored:
                original.backup(restored, pages=100, sleep=0.1)
        with closing(_verified_connection(temporary)):
            pass
        # Linking only succeeds when target is still absent. It also makes the
        # already-complete database appear at the target path atomically.
        os.link(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="Create an online backup")
    backup.add_argument("--source", type=Path, default=os.environ.get("TEXAS_DB_PATH"))
    backup.add_argument("--backup-dir", type=Path,
                        default=os.environ.get("TEXAS_BACKUP_DIR"))
    restore = commands.add_parser("restore", help="Restore into a nonexistent path")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "backup":
        if args.source is None or args.backup_dir is None:
            parser.error("backup requires --source and --backup-dir or both environment variables")
        result = backup_database(Path(args.source), Path(args.backup_dir))
    else:
        result = restore_database(args.backup, args.target)
    print(result.resolve())


if __name__ == "__main__":
    main()

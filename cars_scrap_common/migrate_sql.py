from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv as _load_dotenv
except ModuleNotFoundError:  # pragma: no cover - depends on local environment
    _load_dotenv = None


SOURCE_ORDER = ("mudahmy", "carlist", "mobil123", "carmudi", "olx", "carsomeid", "goonet")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_env(path: Path) -> None:
    if _load_dotenv is not None:
        _load_dotenv(path, override=True)
        return

    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def _source_from_sql_name(name: str) -> str:
    if name.startswith("price_history_scrap_"):
        source = name.removeprefix("price_history_scrap_").removesuffix(".sql")
        return "carlist" if source == "carlistmy" else source
    if name.startswith("cars_scrap_"):
        source = name.removeprefix("cars_scrap_").removesuffix("_details.sql").removesuffix(".sql")
        return "carlist" if source == "carlistmy" else source
    return name.removesuffix(".sql")


def sql_sort_key(path: Path) -> tuple[int, str, int, str]:
    name = path.name
    source = _source_from_sql_name(name)
    try:
        source_rank = SOURCE_ORDER.index(source)
        source_name = ""
    except ValueError:
        source_rank = len(SOURCE_ORDER)
        source_name = source

    if name.startswith("cars_scrap_") and not name.endswith("_details.sql"):
        kind_rank = 0
    elif name.startswith("cars_scrap_") and name.endswith("_details.sql"):
        kind_rank = 1
    elif name.startswith("price_history_scrap_"):
        kind_rank = 2
    else:
        kind_rank = 3
    return source_rank, source_name, kind_rank, name


def discover_sql_files(sql_dir: Path) -> list[Path]:
    return sorted(sql_dir.glob("*.sql"), key=sql_sort_key)


def require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(f"ERROR: {name} is not set (check .env or environment).")
    return value


def build_psql_command(sql_file: Path) -> list[str]:
    psql = shutil.which("psql")
    if not psql:
        raise SystemExit("ERROR: psql command not found. Install PostgreSQL client tools first.")

    db_host = (os.getenv("DB_HOST") or "").strip()
    db_port = (os.getenv("DB_PORT") or "").strip()
    db_name = require_env("DB_NAME")
    db_user = require_env("DB_USER")

    cmd = [psql, "-v", "ON_ERROR_STOP=1"]
    if db_host:
        cmd.extend(["-h", db_host])
    if db_port:
        cmd.extend(["-p", db_port])
    cmd.extend(["-U", db_user, "-d", db_name, "-f", str(sql_file)])
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(description="Run all SQL schema migrations in sql/.")
    parser.add_argument("--sql-dir", default="sql", help="Directory containing .sql files. Default: sql")
    parser.add_argument("--dry-run", action="store_true", help="Print ordered SQL files without running psql")
    args = parser.parse_args()

    root = repo_root()
    load_env(root / ".env")

    sql_dir = Path(args.sql_dir)
    if not sql_dir.is_absolute():
        sql_dir = root / sql_dir
    if not sql_dir.is_dir():
        raise SystemExit(f"ERROR: SQL directory not found: {sql_dir}")

    sql_files = discover_sql_files(sql_dir)
    if not sql_files:
        raise SystemExit(f"ERROR: No .sql files found in {sql_dir}")

    print("SQL files to run:")
    for sql_file in sql_files:
        print(f"- {sql_file.relative_to(root)}")

    if args.dry_run:
        return 0

    db_password = (os.getenv("DB_PASSWORD") or "").strip()
    if db_password:
        os.environ["PGPASSWORD"] = db_password

    for sql_file in sql_files:
        print(f"\nRunning {sql_file.relative_to(root)}")
        subprocess.run(build_psql_command(sql_file), check=True)

    print("\nAll SQL migrations completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

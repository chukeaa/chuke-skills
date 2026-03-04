#!/usr/bin/env python3
"""Fetch journal rows from PostgreSQL and export JSON payload."""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

DB_REQUIRED_ENV_KEYS = {
    "host": "KB_DB_HOST",
    "port": "KB_DB_PORT",
    "database": "KB_DB_NAME",
    "user": "KB_DB_USER",
    "password": "KB_DB_PASSWORD",
}


def _load_dotenv_if_exists() -> None:
    env_candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]
    env_path = next((p for p in env_candidates if p.exists()), None)
    if not env_path:
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
            value = value[1:-1]

        if value == "":
            continue

        if key not in os.environ:
            os.environ[key] = value


def _get_db_config() -> Dict[str, object]:
    missing = [env_key for env_key in DB_REQUIRED_ENV_KEYS.values() if not os.environ.get(env_key, "").strip()]
    if missing:
        raise RuntimeError(
            "Missing database env variables: " + ", ".join(missing) + ". You can set them in .env."
        )

    port_raw = os.environ[DB_REQUIRED_ENV_KEYS["port"]].strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"KB_DB_PORT must be an integer, got: {port_raw!r}") from exc

    return {
        "host": os.environ[DB_REQUIRED_ENV_KEYS["host"]].strip(),
        "port": port,
        "database": os.environ[DB_REQUIRED_ENV_KEYS["database"]].strip(),
        "user": os.environ[DB_REQUIRED_ENV_KEYS["user"]].strip(),
        "password": os.environ[DB_REQUIRED_ENV_KEYS["password"]].strip(),
    }


def _to_json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _load_rows(start_time: datetime) -> List[Dict]:
    try:
        import psycopg2
    except Exception as exc:
        raise RuntimeError("Missing psycopg2 SDK. Install with `pip install psycopg2-binary`.") from exc

    db_cfg = _get_db_config()
    with psycopg2.connect(**db_cfg) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT doi, title, abstract, date
            FROM journals
            WHERE created_at >= %s
            ORDER BY created_at DESC
            """,
            (start_time,),
        )
        rows = cur.fetchall()

    payload: List[Dict] = []
    for doi, title, abstract, date in rows:
        payload.append(
            {
                "doi": (doi or "").strip(),
                "title": (title or "").strip(),
                "abstract": _to_json_safe(abstract),
                "date": _to_json_safe(date),
            }
        )
    return payload


def main() -> int:
    _load_dotenv_if_exists()

    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="Fetch rows where created_at >= now_utc - days")
    parser.add_argument("--output", type=str, default="selected-abstract.json", help="Output JSON path")
    args = parser.parse_args()

    if args.days < 0:
        raise RuntimeError("--days must be >= 0")

    window_start = datetime.now(timezone.utc) - timedelta(days=args.days)
    payload = _load_rows(window_start)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

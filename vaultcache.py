"""Generic sqlite "vault" cache.

A vault is a committed sidecar DB that keeps ORIGINAL rows so the throwaway
build DB can be seeded from it (``restore``) and newly produced rows persisted
back (``sync``) -- no re-download, and a durable backup. Keyed by one unique
column. Used for ``attachment`` (originals) and any future cached table.
"""
from __future__ import annotations

import os
import sqlite3


def _common_cols(conn: sqlite3.Connection, table: str,
                 a: str = "main", b: str = "vault") -> str:
    ca = [r[1] for r in conn.execute(f"PRAGMA {a}.table_info({table})")]
    cb = [r[1] for r in conn.execute(f"PRAGMA {b}.table_info({table})")]
    return ",".join(c for c in ca if c in cb and c != "id")


def restore(build_db: str, vault: str, table: str, key: str) -> int:
    """Seed the build DB from the vault (INSERT OR IGNORE on ``key``). Returns
    the number of rows restored, or 0 if the vault does not exist yet."""
    if not os.path.exists(vault):
        return 0
    conn = sqlite3.connect(build_db)
    conn.execute("ATTACH ? AS vault", (vault,))
    cols = _common_cols(conn, table)
    n = conn.execute(f"INSERT OR IGNORE INTO {table} ({cols}) "
                     f"SELECT {cols} FROM vault.{table}").rowcount
    conn.commit()
    conn.close()
    return n


def sync(build_db: str, vault: str, table: str, key: str) -> int:
    """Persist new build-DB rows back into the vault (creating the vault table on
    first use). Counts new rows first and writes ONLY when there are some, so a
    no-op run leaves the committed vault file byte-identical (no junk git commit).
    Returns the number of new rows."""
    conn = sqlite3.connect(build_db)
    conn.execute("ATTACH ? AS vault", (vault,))
    if not conn.execute("SELECT 1 FROM vault.sqlite_master "
                        "WHERE type='table' AND name=?", (table,)).fetchone():
        ddl = conn.execute("SELECT sql FROM main.sqlite_master "
                           "WHERE type='table' AND name=?", (table,)).fetchone()[0]
        conn.execute(ddl.replace("CREATE TABLE", "CREATE TABLE vault.", 1))
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS "
                     f"vault.idx_{table}_{key} ON {table}({key})")
    cols = _common_cols(conn, table)
    n = conn.execute(f"SELECT COUNT(*) FROM main.{table} WHERE {key} NOT IN "
                     f"(SELECT {key} FROM vault.{table})").fetchone()[0]
    if n:
        conn.execute(f"INSERT OR IGNORE INTO vault.{table} ({cols}) "
                     f"SELECT {cols} FROM main.{table}")
        conn.commit()
    conn.close()
    return n

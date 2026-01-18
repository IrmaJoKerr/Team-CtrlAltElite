import os
import logging
import psycopg2
from typing import Optional
from typing import List, Dict, Iterable
from psycopg2.extras import execute_values


def get_db_connection() -> "psycopg2.extensions.connection":
    """Create and return a psycopg2 DB connection with local-first secrets.

    Behavior mirrors the previous inline helper:
    - Use `DB_PASSWORD` env var when present.
    - If absent and `MODE=cloud`, attempt to fetch via `adapters.secrets_adapter.get_db_password`.
    - If a Cloud SQL unix socket is specified via `CLOUD_SQL_CONNECTION_NAME` and DB_HOST is empty,
      use `/cloudsql/{CLOUD_SQL_CONNECTION_NAME}` as host.
    """
    try:
        DB_HOST = os.environ.get("DB_HOST")
        DB_USER = os.environ.get("DB_USER", "postgres")
        DB_NAME = os.environ.get("DB_NAME", "docintel_db")
        DB_PASSWORD = os.environ.get("DB_PASSWORD")

        host = DB_HOST
        cloud_sql_conn_name = os.environ.get("CLOUD_SQL_CONNECTION_NAME")
        if not host and cloud_sql_conn_name:
            host = f"/cloudsql/{cloud_sql_conn_name}"

        pw = DB_PASSWORD
        MODE = os.environ.get("MODE")
        CLOUD_MODE = True if MODE == "cloud" else False

        if not pw:
            if CLOUD_MODE:
                try:
                    from adapters.secrets_adapter import get_db_password

                    pw = get_db_password(cloud_mode=True, config=None)
                except Exception as e:
                    logging.error(
                        "Failed to obtain DB password from secrets adapter: %s", e
                    )
                    raise RuntimeError(
                        "Cloud mode selected but DB password unavailable. Set SECRET_PROVIDER or DB_PASSWORD env var."
                    )
            else:
                raise RuntimeError(
                    "DB_PASSWORD not set. Set DB_PASSWORD in the environment or run with MODE=cloud and configure a secret provider."
                )

        conn = psycopg2.connect(host=host, user=DB_USER, password=pw, dbname=DB_NAME)
        return conn
    except Exception as e:
        logging.error(f"Failed to connect to database: {e}", exc_info=True)
        raise


def upsert_chunk_records(
    records: List[Dict],
    table: str = "documents",
    conflict_keys: Iterable[str] = ("original_gcs_filename", "chunk_index"),
) -> int:
    """Upsert multiple chunk/document records into `table` using a single multi-row
    INSERT with ON CONFLICT DO UPDATE.

    - `records` is a list of dicts where keys are column names and values are the
      column values. If a record contains `embedding_vector` as a list, it will be
      converted to the textual vector literal (e.g. "[0.1,0.2,...]") and inserted
      using the `::vector` cast in the VALUES template.
    - `conflict_keys` defines the unique constraint keys to use for ON CONFLICT.

    Returns the number of rows inserted/updated.
    """
    if not records:
        return 0

    # Normalize keys/order from the first record
    columns = list(records[0].keys())

    # Prepare template for execute_values; cast embedding_vector to vector if present
    def templ_col(col: str) -> str:
        if col == "embedding_vector":
            return f"%({col})s::vector"
        return f"%({col})s"

    values_template = "(" + ",".join(templ_col(c) for c in columns) + ")"

    # Build ON CONFLICT ... DO UPDATE SET ... (skip updating conflict keys)
    conflict_keys_list = list(conflict_keys)
    update_cols = [c for c in columns if c not in conflict_keys_list]
    if update_cols:
        update_sql = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
    else:
        # If nothing to update, do nothing on conflict
        update_sql = "NOTHING"

    cols_sql = ", ".join(columns)
    conflict_sql = ", ".join(conflict_keys_list)

    sql = f"INSERT INTO {table} ({cols_sql}) VALUES %s ON CONFLICT ({conflict_sql}) DO UPDATE SET {update_sql}"

    # Convert any embedding lists to pgvector literal strings (e.g. [0.1,0.2])
    prepared = []
    for rec in records:
        r = dict(rec)
        if "embedding_vector" in r and isinstance(r["embedding_vector"], (list, tuple)):
            # Format floats with full precision; pgvector accepts '[x,y,...]'
            r["embedding_vector"] = (
                "[" + ",".join(str(float(x)) for x in r["embedding_vector"]) + "]"
            )
        prepared.append(r)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        execute_values(cur, sql, prepared, template=values_template)
        conn.commit()
        return len(prepared)
    except Exception:
        conn.rollback()
        logging.exception("Failed to upsert chunk records")
        raise
    finally:
        cur.close()
        conn.close()

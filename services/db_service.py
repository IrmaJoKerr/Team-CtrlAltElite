import os
import logging
import psycopg2
import json
import hashlib
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


def log_query_activity(
    session_id: str,
    current_user: dict,
    query_text: str,
    query_timestamp,
    response_timestamp,
    status: str,
    confidence_score: float,
    gaps: list,
    sources: list,
    summary_answer: str,
    steps: list,
    model_used: str,
):
    """Log query session, results, audits, and cache into the DB.

    This consolidates the inline SQL previously present in `main.py` so callers
    can perform a single high-level call.
    """
    try:
        # Session record
        session_rec = {
            "session_id": session_id,
            "user_id": current_user.get("id"),
            "user_role": current_user.get("role"),
            "user_departments": json.dumps(current_user.get("departments")),
            "query_text": query_text,
            "query_hash": hashlib.sha256(query_text.encode()).hexdigest(),
            "query_timestamp": query_timestamp,
            "response_timestamp": response_timestamp,
            "status": status,
            "confidence_score": confidence_score,
            "gaps_identified": json.dumps(gaps),
        }

        upsert_chunk_records([session_rec], table="query_sessions", conflict_keys=("session_id",))

        # Results
        results_recs = []
        for idx, src in enumerate(sources):
            results_recs.append(
                {
                    "session_id": session_id,
                    "sop_id": src.get("sop_id"),
                    "sop_version_id": src.get("version_id"),
                    "chunk_id": src.get("chunk_id"),
                    "relevance_score": src.get("relevance_score"),
                    "citation_index": idx,
                }
            )
        if results_recs:
            # Use a sensible conflict key combination for query_results
            upsert_chunk_records(results_recs, table="query_results", conflict_keys=("session_id","sop_id","chunk_id"))

        # Audit log entries (simple inserts; audit events typically append)
        if gaps:
            conn = get_db_connection()
            cur = conn.cursor()
            try:
                for gap in gaps:
                    cur.execute(
                        """
                        INSERT INTO query_audit_log (session_id, event_type, event_details, severity)
                        VALUES (%s, %s, %s, %s)
                    """,
                        (
                            session_id,
                            "coverage_gap" if "gap" in gap.lower() else "info",
                            json.dumps({"message": gap}),
                            "warning" if "gap" in gap.lower() else "info",
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                logging.exception("Failed to insert audit log entries")
            finally:
                cur.close()
                conn.close()

        # Cache response
        cache_rec = {
            "session_id": session_id,
            "summary_answer": summary_answer,
            "steps": json.dumps([s.dict() if hasattr(s, "dict") else s for s in (steps or [])]),
            "full_response": json.dumps({"sources_count": len(sources), "steps_count": len(steps or [])}),
            "model_used": model_used,
        }
        upsert_chunk_records([cache_rec], table="query_response_cache", conflict_keys=("session_id",))

    except Exception:
        logging.exception("Failed to log query activity")
        raise


def create_upload_session_with_metadata(
    session_id: str,
    user_id: Optional[int],
    filename: str,
    original_filename: str,
    ai_metadata: dict,
    model_used: str,
):
    """Create a document upload session and associated metadata draft in a single operation."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO document_uploads (session_id, user_id, filename, original_filename, upload_status)
                VALUES (%s, %s, %s, %s, 'draft')
            """,
                (session_id, user_id, filename, original_filename),
            )

            cur.execute(
                """
                INSERT INTO upload_metadata_drafts 
                (session_id, extracted_title, extracted_department, extracted_author, extracted_type, 
                 ai_confidence, ai_model_used, confirmed_title, confirmed_department, confirmed_author, confirmed_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    session_id,
                    ai_metadata.get("title", {}).get("suggested_value"),
                    ai_metadata.get("department", {}).get("suggested_value"),
                    "Unknown",
                    ai_metadata.get("process_type", {}).get("suggested_value"),
                    json.dumps(
                        {
                            "title": ai_metadata.get("title", {}).get("confidence_score", 0.0),
                            "department": ai_metadata.get("department", {}).get("confidence_score", 0.0),
                            "type": ai_metadata.get("process_type", {}).get("confidence_score", 0.0),
                        }
                    ),
                    model_used,
                    ai_metadata.get("title", {}).get("suggested_value"),
                    ai_metadata.get("department", {}).get("suggested_value"),
                    "Unknown",
                    ai_metadata.get("process_type", {}).get("suggested_value"),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            logging.exception("Failed to create upload session with metadata")
            raise
        finally:
            cur.close()
            conn.close()
    except Exception:
        logging.exception("Failed to connect while creating upload session")
        raise


def log_upload_event(
    session_id: str,
    user_id: Optional[int],
    event_type: str,
    field_changes: Optional[Dict] = None,
    event_details: Optional[Dict] = None,
):
    """Append an upload audit event to `upload_audit_log`."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO upload_audit_log (session_id, user_id, event_type, field_changes, event_details)
            VALUES (%s, %s, %s, %s, %s)
        """,
            (
                str(session_id),
                user_id,
                event_type,
                json.dumps(field_changes) if field_changes else None,
                json.dumps(event_details) if event_details else None,
            ),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception:
        logging.exception("Failed to log upload event via db_service")
        raise


def update_upload_metadata(session_id: str, edits: Dict[str, Optional[str]]) -> int:
    """Update fields on `upload_metadata_drafts` for a session.

    Returns number of rows updated.
    """
    if not edits:
        return 0
    try:
        fields = []
        values = []
        mapping = {
            "title": ("confirmed_title",),
            "department": ("confirmed_department",),
            "author": ("confirmed_author",),
            "type": ("confirmed_type",),
        }
        for k, v in edits.items():
            if k in mapping and v is not None:
                fields.append(f"{mapping[k][0]} = %s")
                values.append(v)

        if not fields:
            return 0

        values.append(session_id)

        sql = f"UPDATE upload_metadata_drafts SET {', '.join(fields)}, last_edited_at = NOW() WHERE session_id = %s"

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(sql, tuple(values))
            affected = cur.rowcount
            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            logging.exception("Failed to update upload metadata via db_service")
            raise
        finally:
            cur.close()
            conn.close()
    except Exception:
        logging.exception("Failed to prepare update_upload_metadata")
        raise


def update_document_metadata(gcs_object_path: str, metadata_update: Dict[str, str]) -> int:
    """Perform an update on the `documents` table for human-edited metadata fields.

    Returns number of rows updated.
    """
    if not metadata_update:
        return 0
    try:
        update_fields = []
        update_values = []
        allowed = [
            "final_title",
            "final_department",
            "final_process_type",
            "final_status",
        ]
        for field in allowed:
            if field in metadata_update:
                update_fields.append(f"{field} = %s")
                update_values.append(metadata_update[field])

        if not update_fields:
            return 0

        update_values.append(gcs_object_path)

        update_query = f"UPDATE documents SET {', '.join(update_fields)}, updated_at = NOW() WHERE gcs_object_path = %s"

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(update_query, tuple(update_values))
            affected = cur.rowcount
            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            logging.exception("Failed to update document metadata via db_service")
            raise
        finally:
            cur.close()
            conn.close()
    except Exception:
        logging.exception("Failed to prepare update_document_metadata")
        raise


def confirm_document_metadata(gcs_object_path: str, reviewer_username: str) -> int:
    """Mark a document as approved (review) and return the document id.

    Returns the document id on success, or raises on failure.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT id FROM documents WHERE gcs_object_path = %s LIMIT 1",
                (gcs_object_path,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Document not found")
            documents_id = row[0]

            cur.execute(
                "UPDATE documents SET review_status = 'approved', last_reviewed_by = %s, last_reviewed_at = NOW(), updated_at = NOW() WHERE gcs_object_path = %s",
                (reviewer_username, gcs_object_path),
            )
            conn.commit()
            return documents_id
        except Exception:
            conn.rollback()
            logging.exception("Failed to confirm document metadata via db_service")
            raise
        finally:
            cur.close()
            conn.close()
    except Exception:
        logging.exception("Failed to connect while confirming document metadata")
        raise


def discard_upload_session(session_id: str) -> int:
    """Mark an upload session as discarded/is_deleted and return affected rows."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE document_uploads SET upload_status = 'discarded', discarded_at = NOW(), is_deleted = TRUE WHERE session_id = %s",
                (session_id,),
            )
            affected = cur.rowcount
            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            logging.exception("Failed to discard upload session via db_service")
            raise
        finally:
            cur.close()
            conn.close()
    except Exception:
        logging.exception("Failed to connect while discarding upload session")
        raise


def delete_document_record(gcs_object_path: str) -> int:
    """Delete document row(s) for a given gcs_object_path. Returns number deleted."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM documents WHERE gcs_object_path = %s", (gcs_object_path,))
            deleted = cur.rowcount
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            logging.exception("Failed to delete document record via db_service")
            raise
        finally:
            cur.close()
            conn.close()
    except Exception:
        logging.exception("Failed to connect while deleting document record")
        raise

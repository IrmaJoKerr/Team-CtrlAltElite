import os
import logging
import psycopg2
from typing import Optional


def get_db_connection() -> 'psycopg2.extensions.connection':
    """Create and return a psycopg2 DB connection with local-first secrets.

    Behavior mirrors the previous inline helper:
    - Use `DB_PASSWORD` env var when present.
    - If absent and `MODE=cloud`, attempt to fetch via `adapters.secrets_adapter.get_db_password`.
    - If a Cloud SQL unix socket is specified via `CLOUD_SQL_CONNECTION_NAME` and DB_HOST is empty,
      use `/cloudsql/{CLOUD_SQL_CONNECTION_NAME}` as host.
    """
    try:
        DB_HOST = os.environ.get('DB_HOST')
        DB_USER = os.environ.get('DB_USER', 'postgres')
        DB_NAME = os.environ.get('DB_NAME', 'docintel_db')
        DB_PASSWORD = os.environ.get('DB_PASSWORD')

        host = DB_HOST
        cloud_sql_conn_name = os.environ.get('CLOUD_SQL_CONNECTION_NAME')
        if not host and cloud_sql_conn_name:
            host = f"/cloudsql/{cloud_sql_conn_name}"

        pw = DB_PASSWORD
        MODE = os.environ.get('MODE')
        CLOUD_MODE = True if MODE == 'cloud' else False

        if not pw:
            if CLOUD_MODE:
                try:
                    from adapters.secrets_adapter import get_db_password
                    pw = get_db_password(cloud_mode=True, config=None)
                except Exception as e:
                    logging.error('Failed to obtain DB password from secrets adapter: %s', e)
                    raise RuntimeError('Cloud mode selected but DB password unavailable. Set SECRET_PROVIDER or DB_PASSWORD env var.')
            else:
                raise RuntimeError('DB_PASSWORD not set. Set DB_PASSWORD in the environment or run with MODE=cloud and configure a secret provider.')

        conn = psycopg2.connect(
            host=host,
            user=DB_USER,
            password=pw,
            dbname=DB_NAME
        )
        return conn
    except Exception as e:
        logging.error(f"Failed to connect to database: {e}", exc_info=True)
        raise

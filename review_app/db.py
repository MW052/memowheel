"""Shared DB connection for the review app - one connection per request,
matching WAL mode's expectation of short-lived writer transactions rather
than one long-held connection across the whole server lifetime."""

from db.schema import get_connection, DEFAULT_DB_PATH


def get_db():
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()

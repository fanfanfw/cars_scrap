from __future__ import annotations

import os

import psycopg2
from dotenv import load_dotenv

load_dotenv(override=True)


def get_connection():
    db_name = (os.getenv("DB_NAME") or os.getenv("DB_NAME_CARMUDI_CARS") or "").strip()
    if not db_name:
        raise ValueError("Missing DB_NAME (or DB_NAME_CARMUDI_CARS) in environment.")
    conn = psycopg2.connect(
        dbname=db_name,
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
    )
    conn.autocommit = True
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1")
    conn.autocommit = False
    return conn

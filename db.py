import sqlite3
from datetime import datetime, UTC
from pathlib import Path

from models import AvailabilitySlot, RunLog

DB_PATH = Path(__file__).parent / "omakase_monitor.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS availability (
            id INTEGER PRIMARY KEY,
            omakase_code TEXT NOT NULL,
            restaurant_name TEXT,
            slot_date TEXT NOT NULL,
            slot_time TEXT,
            course_name TEXT,
            price_jpy INTEGER,
            status TEXT,
            dedup_key TEXT UNIQUE,
            first_seen TEXT,
            last_seen TEXT,
            notified INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS run_log (
            id INTEGER PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            restaurants_checked INTEGER,
            slots_found INTEGER,
            new_slots INTEGER,
            error_message TEXT
        );
    """)
    conn.commit()
    conn.close()


def save_result(slot: AvailabilitySlot) -> bool:
    """Save availability slot. Returns True if new (inserted), False if updated."""
    conn = get_conn()
    now = datetime.now(UTC).isoformat()
    try:
        conn.execute(
            """INSERT INTO availability
               (omakase_code, restaurant_name, slot_date, slot_time,
                course_name, price_jpy, status, dedup_key, first_seen, last_seen, notified)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (slot.omakase_code, slot.restaurant_name, slot.slot_date,
             slot.slot_time, slot.course_name, slot.price_jpy, slot.status,
             slot.dedup_key, now, now)
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.execute(
            "UPDATE availability SET last_seen = ?, status = ? WHERE dedup_key = ?",
            (now, slot.status, slot.dedup_key)
        )
        conn.commit()
        conn.close()
        return False


def mark_notified(slot: AvailabilitySlot):
    conn = get_conn()
    conn.execute(
        "UPDATE availability SET notified = 1 WHERE dedup_key = ?",
        (slot.dedup_key,)
    )
    conn.commit()
    conn.close()


def save_run_log(log: RunLog) -> int:
    conn = get_conn()
    cursor = conn.execute(
        """INSERT INTO run_log (started_at, finished_at, status, restaurants_checked,
                                slots_found, new_slots, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (log.started_at.isoformat(),
         log.finished_at.isoformat() if log.finished_at else None,
         log.status, log.restaurants_checked, log.slots_found,
         log.new_slots, log.error_message)
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


def update_run_log(row_id: int, log: RunLog):
    conn = get_conn()
    conn.execute(
        """UPDATE run_log SET finished_at = ?, status = ?, restaurants_checked = ?,
           slots_found = ?, new_slots = ?, error_message = ?
           WHERE id = ?""",
        (log.finished_at.isoformat() if log.finished_at else None,
         log.status, log.restaurants_checked, log.slots_found,
         log.new_slots, log.error_message, row_id)
    )
    conn.commit()
    conn.close()


def get_last_run() -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM run_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "started_at": row[1], "finished_at": row[2],
        "status": row[3], "restaurants_checked": row[4],
        "slots_found": row[5], "new_slots": row[6], "error_message": row[7]
    }


def get_recent_availability(limit: int = 10) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT omakase_code, restaurant_name, slot_date, slot_time,
                  course_name, price_jpy, status, first_seen
           FROM availability ORDER BY first_seen DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    return [
        {"omakase_code": r[0], "restaurant_name": r[1], "slot_date": r[2],
         "slot_time": r[3], "course_name": r[4], "price_jpy": r[5],
         "status": r[6], "first_seen": r[7]}
        for r in rows
    ]

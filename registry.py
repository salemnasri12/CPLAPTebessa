"""سجل تسجيلات العمال — لجنة المشاركة المحلية لبريد تبسة.

يخزّن بيانات المرسلين قبل السماح لهم بمراسلة الصفحة:
الاسم، اللقب، الرتبة، مكان العمل + حالة التحقق (pending/approved/rejected).

التخزين: SQLite محلي (REGISTRY_PATH). للاستخدام الرسمي المكثف يُفضّل
ربط قاعدة بيانات دائمة، مع إمكانية تصدير CSV من لوحة الإدارة.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("REGISTRY_PATH", str(BASE_DIR / "data" / "registry.db")))
SECRET = (
    os.environ.get("REGISTRY_SECRET")
    or os.environ.get("VERIFY_TOKEN")
    or "barid_tebessa_registry"
)

STATUSES = ("pending", "approved", "rejected")

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                psid TEXT PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                grade TEXT NOT NULL,
                workplace TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            )
            """
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(psid: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM registrations WHERE psid = ?", (psid,)
        ).fetchone()
    return dict(row) if row else None


def status_of(psid: str) -> str | None:
    row = get(psid)
    return row["status"] if row else None


def is_approved(psid: str) -> bool:
    return status_of(psid) == "approved"


def register(
    psid: str,
    first_name: str,
    last_name: str,
    grade: str,
    workplace: str,
) -> dict:
    """يسجّل أو يحدّث بيانات المرسل، ويعيد الحالة إلى pending عند التعديل."""
    values = (
        first_name.strip()[:100],
        last_name.strip()[:100],
        grade.strip()[:100],
        workplace.strip()[:120],
        "pending",
        _now(),
    )
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO registrations
                (psid, first_name, last_name, grade, workplace, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(psid) DO UPDATE SET
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                grade = excluded.grade,
                workplace = excluded.workplace,
                status = 'pending',
                note = '',
                reviewed_at = NULL
            """,
            (psid, *values),
        )
    return get(psid) or {}


def decide(psid: str, status: str, note: str = "") -> bool:
    if status not in STATUSES:
        raise ValueError("حالة غير صالحة")
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE registrations SET status = ?, note = ?, reviewed_at = ? "
            "WHERE psid = ?",
            (status, note.strip()[:300], _now(), psid),
        )
        return cur.rowcount > 0


def delete(psid: str) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM registrations WHERE psid = ?", (psid,))
        return cur.rowcount > 0


def list_all(status: str | None = None) -> list[dict]:
    query = "SELECT * FROM registrations"
    params: tuple = ()
    if status in STATUSES:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY created_at DESC"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def counts() -> dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS total FROM registrations GROUP BY status"
        ).fetchall()
    data = {status: 0 for status in STATUSES}
    for row in rows:
        data[row["status"]] = row["total"]
    data["total"] = sum(data[status] for status in STATUSES)
    return data


# ------------------------------------------------------------------ رموز الربط
def sign(psid: str) -> str:
    """رمز موقّع يربط رابط التسجيل بحساب المرسل في ماسنجر."""
    raw = psid.encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    mac = hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()[:32]
    return f"{body}.{mac}"


def unsign(token: str) -> str | None:
    try:
        body, mac = token.split(".", 1)
    except ValueError:
        return None
    padding = "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(body + padding)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, mac):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

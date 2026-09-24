"""
Telegram Q&A Bot Template
Students send questions via private chat; questions are forwarded to the class
group with the sender's profile. Replies from the group are routed back privately.

Configuration (set as environment variables or edit the constants below):
  BOT_TOKEN   – Telegram bot token from @BotFather
  GROUP_ID    – Telegram chat_id of the class group (negative number, e.g. -100123456789)
"""

import os
import time
import random
import sqlite3
import asyncio
import threading
import logging
import inspect
import hmac
from html import escape
import httpx
from flask import Flask, request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup

try:
    from telegram import ReactionTypeEmoji
except ImportError:
    ReactionTypeEmoji = None
from telegram.helpers import mention_html
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is not set. "
        "Get a token from @BotFather and set it as an environment variable."
    )
GROUP_ID: int = int(os.environ.get("GROUP_ID", "0"))
# Channel username (e.g. "@mychannel") or numeric ID that users must subscribe to
CHANNEL_ID: str = os.environ.get("CHANNEL_ID", "").strip()
CHANNEL_URL: str = os.environ.get("CHANNEL_URL", "").strip()

QUESTION_COOLDOWN: int = int(os.environ.get("QUESTION_COOLDOWN", "600"))  # seconds between questions per user
MAX_QUESTIONS: int = int(os.environ.get("MAX_QUESTIONS", "50"))  # lifetime question limit per user
# Your hosting domain — set via environment variable on Railway/PythonAnywhere
DOMAIN: str = os.environ.get("DOMAIN", "")
WEBHOOK_SECRET_TOKEN: str = os.environ.get("WEBHOOK_SECRET_TOKEN", "")

DB_PATH: str = os.environ.get("DB_PATH", "bot_data.db")

ALLMEMBERS_PAGE_SIZE = 20

# Rate Limiting
RATE_LIMIT_MESSAGES = 3      # Maximum number of messages allowed per time window.
RATE_LIMIT_SECONDS = 8       # Length of each rate-limit time window in seconds.

# In-memory rate limit tracker: user_id -> list of timestamps
_user_message_times: dict[int, list[float]] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _patch_httpx_compat() -> None:
    """Make newer httpx versions compatible with python-telegram-bot 21.x."""
    if "proxies" in inspect.signature(httpx.AsyncClient.__init__).parameters:
        return
    original_init = httpx.AsyncClient.__init__

    def compat_init(self, *args, **kwargs):
        kwargs.pop("proxies", None)
        return original_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = compat_init


_patch_httpx_compat()

# In-memory cooldown tracker: user_id -> timestamp of last question
_last_question: dict[int, float] = {}

# In-memory pending answer confirmations: confirm_id -> dict of answer data
_pending_answers: dict[str, dict] = {}

# In-memory pending broadcast confirmations
_pending_broadcasts: dict[str, dict] = {}

# In-memory admin quiz authoring state: admin_id -> draft payload
_quiz_authoring: dict[int, dict] = {}

# In-memory active quiz sessions for faster callback handling
_quiz_sessions: dict[int, dict] = {}

QUIZ_RESPONSE_LABELS = {
    "knew": "بلد بودم",
    "almost": "تقریبا بلد بودم",
    "not": "نبودم",
}

# ---------------------------------------------------------------------------
# Persistent async event loop (runs in a background thread so Flask's sync
# request handlers can safely call async PTB code)
# ---------------------------------------------------------------------------
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()


def run_async(coro):
    """Run a coroutine on the persistent event loop and block until done."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=30)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create tables if they don't exist."""
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                tracking_id      TEXT PRIMARY KEY,
                student_id       INTEGER NOT NULL,
                question_preview TEXT NOT NULL DEFAULT '',
                answered         INTEGER NOT NULL DEFAULT 0,
                answered_by      INTEGER,
                answered_by_name TEXT NOT NULL DEFAULT '',
                answered_by_username TEXT NOT NULL DEFAULT ''
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                group_message_id  INTEGER PRIMARY KEY,
                tracking_id       TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS destination_groups (
                chat_id INTEGER PRIMARY KEY,
                added_at INTEGER NOT NULL
            )
        """)
        if GROUP_ID:
            con.execute(
                "INSERT OR IGNORE INTO destination_groups (chat_id, added_at) VALUES (?, ?)",
                (GROUP_ID, int(time.time())),
            )
        con.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                student_id  INTEGER PRIMARY KEY
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                student_id  INTEGER PRIMARY KEY
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS student_messages (
                student_msg_id  INTEGER NOT NULL,
                student_id      INTEGER NOT NULL,
                group_msg_id    INTEGER NOT NULL,
                PRIMARY KEY (student_msg_id, student_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS quiz_grades (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE,
                created_by INTEGER,
                created_at INTEGER NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS quiz_courses (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                grade_id   INTEGER NOT NULL,
                name       TEXT NOT NULL,
                created_by INTEGER,
                created_at INTEGER NOT NULL,
                UNIQUE(grade_id, name),
                FOREIGN KEY (grade_id) REFERENCES quiz_grades(id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS quiz_topics (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id  INTEGER NOT NULL,
                name       TEXT NOT NULL,
                created_by INTEGER,
                created_at INTEGER NOT NULL,
                UNIQUE(course_id, name),
                FOREIGN KEY (course_id) REFERENCES quiz_courses(id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS quiz_questions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id         INTEGER NOT NULL,
                question_type    TEXT NOT NULL,
                question_text    TEXT NOT NULL DEFAULT '',
                question_file_id TEXT NOT NULL DEFAULT '',
                question_caption TEXT NOT NULL DEFAULT '',
                answer_type      TEXT NOT NULL DEFAULT 'none',
                answer_text      TEXT NOT NULL DEFAULT '',
                answer_file_id   TEXT NOT NULL DEFAULT '',
                answer_caption   TEXT NOT NULL DEFAULT '',
                answer_published INTEGER NOT NULL DEFAULT 0,
                created_by       INTEGER NOT NULL,
                created_at       INTEGER NOT NULL,
                FOREIGN KEY (topic_id) REFERENCES quiz_topics(id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS quiz_sessions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                topic_id     INTEGER NOT NULL,
                current_idx  INTEGER NOT NULL DEFAULT 0,
                total_count  INTEGER NOT NULL DEFAULT 0,
                completed    INTEGER NOT NULL DEFAULT 0,
                started_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL,
                FOREIGN KEY (topic_id) REFERENCES quiz_topics(id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS quiz_user_answers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    INTEGER NOT NULL,
                user_id       INTEGER NOT NULL,
                question_id   INTEGER NOT NULL,
                response_type TEXT NOT NULL,
                answered_at   INTEGER NOT NULL,
                UNIQUE(session_id, question_id),
                FOREIGN KEY (session_id) REFERENCES quiz_sessions(id),
                FOREIGN KEY (question_id) REFERENCES quiz_questions(id)
            )
        """)
        # Migrate existing DBs that lack the new columns
        for col, definition in [
            ("question_preview",   "TEXT NOT NULL DEFAULT ''"),
            ("answered",           "INTEGER NOT NULL DEFAULT 0"),
            ("student_name",       "TEXT NOT NULL DEFAULT ''"),
            ("student_username",   "TEXT NOT NULL DEFAULT ''"),
            ("answered_by",        "INTEGER"),
            ("answered_by_name",   "TEXT NOT NULL DEFAULT ''"),
            ("answered_by_username","TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                con.execute(f"ALTER TABLE questions ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        for col, definition in [
            ("content", "TEXT NOT NULL DEFAULT ''"),
            ("has_media", "INTEGER NOT NULL DEFAULT 0"),
            ("student_id", "INTEGER"),
        ]:
            try:
                con.execute(f"ALTER TABLE messages ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        con.commit()


def db_add_destination_group(chat_id: int) -> bool:
    """Persist group as a destination. Return True if newly added."""
    with sqlite3.connect(DB_PATH) as con:
        cursor = con.execute(
            "INSERT OR IGNORE INTO destination_groups (chat_id, added_at) VALUES (?, ?)",
            (chat_id, int(time.time())),
        )
        con.commit()
    return cursor.rowcount > 0


def db_get_destination_groups() -> list[int]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT chat_id FROM destination_groups ORDER BY added_at, chat_id"
        ).fetchall()
    return [int(row[0]) for row in rows]


def db_save_question(
    tracking_id: str,
    student_id: int,
    question_preview: str = "",
    student_name: str = "",
    student_username: str = "",
) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """INSERT OR IGNORE INTO questions
               (tracking_id, student_id, question_preview, student_name, student_username)
               VALUES (?, ?, ?, ?, ?)""",
            (tracking_id, student_id, question_preview[:100], student_name, student_username),
        )
        con.commit()


def db_save_message(
    group_message_id: int,
    tracking_id: str,
    content: str = "",
    has_media: bool = False,
    student_id: int | None = None,
) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """INSERT OR IGNORE INTO messages
               (group_message_id, tracking_id, content, has_media, student_id)
               VALUES (?, ?, ?, ?, ?)""",
            (group_message_id, tracking_id, content, int(has_media), student_id),
        )
        con.commit()


def db_get_message_content(group_message_id: int) -> tuple[str, bool]:
    """Return (content, has_media) for a group message."""
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT content, has_media FROM messages WHERE group_message_id = ?",
            (group_message_id,),
        ).fetchone()
    return (row[0], bool(row[1])) if row else ("", False)


def db_get_tracking_id(group_message_id: int) -> str | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT tracking_id FROM messages WHERE group_message_id = ?",
            (group_message_id,),
        ).fetchone()
    return row[0] if row else None


def db_get_message_student_id(group_message_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT student_id FROM messages WHERE group_message_id = ?",
            (group_message_id,),
        ).fetchone()
    return row[0] if row and row[0] is not None else None


def db_get_student_id(tracking_id: str) -> int | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT student_id FROM questions WHERE tracking_id = ?",
            (tracking_id,),
        ).fetchone()
    return row[0] if row else None


def db_tracking_id_exists(tracking_id: str) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT 1 FROM questions WHERE tracking_id = ?",
            (tracking_id,),
        ).fetchone()
    return row is not None


def db_mark_answered(
    tracking_id: str,
    responder_id: int | None = None,
    responder_name: str = "",
    responder_username: str = "",
) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """UPDATE questions
               SET answered = 1,
                   answered_by = COALESCE(answered_by, ?),
                   answered_by_name = COALESCE(answered_by_name, ?),
                   answered_by_username = COALESCE(answered_by_username, ?)
               WHERE tracking_id = ?""",
            (responder_id, responder_name, responder_username, tracking_id),
        )
        con.commit()


def db_question_count(student_id: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT COUNT(*) FROM questions WHERE student_id = ?",
            (student_id,),
        ).fetchone()
    return row[0] if row else 0


def db_ban_user(student_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO banned_users (student_id) VALUES (?)", (student_id,))
        con.commit()


def db_unban_user(student_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM banned_users WHERE student_id = ?", (student_id,))
        con.commit()


def db_is_banned(student_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT 1 FROM banned_users WHERE student_id = ?", (student_id,)
        ).fetchone()
    return row is not None


def db_save_student_message(student_msg_id: int, student_id: int, group_msg_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR IGNORE INTO student_messages (student_msg_id, student_id, group_msg_id) VALUES (?, ?, ?)",
            (student_msg_id, student_id, group_msg_id),
        )
        con.commit()


def db_get_group_msg_for_student_msg(student_msg_id: int, student_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT group_msg_id FROM student_messages WHERE student_msg_id = ? AND student_id = ?",
            (student_msg_id, student_id),
        ).fetchone()
    return row[0] if row else None


def db_get_student_msg_for_group_msg(group_msg_id: int) -> tuple[int, int] | None:
    """Return (student_msg_id, student_id) for a group message id."""
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT student_msg_id, student_id FROM student_messages WHERE group_msg_id = ? ORDER BY student_msg_id DESC LIMIT 1",
            (group_msg_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def db_delete_student_msg_mapping(student_msg_id: int, student_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "DELETE FROM student_messages WHERE student_msg_id = ? AND student_id = ?",
            (student_msg_id, student_id),
        )
        con.commit()


def db_get_recent_student_messages(student_id: int, limit: int) -> list[int]:
    """Return recent bot message IDs sent to a student (newest first)."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT student_msg_id
            FROM student_messages
            WHERE student_id = ?
            ORDER BY student_msg_id DESC
            LIMIT ?
            """,
            (student_id, max(1, limit)),
        ).fetchall()
    return [r[0] for r in rows]


def db_get_all_student_ids() -> list[int]:
    """Return all users who have ever interacted with the bot."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT student_id FROM users").fetchall()
    return [r[0] for r in rows]


def db_register_user(student_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("INSERT OR IGNORE INTO users (student_id) VALUES (?)", (student_id,))
        con.commit()


def db_get_unanswered() -> list[tuple]:
    """Return list of (tracking_id, group_message_id, question_preview, student_name, student_username)."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("""
            SELECT q.tracking_id, fm.group_message_id, q.question_preview,
                   q.student_name, q.student_username
            FROM questions q
            JOIN (
                SELECT tracking_id, MIN(group_message_id) AS group_message_id
                FROM messages
                WHERE tracking_id != ''
                GROUP BY tracking_id
            ) fm ON fm.tracking_id = q.tracking_id
            WHERE q.answered = 0
            ORDER BY fm.group_message_id ASC
        """).fetchall()
    return rows

def db_get_all_students_with_info() -> list[tuple]:
    """Return (student_id, student_name, student_username, question_count)"""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("""
            SELECT 
                u.student_id,
                MAX(q.student_name) as student_name,
                MAX(q.student_username) as student_username,
                COUNT(q.tracking_id) as question_count
            FROM users u
            LEFT JOIN questions q ON u.student_id = q.student_id
            GROUP BY u.student_id
            ORDER BY question_count DESC, u.student_id ASC
        """).fetchall()
    return rows

def db_get_admin_stats() -> list[tuple]:
    """Return (answered_by, answered_by_name, answered_by_username, count) rows."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT answered_by,
                   MAX(answered_by_name) AS answered_by_name,
                   MAX(answered_by_username) AS answered_by_username,
                   COUNT(*) AS answer_count
            FROM questions
            WHERE answered = 1 AND answered_by IS NOT NULL
            GROUP BY answered_by
            ORDER BY answer_count DESC, answered_by ASC
            """
        ).fetchall()
    return rows


def db_get_stats() -> tuple[int, int]:
    """Return (answered_count, unanswered_count) across all questions."""
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT SUM(answered), SUM(1 - answered) FROM questions"
        ).fetchone()
    return (int(row[0] or 0), int(row[1] or 0))


def db_get_or_create_grade(name: str) -> int:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT id FROM quiz_grades WHERE name = ?", (name,)).fetchone()
        if row:
            return int(row[0])
        cur = con.execute("INSERT INTO quiz_grades (name) VALUES (?)", (name,))
        con.commit()
        return int(cur.lastrowid)


def db_get_or_create_course(grade_id: int, name: str) -> int:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT id FROM quiz_courses WHERE grade_id = ? AND name = ?", (grade_id, name)).fetchone()
        if row:
            return int(row[0])
        cur = con.execute("INSERT INTO quiz_courses (grade_id, name) VALUES (?, ?)", (grade_id, name))
        con.commit()
        return int(cur.lastrowid)


def db_get_or_create_topic(course_id: int, name: str) -> int:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT id FROM quiz_topics WHERE course_id = ? AND name = ?", (course_id, name)).fetchone()
        if row:
            return int(row[0])
        cur = con.execute("INSERT INTO quiz_topics (course_id, name) VALUES (?, ?)", (course_id, name))
        con.commit()
        return int(cur.lastrowid)


def db_add_quiz_question(topic_id: int, question_text: str, official_answer: str, publish_answer: int, created_by: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            """INSERT INTO quiz_questions (topic_id, question_text, official_answer, publish_answer, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (topic_id, question_text, official_answer, int(publish_answer), created_by, int(time.time())),
        )
        con.commit()
        return int(cur.lastrowid)


def db_get_quiz_topics() -> list[tuple]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT t.id, g.name, c.name, t.name
            FROM quiz_topics t
            JOIN quiz_courses c ON c.id = t.course_id
            JOIN quiz_grades g ON g.id = c.grade_id
            ORDER BY g.name, c.name, t.name
            """
        ).fetchall()
    return rows


def db_get_quiz_questions(topic_id: int) -> list[tuple]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT id, question_text, official_answer, publish_answer
            FROM quiz_questions
            WHERE topic_id = ?
            ORDER BY id ASC
            """,
            (topic_id,),
        ).fetchall()
    return rows


def db_create_quiz_session(user_id: int, topic_id: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            """INSERT INTO quiz_sessions (user_id, topic_id, started_at, current_index, status)
               VALUES (?, ?, ?, 0, 'active')""",
            (user_id, topic_id, int(time.time())),
        )
        con.commit()
        return int(cur.lastrowid)


def db_save_quiz_answer(session_id: int, question_id: int, response_type: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """INSERT INTO quiz_answers (session_id, question_id, response_type, answered_at)
               VALUES (?, ?, ?, ?)""",
            (session_id, question_id, response_type, int(time.time())),
        )
        con.commit()


def db_finish_quiz_session(session_id: int, current_index: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """UPDATE quiz_sessions SET completed_at = ?, current_index = ?, status = 'completed' WHERE id = ?""",
            (int(time.time()), current_index, session_id),
        )
        con.commit()


def db_get_single_question_users() -> list[tuple]:
    """Return users who have no answered questions."""
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("""
            SELECT q.student_id,
                   MIN(fm.group_message_id) AS group_message_id,
                   MAX(q.student_name) AS student_name,
                   MAX(q.student_username) AS student_username
            FROM questions q
            JOIN (
                SELECT tracking_id, MIN(group_message_id) AS group_message_id
                FROM messages
                WHERE tracking_id != ''
                GROUP BY tracking_id
            ) fm ON fm.tracking_id = q.tracking_id
            WHERE q.answered = 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM questions q2
                  WHERE q2.student_id = q.student_id
                    AND q2.answered = 1
              )
            GROUP BY q.student_id
            ORDER BY q.student_id ASC
        """).fetchall()
    return rows


def _now_ts() -> int:
    return int(time.time())


def db_quiz_add_grade(name: str, created_by: int) -> tuple[bool, int | None]:
    with sqlite3.connect(DB_PATH) as con:
        try:
            cur = con.execute(
                "INSERT INTO quiz_grades (name, created_by, created_at) VALUES (?, ?, ?)",
                (name.strip(), created_by, _now_ts()),
            )
            con.commit()
            return True, cur.lastrowid
        except sqlite3.IntegrityError:
            row = con.execute("SELECT id FROM quiz_grades WHERE name = ?", (name.strip(),)).fetchone()
            return False, (row[0] if row else None)


def db_quiz_add_course(grade_id: int, name: str, created_by: int) -> tuple[bool, int | None]:
    with sqlite3.connect(DB_PATH) as con:
        grade_exists = con.execute("SELECT 1 FROM quiz_grades WHERE id = ?", (grade_id,)).fetchone()
        if not grade_exists:
            return False, None
        try:
            cur = con.execute(
                "INSERT INTO quiz_courses (grade_id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
                (grade_id, name.strip(), created_by, _now_ts()),
            )
            con.commit()
            return True, cur.lastrowid
        except sqlite3.IntegrityError:
            row = con.execute(
                "SELECT id FROM quiz_courses WHERE grade_id = ? AND name = ?",
                (grade_id, name.strip()),
            ).fetchone()
            return False, (row[0] if row else None)


def db_quiz_add_topic(course_id: int, name: str, created_by: int) -> tuple[bool, int | None]:
    with sqlite3.connect(DB_PATH) as con:
        course_exists = con.execute("SELECT 1 FROM quiz_courses WHERE id = ?", (course_id,)).fetchone()
        if not course_exists:
            return False, None
        try:
            cur = con.execute(
                "INSERT INTO quiz_topics (course_id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
                (course_id, name.strip(), created_by, _now_ts()),
            )
            con.commit()
            return True, cur.lastrowid
        except sqlite3.IntegrityError:
            row = con.execute(
                "SELECT id FROM quiz_topics WHERE course_id = ? AND name = ?",
                (course_id, name.strip()),
            ).fetchone()
            return False, (row[0] if row else None)


def db_quiz_list_grades() -> list[tuple[int, str]]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT id, name FROM quiz_grades ORDER BY name ASC").fetchall()
    return [(r[0], r[1]) for r in rows]


def db_quiz_list_courses(grade_id: int) -> list[tuple[int, str]]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT id, name FROM quiz_courses WHERE grade_id = ? ORDER BY name ASC",
            (grade_id,),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def db_quiz_list_topics(course_id: int) -> list[tuple[int, str, int]]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT t.id, t.name, COUNT(q.id) AS question_count
            FROM quiz_topics t
            LEFT JOIN quiz_questions q ON q.topic_id = t.id
            WHERE t.course_id = ?
            GROUP BY t.id, t.name
            ORDER BY t.name ASC
            """,
            (course_id,),
        ).fetchall()
    return [(r[0], r[1], int(r[2] or 0)) for r in rows]


def db_quiz_topic_exists(topic_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT 1 FROM quiz_topics WHERE id = ?", (topic_id,)).fetchone()
    return row is not None


def db_quiz_get_topic_path(topic_id: int) -> tuple[str, str, str] | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            """
            SELECT g.name, c.name, t.name
            FROM quiz_topics t
            JOIN quiz_courses c ON c.id = t.course_id
            JOIN quiz_grades g ON g.id = c.grade_id
            WHERE t.id = ?
            """,
            (topic_id,),
        ).fetchone()
    if not row:
        return None
    return row[0], row[1], row[2]


def db_quiz_add_question(
    topic_id: int,
    question_type: str,
    question_text: str,
    question_file_id: str,
    question_caption: str,
    answer_type: str,
    answer_text: str,
    answer_file_id: str,
    answer_caption: str,
    created_by: int,
) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            """
            INSERT INTO quiz_questions (
                topic_id, question_type, question_text, question_file_id, question_caption,
                answer_type, answer_text, answer_file_id, answer_caption,
                answer_published, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                topic_id,
                question_type,
                question_text,
                question_file_id,
                question_caption,
                answer_type,
                answer_text,
                answer_file_id,
                answer_caption,
                created_by,
                _now_ts(),
            ),
        )
        con.commit()
    return int(cur.lastrowid)


def db_quiz_get_question(question_id: int) -> tuple | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            """
            SELECT id, topic_id, question_type, question_text, question_file_id, question_caption,
                   answer_type, answer_text, answer_file_id, answer_caption,
                   answer_published, created_by
            FROM quiz_questions
            WHERE id = ?
            """,
            (question_id,),
        ).fetchone()
    return row


def db_quiz_set_answer_published(question_id: int, published: bool) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "UPDATE quiz_questions SET answer_published = ? WHERE id = ?",
            (1 if published else 0, question_id),
        )
        con.commit()
    return cur.rowcount > 0


def db_quiz_get_questions_by_topic(topic_id: int) -> list[tuple]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT id, question_type, question_text, question_file_id, question_caption,
                   answer_type, answer_text, answer_file_id, answer_caption,
                   answer_published
            FROM quiz_questions
            WHERE topic_id = ?
            ORDER BY id ASC
            """,
            (topic_id,),
        ).fetchall()
    return rows


def db_quiz_create_session(user_id: int, topic_id: int, total_count: int) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            """
            INSERT INTO quiz_sessions (user_id, topic_id, current_idx, total_count, completed, started_at, updated_at)
            VALUES (?, ?, 0, ?, 0, ?, ?)
            """,
            (user_id, topic_id, total_count, _now_ts(), _now_ts()),
        )
        con.commit()
    return int(cur.lastrowid)


def db_quiz_update_session_index(session_id: int, idx: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE quiz_sessions SET current_idx = ?, updated_at = ? WHERE id = ?",
            (idx, _now_ts(), session_id),
        )
        con.commit()


def db_quiz_complete_session(session_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE quiz_sessions SET completed = 1, updated_at = ? WHERE id = ?",
            (_now_ts(), session_id),
        )
        con.commit()


def db_quiz_get_session(session_id: int) -> tuple | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT id, user_id, topic_id, current_idx, total_count, completed FROM quiz_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return row


def db_quiz_save_user_answer(session_id: int, user_id: int, question_id: int, response_type: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO quiz_user_answers (session_id, user_id, question_id, response_type, answered_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, user_id, question_id, response_type, _now_ts()),
        )
        con.commit()


def db_quiz_get_session_summary(session_id: int) -> dict[str, int]:
    summary = {"knew": 0, "almost": 0, "not": 0}
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT response_type, COUNT(*)
            FROM quiz_user_answers
            WHERE session_id = ?
            GROUP BY response_type
            """,
            (session_id,),
        ).fetchall()
    for rtype, cnt in rows:
        if rtype in summary:
            summary[rtype] = int(cnt)
    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_tracking_id() -> str:
    """Return a unique random tracking ID like #ID_12345."""
    while True:
        tid = f"#ID_{random.randint(10000, 99999)}"
        if not db_tracking_id_exists(tid):
            return tid


def generate_confirm_id() -> str:
    """Return a unique random confirmation ID."""
    return f"cfm_{random.randint(100000, 999999)}"


def _build_user_profile_block(user_id: int, full_name: str, username: str | None) -> str:
    """Return HTML lines with maximum profile visibility for group admins."""
    safe_name = escape(full_name or "کاربر")
    normalized_username = (username or "").lstrip("@")
    safe_username = escape(normalized_username)
    lines = [
        f"👤 {mention_html(user_id, safe_name)}",
        f"🆔 <code>{user_id}</code>",
    ]
    if safe_username:
        lines.append(f"🔸 @{safe_username}")
    return "\n".join(lines)
        
def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """
    Returns (allowed: bool, remaining_seconds: int)
    """
    now = time.time()
    if user_id not in _user_message_times:
        _user_message_times[user_id] = []
    
    # Remove timestamps older than RATE_LIMIT_SECONDS.
    _user_message_times[user_id] = [
        t for t in _user_message_times[user_id] 
        if now - t < RATE_LIMIT_SECONDS
    ]
    
    if len(_user_message_times[user_id]) >= RATE_LIMIT_MESSAGES:
        oldest = _user_message_times[user_id][0]
        remaining = int(RATE_LIMIT_SECONDS - (now - oldest)) + 1
        return False, max(1, remaining)
    
    # Record the current timestamp.
    _user_message_times[user_id].append(now)
    return True, 0

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _subscribe_keyboard() -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    join_url = CHANNEL_URL
    if not join_url and CHANNEL_ID.startswith("@"):
        join_url = f"https://t.me/{CHANNEL_ID.lstrip('@')}"
    if join_url:
        buttons.append([InlineKeyboardButton("📢 عضویت در کانال", url=join_url)])
    buttons.append([InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_sub")])
    return InlineKeyboardMarkup(buttons)


async def _reply_subscription_required(message, check_result: bool | None) -> None:
    if check_result is None:
        text = (
            "⚠️ بات نتوانست عضویت شما را از تلگرام بررسی کند. "
            "ادمین بات باید دسترسی ادمین کانال را به بات بدهد و CHANNEL_ID را بررسی کند."
        )
    else:
        text = "⚠️ برای استفاده از بات، ابتدا در کانال عضو شوید:"
    await message.reply_text(text, reply_markup=_subscribe_keyboard())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply to /start in the bot's private chat."""
    if update.effective_chat.type != "private":
        return
    db_register_user(update.effective_user.id)
    subscription = await _check_subscription(update.effective_user.id, context)
    if subscription is not True:
        await _reply_subscription_required(update.message, subscription)
        return
    cooldown_mins = QUESTION_COOLDOWN // 60
    await update.message.reply_text(
        f"سلام! خوش آمدید 👋\n\n"
        f"📌 قوانین استفاده از بات:\n"
        f"• حداکثر {MAX_QUESTIONS} سوال می‌توانید ارسال کنید.\n"
        f"• بین هر دو سوال باید {cooldown_mins} دقیقه صبر کنید.\n"
        f"• ارسال گیف ممنوع است.\n"
        f"• فقط سوالات درسی مجاز است.\n\n"
        f"سوال خود را بفرستید."
    )


async def check_sub_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle the ✅ Check subscription button press."""
    query = update.callback_query
    if not CHANNEL_ID:
        await query.answer("عضویت در کانال برای این بات الزامی نیست.", show_alert=True)
        return

    subscribed = await _check_subscription(query.from_user.id, context)
    if subscribed is True:
        await query.answer("✅ عضویت شما تایید شد.")
        try:
            await query.edit_message_text(
                "✅ عضویت شما تایید شد. حالا سوال خود را برای بات بفرستید."
            )
        except Exception as exc:
            logger.info("Could not edit subscription prompt for user %s: %s", query.from_user.id, exc)
    elif subscribed is False:
        await query.answer("هنوز عضویت شما در کانال تایید نشده است.", show_alert=True)
    else:
        await query.answer(
            "بات نتوانست عضویت را بررسی کند. ادمین بات باید دسترسی ادمین کانال داشته باشد و CHANNEL_ID درست باشد.",
            show_alert=True,
        )


async def _check_subscription(
    user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> bool | None:
    """Return True/False for membership, or None when Telegram check fails."""
    if not CHANNEL_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        if member.status == "restricted":
            return bool(getattr(member, "is_member", False))
        return member.status in ("member", "administrator", "creator")
    except Exception as exc:
        logger.warning("Subscription check failed for user %s in %s: %s", user_id, CHANNEL_ID, exc)
        return None


async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True only when Telegram confirms user membership."""
    return await _check_subscription(user_id, context) is True


async def handle_private_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Receive a question from a student...
    """
    message = update.message
    if message is None:
        return

    if update.effective_chat.type != "private":
        return

    db_register_user(update.effective_user.id)

    # Set student_id before using it in checks below.
    student_id = update.effective_user.id

    subscription = await _check_subscription(update.effective_user.id, context)
    if subscription is not True:
        await _reply_subscription_required(update.message, subscription)
        return

    # --- Ban check ---
    if db_is_banned(student_id):
        await update.message.reply_text("⛔ شما بن شده‌اید...")
        return

    # Apply the rate limit before processing the question.
    allowed, wait_time = check_rate_limit(student_id)
    if not allowed:
        await update.message.reply_text(
            f"⏳ سرعت ارسال پیام شما بالاست.\n"
            f"لطفاً {wait_time} ثانیه صبر کنید."
        )
        return
    # =====================================

    # --- Cooldown check ---
    now = time.time()
    last = _last_question.get(student_id, 0)
    remaining = QUESTION_COOLDOWN - (now - last)
    if remaining > 0:
        mins, secs = divmod(int(remaining), 60)
        await update.message.reply_text(
            f"⏳ لطفاً صبر کنید. تا {mins} دقیقه و {secs} ثانیه دیگر می‌توانید سوال بفرستید."
        )
        return

    # --- Block GIFs ---
    if message.animation:
        await update.message.reply_text("⛔ ارسال گیف مجاز نیست.")
        return

    # --- Lifetime question limit ---
    if db_question_count(student_id) >= MAX_QUESTIONS:
        await update.message.reply_text(
            f"⛔ شما به حداکثر تعداد مجاز سوال ({MAX_QUESTIONS} سوال) رسیده‌اید."
        )
        return
    student_name = update.effective_user.full_name or ""
    student_username_raw = update.effective_user.username or ""
    student_username = f"@{student_username_raw}" if student_username_raw else ""
    tracking_id = generate_tracking_id()

    # Build a short preview for the unanswered list
    if message.text:
        preview = message.text[:100]
    elif message.photo:
        preview = "[📷 عکس]"
    elif message.document:
        preview = f"[📎 {message.document.file_name or 'فایل'}]"
    elif message.voice:
        preview = "[🎤 ویس]"
    else:
        preview = "[پیام]"

    db_save_question(tracking_id, student_id, preview, student_name, student_username)

    profile_block = _build_user_profile_block(student_id, student_name, student_username_raw)

    header = (
        f"❓ سوال جدید\n"
        f"کد پیگیری: {tracking_id}\n"
        f"{profile_block}\n\n"
    )
    sent = None

    if message.text:
        text = f"{header}{message.text}"
        sent = await context.bot.send_message(chat_id=GROUP_ID, text=text, parse_mode="HTML")

    elif message.photo:
        # Use the highest-resolution version
        photo = message.photo[-1]
        caption = f"{header}{message.caption or ''}"
        sent = await context.bot.send_photo(
            chat_id=GROUP_ID,
            photo=photo.file_id,
            caption=caption,
            parse_mode="HTML",
        )

    elif message.document:
        caption = f"{header}{message.caption or ''}"
        sent = await context.bot.send_document(
            chat_id=GROUP_ID,
            document=message.document.file_id,
            caption=caption,
            parse_mode="HTML",
        )

    elif message.voice:
        caption = f"{header}{message.caption or ''}"
        sent = await context.bot.send_voice(
            chat_id=GROUP_ID,
            voice=message.voice.file_id,
            caption=caption,
            parse_mode="HTML",
        )

    else:
        await message.reply_text(
            "⚠️ فقط متن، عکس، فایل یا ویس قابل ارسال است."
        )
        return

    if sent:
        _last_question[student_id] = time.time()
        has_media = bool(message.photo or message.document or message.voice)
        # Store the HTML-formatted content so the link survives future edits
        if has_media:
            original_content = f"{header}{message.caption or ''}"
        else:
            original_content = f"{header}{message.text or ''}"
        db_save_message(sent.message_id, tracking_id, original_content, has_media, student_id)
        confirm = await message.reply_text(
            f"✅ سوال شما با کد پیگیری {tracking_id} ارسال شد."
        )
        db_save_student_message(confirm.message_id, student_id, sent.message_id)


def _answer_confirmation_keyboard(confirm_id: str) -> InlineKeyboardMarkup:
    """Return inline keyboard for answer confirmation."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ ارسال پاسخ", callback_data=f"confirm_answer:{confirm_id}"),
            InlineKeyboardButton("❌ لغو", callback_data=f"cancel_answer:{confirm_id}"),
        ]
    ])

def _broadcast_confirmation_keyboard(confirm_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ ارسال به همه", callback_data=f"confirm_broadcast:{confirm_id}"),
            InlineKeyboardButton("❌ لغو", callback_data=f"cancel_broadcast:{confirm_id}"),
        ]
    ])

async def handle_group_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Monitor the class group. When someone replies to one of the bot's question
    messages, show a confirmation keyboard before sending to the student.
    """
    message = update.message
    if message is None:
        return

    # Only handle messages inside the configured group
    if update.effective_chat.id != GROUP_ID:
        return

    # Must be a reply to another message
    if message.reply_to_message is None:
        return

    replied_id = message.reply_to_message.message_id

    # Only route replies when the replied message is student-originated in group.
    student_id = db_get_message_student_id(replied_id)
    tracking_id = db_get_tracking_id(replied_id)
    if student_id is None and tracking_id:
        # Backward compatibility for old question rows created before student_id column existed.
        student_id = db_get_student_id(tracking_id)
    if student_id is None:
        return

    responder = message.from_user
    responder_id = responder.id if responder is not None else None
    responder_name = responder.first_name if responder is not None and responder.first_name else "ادمین"
    responder_username_raw = responder.username if responder is not None and responder.username else ""
    responder_username = f"@{responder_username_raw}" if responder_username_raw else ""
    responder_profile = _build_user_profile_block(
        responder_id or 0,
        responder.full_name if responder is not None else "ادمین",
        responder_username_raw,
    )

    tracking_label = f" ({tracking_id})" if tracking_id else ""
    header = f"✨ پاسخ جدید برای سوال شما{tracking_label}:\n👤 پاسخ‌دهنده: {responder_name}\n\n"

    # Store the answer data for confirmation - include message content details
    confirm_id = generate_confirm_id()
    _pending_answers[confirm_id] = {
        "student_id": student_id,
        "tracking_id": tracking_id,
        "replied_id": replied_id,
        "group_msg_id": message.message_id,
        "responder_id": responder_id,
        "responder_name": responder_name,
        "responder_username": responder_username,
        "header": header,
        # Message content for reconstructing single combined message
        "msg_type": "text" if message.text else ("photo" if message.photo else ("document" if message.document else ("voice" if message.voice else ("video" if message.video else ("audio" if message.audio else ("video_note" if message.video_note else ("sticker" if message.sticker else "other"))))))),
        "text": message.text,
        "caption": message.caption,
        "photo_file_id": message.photo[-1].file_id if message.photo else None,
        "document_file_id": message.document.file_id if message.document else None,
        "document_file_name": message.document.file_name if message.document else None,
        "voice_file_id": message.voice.file_id if message.voice else None,
        "video_file_id": message.video.file_id if message.video else None,
        "audio_file_id": message.audio.file_id if message.audio else None,
        "video_note_file_id": message.video_note.file_id if message.video_note else None,
        "sticker_file_id": message.sticker.file_id if message.sticker else None,
    }

    # Send confirmation message to the group
    preview = ""
    if message.text:
        preview = message.text[:200]
    elif message.caption:
        preview = message.caption[:200]
    elif message.photo:
        preview = "[📷 عکس]"
    elif message.document:
        preview = f"[📎 {message.document.file_name or 'فایل'}]"
    elif message.voice:
        preview = "[🎤 ویس]"
    elif message.video:
        preview = "[🎥 ویدیو]"
    elif message.audio:
        preview = "[🎵 فایل صوتی]"
    elif message.video_note:
        preview = "[📹 ویدیو نوت]"
    elif message.sticker:
        preview = "[🎭 استیکر]"
    else:
        preview = "[پیام]"

    confirm_text = (
        f"❓ <b>تایید ارسال پاسخ</b>\n\n"
        f"پاسخ‌دهنده:\n{responder_profile}\n"
        f"📝 پیش‌نمایش پاسخ: {preview}\n\n"
        f"آیا مطمئن هستید که می‌خواهید این پاسخ به دانشجو ارسال شود؟"
    )

    await message.reply_text(
        confirm_text,
        parse_mode="HTML",
        reply_markup=_answer_confirmation_keyboard(confirm_id),
    )


async def confirm_answer_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle ✅ Confirm answer button press."""
    query = update.callback_query
    await query.answer()
    
    confirm_id = query.data.split(":")[1]
    answer_data = _pending_answers.pop(confirm_id, None)
    
    if not answer_data:
        await query.edit_message_text("⚠️ این تایید منقضی شده یا نامعتبر است.")
        return
    
    student_id = answer_data["student_id"]
    tracking_id = answer_data["tracking_id"]
    replied_id = answer_data["replied_id"]
    group_msg_id = answer_data["group_msg_id"]
    responder_id = answer_data["responder_id"]
    responder_name = answer_data["responder_name"]
    responder_username = answer_data["responder_username"]
    header = answer_data["header"]
    
    # Only the original responder or an admin can confirm
    if query.from_user.id != responder_id:
        try:
            admins = await context.bot.get_chat_administrators(chat_id=GROUP_ID)
            admin_ids = [admin.user.id for admin in admins]
            if query.from_user.id not in admin_ids:
                await query.answer("⛔ فقط پاسخ‌دهنده یا ادمین‌ها می‌توانند تایید کنند.", show_alert=True)
                return
        except Exception:
            await query.answer("⛔ فقط پاسخ‌دهنده می‌تواند تایید کند.", show_alert=True)
            return
    
    # Mark as answered
    if tracking_id:
        db_mark_answered(tracking_id, responder_id, responder_name, responder_username)
    
    # Send a single combined message to student (header + content together)
    # Find the student's private message to use as the reply target.
    reply_to_private_id = None
    mapping = db_get_student_msg_for_group_msg(replied_id)
    if mapping is not None:
        reply_to_private_id = mapping[0]  # student_msg_id

    # Send a single combined message to student (header + content together)
    msg_type = answer_data.get("msg_type", "text")
    sent_msg = None
    try:
        if msg_type == "text":
            full_text = f"{header}{answer_data['text'] or ''}"
            sent_msg = await context.bot.send_message(
                chat_id=student_id,
                text=full_text,
                reply_to_message_id=reply_to_private_id,
            )

        elif msg_type == "photo":
            full_caption = f"{header}{answer_data['caption'] or ''}"
            sent_msg = await context.bot.send_photo(
                chat_id=student_id,
                photo=answer_data["photo_file_id"],
                caption=full_caption,
                reply_to_message_id=reply_to_private_id,
            )

        elif msg_type == "document":
            full_caption = f"{header}{answer_data['caption'] or ''}"
            sent_msg = await context.bot.send_document(
                chat_id=student_id,
                document=answer_data["document_file_id"],
                caption=full_caption,
                reply_to_message_id=reply_to_private_id,
            )

        elif msg_type == "voice":
            full_caption = f"{header}{answer_data['caption'] or ''}"
            sent_msg = await context.bot.send_voice(
                chat_id=student_id,
                voice=answer_data["voice_file_id"],
                caption=full_caption,
                reply_to_message_id=reply_to_private_id,
            )

        elif msg_type == "video":
            full_caption = f"{header}{answer_data['caption'] or ''}"
            sent_msg = await context.bot.send_video(
                chat_id=student_id,
                video=answer_data["video_file_id"],
                caption=full_caption,
                reply_to_message_id=reply_to_private_id,
            )

        elif msg_type == "audio":
            full_caption = f"{header}{answer_data['caption'] or ''}"
            sent_msg = await context.bot.send_audio(
                chat_id=student_id,
                audio=answer_data["audio_file_id"],
                caption=full_caption,
                reply_to_message_id=reply_to_private_id,
            )

        elif msg_type == "video_note":
            await context.bot.send_message(
                chat_id=student_id,
                text=header,
                reply_to_message_id=reply_to_private_id,
            )
            sent_msg = await context.bot.send_video_note(
                chat_id=student_id,
                video_note=answer_data["video_note_file_id"],
            )

        elif msg_type == "sticker":
            await context.bot.send_message(
                chat_id=student_id,
                text=header,
                reply_to_message_id=reply_to_private_id,
            )
            sent_msg = await context.bot.send_sticker(
                chat_id=student_id,
                sticker=answer_data["sticker_file_id"],
            )

        else:
            sent_msg = await context.bot.forward_message(
                chat_id=student_id,
                from_chat_id=GROUP_ID,
                message_id=group_msg_id,
            )

    except Exception as exc:
        logger.warning("Could not deliver answer to student %s: %s", student_id, exc)
        # If replying fails (for example, if the original message was deleted), retry without a reply target.
        if reply_to_private_id is not None:
            try:
                if msg_type == "text":
                    sent_msg = await context.bot.send_message(
                        chat_id=student_id,
                        text=f"{header}{answer_data['text'] or ''}",
                    )
                # Add equivalent fallback handling here for other message types if needed.
            except Exception as exc2:
                logger.warning("Fallback send also failed for student %s: %s", student_id, exc2)
                sent_msg = None
        else:
            sent_msg = None
    
    if sent_msg is not None:
        db_save_student_message(sent_msg.message_id, student_id, group_msg_id)
    
    # Edit the original question message to show answered
    if tracking_id:
        original_content, has_media = db_get_message_content(replied_id)
        answered_tag = "✅ جواب داده شد\n" + "─" * 20 + "\n"
        new_content = answered_tag + (original_content or "")
        try:
            if has_media:
                await context.bot.edit_message_caption(
                    chat_id=GROUP_ID,
                    message_id=replied_id,
                    caption=new_content,
                    parse_mode="HTML",
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=GROUP_ID,
                    message_id=replied_id,
                    text=new_content,
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.warning("Could not edit question message %s: %s", replied_id, e)
    
    # Delete confirmation message, react with 👍 on the answer
    try:
        await query.delete_message()
        if ReactionTypeEmoji is not None:
            await context.bot.set_message_reaction(
                chat_id=GROUP_ID,
                message_id=group_msg_id,
                reaction=[ReactionTypeEmoji("👍")],
            )
    except Exception as exc:
        logger.warning("Could not delete confirmation or react: %s", exc)


async def cancel_answer_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle ❌ Cancel answer button press."""
    query = update.callback_query
    await query.answer()
    
    confirm_id = query.data.split(":")[1]
    answer_data = _pending_answers.pop(confirm_id, None)
    
    if not answer_data:
        await query.edit_message_text("⚠️ این تایید منقضی شده یا نامعتبر است.")
        return

    group_msg_id = answer_data["group_msg_id"]
    
    # Only the original responder or an admin can cancel
    if query.from_user.id != answer_data["responder_id"]:
        try:
            admins = await context.bot.get_chat_administrators(chat_id=GROUP_ID)
            admin_ids = [admin.user.id for admin in admins]
            if query.from_user.id not in admin_ids:
                await query.answer("⛔ فقط پاسخ‌دهنده یا ادمین‌ها می‌توانند لغو کنند.", show_alert=True)
                return
        except Exception:
            await query.answer("⛔ فقط پاسخ‌دهنده می‌تواند لغو کند.", show_alert=True)
            return
    
    # Delete confirmation message, react with 👎 on the answer
    try:
        await query.delete_message()
        if ReactionTypeEmoji is not None:
            await context.bot.set_message_reaction(
                chat_id=GROUP_ID,
                message_id=group_msg_id,
                reaction=[ReactionTypeEmoji("👎")],
            )
    except Exception as exc:
        logger.warning("Could not delete confirmation or react: %s", exc)


async def confirm_broadcast_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    confirm_id = query.data.split(":")[1]
    data = _pending_broadcasts.pop(confirm_id, None)

    if not data:
        await query.edit_message_text("⚠️ این تایید منقضی شده یا نامعتبر است.")
        return

    # Allow only the initiating admin or another group admin to confirm.
    if query.from_user.id != data["admin_id"]:
        try:
            admins = await context.bot.get_chat_administrators(chat_id=GROUP_ID)
            if query.from_user.id not in {a.user.id for a in admins}:
                await query.answer("⛔ فقط ادمین‌ها می‌توانند تایید کنند.", show_alert=True)
                return
        except Exception:
            await query.answer("⛔ فقط ادمین می‌تواند تایید کند.", show_alert=True)
            return

    BROADCAST_TRACKING = "#BROADCAST"
    group_anchor_id = data["group_anchor_id"]
    broadcast_text = data["broadcast_text"]
    reply_msg = data["reply_msg"]
    student_ids = data["student_ids"]
    header = data["header"]

    # Store the broadcast anchor message in the database.
    db_save_message(group_anchor_id, BROADCAST_TRACKING, broadcast_text or "", False, None)

    await query.edit_message_text("📤 در حال ارسال به همه کاربران...")

    sent_count = 0
    fail_count = 0

    for uid in student_ids:
        try:
            if reply_msg:
                if reply_msg.poll:
                    # Forwarding a poll shares its results with recipients.
                    sent = await context.bot.forward_message(
                        chat_id=uid,
                        from_chat_id=update.effective_chat.id,  # GROUP_ID can also be used here.
                        message_id=reply_msg.message_id,
                    )
                else:
                    sent = await reply_msg.copy(chat_id=uid)
            else:
                sent = await context.bot.send_message(
                    chat_id=uid,
                    text=header + broadcast_text,
                    parse_mode="HTML",
                )

            db_save_student_message(sent.message_id, uid, group_anchor_id)
            sent_count += 1
            await asyncio.sleep(0.035)
        except Exception:
            fail_count += 1

    await query.edit_message_text(
        f"✅ ارسال انجام شد.\n"
        f"موفق: {sent_count} | ناموفق: {fail_count}\n"
        f"(دانشجوها می‌توانند به این پیام ریپلای بزنند)"
    )


async def cancel_broadcast_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    confirm_id = query.data.split(":")[1]
    data = _pending_broadcasts.pop(confirm_id, None)

    if not data:
        await query.edit_message_text("⚠️ این تایید منقضی شده یا نامعتبر است.")
        return

    if query.from_user.id != data["admin_id"]:
        try:
            admins = await context.bot.get_chat_administrators(chat_id=GROUP_ID)
            if query.from_user.id not in {a.user.id for a in admins}:
                await query.answer("⛔ فقط ادمین‌ها می‌توانند لغو کنند.", show_alert=True)
                return
        except Exception:
            await query.answer("⛔ فقط ادمین می‌تواند لغو کند.", show_alert=True)
            return

    await query.edit_message_text("❌ ارسال پیام عمومی لغو شد.")


UNANSWERED_PAGE_SIZE = 10


def _unanswered_page_text(rows: list, page: int) -> str:
    total = len(rows)
    total_pages = max(1, (total + UNANSWERED_PAGE_SIZE - 1) // UNANSWERED_PAGE_SIZE)
    start = page * UNANSWERED_PAGE_SIZE
    slice_ = rows[start: start + UNANSWERED_PAGE_SIZE]
    group_numeric = str(GROUP_ID).lstrip("-").removeprefix("100")
    lines = [f"📋 سوالات بی‌پاسخ: {total}  |  صفحه {page + 1} از {total_pages}\n"]
    for tracking_id, msg_id, *_ in slice_:
        link = f"https://t.me/c/{group_numeric}/{msg_id}"
        lines.append(f"• {tracking_id} — {link}")
    return "\n".join(lines)


def _unanswered_keyboard(page: int, total: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (total + UNANSWERED_PAGE_SIZE - 1) // UNANSWERED_PAGE_SIZE)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀ قبلی", callback_data=f"unanswered_page:{page - 1}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("بعدی ▶", callback_data=f"unanswered_page:{page + 1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else InlineKeyboardMarkup([])


def _single_question_users_page_text(rows: list, page: int) -> str:
    total = len(rows)
    total_pages = max(1, (total + UNANSWERED_PAGE_SIZE - 1) // UNANSWERED_PAGE_SIZE)
    start = page * UNANSWERED_PAGE_SIZE
    slice_ = rows[start: start + UNANSWERED_PAGE_SIZE]
    lines = [f"📋 کاربران با فقط یک سوال: {total}  |  صفحه {page + 1} از {total_pages}\n"]
    group_numeric = str(GROUP_ID).lstrip("-").removeprefix("100")
    for student_id, msg_id, student_name, student_username in slice_:
        display_name = student_name or "بدون نام"
        if student_username:
            display_name += f" ({student_username})"
        link = f"https://t.me/c/{group_numeric}/{msg_id}"
        lines.append(f"• {display_name} — {link}")
    return "\n".join(lines)

def _allmembers_page_text(rows: list, page: int) -> str:
    """rows: list of (student_id, student_name, student_username, question_count)"""
    total = len(rows)
    total_pages = max(1, (total + ALLMEMBERS_PAGE_SIZE - 1) // ALLMEMBERS_PAGE_SIZE)
    start = page * ALLMEMBERS_PAGE_SIZE
    slice_ = rows[start: start + ALLMEMBERS_PAGE_SIZE]
    
    lines = [f"👥 تمام دانشجویان: {total} نفر  |  صفحه {page + 1} از {total_pages}\n"]
    
    for student_id, name, username, qcount in slice_:
        display = name or "بدون نام"
        if username:
            display += f" ({username})"
        lines.append(f"• {display} | ID: <code>{student_id}</code> | سوال: {qcount}")
    
    return "\n".join(lines)


def _allmembers_keyboard(page: int, total: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (total + ALLMEMBERS_PAGE_SIZE - 1) // ALLMEMBERS_PAGE_SIZE)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀ قبلی", callback_data=f"allmembers_page:{page - 1}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("بعدی ▶", callback_data=f"allmembers_page:{page + 1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else InlineKeyboardMarkup([])


def _single_question_users_keyboard(page: int, total: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (total + UNANSWERED_PAGE_SIZE - 1) // UNANSWERED_PAGE_SIZE)
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("◀ قبلی", callback_data=f"single_question_page:{page - 1}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("بعدی ▶", callback_data=f"single_question_page:{page + 1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else InlineKeyboardMarkup([])


async def _is_group_admin(
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | None = None,
) -> bool:
    try:
        admins = await context.bot.get_chat_administrators(
            chat_id=GROUP_ID if chat_id is None else chat_id
        )
        return user_id in {admin.user.id for admin in admins}
    except Exception:
        return False


async def setgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add current group as a question destination; only current group admins may run it."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("⚠️ این دستور را داخل گروه مقصد اجرا کنید.")
        return
    if not await _is_group_admin(user.id, context, chat.id):
        await message.reply_text("⛔ فقط ادمین‌های همین گروه می‌توانند آن را ثبت کنند.")
        return
    added = db_add_destination_group(chat.id)
    try:
        await context.bot.set_my_commands(
            _group_commands,
            scope=BotCommandScopeChat(chat_id=chat.id),
        )
    except Exception as exc:
        logger.warning("Could not register command menu for group %s: %s", chat.id, exc)
    if added:
        await message.reply_text("✅ این گروه به‌عنوان مقصد سوال‌ها ثبت شد.")
    else:
        await message.reply_text("ℹ️ این گروه از قبل به‌عنوان مقصد ثبت شده است.")


async def quiz_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != GROUP_ID:
        return
    if not await _is_group_admin(update.effective_user.id, context):
        await update.message.reply_text("⛔ فقط ادمین‌ها می‌توانند سوال بسازند.")
        return
    if not context.args:
        await update.message.reply_text(
            "📚 فرمت ساخت سوال:\n/quizadd پایه|درس|موضوع|سوال|پاسخ|yes\n\nمثال:\n/quizadd دهم|فیزیک|نیرو|نیرو چیست؟|نیرو پدیده‌ای است که باعث تغییر حرکت می‌شود|yes"
        )
        return
    payload = " ".join(context.args)
    parts = [p.strip() for p in payload.split("|")]
    if len(parts) < 5:
        await update.message.reply_text("⚠️ فرمت اشتباه است. از | برای جداکردن بخش‌ها استفاده کنید.")
        return
    grade_name = parts[0]
    course_name = parts[1]
    topic_name = parts[2]
    question_text = parts[3]
    official_answer = parts[4]
    publish_answer = 1 if len(parts) > 5 and parts[5].lower() in {"yes", "y", "true", "1", "publish"} else 0
    grade_id = db_get_or_create_grade(grade_name)
    course_id = db_get_or_create_course(grade_id, course_name)
    topic_id = db_get_or_create_topic(course_id, topic_name)
    db_add_quiz_question(topic_id, question_text, official_answer, publish_answer, update.effective_user.id)
    await update.message.reply_text(
        f"✅ سوال با موفقیت اضافه شد.\n📚 {grade_name} > {course_name} > {topic_name}"
    )


async def quiz_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return
    rows = db_get_quiz_topics()
    if not rows:
        await update.message.reply_text("⚠️ هنوز سوالی برای کوییز ثبت نشده است.")
        return
    lines = ["🧠 سوالات درسی موجود:\n"]
    buttons = []
    for topic_id, grade_name, course_name, topic_name in rows:
        lines.append(f"• {grade_name} › {course_name} › {topic_name}")
        buttons.append([InlineKeyboardButton(f"{grade_name} › {course_name} › {topic_name}", callback_data=f"quiz_start:{topic_id}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def quiz_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not query.data:
        return
    try:
        topic_id = int(query.data.split(":", 1)[1])
    except ValueError:
        return
    rows = db_get_quiz_questions(topic_id)
    if not rows:
        await query.edit_message_text("⚠️ این موضوع هنوز سوالی ندارد.")
        return
    session_id = db_create_quiz_session(query.from_user.id, topic_id)
    _quiz_sessions[query.from_user.id] = {
        "session_id": session_id,
        "topic_id": topic_id,
        "questions": [
            {
                "id": qid,
                "question_text": qtext,
                "official_answer": answer,
                "publish_answer": int(publish),
            }
            for qid, qtext, answer, publish in rows
        ],
        "index": 0,
    }
    await _send_quiz_question(query, context)


async def _send_quiz_question(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    session = _quiz_sessions.get(query.from_user.id)
    if not session:
        await query.edit_message_text("⚠️ جلسه کوییز منقضی شده است.")
        return
    questions = session["questions"]
    if session["index"] >= len(questions):
        db_finish_quiz_session(session["session_id"], session["index"])
        _quiz_sessions.pop(query.from_user.id, None)
        await query.edit_message_text("✅ پایان کوییز. با موفقیت تمام شد.")
        return
    current = questions[session["index"]]
    text = (
        f"🧠 سوال {session['index'] + 1} از {len(questions)}\n\n"
        f"{current['question_text']}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ بلد بودم", callback_data=f"quiz_response:{current['id']}:knew")],
        [InlineKeyboardButton("🟡 تقریبا بلد بودم", callback_data=f"quiz_response:{current['id']}:almost")],
        [InlineKeyboardButton("❌ نبودم", callback_data=f"quiz_response:{current['id']}:dontknow")],
    ])
    await query.edit_message_text(text, reply_markup=keyboard)


async def quiz_response_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not query.data:
        return
    try:
        _, question_id_text, response_type = query.data.split(":", 2)
        question_id = int(question_id_text)
    except ValueError:
        return
    session = _quiz_sessions.get(query.from_user.id)
    if not session:
        await query.edit_message_text("⚠️ جلسه کوییز منقضی شده است.")
        return
    current = None
    for item in session["questions"]:
        if item["id"] == question_id:
            current = item
            break
    if current is None:
        return
    db_save_quiz_answer(session["session_id"], question_id, response_type)
    label_map = {
        "knew": "✅ بلد بودم",
        "almost": "🟡 تقریبا بلد بودم",
        "dontknow": "❌ نبودم",
    }
    answer_text = ""
    if current.get("publish_answer"):
        answer_text = f"\n\n📘 پاسخ رسمی:\n{current['official_answer']}"
    else:
        answer_text = "\n\n📘 پاسخ رسمی منتشر نشده است."
    session["index"] += 1
    if session["index"] < len(session["questions"]):
        await query.edit_message_text(
            f"پاسخ شما ثبت شد: {label_map.get(response_type, response_type)}{answer_text}\n\nدر حال آماده‌سازی سوال بعدی...",
        )
        await _send_quiz_question(query, context)
    else:
        db_finish_quiz_session(session["session_id"], session["index"])
        _quiz_sessions.pop(query.from_user.id, None)
        await query.edit_message_text(
            f"پاسخ شما ثبت شد: {label_map.get(response_type, response_type)}{answer_text}\n\n✅ کوییز تمام شد."
        )


async def handle_student_followup(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    When a student replies to any bot message in their private chat,
    route it back to the group as a follow-up on the original question.
    """
    message = update.message
    if message is None or update.effective_chat.type != "private":
        return
    if message.reply_to_message is None:
        return
    
    student_id = update.effective_user.id
    allowed, wait_time = check_rate_limit(student_id)
    if not allowed:
        await message.reply_text(
            f"⏳ سرعت ارسال پیام شما بالاست.\nلطفاً {wait_time} ثانیه صبر کنید."
        )
        return

    student_id = update.effective_user.id
    replied_bot_msg_id = message.reply_to_message.message_id

    group_msg_id = db_get_group_msg_for_student_msg(replied_bot_msg_id, student_id)
    if group_msg_id is None:
        await message.reply_text("⚠️ پیامی که بهش ریپلای زدید در پایگاه داده ثبت نیست.")
        return

    tracking_id = db_get_tracking_id(group_msg_id) or ""
    student_name = update.effective_user.full_name or ""
    student_username = update.effective_user.username or ""
    profile_block = _build_user_profile_block(student_id, student_name, student_username)

    if tracking_id == "#BROADCAST":
        header = (
            "📢 پاسخ به پیام عمومی\n"
            f"{profile_block}\n\n"
        )
    else:
        header = (
            "🔄 پیگیری دانشجو\n"
            f"{profile_block}\n\n"
        )

    try:
        if message.text:
            sent = await context.bot.send_message(
                chat_id=GROUP_ID, text=f"{header}{message.text}",
                reply_to_message_id=group_msg_id,
                parse_mode="HTML",
            )
        elif message.photo:
            sent = await context.bot.send_photo(
                chat_id=GROUP_ID, photo=message.photo[-1].file_id,
                caption=f"{header}{message.caption or ''}",
                reply_to_message_id=group_msg_id,
                parse_mode="HTML",
            )
        elif message.document:
            sent = await context.bot.send_document(
                chat_id=GROUP_ID, document=message.document.file_id,
                caption=f"{header}{message.caption or ''}",
                reply_to_message_id=group_msg_id,
                parse_mode="HTML",
            )
        elif message.voice:
            sent = await context.bot.send_voice(
                chat_id=GROUP_ID,
                voice=message.voice.file_id,
                caption=f"{header}{message.caption or ''}",
                reply_to_message_id=group_msg_id,
                parse_mode="HTML",
            )
        else:
            sent = await context.bot.forward_message(
                chat_id=GROUP_ID, from_chat_id=student_id, message_id=message.message_id,
            )
        await message.reply_text("✅ پیگیری شما در گروه ارسال شد.")
        # Save in messages table so teacher can reply to it and it routes back to student
        has_media = bool(message.photo or message.document or message.voice)
        content = f"{header}{message.caption or ''}" if has_media else f"{header}{message.text or ''}"
        db_save_message(sent.message_id, tracking_id, content, has_media, student_id)
        # Track this new group message too so student can chain replies
        db_save_student_message(message.message_id, student_id, sent.message_id)
    except Exception as exc:
        logger.warning("Could not send follow-up to group: %s", exc)
        await message.reply_text("⚠️ ارسال ناموفق بود.")


async def ban_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reply to a bot question message with /ban to ban that student."""
    if update.effective_chat.id != GROUP_ID:
        return
    message = update.message
    if message.reply_to_message is None:
        await message.reply_text("⚠️ برای بن، روی پیام سوال ریپلای بزنید سپس /ban بنویسید.")
        return
    tracking_id = db_get_tracking_id(message.reply_to_message.message_id)
    if tracking_id is None:
        await message.reply_text("⚠️ این پیام مربوط به یک سوال ثبت‌شده نیست.")
        return
    student_id = db_get_student_id(tracking_id)
    if student_id is None:
        await message.reply_text("کاربر یافت نشد.")
        return
    db_ban_user(student_id)
    await message.reply_text(f"✅ کاربر بن شد (ID: {student_id}).")
    try:
        await context.bot.send_message(
            chat_id=student_id,
            text="⛔ شما بن شده‌اید و امکان ارسال پیام برای شما وجود ندارد.",
        )
    except Exception:
        pass


async def unban_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reply to a bot question message with /unban to unban that student."""
    if update.effective_chat.id != GROUP_ID:
        return
    message = update.message
    if message.reply_to_message is None:
        await message.reply_text("⚠️ روی پیام سوال ریپلای بزنید سپس /unban بنویسید.")
        return
    tracking_id = db_get_tracking_id(message.reply_to_message.message_id)
    if tracking_id is None:
        await message.reply_text("⚠️ این پیام مربوط به یک سوال ثبت‌شده نیست.")
        return
    student_id = db_get_student_id(tracking_id)
    if student_id is None:
        await message.reply_text("کاربر یافت نشد.")
        return
    db_unban_user(student_id)
    await message.reply_text(f"✅ کاربر انبن شد (ID: {student_id}).")
    try:
        await context.bot.send_message(
            chat_id=student_id,
            text="✅ بن شما برداشته شد. اکنون می‌توانید دوباره سوال بفرستید.",
        )
    except Exception:
        pass


async def delanswer_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete the bot's delivered answer in student's private chat.

    Usage in group: reply to the answer message and send /delanswer
    """
    if update.effective_chat.id != GROUP_ID:
        return

    message = update.message
    if message.reply_to_message is None:
        await message.reply_text("⚠️ روی پیام جواب ریپلای بزنید سپس /delanswer را بزنید.")
        return

    target_group_msg_id = message.reply_to_message.message_id
    mapping = db_get_student_msg_for_group_msg(target_group_msg_id)
    if mapping is None:
        await message.reply_text("⚠️ برای این پیام، نسخه‌ای که به کاربر ارسال شده ثبت نشده است.")
        return

    student_msg_id, student_id = mapping
    try:
        await context.bot.delete_message(chat_id=student_id, message_id=student_msg_id)
        db_delete_student_msg_mapping(student_msg_id, student_id)
        await message.reply_text("✅ پاسخ ارسالی از چت کاربر پاک شد.")
    except Exception as exc:
        logger.warning("Could not delete student message %s for user %s: %s", student_msg_id, student_id, exc)
        await message.reply_text("⚠️ حذف انجام نشد (ممکن است قبلاً پاک شده باشد یا دسترسی وجود نداشته باشد).")


async def delrecent_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Delete last N bot-sent messages in a student's private chat.

    Usage in group: reply to any student-related message then /delrecent <count>
    Example: /delrecent 5
    """
    if update.effective_chat.id != GROUP_ID:
        return

    message = update.message
    if message.reply_to_message is None:
        await message.reply_text("⚠️ روی پیام کاربر ریپلای بزنید سپس /delrecent 5 را بزنید.")
        return

    count = 5
    if context.args:
        try:
            count = int(context.args[0])
        except ValueError:
            await message.reply_text("⚠️ تعداد معتبر نیست. مثال: /delrecent 5")
            return
    if count < 1 or count > 50:
        await message.reply_text("⚠️ تعداد باید بین 1 تا 50 باشد.")
        return

    replied_id = message.reply_to_message.message_id
    student_id = None

    tracking_id = db_get_tracking_id(replied_id)
    if tracking_id is not None:
        student_id = db_get_student_id(tracking_id)

    if student_id is None:
        mapping = db_get_student_msg_for_group_msg(replied_id)
        if mapping is not None:
            _, student_id = mapping

    if student_id is None:
        await message.reply_text("⚠️ نتوانستم کاربر مقصد را از این پیام تشخیص بدهم.")
        return

    recent_msg_ids = db_get_recent_student_messages(student_id, count)
    if not recent_msg_ids:
        await message.reply_text("⚠️ پیام ثبت‌شده‌ای برای حذف از این کاربر پیدا نشد.")
        return

    success = 0
    failed = 0
    for student_msg_id in recent_msg_ids:
        try:
            await context.bot.delete_message(chat_id=student_id, message_id=student_msg_id)
            db_delete_student_msg_mapping(student_msg_id, student_id)
            success += 1
        except Exception as exc:
            logger.warning(
                "Could not bulk-delete student message %s for user %s: %s",
                student_msg_id,
                student_id,
                exc,
            )
            failed += 1

    await message.reply_text(
        f"✅ حذف انجام شد.\n"
        f"موفق: {success}\n"
        f"ناموفق: {failed}"
    )


async def delbroadcast_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    حذف پیام broadcast از چت همه دانشجوها.
    استفاده: روی پیام /broadcast ریپلای بزن و /delbroadcast بنویس.
    """
    if update.effective_chat.id != GROUP_ID:
        return

    message = update.message
    if message.reply_to_message is None:
        await message.reply_text(
            "⚠️ روی پیام دستور /broadcast ریپلای بزنید سپس /delbroadcast بنویسید."
        )
        return

    group_anchor_id = message.reply_to_message.message_id
    tracking_id = db_get_tracking_id(group_anchor_id)

    if tracking_id != "#BROADCAST":
        await message.reply_text(
            "⚠️ این پیام مربوط به یک broadcast ثبت‌شده نیست."
        )
        return

    # Find all messages sent to students that are linked to this broadcast.
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            SELECT student_msg_id, student_id
            FROM student_messages
            WHERE group_msg_id = ?
            """,
            (group_anchor_id,),
        ).fetchall()

    if not rows:
        await message.reply_text("⚠️ هیچ پیام broadcastی برای حذف پیدا نشد.")
        return

    success = 0
    failed = 0

    for student_msg_id, student_id in rows:
        try:
            await context.bot.delete_message(
                chat_id=student_id,
                message_id=student_msg_id,
            )
            db_delete_student_msg_mapping(student_msg_id, student_id)
            success += 1
            await asyncio.sleep(0.03)  # Avoid flooding Telegram with requests.
        except Exception as exc:
            logger.warning(
                "Could not delete broadcast msg %s for user %s: %s",
                student_msg_id,
                student_id,
                exc,
            )
            failed += 1

    await message.reply_text(
        f"✅ حذف پیام عمومی انجام شد.\n"
        f"موفق: {success}\n"
        f"ناموفق: {failed}"
    )


async def unanswered_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show unanswered questions with pagination. Only works inside the class group."""
    if update.effective_chat.id != GROUP_ID:
        return

    rows = db_get_unanswered()

    if not rows:
        await update.message.reply_text("✅ هیچ سوال بی‌پاسخی وجود ندارد.")
        return

    await update.message.reply_text(
        _unanswered_page_text(rows, 0),
        reply_markup=_unanswered_keyboard(0, len(rows)),
    )


async def unanswered_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle pagination button presses for /unanswered."""
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1])
    rows = db_get_unanswered()

    if not rows:
        await query.edit_message_text("✅ هیچ سوال بی‌پاسخی وجود ندارد.")
        return

    total_pages = max(1, (len(rows) + UNANSWERED_PAGE_SIZE - 1) // UNANSWERED_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    await query.edit_message_text(
        _unanswered_page_text(rows, page),
        reply_markup=_unanswered_keyboard(page, len(rows)),
    )


async def single_question_users_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show users who have asked exactly one question with pagination."""
    if update.effective_chat.id != GROUP_ID:
        return

    rows = db_get_single_question_users()

    if not rows:
        await update.message.reply_text("✅ کاربری که فقط یک سوال پرسیده باشد وجود ندارد.")
        return

    await update.message.reply_text(
        _single_question_users_page_text(rows, 0),
        reply_markup=_single_question_users_keyboard(0, len(rows)),
    )


async def single_question_users_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle pagination button presses for /singlequestion."""
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1])
    rows = db_get_single_question_users()

    if not rows:
        await query.edit_message_text("✅ کاربری که فقط یک سوال پرسیده باشد وجود ندارد.")
        return

    total_pages = max(1, (len(rows) + UNANSWERED_PAGE_SIZE - 1) // UNANSWERED_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    await query.edit_message_text(
        _single_question_users_page_text(rows, page),
        reply_markup=_single_question_users_keyboard(page, len(rows)),
    )

async def allmembers_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show all students with pagination."""
    if update.effective_chat.id != GROUP_ID:
        return

    rows = db_get_all_students_with_info()

    if not rows:
        await update.message.reply_text("⚠️ هنوز هیچ دانشجویی ثبت نشده است.")
        return

    await update.message.reply_text(
        _allmembers_page_text(rows, 0),
        reply_markup=_allmembers_keyboard(0, len(rows)),
        parse_mode="HTML"
    )


async def allmembers_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1])
    rows = db_get_all_students_with_info()

    if not rows:
        await query.edit_message_text("⚠️ هیچ دانشجویی یافت نشد.")
        return

    await query.edit_message_text(
        _allmembers_page_text(rows, page),
        reply_markup=_allmembers_keyboard(page, len(rows)),
        parse_mode="HTML"
    )


async def adminstats_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show how many questions each admin has answered."""
    if update.effective_chat.id != GROUP_ID:
        return

    rows = db_get_admin_stats()
    if not rows:
        await update.message.reply_text("✅ هنوز پاسخی برای شمارش ثبت نشده است.")
        return

    admin_map: dict[int, tuple[str, str]] = {}
    try:
        admins = await context.bot.get_chat_administrators(chat_id=GROUP_ID)
        for admin in admins:
            admin_map[admin.user.id] = (
                admin.user.full_name or "",
                f"@{admin.user.username}" if admin.user.username else "",
            )
    except Exception as exc:
        logger.warning("Could not fetch group administrators for stats: %s", exc)

    lines = ["📊 آمار پاسخ ادمین‌ها\n"]
    for answered_by, stored_name, stored_username, answer_count in rows:
        display_name, display_username = admin_map.get(
            answered_by,
            (stored_name or f"ID {answered_by}", stored_username or ""),
        )
        if display_username:
            display_name = f"{display_name} ({display_username})"
        lines.append(f"• {display_name} — {answer_count} سوال")

    await update.message.reply_text("\n".join(lines))


async def stats_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show answered/unanswered question counts. Works in the class group."""
    if update.effective_chat.id != GROUP_ID:
        return
    answered, unanswered = db_get_stats()
    total = answered + unanswered
    await update.message.reply_text(
        f"📊 آمار سوالات\n"
        f"─────────────────\n"
        f"✅ جواب داده شده: {answered}\n"
        f"❓ بی‌پاسخ: {unanswered}\n"
        f"📦 مجموع: {total}"
    )


async def broadcast_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.id != GROUP_ID:
        return

    message = update.message
    student_ids = db_get_all_student_ids()
    if not student_ids:
        await message.reply_text("⚠️ هیچ کاربری در پایگاه داده وجود ندارد.")
        return

    BROADCAST_HEADER = (
        "📢 <b>پیام عمومی از طرف مدیریت کلاس</b>\n"
        "────────────────────────────\n\n"
    )

    broadcast_text = " ".join(context.args) if context.args else None
    reply_msg = message.reply_to_message

    if not broadcast_text and not reply_msg:
        await message.reply_text(
            "نحوه استفاده:\n"
            "• /broadcast متن پیام\n"
            "• یا روی یک پیام Reply بزن و /broadcast بنویس"
        )
        return

    # Build a message preview.
    if reply_msg:
        if reply_msg.text:
            preview = reply_msg.text[:300]
        elif reply_msg.caption:
            preview = reply_msg.caption[:300]
        elif reply_msg.photo:
            preview = "[📷 عکس]"
        elif reply_msg.document:
            preview = f"[📎 {reply_msg.document.file_name or 'فایل'}]"
        elif reply_msg.voice:
            preview = "[🎤 ویس]"
        else:
            preview = "[پیام]"
    else:
        preview = broadcast_text[:300] if broadcast_text else "[پیام]"

    confirm_id = generate_confirm_id()
    _pending_broadcasts[confirm_id] = {
        "admin_id": update.effective_user.id,
        "group_anchor_id": message.message_id,
        "broadcast_text": broadcast_text,
        "reply_msg": reply_msg,          # May be None.
        "student_ids": student_ids,
        "header": BROADCAST_HEADER,
    }

    confirm_text = (
        f"📢 <b>تایید ارسال پیام عمومی</b>\n\n"
        f"تعداد گیرندگان: <b>{len(student_ids)}</b> نفر\n\n"
        f"📝 پیش‌نمایش:\n{preview}\n\n"
        f"آیا مطمئن هستید که می‌خواهید این پیام برای همه ارسال شود؟"
    )

    await message.reply_text(
        confirm_text,
        parse_mode="HTML",
        reply_markup=_broadcast_confirmation_keyboard(confirm_id),
    )

# ---------------------------------------------------------------------------
# PTB Application (module-level so PythonAnywhere's WSGI import picks it up)
# ---------------------------------------------------------------------------

ptb_app = Application.builder().token(BOT_TOKEN).build()

ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("setgroup", setgroup_command))
ptb_app.add_handler(CommandHandler("quiz", quiz_list_command))
ptb_app.add_handler(CommandHandler("quizadd", quiz_add_command))
ptb_app.add_handler(CommandHandler("unanswered", unanswered_command))
ptb_app.add_handler(CommandHandler("singlequestion", single_question_users_command))
ptb_app.add_handler(CommandHandler("allmembers", allmembers_command))
ptb_app.add_handler(CommandHandler("ban", ban_command))
ptb_app.add_handler(CommandHandler("unban", unban_command))
ptb_app.add_handler(CommandHandler("delanswer", delanswer_command))
ptb_app.add_handler(CommandHandler("delrecent", delrecent_command))
ptb_app.add_handler(CommandHandler("adminstats", adminstats_command))
ptb_app.add_handler(CommandHandler("stats", stats_command))
ptb_app.add_handler(CommandHandler("broadcast", broadcast_command))
ptb_app.add_handler(CommandHandler("delbroadcast", delbroadcast_command))
ptb_app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
ptb_app.add_handler(CallbackQueryHandler(quiz_start_callback, pattern="^quiz_start:"))
ptb_app.add_handler(CallbackQueryHandler(quiz_response_callback, pattern="^quiz_response:"))
ptb_app.add_handler(CallbackQueryHandler(unanswered_page_callback, pattern="^unanswered_page:"))
ptb_app.add_handler(CallbackQueryHandler(single_question_users_page_callback, pattern="^single_question_page:"))
ptb_app.add_handler(CallbackQueryHandler(allmembers_page_callback, pattern="^allmembers_page:"))
ptb_app.add_handler(CallbackQueryHandler(confirm_answer_callback, pattern="^confirm_answer:"))
ptb_app.add_handler(CallbackQueryHandler(cancel_answer_callback, pattern="^cancel_answer:"))
ptb_app.add_handler(CallbackQueryHandler(confirm_broadcast_callback, pattern="^confirm_broadcast:"))
ptb_app.add_handler(CallbackQueryHandler(cancel_broadcast_callback, pattern="^cancel_broadcast:"))
# Follow-up replies (private chat, replying to a bot message) — must come BEFORE handle_private_message
ptb_app.add_handler(
    MessageHandler(
        filters.ChatType.PRIVATE & filters.REPLY,
        handle_student_followup,
    )
)
ptb_app.add_handler(
    MessageHandler(
        filters.ChatType.PRIVATE & ~filters.REPLY & (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.ANIMATION),
        handle_private_message,
    )
)
ptb_app.add_handler(
    MessageHandler(
        filters.Chat(GROUP_ID) & filters.REPLY,
        handle_group_reply,
    )
)

# ---------------------------------------------------------------------------
# Flask web app
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.route("/webhook", methods=["POST"])
def webhook():
    """Receive Telegram updates after validating Telegram's webhook secret."""
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not WEBHOOK_SECRET_TOKEN or not hmac.compare_digest(
        supplied_secret, WEBHOOK_SECRET_TOKEN
    ):
        return Response("Unauthorized", status=401)

    data = request.get_json(force=True)
    update = Update.de_json(data, ptb_app.bot)
    # Fire-and-forget: return 200 immediately so Telegram never retries
    asyncio.run_coroutine_threadsafe(ptb_app.process_update(update), _loop)
    return Response("OK", status=200)


@flask_app.route("/set_webhook", methods=["POST"])
def set_webhook():
    """Register webhook only when caller presents a separate setup secret."""
    setup_key = os.environ.get("WEBHOOK_SETUP_KEY", "")
    supplied_key = request.headers.get("X-Webhook-Setup-Key", "")
    if not setup_key or not hmac.compare_digest(supplied_key, setup_key):
        return Response("Unauthorized", status=401)
    if not WEBHOOK_SECRET_TOKEN:
        return Response("WEBHOOK_SECRET_TOKEN is not configured.", status=503)

    host = os.environ.get("DOMAIN") or request.host
    webhook_url = f"https://{host}/webhook"
    run_async(ptb_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET_TOKEN,
        allowed_updates=["message", "callback_query", "poll"],
    ))
    return "Webhook registered.", 200


@flask_app.route("/", methods=["GET"])
def index():
    return "Bot is running."


# ---------------------------------------------------------------------------
# Initialise once at import time (required by PythonAnywhere WSGI)
# ---------------------------------------------------------------------------

init_db()
run_async(ptb_app.initialize())
run_async(ptb_app.start())

# Register bot commands so Telegram shows autocomplete in chats
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeAllPrivateChats,
)
_private_commands = [
    BotCommand("start", "🚀 شروع / قوانین"),
 #   BotCommand("quiz", "🧠 Start study quiz"),
]
_group_commands = [
    BotCommand("setgroup", "➕ ثبت این گروه به‌عنوان مقصد سوال‌ها"),
    BotCommand("quizadd", "📝 ساخت سوال درسی جدید"),
    BotCommand("unanswered", "📋 لیست سوالات بی‌پاسخ"),
    BotCommand("singlequestion", "📋 کاربران با فقط یک سوال"),
    BotCommand("ban", "⛔ بن کردن دانشجو (ریپلای روی سوال)"),
    BotCommand("unban", "✅ رفع بن دانشجو (ریپلای روی سوال)"),
    BotCommand("delanswer", "🗑 حذف پاسخ ارسالی کاربر (ریپلای روی جواب)"),
    BotCommand("delrecent", "🧹 حذف چند پیام اخیر بات برای کاربر"),
    BotCommand("adminstats", "📊 آمار پاسخ هر ادمین"),
    BotCommand("broadcast", "📢 ارسال پیام به همه دانشجوها"),
    BotCommand("stats", "📊 آمار سوالات جواب داده و بی‌پاسخ"),
    BotCommand("allmembers", "👥 نمایش تمام دانشجویان"),
    BotCommand("delbroadcast", "🗑 حذف پیام عمومی از چت همه"),
]
run_async(ptb_app.bot.set_my_commands(_private_commands, scope=BotCommandScopeAllPrivateChats()))
run_async(ptb_app.bot.set_my_commands(_group_commands, scope=BotCommandScopeChat(chat_id=GROUP_ID)))

logger.info("Bot initialised and ready.")

# PythonAnywhere looks for a variable named 'application'
application = flask_app

## --------------------------------------------------------------------------- 
# Rate-limit memory cleanup (to reduce RAM usage).
# ---------------------------------------------------------------------------

async def cleanup_rate_limit():
    """Clean up the in-memory rate-limit tracker every 10 minutes."""
    while True:
        try:
            await asyncio.sleep(600)  # Run every 10 minutes.
            now = time.time()
            to_delete = []
            
            for uid, times in list(_user_message_times.items()):
                # Remove timestamps older than twice the rate-limit window.
                _user_message_times[uid] = [t for t in times if now - t < RATE_LIMIT_SECONDS * 2]
                
                if not _user_message_times[uid]:
                    to_delete.append(uid)
            
            # Remove users whose timestamp lists are empty.
            for uid in to_delete:
                _user_message_times.pop(uid, None)
                
        except Exception as e:
            logger.warning(f"Cleanup rate limit error: {e}")


# Start the cleanup task.
asyncio.run_coroutine_threadsafe(cleanup_rate_limit(), _loop)

if __name__ == "__main__":
    # Railway injects PORT; fall back to 5000 for local dev
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, debug=False)

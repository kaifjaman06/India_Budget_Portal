from __future__ import annotations

import csv
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import parse_qs, unquote, urlsplit


APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
SEED_FILE = DATA_DIR / "budget_seed.csv"
DISTRICT_CATALOG_FILE = DATA_DIR / "india_district_catalog.json"
DB_PATH = Path(os.environ.get("BUDGET_DB_PATH", DATA_DIR / "budget.db"))
BACKUP_DIR = Path(os.environ.get("BUDGET_BACKUP_DIR", DATA_DIR / "backups"))
BACKUP_RETENTION_COUNT = 30
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
FISCAL_YEARS = ("2018-19", "2019-20", "2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26", "2026-27")
BUDGET_STAGES = ("budget_estimate", "revised_estimate", "actual", "funds_received")
MAX_FILTER_LENGTH = 120
MAX_IMPORT_REQUEST_BYTES = 32 * 1024 * 1024
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
CAPTCHA_MAX_AGE_SECONDS = 5 * 60
MAX_JSON_BODY_BYTES = 6 * 1024 * 1024
WEB_DEMO_PASSWORD = "india@"
WEB_PASSWORD_ITERATIONS = 310_000
CAPTCHA_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
SESSION_LOCK = Lock()
WEB_SESSIONS: dict[str, tuple[dict[str, object], float]] = {}
CAPTCHA_CHALLENGES: dict[str, tuple[str, float, str]] = {}

SECTORS = (
    ("education", "Education"),
    ("agriculture", "Agriculture"),
    ("roadways_transport", "Roadways & Transport"),
    ("healthcare", "Healthcare"),
)

# These are internal stable keys, not official government/ISO codes.
JURISDICTIONS = (
    ("IN-UNION", "Union Government", "union", None),
    ("IN-AP", "Andhra Pradesh", "state", "IN-UNION"),
    ("IN-AR", "Arunachal Pradesh", "state", "IN-UNION"),
    ("IN-AS", "Assam", "state", "IN-UNION"),
    ("IN-BR", "Bihar", "state", "IN-UNION"),
    ("IN-CG", "Chhattisgarh", "state", "IN-UNION"),
    ("IN-GA", "Goa", "state", "IN-UNION"),
    ("IN-GJ", "Gujarat", "state", "IN-UNION"),
    ("IN-HR", "Haryana", "state", "IN-UNION"),
    ("IN-HP", "Himachal Pradesh", "state", "IN-UNION"),
    ("IN-JH", "Jharkhand", "state", "IN-UNION"),
    ("IN-KA", "Karnataka", "state", "IN-UNION"),
    ("IN-KL", "Kerala", "state", "IN-UNION"),
    ("IN-MP", "Madhya Pradesh", "state", "IN-UNION"),
    ("IN-MH", "Maharashtra", "state", "IN-UNION"),
    ("IN-MH-THANE", "Thane", "district", "IN-MH"),
    ("IN-MH-RAIGAD", "Raigad", "district", "IN-MH"),
    ("IN-MH-RATNAGIRI", "Ratnagiri", "district", "IN-MH"),
    ("IN-MH-SINDHUDURG", "Sindhudurg", "district", "IN-MH"),
    ("IN-MH-PALGHAR", "Palghar", "district", "IN-MH"),
    ("IN-MH-SATARA", "Satara", "district", "IN-MH"),
    ("IN-MH-SANGLI", "Sangli", "district", "IN-MH"),
    ("IN-MH-SOLAPUR", "Solapur", "district", "IN-MH"),
    ("IN-MH-KOLHAPUR", "Kolhapur", "district", "IN-MH"),
    ("IN-MH-CHHATRAPATI-SAMBHAJINAGAR", "Chhatrapati Sambhajinagar", "district", "IN-MH"),
    ("IN-MH-PARBHANI", "Parbhani", "district", "IN-MH"),
    ("IN-MH-BEED", "Beed", "district", "IN-MH"),
    ("IN-MH-NANDED", "Nanded", "district", "IN-MH"),
    ("IN-MH-DHARASHIV", "Dharashiv", "district", "IN-MH"),
    ("IN-MH-JALNA", "Jalna", "district", "IN-MH"),
    ("IN-MH-LATUR", "Latur", "district", "IN-MH"),
    ("IN-MH-HINGOLI", "Hingoli", "district", "IN-MH"),
    ("IN-MH-GONDIA", "Gondia", "district", "IN-MH"),
    ("IN-MH-WARDHA", "Wardha", "district", "IN-MH"),
    ("IN-MH-NAGPUR", "Nagpur", "district", "IN-MH"),
    ("IN-MH-BHANDARA", "Bhandara", "district", "IN-MH"),
    ("IN-MH-CHANDRAPUR", "Chandrapur", "district", "IN-MH"),
    ("IN-MH-GADCHIROLI", "Gadchiroli", "district", "IN-MH"),
    ("IN-MH-NASHIK", "Nashik", "district", "IN-MH"),
    ("IN-MH-DHULE", "Dhule", "district", "IN-MH"),
    ("IN-MH-JALGAON", "Jalgaon", "district", "IN-MH"),
    ("IN-MH-AHILYANAGAR", "Ahilyanagar", "district", "IN-MH"),
    ("IN-MH-NANDURBAR", "Nandurbar", "district", "IN-MH"),
    ("IN-MH-AMRAVATI", "Amravati", "district", "IN-MH"),
    ("IN-MH-AKOLA", "Akola", "district", "IN-MH"),
    ("IN-MH-BULDHANA", "Buldhana", "district", "IN-MH"),
    ("IN-MH-YAVATMAL", "Yavatmal", "district", "IN-MH"),
    ("IN-MH-WASHIM", "Washim", "district", "IN-MH"),
    ("IN-MH-MUMBAI-CITY", "Mumbai City", "district", "IN-MH"),
    ("IN-MH-MUMBAI-SUBURBAN", "Mumbai Suburban", "district", "IN-MH"),
    ("IN-MN", "Manipur", "state", "IN-UNION"),
    ("IN-ML", "Meghalaya", "state", "IN-UNION"),
    ("IN-MZ", "Mizoram", "state", "IN-UNION"),
    ("IN-NL", "Nagaland", "state", "IN-UNION"),
    ("IN-OD", "Odisha", "state", "IN-UNION"),
    ("IN-PB", "Punjab", "state", "IN-UNION"),
    ("IN-RJ", "Rajasthan", "state", "IN-UNION"),
    ("IN-SK", "Sikkim", "state", "IN-UNION"),
    ("IN-TN", "Tamil Nadu", "state", "IN-UNION"),
    ("IN-TS", "Telangana", "state", "IN-UNION"),
    ("IN-TR", "Tripura", "state", "IN-UNION"),
    ("IN-UP", "Uttar Pradesh", "state", "IN-UNION"),
    ("IN-UK", "Uttarakhand", "state", "IN-UNION"),
    ("IN-WB", "West Bengal", "state", "IN-UNION"),
    ("IN-AN", "Andaman and Nicobar Islands", "union_territory", "IN-UNION"),
    ("IN-CH", "Chandigarh", "union_territory", "IN-UNION"),
    ("IN-DNHDD", "Dadra and Nagar Haveli and Daman and Diu", "union_territory", "IN-UNION"),
    ("IN-DL", "National Capital Territory of Delhi", "union_territory", "IN-UNION"),
    ("IN-JK", "Jammu and Kashmir", "union_territory", "IN-UNION"),
    ("IN-LA", "Ladakh", "union_territory", "IN-UNION"),
    ("IN-LD", "Lakshadweep", "union_territory", "IN-UNION"),
    ("IN-PY", "Puducherry", "union_territory", "IN-UNION"),
    ("IN-MH-PUNE", "Pune", "district", "IN-MH"),
)
with DISTRICT_CATALOG_FILE.open("r", encoding="utf-8") as district_catalog_file:
    additional_districts = json.load(district_catalog_file)
JURISDICTIONS = (
    *JURISDICTIONS,
    *(
        (district["code"], district["name"], "district", district["parent_code"])
        for district in additional_districts
    ),
)
JURISDICTION_BY_CODE = {code: (name, kind, parent) for code, name, kind, parent in JURISDICTIONS}
JURISDICTION_BY_NAME = {name.casefold(): code for code, name, _, _ in JURISDICTIONS}
DISTRICT_NAME_ALIASES = {
    "ahmednagar": "Ahilyanagar",
    "aurangabad": "Chhatrapati Sambhajinagar",
    "osmanabad": "Dharashiv",
    "mumbai": "Mumbai City",
}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY,
    source_document TEXT NOT NULL,
    source_url TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staged', 'reviewed', 'published')),
    corrections TEXT NOT NULL,
    staged_by TEXT NOT NULL,
    reviewed_by TEXT,
    review_notes TEXT,
    reviewed_at TEXT,
    published_at TEXT,
    row_count INTEGER NOT NULL,
    scope_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    username TEXT PRIMARY KEY COLLATE NOCASE,
    password_salt BLOB NOT NULL,
    password_hash BLOB NOT NULL,
    password_iterations INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN (
        'admin', 'data_entry', 'reviewer', 'citizen', 'central_admin', 'state_auditor'
    )),
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    email TEXT UNIQUE COLLATE NOCASE,
    jurisdiction_code TEXT REFERENCES jurisdictions(code),
    force_password_change INTEGER NOT NULL DEFAULT 0 CHECK (force_password_change IN (0, 1))
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;

CREATE TABLE IF NOT EXISTS staged_budget_figures (
    id INTEGER PRIMARY KEY,
    import_batch_id TEXT NOT NULL REFERENCES import_batches(id),
    line_number INTEGER NOT NULL,
    jurisdiction_code TEXT NOT NULL REFERENCES jurisdictions(code),
    fiscal_year TEXT NOT NULL,
    budget_stage TEXT NOT NULL,
    department_name_original TEXT,
    sector_name_original TEXT,
    sector_category_slug TEXT,
    amount NUMERIC NOT NULL,
    spent_amount NUMERIC CHECK (spent_amount IS NULL OR spent_amount >= 0),
    currency_code TEXT NOT NULL,
    amount_unit TEXT NOT NULL,
    source_page_or_table TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    correction_notes TEXT NOT NULL,
    UNIQUE (import_batch_id, line_number)
);

CREATE TABLE IF NOT EXISTS jurisdictions (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    jurisdiction_type TEXT NOT NULL CHECK (
        jurisdiction_type IN ('union', 'state', 'union_territory', 'district')
    ),
    parent_code TEXT REFERENCES jurisdictions(code),
    UNIQUE (parent_code, name)
);

CREATE TABLE IF NOT EXISTS sector_categories (
    slug TEXT PRIMARY KEY,
    display_name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS budget_figures (
    id INTEGER PRIMARY KEY,
    jurisdiction_code TEXT NOT NULL REFERENCES jurisdictions(code),
    fiscal_year TEXT NOT NULL,
    budget_stage TEXT NOT NULL CHECK (
        budget_stage IN ('budget_estimate', 'revised_estimate', 'actual', 'funds_received')
    ),
    department_name_original TEXT,
    sector_name_original TEXT,
    sector_category_slug TEXT REFERENCES sector_categories(slug),
    amount NUMERIC NOT NULL CHECK (amount >= 0),
    spent_amount NUMERIC CHECK (spent_amount IS NULL OR spent_amount >= 0),
    currency_code TEXT NOT NULL,
    amount_unit TEXT NOT NULL,
    source TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    import_batch_id TEXT REFERENCES import_batches(id),
    source_page_or_table TEXT,
    correction_notes TEXT,
    UNIQUE (
        jurisdiction_code, fiscal_year, budget_stage, department_name_original,
        sector_name_original, sector_category_slug, source, source_locator
    )
);

CREATE INDEX IF NOT EXISTS idx_figures_year_area_stage
    ON budget_figures (fiscal_year, jurisdiction_code, budget_stage);
CREATE INDEX IF NOT EXISTS idx_figures_category
    ON budget_figures (sector_category_slug);
CREATE UNIQUE INDEX IF NOT EXISTS uq_budget_figure_source_line_v2
    ON budget_figures (
        jurisdiction_code, fiscal_year, budget_stage,
        COALESCE(department_name_original, ''),
        COALESCE(sector_name_original, ''),
        COALESCE(sector_category_slug, ''), source, source_locator
    );
"""

ALLOWED_STATIC_SUFFIXES = {".html", ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".webp", ".csv"}


def connect_database() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database_connection() -> Iterator[sqlite3.Connection]:
    connection = connect_database()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


# The log is chained via the previous event hash so tampering is detectable without trusting a separate ledger.

def append_audit_event(
    connection: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, object],
) -> None:
    previous = connection.execute(
        "SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    previous_hash = previous["event_hash"] if previous else "0" * 64
    occurred_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    details_json = json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    hash_input = "\n".join(
        (occurred_at, actor, action, entity_type, entity_id, details_json, previous_hash)
    )
    event_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
    connection.execute(
        """
        INSERT INTO audit_events (
            occurred_at, actor, action, entity_type, entity_id,
            details_json, previous_hash, event_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            occurred_at, actor, action, entity_type, entity_id,
            details_json, previous_hash, event_hash,
        ),
    )


def verify_audit_log() -> dict[str, object]:
    with database_connection() as connection:
        events = connection.execute(
            """
            SELECT occurred_at, actor, action, entity_type, entity_id,
                   details_json, previous_hash, event_hash
            FROM audit_events ORDER BY id
            """
        ).fetchall()
    previous_hash = "0" * 64
    for event_index, event in enumerate(events, start=1):
        if event["previous_hash"] != previous_hash:
            return {
                "valid": False,
                "verified_events": event_index - 1,
                "reason": "Audit hash chain is broken.",
            }
        hash_input = "\n".join(
            (
                event["occurred_at"], event["actor"], event["action"],
                event["entity_type"], event["entity_id"], event["details_json"],
                event["previous_hash"],
            )
        )
        calculated_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(calculated_hash, event["event_hash"]):
            return {
                "valid": False,
                "verified_events": event_index - 1,
                "reason": f"Audit event {event['event_hash']} failed integrity verification.",
            }
        previous_hash = event["event_hash"]
    return {"valid": True, "verified_events": len(events), "reason": None}


def issue_captcha() -> str:
    challenge_id = secrets.token_urlsafe(24)
    answer = "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(5))
    escaped_answer = html.escape(answer)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="150" height="48" '
        'viewBox="0 0 150 48" role="img" aria-label="CAPTCHA challenge">'
        '<rect width="150" height="48" rx="5" fill="#f5eee8"/>'
        f'<path d="M2 14 Q45 3 74 30 T148 16 M0 40 Q50 28 95 44" '
        f'fill="none" stroke="#c77a45" stroke-width="1.2"/>'
        f'<text x="75" y="33" text-anchor="middle" letter-spacing="8" '
        f'font-family="monospace" font-size="24" font-weight="700" fill="#593b2d">{escaped_answer}</text>'
        '</svg>'
    )
    expiry = time.monotonic() + CAPTCHA_MAX_AGE_SECONDS
    with SESSION_LOCK:
        CAPTCHA_CHALLENGES[challenge_id] = (answer, expiry, svg)
        expired = [
            key for key, (_, challenge_expiry, _) in CAPTCHA_CHALLENGES.items()
            if challenge_expiry <= time.monotonic()
        ]
        for key in expired:
            CAPTCHA_CHALLENGES.pop(key, None)
    return challenge_id


def consume_captcha(challenge_id: str, answer: str) -> bool:
    with SESSION_LOCK:
        challenge = CAPTCHA_CHALLENGES.pop(challenge_id, None)
    if challenge is None or challenge[1] <= time.monotonic():
        return False
    return secrets.compare_digest(challenge[0], answer.strip().upper())


def captcha_svg(challenge_id: str) -> str | None:
    with SESSION_LOCK:
        challenge = CAPTCHA_CHALLENGES.get(challenge_id)
        if challenge is None:
            return None
        if challenge[1] <= time.monotonic():
            CAPTCHA_CHALLENGES.pop(challenge_id, None)
            return None
        return challenge[2]


def create_web_session(user: dict[str, object]) -> str:
    token = secrets.token_urlsafe(32)
    with SESSION_LOCK:
        WEB_SESSIONS[token] = (user, time.monotonic() + SESSION_MAX_AGE_SECONDS)
    return token


def get_web_session(cookie_header: str) -> tuple[str, dict[str, object]] | None:
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except (CookieError, ValueError):
        return None
    morsel = cookie.get("budget_session")
    if morsel is None:
        return None
    token = morsel.value
    with SESSION_LOCK:
        session = WEB_SESSIONS.get(token)
        if session is None:
            return None
        user, expiry = session
        if expiry <= time.monotonic():
            WEB_SESSIONS.pop(token, None)
            return None
    with database_connection() as connection:
        current = connection.execute(
            """
            SELECT username, role, email, jurisdiction_code, force_password_change
            FROM admin_users WHERE username = ? COLLATE NOCASE AND disabled = 0
            """,
            (user["username"],),
        ).fetchone()
    if current is None:
        with SESSION_LOCK:
            WEB_SESSIONS.pop(token, None)
        return None
    refreshed_user = dict(current)
    with SESSION_LOCK:
        WEB_SESSIONS[token] = (refreshed_user, expiry)
    return token, refreshed_user


def authenticate_web_user(username: str, password: str) -> dict[str, object] | None:
    with database_connection() as connection:
        user = connection.execute(
            """
            SELECT username, password_salt, password_hash, password_iterations,
                   role, email, jurisdiction_code, force_password_change
            FROM admin_users
            WHERE (username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE)
              AND disabled = 0
            """,
            (username.strip(), username.strip()),
        ).fetchone()
        if user is None:
            salt = bytes(16)
            expected_hash = bytes(32)
            iterations = WEB_PASSWORD_ITERATIONS
        else:
            salt = user["password_salt"]
            expected_hash = user["password_hash"]
            iterations = user["password_iterations"]
        supplied_hash = _web_password_hash(password, salt, iterations)
        if user is None or not secrets.compare_digest(supplied_hash, expected_hash):
            append_audit_event(
                connection,
                actor="anonymous",
                action="web_login_failed",
                entity_type="account",
                entity_id=username.strip()[:128] or "(empty)",
                details={"reason": "invalid_credentials"},
            )
            return None
        append_audit_event(
            connection,
            actor=user["username"],
            action="web_login_succeeded",
            entity_type="account",
            entity_id=user["username"],
            details={"role": user["role"]},
        )
        return {
            "username": user["username"],
            "role": user["role"],
            "email": user["email"],
            "jurisdiction_code": user["jurisdiction_code"],
            "force_password_change": bool(user["force_password_change"]),
        }


def register_citizen(email: str, password: str) -> dict[str, object]:
    email = email.strip().casefold()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email address.")
    if len(password) < 8 or len(password) > 128:
        raise ValueError("Password must be between 8 and 128 characters.")
    salt = secrets.token_bytes(16)
    password_hash = _web_password_hash(password, salt, WEB_PASSWORD_ITERATIONS)
    with database_connection() as connection:
        try:
            connection.execute(
                """
                INSERT INTO admin_users (
                    username, password_salt, password_hash, password_iterations,
                    role, created_at, created_by, email
                ) VALUES (?, ?, ?, ?, 'citizen', ?, 'self-registration', ?)
                """,
                (
                    email, salt, password_hash, WEB_PASSWORD_ITERATIONS,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"), email,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("An account with that email already exists.") from error
        append_audit_event(
            connection,
            actor=email,
            action="citizen_account_registered",
            entity_type="account",
            entity_id=email,
            details={"role": "citizen"},
        )
    return {
        "username": email,
        "role": "citizen",
        "email": email,
        "jurisdiction_code": None,
        "force_password_change": False,
    }


def create_database_backup(destination: Path | None = None, *, actor: str = "system") -> Path:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Budget database does not exist: {DB_PATH}")
    backup_directory = destination.parent if destination else BACKUP_DIR
    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = destination or (
        backup_directory
        / f"budget-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
    )
    if backup_path.resolve() == DB_PATH.resolve():
        raise ValueError("Backup destination must not be the active budget database.")
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{backup_path.name}.", suffix=".tmp", dir=backup_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with database_connection() as connection:
            append_audit_event(
                connection,
                actor=actor,
                action="database_backup_started",
                entity_type="database",
                entity_id=DB_PATH.name,
                details={"destination": str(backup_path)},
            )
        source_connection = sqlite3.connect(DB_PATH, timeout=10)
        backup_connection = sqlite3.connect(temporary_path)
        try:
            source_connection.backup(backup_connection)
        finally:
            backup_connection.close()
            source_connection.close()
        backup_connection = sqlite3.connect(temporary_path)
        try:
            integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            backup_connection.close()
        if not integrity or integrity[0] != "ok":
            raise sqlite3.DatabaseError("Backup integrity check failed.")
        os.replace(temporary_path, backup_path)
        with database_connection() as connection:
            append_audit_event(
                connection,
                actor=actor,
                action="database_backup_completed",
                entity_type="database",
                entity_id=DB_PATH.name,
                details={"destination": str(backup_path), "integrity": "ok"},
            )
        if destination is None:
            backups = sorted(
                BACKUP_DIR.glob("budget-*.sqlite3"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for expired_backup in backups[BACKUP_RETENTION_COUNT:]:
                expired_backup.unlink()
        return backup_path
    except (OSError, sqlite3.Error):
        temporary_path.unlink(missing_ok=True)
        logging.exception("Could not create budget database backup at %s.", backup_path)
        raise


def start_daily_backup_scheduler() -> Thread:
    stop_event = Event()

    def backup_daily() -> None:
        while not stop_event.wait(24 * 60 * 60):
            try:
                create_database_backup()
            except (OSError, sqlite3.Error, ValueError):
                logging.exception("Scheduled budget database backup failed.")

    scheduler = Thread(target=backup_daily, name="budget-daily-backup", daemon=True)
    scheduler.start()
    return scheduler


def read_seed_figures() -> list[tuple[object, ...]]:
    figures: list[tuple[object, ...]] = []
    with SEED_FILE.open("r", encoding="utf-8-sig", newline="") as seed_file:
        reader = csv.DictReader(seed_file)
        required_columns = {
            "jurisdiction_code", "fiscal_year", "budget_stage",
            "department_name_original", "sector_name_original",
            "sector_category_slug", "amount", "currency_code",
            "amount_unit", "source", "source_locator",
        }
        if not reader.fieldnames or not required_columns.issubset(reader.fieldnames):
            raise ValueError("Budget seed CSV is missing one or more required columns.")

        for line_number, row in enumerate(reader, start=2):
            try:
                jurisdiction_code = row["jurisdiction_code"].strip()
                fiscal_year = row["fiscal_year"].strip()
                budget_stage = row["budget_stage"].strip()
                department_name = row["department_name_original"].strip() or None
                sector_name = row["sector_name_original"].strip() or None
                sector_slug = row["sector_category_slug"].strip() or None
                amount = Decimal(row["amount"].strip())
                currency_code = row["currency_code"].strip().upper()
                amount_unit = row["amount_unit"].strip().lower()
                source = row["source"].strip()
                source_locator = row["source_locator"].strip()
            except (AttributeError, InvalidOperation) as error:
                raise ValueError(f"Invalid budget seed data at CSV line {line_number}.") from error

            if jurisdiction_code not in JURISDICTION_BY_CODE:
                raise ValueError(f"Unknown jurisdiction at CSV line {line_number}: {jurisdiction_code}.")
            if fiscal_year not in FISCAL_YEARS:
                raise ValueError(f"Unsupported fiscal year at CSV line {line_number}: {fiscal_year}.")
            if budget_stage not in BUDGET_STAGES:
                raise ValueError(f"Unsupported budget stage at CSV line {line_number}: {budget_stage}.")
            if sector_slug is not None and sector_slug not in {slug for slug, _ in SECTORS}:
                raise ValueError(f"Unknown sector category at CSV line {line_number}: {sector_slug}.")
            if bool(sector_name) != bool(sector_slug):
                raise ValueError(f"Original sector and standardized sector must both be present or absent at CSV line {line_number}.")
            if not all((currency_code, amount_unit, source, source_locator)):
                raise ValueError(f"Required budget seed field is empty at CSV line {line_number}.")
            if not amount.is_finite() or amount < 0:
                raise ValueError(f"Budget amount must be finite and non-negative at CSV line {line_number}.")
            if len(currency_code) != 3 or not currency_code.isalpha():
                raise ValueError(f"Currency must be a three-letter code at CSV line {line_number}.")
            if source == "Maharashtra BEAMS District wise Expenditure report":
                source = (
                    "https://beams.mahakosh.gov.in/Beams5/BudgetMVC/"
                    "MISRPT/DistrictWiseExpenditure.jsp"
                )

            figures.append(
                (
                    jurisdiction_code, fiscal_year, budget_stage, department_name,
                    sector_name, sector_slug, str(amount), currency_code, amount_unit,
                    source, source_locator,
                )
            )
    return figures


def _web_password_hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def jurisdiction_is_in_scope(jurisdiction_code: str, state_code: str) -> bool:
    current_code = jurisdiction_code
    while current_code in JURISDICTION_BY_CODE:
        if current_code == state_code:
            return True
        current_code = JURISDICTION_BY_CODE[current_code][2]
        if current_code is None:
            return False
    return False


def seed_demo_web_accounts(connection: sqlite3.Connection) -> None:
    if HOST not in {"127.0.0.1", "localhost", "::1"}:
        logging.warning("Sample web-login accounts are not seeded when HOST is not loopback.")
        return
    demo_accounts = [("centraladminUN", "central_admin", "IN-UNION")]
    demo_accounts.extend(
        (f"stadmin{code.removeprefix('IN-').lower()}", "state_auditor", code)
        for code, _, kind, _ in JURISDICTIONS
        if kind == "state"
    )
    for username, role, jurisdiction_code in demo_accounts:
        if connection.execute(
            "SELECT 1 FROM admin_users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone():
            continue
        salt = secrets.token_bytes(16)
        connection.execute(
            """
            INSERT INTO admin_users (
                username, password_salt, password_hash, password_iterations,
                role, created_at, created_by, jurisdiction_code
            ) VALUES (?, ?, ?, ?, ?, ?, 'local-demo-bootstrap', ?)
            """,
            (
                username,
                salt,
                _web_password_hash(WEB_DEMO_PASSWORD, salt, WEB_PASSWORD_ITERATIONS),
                WEB_PASSWORD_ITERATIONS,
                role,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                jurisdiction_code,
            ),
        )
        append_audit_event(
            connection,
            actor="local-demo-bootstrap",
            action="demo_admin_account_seeded",
            entity_type="admin_user",
            entity_id=username,
            details={"role": role, "jurisdiction_code": jurisdiction_code},
        )


# Database bootstrap upgrades older schema layouts and rehydrates demo coverage without discarding newer published rows.

def initialize_database() -> None:
    seed_figures = read_seed_figures()
    with database_connection() as connection:
        legacy_admin = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'admin_users'"
        ).fetchone()
        if legacy_admin and "central_admin" not in legacy_admin["sql"]:
            connection.execute("ALTER TABLE admin_users RENAME TO admin_users_legacy_roles")
        legacy = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'budget_figures'"
        ).fetchone()
        legacy_figures: list[sqlite3.Row] = []
        if legacy and "funds_received" not in legacy["sql"]:
            indexes = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'budget_figures' AND sql IS NOT NULL"
            ).fetchall()
            for index in indexes:
                connection.execute(f'DROP INDEX IF EXISTS "{index["name"]}"')
            legacy_figures = connection.execute("SELECT * FROM budget_figures").fetchall()
            connection.execute("ALTER TABLE budget_figures RENAME TO budget_figures_legacy_stage_model")
        elif legacy:
            connection.execute(
                """
                DELETE FROM budget_figures
                WHERE id NOT IN (
                    SELECT MIN(id) FROM budget_figures
                    GROUP BY jurisdiction_code, fiscal_year, budget_stage,
                             COALESCE(department_name_original, ''),
                             COALESCE(sector_name_original, ''),
                             COALESCE(sector_category_slug, ''), source, source_locator
                )
                """
            )
        connection.executescript(SCHEMA)
        legacy_admin_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(admin_users_legacy_roles)"
            )
        }
        if legacy_admin_columns:
            connection.execute(
                """
                INSERT OR IGNORE INTO admin_users (
                    username, password_salt, password_hash, password_iterations,
                    role, disabled, created_at, created_by
                )
                SELECT username, password_salt, password_hash, password_iterations,
                       role, disabled, created_at, created_by
                FROM admin_users_legacy_roles
                """
            )
            connection.execute("DROP TABLE admin_users_legacy_roles")
        user_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(admin_users)")
        }
        if "email" not in user_columns:
            connection.execute("ALTER TABLE admin_users ADD COLUMN email TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_users_email ON admin_users(email COLLATE NOCASE)"
            )
        if "jurisdiction_code" not in user_columns:
            connection.execute(
                "ALTER TABLE admin_users ADD COLUMN jurisdiction_code TEXT REFERENCES jurisdictions(code)"
            )
        if "force_password_change" not in user_columns:
            connection.execute(
                "ALTER TABLE admin_users ADD COLUMN force_password_change INTEGER NOT NULL DEFAULT 0"
            )
        existing_figure_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(budget_figures)")
        }
        for column_name, column_type in (
            ("import_batch_id", "TEXT REFERENCES import_batches(id)"),
            ("source_page_or_table", "TEXT"),
            ("correction_notes", "TEXT"),
            ("spent_amount", "NUMERIC"),
        ):
            if column_name not in existing_figure_columns:
                connection.execute(
                    f"ALTER TABLE budget_figures ADD COLUMN {column_name} {column_type}"
                )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS staged_budget_figures (
                id INTEGER PRIMARY KEY,
                import_batch_id TEXT NOT NULL REFERENCES import_batches(id),
                line_number INTEGER NOT NULL,
                jurisdiction_code TEXT NOT NULL REFERENCES jurisdictions(code),
                fiscal_year TEXT NOT NULL,
                budget_stage TEXT NOT NULL,
                department_name_original TEXT,
                sector_name_original TEXT,
                sector_category_slug TEXT,
                amount NUMERIC NOT NULL,
                spent_amount NUMERIC,
                currency_code TEXT NOT NULL,
                amount_unit TEXT NOT NULL,
                source_page_or_table TEXT NOT NULL,
                source_locator TEXT NOT NULL,
                correction_notes TEXT NOT NULL,
                UNIQUE (import_batch_id, line_number)
            );
            """
        )
        staged_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(staged_budget_figures)")
        }
        if "source_locator" not in staged_columns:
            connection.execute(
                "ALTER TABLE staged_budget_figures ADD COLUMN source_locator TEXT NOT NULL DEFAULT ''"
            )
        if "spent_amount" not in staged_columns:
            connection.execute("ALTER TABLE staged_budget_figures ADD COLUMN spent_amount NUMERIC")
        connection.executemany(
            """
            INSERT INTO jurisdictions (code, name, jurisdiction_type, parent_code)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                jurisdiction_type = excluded.jurisdiction_type,
                parent_code = excluded.parent_code
            """,
            JURISDICTIONS,
        )
        connection.executemany(
            "INSERT OR IGNORE INTO sector_categories (slug, display_name) VALUES (?, ?)",
            SECTORS,
        )
        seed_demo_web_accounts(connection)
        connection.execute(
            """
            DELETE FROM budget_figures
            WHERE source = 'Illustrative demo dataset (synthetic, not official)'
              AND fiscal_year = '2026-27'
            """
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO budget_figures (
                jurisdiction_code, fiscal_year, budget_stage,
                department_name_original, sector_name_original,
                sector_category_slug, amount, spent_amount, currency_code, amount_unit,
                source, source_locator
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    *figure[:7],
                    str(
                        (Decimal(str(figure[6])) * Decimal("0.60")).quantize(
                            Decimal("0.01")
                        )
                    )
                    if figure[9] == "Illustrative demo dataset (synthetic, not official)"
                    else None,
                    *figure[7:],
                )
                for figure in seed_figures
            ],
        )
        if legacy_figures:
            for row in legacy_figures:
                if row["source"] == "India Open Budget Portal presentation":
                    continue
                legacy_columns = set(row.keys())
                state_code = row["jurisdiction_code"] if "jurisdiction_code" in legacy_columns else row["state_code"]
                if state_code == "MH":
                    state_code = "IN-MH"
                elif state_code not in JURISDICTION_BY_CODE and "district_name" in legacy_columns:
                    district_code = JURISDICTION_BY_NAME.get(row["district_name"].casefold())
                    if district_code:
                        state_code = district_code
                if state_code not in JURISDICTION_BY_CODE:
                    logging.warning("Skipping legacy budget row with unmapped jurisdiction %s.", state_code)
                    continue
                if "amount" in legacy_columns:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO budget_figures (
                            jurisdiction_code, fiscal_year, budget_stage,
                            department_name_original, sector_name_original,
                            sector_category_slug, amount, currency_code, amount_unit,
                            source, source_locator
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            state_code, row["fiscal_year"], row["budget_stage"],
                            row["department_name_original"], row["sector_name_original"],
                            row["sector_category_slug"], str(row["amount"]),
                            row["currency_code"], row["amount_unit"], row["source"],
                            row["source_locator"],
                        ),
                    )
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO budget_figures (
                        jurisdiction_code, fiscal_year, budget_stage,
                        department_name_original, sector_name_original,
                        sector_category_slug, amount, currency_code, amount_unit,
                        source, source_locator
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, 'INR', 'crore', ?, ?)
                    """,
                    (
                        state_code, row["fiscal_year"], row["budget_stage"],
                        row["sector_slug"].replace("_", " ").title(), row["sector_slug"],
                        str(row["allocated_amount"]), row["source"],
                        f"Legacy sample allocation ({row['sector_slug']})",
                    ),
                )
                if row["spent_amount"]:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO budget_figures (
                            jurisdiction_code, fiscal_year, budget_stage,
                            department_name_original, sector_name_original,
                            sector_category_slug, amount, currency_code, amount_unit,
                            source, source_locator
                        ) VALUES (?, ?, 'actual', NULL, ?, ?, ?, 'INR', 'crore', ?, ?)
                        """,
                        (
                            state_code, row["fiscal_year"],
                            row["sector_slug"].replace("_", " ").title(), row["sector_slug"],
                            str(row["spent_amount"]), row["source"],
                            f"Legacy sample actual ({row['sector_slug']})",
                        ),
                    )


# The coverage query walks each jurisdiction tree and classifies rows by direct versus descendant provenance, so summaries reflect the true data source mix.

def query_coverage() -> dict[str, object]:
    with database_connection() as connection:
        raw_rows = connection.execute(
            """
            WITH RECURSIVE descendants(root_code, code) AS (
                SELECT code, code FROM jurisdictions
                UNION ALL
                SELECT descendants.root_code, child.code
                FROM jurisdictions AS child
                JOIN descendants ON child.parent_code = descendants.code
            ),
            coverage AS (
                SELECT descendants.root_code AS jurisdiction_code,
                       COUNT(budget_figures.id) AS figure_count,
                       COUNT(DISTINCT budget_figures.fiscal_year) AS fiscal_year_count,
                       SUM(CASE
                           WHEN budget_figures.source LIKE 'Illustrative demo dataset%' THEN 0
                           WHEN budget_figures.import_batch_id IS NOT NULL THEN 0
                           WHEN budget_figures.id IS NULL THEN 0
                           ELSE 1
                       END) AS source_figure_count,
                       SUM(CASE
                           WHEN budget_figures.import_batch_id IS NOT NULL THEN 1
                           ELSE 0
                       END) AS user_provided_figure_count,
                       SUM(CASE
                           WHEN budget_figures.source LIKE 'Illustrative demo dataset%' THEN 1
                           ELSE 0
                       END) AS illustrative_figure_count
                FROM descendants
                LEFT JOIN budget_figures ON budget_figures.jurisdiction_code = descendants.code
                GROUP BY descendants.root_code
            )
            SELECT jurisdiction.code, jurisdiction.name, jurisdiction.jurisdiction_type,
                   jurisdiction.parent_code, coverage.figure_count, coverage.fiscal_year_count,
                   coverage.source_figure_count, coverage.user_provided_figure_count,
                   coverage.illustrative_figure_count
            FROM jurisdictions AS jurisdiction
            JOIN coverage ON coverage.jurisdiction_code = jurisdiction.code
            ORDER BY CASE jurisdiction.jurisdiction_type
                WHEN 'union' THEN 0
                WHEN 'state' THEN 1
                WHEN 'union_territory' THEN 2
                ELSE 3
            END, jurisdiction.name
            """
        ).fetchall()
        direct_rows = connection.execute(
            """
            SELECT jurisdiction_code,
                   COUNT(*) AS figure_count,
                   COUNT(DISTINCT fiscal_year) AS fiscal_year_count,
                   SUM(CASE
                       WHEN source LIKE 'Illustrative demo dataset%' THEN 0
                       WHEN import_batch_id IS NOT NULL THEN 0
                       ELSE 1
                   END) AS source_figure_count,
                   SUM(CASE
                       WHEN import_batch_id IS NOT NULL THEN 1
                       ELSE 0
                   END) AS user_provided_figure_count,
                   SUM(CASE
                       WHEN source LIKE 'Illustrative demo dataset%' THEN 1
                       ELSE 0
                   END) AS illustrative_figure_count
            FROM budget_figures GROUP BY jurisdiction_code
            """
        ).fetchall()

    direct = {
        row["jurisdiction_code"]: {
            "figure_count": row["figure_count"],
            "fiscal_year_count": row["fiscal_year_count"],
            "source_figure_count": row["source_figure_count"],
            "user_provided_figure_count": row["user_provided_figure_count"],
            "illustrative_figure_count": row["illustrative_figure_count"],
        }
        for row in direct_rows
    }
    entries: dict[str, dict[str, object]] = {}
    for row in raw_rows:
        direct_coverage = direct.get(
            row["code"],
            {
                "figure_count": 0,
                "fiscal_year_count": 0,
                "source_figure_count": 0,
                "user_provided_figure_count": 0,
                "illustrative_figure_count": 0,
            },
        )
        source_descendant_count = max(
            0, row["source_figure_count"] - direct_coverage["source_figure_count"]
        )
        illustrative_descendant_count = max(
            0, row["illustrative_figure_count"] - direct_coverage["illustrative_figure_count"]
        )
        user_provided_descendant_count = max(
            0, row["user_provided_figure_count"]
            - direct_coverage["user_provided_figure_count"]
        )
        if (
            direct_coverage["source_figure_count"]
            and (
                direct_coverage["user_provided_figure_count"]
                or user_provided_descendant_count
            )
        ):
            data_status = "mixed_provenance_data"
        elif direct_coverage["user_provided_figure_count"]:
            data_status = "user_provided_data"
        elif (
            direct_coverage["source_figure_count"]
            and (source_descendant_count or user_provided_descendant_count)
        ):
            data_status = (
                "source_and_user_descendant_data"
                if user_provided_descendant_count
                else "source_and_child_data"
            )
        elif direct_coverage["source_figure_count"]:
            data_status = "source_data"
        elif source_descendant_count and user_provided_descendant_count:
            data_status = "source_and_user_descendant_data"
        elif source_descendant_count and illustrative_descendant_count:
            data_status = "source_and_illustrative_descendant_data"
        elif source_descendant_count:
            data_status = "descendant_data"
        elif user_provided_descendant_count:
            data_status = "user_provided_descendant_data"
        elif direct_coverage["illustrative_figure_count"]:
            data_status = "illustrative_data"
        elif illustrative_descendant_count:
            data_status = "illustrative_descendant_data"
        else:
            data_status = "catalog_only"
        entries[row["code"]] = {
            "code": row["code"],
            "name": row["name"],
            "type": row["jurisdiction_type"],
            "parent_code": row["parent_code"],
            "direct_figure_count": direct_coverage["source_figure_count"],
            "figure_count_including_descendants": row["figure_count"],
            "source_figure_count_including_descendants": row["source_figure_count"],
            "user_provided_figure_count_including_descendants": row["user_provided_figure_count"],
            "illustrative_figure_count_including_descendants": row["illustrative_figure_count"],
            "fiscal_year_count_including_descendants": row["fiscal_year_count"],
            "descendant_figure_count": source_descendant_count,
            "illustrative_direct_figure_count": direct_coverage["illustrative_figure_count"],
            "illustrative_descendant_figure_count": illustrative_descendant_count,
            "user_provided_direct_figure_count": direct_coverage["user_provided_figure_count"],
            "user_provided_descendant_figure_count": user_provided_descendant_count,
            "data_status": data_status,
            "children": [],
        }
    roots: list[dict[str, object]] = []
    for entry in entries.values():
        parent_code = entry["parent_code"]
        if parent_code and parent_code in entries:
            entries[parent_code]["children"].append(entry)
        else:
            roots.append(entry)
    return {
        "jurisdiction_count": len(entries),
        "counts_by_type": {
            kind: sum(entry["type"] == kind for entry in entries.values())
            for kind in ("union", "state", "union_territory", "district")
        },
        "figure_count": sum(item["figure_count"] for item in direct.values()),
        "source_figure_count": sum(item["source_figure_count"] for item in direct.values()),
        "user_provided_figure_count": sum(
            item["user_provided_figure_count"] for item in direct.values()
        ),
        "illustrative_figure_count": sum(
            item["illustrative_figure_count"] for item in direct.values()
        ),
        "districts_with_figures": sum(
            entry["type"] == "district"
            and direct.get(entry["code"], {}).get("source_figure_count", 0) > 0
            for entry in entries.values()
        ),
        "districts_with_illustrative_data": sum(
            entry["type"] == "district"
            and direct.get(entry["code"], {}).get("illustrative_figure_count", 0) > 0
            for entry in entries.values()
        ),
        "districts_with_user_provided_data": sum(
            entry["type"] == "district"
            and direct.get(entry["code"], {}).get("user_provided_figure_count", 0) > 0
            for entry in entries.values()
        ),
        "fiscal_years": list(FISCAL_YEARS),
        "budget_stages": list(BUDGET_STAGES),
        "currency_code": "INR",
        "jurisdictions": roots,
        "note": (
            "The jurisdiction catalogue covers the Union Government, 28 states and 8 Union Territories. "
            "Imported figures are user-provided and reviewer-approved but not independently verified. "
            "Illustrative synthetic figures are not official. "
            "catalog_only does not mean zero budget."
        ),
    }


def query_budget_records(
    fiscal_year: str,
    jurisdiction_code: str = "IN-MH",
    budget_stage: str = "",
    district: str = "",
    search: str = "",
    sector: str = "",
    department: str = "",
) -> dict[str, object]:
    if fiscal_year not in FISCAL_YEARS:
        raise ValueError(f"fiscal_year must be one of: {', '.join(FISCAL_YEARS)}.")
    if jurisdiction_code not in JURISDICTION_BY_CODE:
        raise ValueError(f"Unknown jurisdiction_code: {jurisdiction_code}.")
    if budget_stage and budget_stage not in BUDGET_STAGES:
        raise ValueError(f"budget_stage must be one of: {', '.join(BUDGET_STAGES)}.")
    if sector and sector not in {slug for slug, _ in SECTORS} | {"unclassified"}:
        raise ValueError(f"sector must be one of: {', '.join([slug for slug, _ in SECTORS] + ['unclassified'])}.")
    if any(len(value) > MAX_FILTER_LENGTH for value in (district, search, department)):
        raise ValueError(
            f"district, q, and department must be {MAX_FILTER_LENGTH} characters or fewer."
        )

    # The record query keeps source-backed and illustrative demo rows distinct so a jurisdiction view never silently mixes official and synthetic totals.
    hierarchy_filter = (
        "figure.jurisdiction_code = ?"
        if JURISDICTION_BY_CODE[jurisdiction_code][1] == "union"
        else """
        figure.jurisdiction_code IN (
            WITH RECURSIVE subtree(code) AS (
                SELECT code FROM jurisdictions WHERE code = ?
                UNION ALL
                SELECT child.code FROM jurisdictions AS child
                JOIN subtree ON child.parent_code = subtree.code
            )
            SELECT code FROM subtree
        )
        """
    )
    filters = ["figure.fiscal_year = ?", hierarchy_filter]
    filters.append(
        """
        NOT (
            figure.source LIKE 'Illustrative demo dataset%'
            AND EXISTS (
                SELECT 1
                FROM budget_figures AS imported_figure
                JOIN import_batches AS imported_batch
                  ON imported_batch.id = imported_figure.import_batch_id
                WHERE imported_batch.status = 'published'
                  AND imported_figure.jurisdiction_code = figure.jurisdiction_code
                  AND imported_figure.fiscal_year = figure.fiscal_year
                  AND imported_figure.budget_stage = figure.budget_stage
            )
        )
        """
    )
    parameters: list[object] = [fiscal_year, jurisdiction_code]
    if budget_stage:
        filters.append("figure.budget_stage = ?")
        parameters.append(budget_stage)
    if district:
        filters.append("area.jurisdiction_type = 'district' AND area.name = ?")
        parameters.append(DISTRICT_NAME_ALIASES.get(district.casefold(), district))
    if sector == "unclassified":
        filters.append("figure.sector_category_slug IS NULL")
    elif sector:
        filters.append("figure.sector_category_slug = ?")
        parameters.append(sector)
    if department:
        filters.append("LOWER(COALESCE(figure.department_name_original, '')) LIKE ? ESCAPE '\\'")
        escaped_department = department.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parameters.append(f"%{escaped_department}%")
    if search:
        filters.append(
            "(COALESCE(figure.department_name_original, '') LIKE ? ESCAPE '\\' OR "
            "figure.sector_name_original LIKE ? ESCAPE '\\' OR "
            "category.display_name LIKE ? ESCAPE '\\' OR area.name LIKE ? ESCAPE '\\')"
        )
        escaped_search = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        parameters.extend([f"%{escaped_search}%"] * 4)
    where_clause = " AND ".join(filters)
    name, jurisdiction_type, _ = JURISDICTION_BY_CODE[jurisdiction_code]

    with database_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT figure.id, figure.jurisdiction_code, area.name AS jurisdiction_name,
                   area.jurisdiction_type, figure.fiscal_year, figure.budget_stage,
                   figure.department_name_original, figure.sector_name_original,
                   figure.sector_category_slug, category.display_name AS sector_category_name,
                   figure.amount, figure.spent_amount, figure.currency_code, figure.amount_unit,
                   figure.source, figure.source_locator, figure.import_batch_id,
                   figure.source_page_or_table, figure.correction_notes,
                   import_batch.source_document, import_batch.source_url,
                   import_batch.imported_at, import_batch.reviewed_by,
                   import_batch.review_notes, import_batch.corrections AS dataset_corrections,
                   import_batch.content_sha256, import_batch.scope_json,
                   import_batch.status AS import_status
            FROM budget_figures AS figure
            JOIN jurisdictions AS area ON area.code = figure.jurisdiction_code
            LEFT JOIN sector_categories AS category ON category.slug = figure.sector_category_slug
            LEFT JOIN import_batches AS import_batch ON import_batch.id = figure.import_batch_id
            WHERE {where_clause}
            ORDER BY area.jurisdiction_type, area.name, figure.sector_category_slug,
                     figure.budget_stage
            """,
            parameters,
        ).fetchall()
        totals = connection.execute(
            f"""
            SELECT figure.budget_stage, figure.currency_code, figure.amount_unit,
                   CASE WHEN figure.source LIKE 'Illustrative demo dataset%' THEN 1 ELSE 0 END
                       AS is_illustrative,
                   CASE WHEN figure.import_batch_id IS NOT NULL THEN 1 ELSE 0 END
                       AS is_user_provided,
                   COUNT(*) AS figure_count, COALESCE(SUM(figure.amount), 0) AS amount,
                   SUM(figure.spent_amount) AS spent_amount
            FROM budget_figures AS figure
            JOIN jurisdictions AS area ON area.code = figure.jurisdiction_code
            LEFT JOIN sector_categories AS category ON category.slug = figure.sector_category_slug
            WHERE {where_clause}
            GROUP BY figure.budget_stage, figure.currency_code, figure.amount_unit,
                     is_illustrative, is_user_provided
            ORDER BY figure.budget_stage
            """,
            parameters,
        ).fetchall()
        sector_rows = connection.execute(
            f"""
            SELECT category.slug, category.display_name,
                   figure.budget_stage, figure.currency_code, figure.amount_unit,
                   COUNT(figure.id) AS figure_count,
                   COALESCE(SUM(figure.amount), 0) AS amount,
                   SUM(figure.spent_amount) AS spent_amount
            FROM sector_categories AS category
            JOIN budget_figures AS figure ON figure.sector_category_slug = category.slug
            JOIN jurisdictions AS area ON area.code = figure.jurisdiction_code
            WHERE {where_clause}
            GROUP BY category.slug, category.display_name,
                     figure.budget_stage, figure.currency_code, figure.amount_unit
            ORDER BY CASE category.slug
                WHEN 'education' THEN 0
                WHEN 'agriculture' THEN 1
                WHEN 'roadways_transport' THEN 2
                WHEN 'healthcare' THEN 3
                ELSE 4
            END, figure.budget_stage
            """,
            parameters,
        ).fetchall()

    sectors_by_slug: dict[str, dict[str, object]] = {
        slug: {"slug": slug, "name": name, "stage_totals": []}
        for slug, name in SECTORS
    }
    for row in sector_rows:
        sectors_by_slug[row["slug"]]["stage_totals"].append(
            {
                "budget_stage": row["budget_stage"],
                "figure_count": row["figure_count"],
                "amount": row["amount"],
                "spent_amount": row["spent_amount"],
                "currency_code": row["currency_code"],
                "amount_unit": row["amount_unit"],
            }
        )

    message = ""
    if not rows:
        if search:
            message = f"No budget figures match '{search}' for {name} in {fiscal_year}."
        elif jurisdiction_type == "district" or district:
            message = (
                f"No source-backed {fiscal_year} budget figures are available for "
                f"{district or name}; no values are inferred from state totals."
            )
        else:
            message = f"No source-backed {fiscal_year} budget figures are available for {name}."

    has_illustrative_rows = any(
        str(row["source"]).startswith("Illustrative demo dataset") for row in rows
    )
    has_beams_rows = any(
        str(row["source"]).startswith("https://beams.mahakosh.gov.in/") for row in rows
    )
    has_presentation_rows = any(
        row["source"] == "India Open Budget Portal presentation" for row in rows
    )
    has_user_import_rows = any(row["import_batch_id"] is not None for row in rows)
    if has_user_import_rows:
        source_note = (
            "User-provided structured data, staged, reviewed, and explicitly published in this portal. "
            "The source document and row locator are recorded below. The portal does not independently "
            "verify the provider or certify these values as official government data."
        )
        if has_illustrative_rows:
            source_note += (
                " Any rows labelled Illustrative are synthetic demo values and must not be treated as official."
            )
    elif has_illustrative_rows:
        source_note = (
            "Illustrative synthetic demonstration values, generated for this portal. "
            "They are not official government data, must not be cited as actual budgets, "
            "and are shown only to demonstrate the district table and filters. Demo Spent amounts are "
            "fictional examples calculated as 60% of each illustrative amount. These illustrative department "
            "and sector labels are demonstration classifications, not source-reported categories. Each original "
            "district/stage total is split approximately evenly across Education, Agriculture, Healthcare, and "
            "Transport, with matching standardized sector categories."
        )
    elif has_beams_rows:
        source_note = (
            "The Maharashtra BEAMS district report labels these columns “Budget Received” and “Expenditure”; "
            "it does not state the numeric unit or provide department/sector breakdowns. Values are stored exactly "
            "as displayed, with source_unit_not_stated, and Budget Received is not classified as a Budget Estimate "
            "or Revised Estimate. The report page was accessed 2026-10-08."
        )
    elif has_presentation_rows:
        source_note = (
            "Source: sample ledger examples on slide 5 of the supplied presentation. "
            "These are sample rows, not a complete Maharashtra budget; department names are not provided by the slide."
        )
    else:
        source_note = "No source-backed or illustrative budget figures are available for this selection."

    return {
        "jurisdiction": {"code": jurisdiction_code, "name": name, "type": jurisdiction_type},
        "fiscal_year": fiscal_year,
        "budget_stage_filter": budget_stage or None,
        "district_filter": district or None,
        "search": search,
        "sector_filter": sector or None,
        "department_filter": department or None,
        "source_note": source_note,
        "records": [
            {
                "id": row["id"],
                "jurisdiction_code": row["jurisdiction_code"],
                "jurisdiction": row["jurisdiction_name"],
                "jurisdiction_type": row["jurisdiction_type"],
                "fiscal_year": row["fiscal_year"],
                "budget_stage": row["budget_stage"],
                "department_name_original": row["department_name_original"],
                "sector_name_original": row["sector_name_original"],
                "sector_category_slug": row["sector_category_slug"],
                "sector_category_name": row["sector_category_name"],
                "amount": row["amount"],
                "spent_amount": row["spent_amount"],
                "currency_code": row["currency_code"],
                "amount_unit": row["amount_unit"],
                "source": row["source"],
                "source_locator": row["source_locator"],
                "source_document": row["source_document"],
                "source_url": row["source_url"],
                "source_page_or_table": row["source_page_or_table"],
                "imported_at": row["imported_at"],
                "correction_notes": row["correction_notes"],
                "import_status": row["import_status"],
                "reviewed_by": row["reviewed_by"],
                "review_notes": row["review_notes"],
                "dataset_corrections": row["dataset_corrections"],
                "import_batch_id": row["import_batch_id"],
                "content_sha256": row["content_sha256"],
                "import_scope": row["scope_json"],
            }
            for row in rows
        ],
        "figure_count": len(rows),
        "illustrative_figure_count": sum(
            str(row["source"]).startswith("Illustrative demo dataset") for row in rows
        ),
        "unclassified_figure_count": sum(
            row["sector_category_slug"] is None for row in rows
        ),
        "totals_by_stage": [
            {
                "budget_stage": row["budget_stage"],
                "is_illustrative": bool(row["is_illustrative"]),
                "is_user_provided": bool(row["is_user_provided"]),
                "figure_count": row["figure_count"],
                "amount": row["amount"],
                "spent_amount": row["spent_amount"],
                "currency_code": row["currency_code"],
                "amount_unit": row["amount_unit"],
            }
            for row in totals
        ],
        "sectors": list(sectors_by_slug.values()),
        "message": message,
    }


def query_budget_comparison(
    jurisdiction_codes: list[str],
    fiscal_years: list[str],
    budget_stage: str = "",
    sector: str = "",
    department: str = "",
    sectors: list[str] | None = None,
) -> dict[str, object]:
    if not jurisdiction_codes or len(jurisdiction_codes) > 12:
        raise ValueError("Select between 1 and 12 states or Union Territories to compare.")
    if not fiscal_years or len(fiscal_years) > len(FISCAL_YEARS):
        raise ValueError("Select between 1 and 9 fiscal years to compare.")
    if len(set(jurisdiction_codes)) != len(jurisdiction_codes):
        raise ValueError("A jurisdiction may be selected only once.")
    if len(set(fiscal_years)) != len(fiscal_years):
        raise ValueError("A fiscal year may be selected only once.")
    invalid_codes = [
        code for code in jurisdiction_codes
        if code not in JURISDICTION_BY_CODE
        or JURISDICTION_BY_CODE[code][1] not in {"state", "union_territory"}
    ]
    if invalid_codes:
        raise ValueError("Comparisons accept state and Union Territory jurisdictions only.")
    invalid_years = [year for year in fiscal_years if year not in FISCAL_YEARS]
    if invalid_years:
        raise ValueError(f"Unsupported fiscal year: {invalid_years[0]}.")
    selected_sectors = sectors if sectors is not None else ([sector] if sector else [""])
    valid_sectors = {slug for slug, _ in SECTORS}
    invalid_sectors = [slug for slug in selected_sectors if slug and slug not in valid_sectors]
    if (sectors is not None and not selected_sectors) or invalid_sectors:
        raise ValueError(
            f"Select one or more valid sectors: {', '.join(slug for slug, _ in SECTORS)}."
        )
    if len(set(selected_sectors)) != len(selected_sectors):
        raise ValueError("A sector may be selected only once.")

    comparison_rows: list[dict[str, object]] = []
    for jurisdiction_code in jurisdiction_codes:
        jurisdiction_name = JURISDICTION_BY_CODE[jurisdiction_code][0]
        for fiscal_year in fiscal_years:
            for sector_slug in selected_sectors:
                result = query_budget_records(
                    fiscal_year=fiscal_year,
                    jurisdiction_code=jurisdiction_code,
                    budget_stage=budget_stage,
                    sector=sector_slug,
                    department=department,
                )
                sector_name = dict(SECTORS).get(sector_slug, "All sectors")
                for total in result["totals_by_stage"]:
                    comparison_rows.append(
                        {
                            "jurisdiction_code": jurisdiction_code,
                            "jurisdiction": jurisdiction_name,
                            "fiscal_year": fiscal_year,
                            "sector_slug": sector_slug or None,
                            "sector": sector_name,
                            **total,
                        }
                    )

    comparison_rows.sort(
        key=lambda row: (
            str(row["jurisdiction"]),
            str(row["fiscal_year"]),
            str(row["sector"]),
            str(row["budget_stage"]),
            bool(row["is_illustrative"]),
            bool(row["is_user_provided"]),
        )
    )
    return {
        "jurisdictions": jurisdiction_codes,
        "fiscal_years": fiscal_years,
        "filters": {
            "budget_stage": budget_stage or None,
            "sector": sector or None,
            "sectors": [slug for slug in selected_sectors if slug],
            "department": department or None,
        },
        "rows": comparison_rows,
        "row_count": len(comparison_rows),
        "note": (
            "Amounts are grouped by fiscal year, stage, currency, unit, and provenance. "
            "State rows include available child-district figures; where a source provides both "
            "state and district aggregates, these may overlap and should not be summed together. "
            "Illustrative and user-provided values are explicitly labelled and are not certified official."
        ),
    }


class BudgetRequestHandler(BaseHTTPRequestHandler):
    server_version = "OpenBudgetPortal/1.0"

    def send_json(
        self,
        status: int,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def web_import_actor(self):
        session = get_web_session(self.headers.get("Cookie", ""))
        if session is None:
            raise PermissionError("Sign in with an authorized budget administrator account.")
        user = session[1]
        if user["role"] not in {"central_admin", "state_auditor"}:
            raise PermissionError("Budget import tools are available only to Central Admin and State Auditors.")
        import import_budget

        return import_budget.Principal(
            str(user["username"]),
            str(user["role"]),
            str(user["jurisdiction_code"]) if user["jurisdiction_code"] else None,
        )

    def handle_import_get(self, request) -> None:
        try:
            actor = self.web_import_actor()
            import import_budget

            if request.path == "/api/import/batches":
                if request.query:
                    self.send_json(400, {"error": "Batch list does not accept query parameters."})
                    return
                batches = [dict(batch) for batch in import_budget.list_batches(actor=actor)]
                self.send_json(200, {"batches": batches})
                return

            match = re.fullmatch(r"/api/import/batches/([0-9a-fA-F-]{36})", request.path)
            if match is None or request.query:
                self.send_json(404, {"error": "Import endpoint not found."})
                return
            batch_id = match.group(1)
            visible_ids = {
                str(batch["id"]) for batch in import_budget.list_batches(actor=actor)
            }
            if batch_id not in visible_ids:
                self.send_json(404, {"error": "Import batch not found."})
                return
            batch = import_budget.get_batch(batch_id)
            with database_connection() as connection:
                rows = connection.execute(
                    """
                    SELECT line_number, jurisdiction_code, fiscal_year, budget_stage,
                           department_name_original, sector_name_original,
                           sector_category_slug, amount, spent_amount, currency_code, amount_unit,
                           source_page_or_table, source_locator, correction_notes
                    FROM staged_budget_figures
                    WHERE import_batch_id = ?
                    ORDER BY line_number
                    """,
                    (batch_id,),
                ).fetchall()
            self.send_json(
                200,
                {"batch": dict(batch), "rows": [dict(row) for row in rows]},
            )
        except PermissionError as error:
            self.send_json(403, {"error": str(error)})
        except ValueError as error:
            self.send_json(404, {"error": str(error)})
        except sqlite3.Error:
            logging.exception("Budget import listing failed.")
            self.send_json(500, {"error": "Budget imports could not be loaded."})

    def handle_import_post(self, path: str, body: dict[str, str]) -> None:
        try:
            actor = self.web_import_actor()
            import import_budget

            if path == "/api/import/batches":
                if set(body) - {"csv_text", "source_document", "source_url", "corrections"}:
                    self.send_json(400, {"error": "Unsupported import field."})
                    return
                csv_text = body.get("csv_text", "")
                if not csv_text.strip():
                    self.send_json(400, {"error": "Choose a non-empty CSV file to import."})
                    return
                csv_bytes = csv_text.encode("utf-8")
                if len(csv_bytes) > 5 * 1024 * 1024:
                    self.send_json(413, {"error": "CSV files must be 5 MB or smaller."})
                    return
                temporary_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as temporary_file:
                        temporary_file.write(csv_bytes)
                        temporary_path = Path(temporary_file.name)
                    batch_id, row_count = import_budget.stage_import(
                        temporary_path,
                        source_document=body.get("source_document", ""),
                        source_url=body.get("source_url", ""),
                        actor=actor,
                        corrections=body.get("corrections", "None declared."),
                    )
                finally:
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
                self.send_json(201, {"batch_id": batch_id, "row_count": row_count})
                return

            match = re.fullmatch(r"/api/import/batches/([0-9a-fA-F-]{36})/(review|publish)", path)
            if match is not None:
                batch_id, action = match.groups()
                if action == "review":
                    if set(body) - {"notes"}:
                        self.send_json(400, {"error": "Unsupported review field."})
                        return
                    import_budget.review_import(
                        batch_id,
                        actor=actor,
                        review_notes=body.get("notes", ""),
                    )
                    self.send_json(200, {"status": "reviewed"})
                else:
                    if set(body) - {"confirm_batch"}:
                        self.send_json(400, {"error": "Unsupported publication field."})
                        return
                    count = import_budget.publish_import(
                        batch_id,
                        actor=actor,
                        confirm_batch=body.get("confirm_batch", ""),
                    )
                    self.send_json(200, {"status": "published", "row_count": count})
                return

            match = re.fullmatch(
                r"/api/import/batches/([0-9a-fA-F-]{36})/rows/([1-9][0-9]*)/correct",
                path,
            )
            if match is not None:
                batch_id, line_number = match.groups()
                correction_note = body.get("correction_note", "")
                changes = {
                    key: value
                    for key, value in body.items()
                    if key != "correction_note"
                }
                import_budget.correct_import_row(
                    batch_id,
                    int(line_number),
                    changes=changes,
                    correction_note=correction_note,
                    actor=actor,
                )
                self.send_json(200, {"status": "corrected"})
                return

            self.send_json(404, {"error": "Import endpoint not found."})
        except PermissionError as error:
            self.send_json(403, {"error": str(error)})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except (OSError, sqlite3.Error):
            logging.exception("Budget import operation failed.")
            self.send_json(500, {"error": "The budget import operation failed."})

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        if request.path == "/api/captcha":
            if request.query:
                self.send_json(400, {"error": "CAPTCHA does not accept query parameters."})
                return
            challenge_id = issue_captcha()
            self.send_json(
                200,
                {
                    "challenge_id": challenge_id,
                    "image_url": f"/api/captcha/image/{challenge_id}.svg",
                    "expires_in": CAPTCHA_MAX_AGE_SECONDS,
                },
            )
            return
        if request.path == "/api/auth/session":
            session = get_web_session(self.headers.get("Cookie", ""))
            if session is None:
                self.send_json(200, {"authenticated": False, "user": None})
            else:
                self.send_json(200, {"authenticated": True, "user": session[1]})
            return
        if request.path == "/api/auth/captcha-image":
            parameters = parse_qs(request.query, keep_blank_values=True)
            challenge_ids = parameters.get("challenge_id", [])
            if len(parameters) != 1 or len(challenge_ids) != 1:
                self.send_json(400, {"error": "Provide one CAPTCHA challenge_id."})
                return
            image = captcha_svg(challenge_ids[0])
            if image is None:
                self.send_json(404, {"error": "CAPTCHA expired. Request a new challenge."})
                return
            encoded = image.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)
            return
        if request.path.startswith("/api/import/"):
            self.handle_import_get(request)
            return
        if request.path == "/api/health":
            self.send_json(200, {"status": "ok", "database": "sqlite"})
            return
        if request.path == "/api/coverage":
            if request.query:
                self.send_json(400, {"error": "/api/coverage does not accept query parameters."})
                return
            self.send_json(200, query_coverage())
            return
        if request.path == "/api/budget":
            self.handle_budget_request(request.query)
            return
        if request.path == "/api/compare":
            self.handle_comparison_request(request.query)
            return
        if request.path.startswith("/api/"):
            self.send_json(404, {"error": "API endpoint not found."})
            return
        self.serve_static(unquote(request.path))

    def do_POST(self) -> None:
        request = urlsplit(self.path)
        is_import_request = request.path.startswith("/api/import/")
        if not request.path.startswith("/api/auth/") and not is_import_request:
            self.send_json(404, {"error": "API endpoint not found."})
            return
        origin = self.headers.get("Origin")
        if origin:
            parsed_origin = urlsplit(origin)
            if parsed_origin.netloc.casefold() != self.headers.get("Host", "").casefold():
                self.send_json(403, {"error": "Cross-origin requests are not accepted."})
                return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Invalid request body length."})
            return
        body_limit = MAX_IMPORT_REQUEST_BYTES if is_import_request else MAX_JSON_BODY_BYTES
        if content_length <= 0 or content_length > body_limit:
            self.send_json(413 if content_length > body_limit else 400, {"error": "Request body is empty or too large."})
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"error": "Requests must use application/json."})
            return
        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Request body must be valid JSON."})
            return
        if not isinstance(body, dict) or any(not isinstance(value, str) for value in body.values()):
            self.send_json(400, {"error": "Request body must be an object containing string values."})
            return

        # Auth routes validate a browser challenge before checking credentials; import routes depend on the signed session and role guard instead.
        if request.path in {"/api/auth/login", "/api/auth/register"}:
            challenge_id = body.get("challenge_id", "")
            captcha_answer = body.get("captcha", "")
            if not consume_captcha(challenge_id, captcha_answer):
                self.send_json(400, {"error": "CAPTCHA is incorrect or expired. Please try again."})
                return

        if request.path == "/api/auth/login":
            username = body.get("username", "").strip()
            password = body.get("password", "")
            if not username or not password:
                self.send_json(400, {"error": "Username/email and password are required."})
                return
            user = authenticate_web_user(username, password)
            if user is None:
                self.send_json(401, {"error": "Username/email or password is incorrect."})
                return
            token = create_web_session(user)
            self.send_json(
                200,
                {"authenticated": True, "user": user},
                {
                    "Set-Cookie": (
                        f"budget_session={token}; HttpOnly; Path=/; SameSite=Strict; "
                        f"Max-Age={SESSION_MAX_AGE_SECONDS}"
                    )
                },
            )
            return
        if request.path == "/api/auth/register":
            try:
                user = register_citizen(body.get("email", ""), body.get("password", ""))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            token = create_web_session(user)
            self.send_json(
                201,
                {"authenticated": True, "user": user},
                {
                    "Set-Cookie": (
                        f"budget_session={token}; HttpOnly; Path=/; SameSite=Strict; "
                        f"Max-Age={SESSION_MAX_AGE_SECONDS}"
                    )
                },
            )
            return
        if request.path == "/api/auth/logout":
            session = get_web_session(self.headers.get("Cookie", ""))
            if session is not None:
                with SESSION_LOCK:
                    WEB_SESSIONS.pop(session[0], None)
                with database_connection() as connection:
                    append_audit_event(
                        connection,
                        actor=str(session[1]["username"]),
                        action="web_logout",
                        entity_type="account",
                        entity_id=str(session[1]["username"]),
                        details={"role": session[1]["role"]},
                    )
            self.send_json(
                200,
                {"authenticated": False},
                {"Set-Cookie": "budget_session=; HttpOnly; Path=/; SameSite=Strict; Max-Age=0"},
            )
            return
        if is_import_request:
            self.handle_import_post(request.path, body)
            return
        self.send_json(404, {"error": "Authentication endpoint not found."})

    def handle_budget_request(self, query_string: str) -> None:
        parameters = parse_qs(query_string, keep_blank_values=True, strict_parsing=False)
        allowed_parameters = {
            "fiscal_year", "jurisdiction_code", "budget_stage", "district", "q",
            "sector", "department",
        }
        unknown = set(parameters) - allowed_parameters
        if unknown:
            self.send_json(400, {"error": f"Unsupported query parameter: {sorted(unknown)[0]}."})
            return
        if any(len(values) != 1 for values in parameters.values()):
            self.send_json(400, {"error": "Each query parameter can be provided only once."})
            return

        try:
            payload = query_budget_records(
                fiscal_year=parameters.get("fiscal_year", ["2026-27"])[0],
                jurisdiction_code=parameters.get("jurisdiction_code", ["IN-MH"])[0].strip(),
                budget_stage=parameters.get("budget_stage", [""])[0].strip(),
                district=parameters.get("district", [""])[0].strip(),
                search=parameters.get("q", [""])[0].strip(),
                sector=parameters.get("sector", [""])[0].strip(),
                department=parameters.get("department", [""])[0].strip(),
            )
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        except sqlite3.Error:
            logging.exception("Budget database query failed.")
            self.send_json(500, {"error": "The budget database query failed."})
            return
        self.send_json(200, payload)

    def handle_comparison_request(self, query_string: str) -> None:
        parameters = parse_qs(query_string, keep_blank_values=True, strict_parsing=False)
        allowed_parameters = {
            "jurisdiction_codes", "fiscal_years", "budget_stage", "sector", "sectors", "department"
        }
        unknown = set(parameters) - allowed_parameters
        if unknown:
            self.send_json(400, {"error": f"Unsupported query parameter: {sorted(unknown)[0]}."})
            return
        if any(len(values) != 1 for values in parameters.values()):
            self.send_json(400, {"error": "Each query parameter can be provided only once."})
            return
        try:
            payload = query_budget_comparison(
                jurisdiction_codes=[
                    code.strip()
                    for code in parameters.get("jurisdiction_codes", [""])[0].split(",")
                    if code.strip()
                ],
                fiscal_years=[
                    year.strip()
                    for year in parameters.get("fiscal_years", [""])[0].split(",")
                    if year.strip()
                ],
                budget_stage=parameters.get("budget_stage", [""])[0].strip(),
                sector=parameters.get("sector", [""])[0].strip(),
                department=parameters.get("department", [""])[0].strip(),
                sectors=(
                    [
                        slug.strip()
                        for slug in parameters["sectors"][0].split(",")
                        if slug.strip()
                    ]
                    if "sectors" in parameters
                    else None
                ),
            )
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        except sqlite3.Error:
            logging.exception("Budget comparison query failed.")
            self.send_json(500, {"error": "The budget comparison query failed."})
            return
        self.send_json(200, payload)

    def serve_static(self, request_path: str) -> None:
        relative_path = request_path.lstrip("/") or "index.html"
        requested_file = (APP_ROOT / relative_path).resolve()
        try:
            requested_file.relative_to(APP_ROOT)
        except ValueError:
            self.send_error(404)
            return
        if requested_file.suffix.lower() not in ALLOWED_STATIC_SUFFIXES or not requested_file.is_file():
            self.send_error(404)
            return

        body = requested_file.read_bytes()
        content_type = mimetypes.guess_type(requested_file.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: object) -> None:
        logging.info("%s - %s", self.address_string(), format_string % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        initialize_database()
        backup_path = create_database_backup()
        logging.info("Created startup database backup at %s.", backup_path)
        start_daily_backup_scheduler()
        server = ThreadingHTTPServer((HOST, PORT), BudgetRequestHandler)
    except (OSError, sqlite3.Error, ValueError) as error:
        logging.error("Could not start the budget portal: %s", error)
        raise SystemExit(1) from error

    logging.info("Budget portal is running at http://%s:%s", HOST, PORT)
    logging.info("Coverage API: http://%s:%s/api/coverage", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Shutting down budget portal.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

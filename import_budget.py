from __future__ import annotations

import argparse
import csv
import hashlib
import getpass
import hmac
import io
import json
import re
import secrets
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import app


REQUIRED_COLUMNS = {
    "jurisdiction_code",
    "jurisdiction_name",
    "fiscal_year",
    "budget_stage",
    "department_name_original",
    "sector_name_original",
    "sector_category_slug",
    "amount",
    "currency_code",
    "amount_unit",
    "source_page_or_table",
    "source_locator",
    "correction_notes",
}
SOURCE_LABEL = "User-provided dataset (not independently verified)"
PASSWORD_ITERATIONS = 310_000
MIN_PASSWORD_LENGTH = 12
VALID_ROLES = {"admin", "central_admin", "state_auditor", "data_entry", "reviewer"}
ROLE_PERMISSIONS = {
    "admin": {"stage", "correct", "review", "list", "show", "users", "backup", "audit"},
    "central_admin": {"stage", "correct", "review", "publish", "list", "show", "users", "backup", "audit"},
    "state_auditor": {"stage", "correct", "list", "show"},
    "data_entry": {"stage", "correct", "list", "show"},
    "reviewer": {"review", "list", "show"},
    "citizen": set(),
}
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
AUDIT_ACTIONS = {
    "stage": "budget_batch_staged",
    "correct": "budget_staged_row_corrected",
    "review": "budget_batch_reviewed",
    "publish": "budget_batch_published",
    "add_user": "admin_user_created",
    "change_role": "admin_user_role_changed",
    "disable_user": "admin_user_disabled",
}


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    jurisdiction_code: str | None = None


def _password_digest(password: str, salt: bytes, iterations: int = PASSWORD_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


# Permission checks are resolved against the live database record so disabled accounts and session scope stay authoritative even if a stale Principal object is reused.

def _audit_actor(principal: Principal, permission: str) -> str:
    with app.database_connection() as connection:
        user = connection.execute(
            "SELECT role, disabled, jurisdiction_code FROM admin_users WHERE username = ? COLLATE NOCASE",
            (principal.username,),
        ).fetchone()
    if user is None or user["disabled"]:
        raise PermissionError("This administrative account is unavailable.")
    if user["role"] != principal.role or permission not in ROLE_PERMISSIONS[user["role"]]:
        raise PermissionError(f"The {user['role']} role is not permitted to {permission} budget data.")
    if principal.role == "state_auditor" and user["jurisdiction_code"] != principal.jurisdiction_code:
        raise PermissionError("The state auditor session scope no longer matches the assigned state.")
    return principal.username


def authenticate_user(username: str, password: str) -> Principal:
    authentication_failed = False
    with app.database_connection() as connection:
        user = connection.execute(
            """
            SELECT username, password_salt, password_hash, password_iterations,
                   role, disabled, jurisdiction_code
            FROM admin_users WHERE username = ? COLLATE NOCASE
            """,
            (username.strip(),),
        ).fetchone()
        if user is None:
            salt = bytes(16)
            expected_hash = bytes(32)
            iterations = PASSWORD_ITERATIONS
        else:
            salt = user["password_salt"]
            expected_hash = user["password_hash"]
            iterations = user["password_iterations"]
        supplied_hash = _password_digest(password, salt, iterations)
        if (
            user is None
            or user["disabled"]
            or not hmac.compare_digest(supplied_hash, expected_hash)
        ):
            app.append_audit_event(
                connection,
                actor="anonymous",
                action="admin_authentication_failed",
                entity_type="admin_user",
                entity_id=username.strip()[:64] or "(empty)",
                details={"reason": "invalid_credentials_or_disabled"},
            )
            authentication_failed = True
        else:
            principal = Principal(user["username"], user["role"], user["jurisdiction_code"])
    if authentication_failed:
        raise PermissionError("Invalid username or password, or the account is disabled.")
    return principal


def create_admin_user(
    username: str,
    password: str,
    *,
    role: str,
    actor: Principal | None = None,
    jurisdiction_code: str | None = None,
) -> None:
    username = username.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Username must be 3-64 characters using letters, numbers, dot, underscore, or hyphen.")
    if role not in VALID_ROLES:
        raise ValueError(f"Role must be one of: {', '.join(sorted(VALID_ROLES))}.")
    if role == "state_auditor":
        jurisdiction = app.JURISDICTION_BY_CODE.get(jurisdiction_code or "")
        if jurisdiction is None or jurisdiction[1] != "state":
            raise ValueError("State Auditor accounts require a valid state jurisdiction code.")
    elif jurisdiction_code is not None:
        raise ValueError("Only State Auditor accounts can be assigned a jurisdiction.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must contain at least {MIN_PASSWORD_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    password_hash = _password_digest(password, salt)
    with app.database_connection() as connection:
        user_count = connection.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
        if user_count == 0:
            if actor is not None or role not in {"admin", "central_admin"}:
                raise PermissionError("The first local administrative account must be a bootstrapped admin.")
            audit_actor = "bootstrap"
        else:
            if actor is None:
                raise PermissionError("An authenticated admin is required to create additional accounts.")
            audit_actor = _audit_actor(actor, "users")
        try:
            connection.execute(
                """
                INSERT INTO admin_users (
                    username, password_salt, password_hash, password_iterations,
                    role, created_at, created_by, jurisdiction_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username, salt, password_hash, PASSWORD_ITERATIONS, role,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"), audit_actor,
                    jurisdiction_code,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"Administrative username already exists: {username}.") from error
        app.append_audit_event(
            connection,
            actor=audit_actor,
            action=AUDIT_ACTIONS["add_user"],
            entity_type="admin_user",
            entity_id=username,
            details={"role": role},
        )


def set_user_role(username: str, role: str, *, actor: Principal) -> None:
    if role not in VALID_ROLES:
        raise ValueError(f"Role must be one of: {', '.join(sorted(VALID_ROLES))}.")
    audit_actor = _audit_actor(actor, "users")
    with app.database_connection() as connection:
        user = connection.execute(
            "SELECT role, jurisdiction_code FROM admin_users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if user is None:
            raise ValueError(f"Unknown administrative user: {username}.")
        if user["role"] == role:
            raise ValueError("The account already has that role.")
        if role == "state_auditor" and (
            not user["jurisdiction_code"]
            or app.JURISDICTION_BY_CODE.get(user["jurisdiction_code"], (None, None, None))[1] != "state"
        ):
            raise ValueError("Assign a valid state jurisdiction before changing this account to State Auditor.")
        if user["role"] == "admin":
            active_admins = connection.execute(
                "SELECT COUNT(*) FROM admin_users WHERE role = 'admin' AND disabled = 0"
            ).fetchone()[0]
            target_disabled = connection.execute(
                "SELECT disabled FROM admin_users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()["disabled"]
            if not target_disabled and active_admins <= 1:
                raise ValueError("The last active admin cannot be demoted.")
        connection.execute(
            "UPDATE admin_users SET role = ? WHERE username = ? COLLATE NOCASE",
            (role, username),
        )
        app.append_audit_event(
            connection,
            actor=audit_actor,
            action=AUDIT_ACTIONS["change_role"],
            entity_type="admin_user",
            entity_id=username,
            details={"previous_role": user["role"], "role": role},
        )


def disable_user(username: str, *, actor: Principal) -> None:
    audit_actor = _audit_actor(actor, "users")
    if username.casefold() == audit_actor.casefold():
        raise ValueError("An admin cannot disable their own account.")
    with app.database_connection() as connection:
        target = connection.execute(
            "SELECT role FROM admin_users WHERE username = ? COLLATE NOCASE AND disabled = 0",
            (username,),
        ).fetchone()
        if target is None:
            raise ValueError(f"Unknown or already disabled administrative user: {username}.")
        if target["role"] == "admin":
            active_admins = connection.execute(
                "SELECT COUNT(*) FROM admin_users WHERE role = 'admin' AND disabled = 0"
            ).fetchone()[0]
            if active_admins <= 1:
                raise ValueError("The last active admin cannot be disabled.")
        updated = connection.execute(
            "UPDATE admin_users SET disabled = 1 WHERE username = ? COLLATE NOCASE AND disabled = 0",
            (username,),
        )
        if updated.rowcount == 0:
            raise ValueError(f"Unknown or already disabled administrative user: {username}.")
        app.append_audit_event(
            connection,
            actor=audit_actor,
            action=AUDIT_ACTIONS["disable_user"],
            entity_type="admin_user",
            entity_id=username,
            details={"disabled": True},
        )


def list_users(*, actor: Principal) -> list[sqlite3.Row]:
    _audit_actor(actor, "users")
    with app.database_connection() as connection:
        return connection.execute(
            """
            SELECT username, role, disabled, created_at, created_by
            FROM admin_users ORDER BY username COLLATE NOCASE
            """
        ).fetchall()


def list_audit_events(*, actor: Principal, limit: int = 100) -> list[sqlite3.Row]:
    _audit_actor(actor, "audit")
    if not 1 <= limit <= 1000:
        raise ValueError("Audit event limit must be between 1 and 1000.")
    with app.database_connection() as connection:
        return connection.execute(
            """
            SELECT id, occurred_at, actor, action, entity_type, entity_id,
                   details_json, previous_hash, event_hash
            FROM audit_events ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()


def read_import_csv(path: Path) -> tuple[list[dict[str, object]], str]:
    if path.suffix.casefold() != ".csv":
        raise ValueError("Structured imports currently accept CSV only; convert Excel workbooks to UTF-8 CSV first.")
    raw = path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV must be UTF-8 encoded (UTF-8 with BOM is also accepted).") from error

    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    if not reader.fieldnames:
        raise ValueError("CSV is empty or has no header row.")
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError("CSV contains duplicate column names.")
    missing = REQUIRED_COLUMNS - set(reader.fieldnames)
    if missing:
        raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}.")

    figures: list[dict[str, object]] = []
    seen: dict[tuple[object, ...], int] = {}
    try:
        source_rows = list(reader)
    except csv.Error as error:
        raise ValueError(f"Malformed CSV: {error}.") from error
    for line_number, raw_row in enumerate(source_rows, start=2):
        if None in raw_row or any(value is None for value in raw_row.values()):
            raise ValueError(f"CSV row has missing or extra fields on line {line_number}.")
        try:
            row = {key: (value or "").strip() for key, value in raw_row.items()}
            code = row["jurisdiction_code"]
            name = row["jurisdiction_name"]
            fiscal_year = row["fiscal_year"]
            stage = row["budget_stage"]
            department = row["department_name_original"] or None
            sector = row["sector_name_original"] or None
            sector_slug = row["sector_category_slug"] or None
            amount = Decimal(row["amount"])
            spent_amount_value = row.get("spent_amount", "")
            currency = row["currency_code"].upper()
            amount_unit = row["amount_unit"].lower()
            page_or_table = row["source_page_or_table"]
            locator = row["source_locator"]
            corrections = row["correction_notes"]
        except (KeyError, InvalidOperation, AttributeError) as error:
            raise ValueError(f"Invalid required value on CSV line {line_number}.") from error

        if code not in app.JURISDICTION_BY_CODE:
            raise ValueError(f"Unknown jurisdiction_code on CSV line {line_number}: {code}.")
        canonical_name, jurisdiction_type, _ = app.JURISDICTION_BY_CODE[code]
        if name.casefold() != canonical_name.casefold():
            raise ValueError(
                f"Jurisdiction name/code mismatch on CSV line {line_number}: "
                f"{name!r} does not match {code} ({canonical_name})."
            )
        if jurisdiction_type not in {"state", "union_territory", "district"}:
            raise ValueError(f"Imports must target a state, UT, or district (CSV line {line_number}).")
        if fiscal_year not in app.FISCAL_YEARS:
            raise ValueError(f"Unsupported fiscal_year on CSV line {line_number}: {fiscal_year}.")
        if stage not in app.BUDGET_STAGES:
            raise ValueError(f"Unsupported budget_stage on CSV line {line_number}: {stage}.")
        if sector_slug is not None and sector_slug not in {slug for slug, _ in app.SECTORS}:
            raise ValueError(f"Unknown sector_category_slug on CSV line {line_number}: {sector_slug}.")
        if bool(sector) != bool(sector_slug):
            raise ValueError(
                f"Original sector and standardized sector must both be present or absent "
                f"on CSV line {line_number}."
            )
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"Amount must be finite and non-negative on CSV line {line_number}.")
        try:
            spent_amount = Decimal(spent_amount_value) if spent_amount_value else None
        except InvalidOperation as error:
            raise ValueError(
                f"Spent amount must be finite and non-negative on CSV line {line_number}."
            ) from error
        if spent_amount is not None and (not spent_amount.is_finite() or spent_amount < 0):
            raise ValueError(f"Spent amount must be finite and non-negative on CSV line {line_number}.")
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError(f"Currency must be a three-letter code on CSV line {line_number}.")
        if not all((amount_unit, page_or_table, locator)):
            raise ValueError(
                f"amount_unit, source_page_or_table, and source_locator are required "
                f"on CSV line {line_number}."
            )

        duplicate_key = (
            code, fiscal_year, stage, department, sector, sector_slug, str(amount),
            currency, amount_unit, page_or_table.casefold(), locator.casefold(),
        )
        if duplicate_key in seen:
            raise ValueError(
                f"Duplicate figure on CSV lines {seen[duplicate_key]} and {line_number}."
            )
        seen[duplicate_key] = line_number
        figures.append(
            {
                "line_number": line_number,
                "jurisdiction_code": code,
                "fiscal_year": fiscal_year,
                "budget_stage": stage,
                "department_name_original": department,
                "sector_name_original": sector,
                "sector_category_slug": sector_slug,
                "amount": str(amount),
                "spent_amount": str(spent_amount) if spent_amount is not None else None,
                "currency_code": currency,
                "amount_unit": amount_unit,
                "source_page_or_table": page_or_table,
                "source_locator": locator,
                "correction_notes": corrections,
            }
        )
    if not figures:
        raise ValueError("CSV has a header but contains no data rows.")
    return figures, content_hash


def stage_import(
    path: Path,
    *,
    source_document: str,
    source_url: str = "",
    actor: Principal,
    corrections: str = "None declared.",
) -> tuple[str, int]:
    source_document = source_document.strip()
    staged_by = _audit_actor(actor, "stage")
    source_url = source_url.strip()
    if not source_document or not staged_by:
        raise ValueError("source_document and staged_by are required.")
    if source_url:
        parsed_url = urlsplit(source_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("source_url must be an absolute HTTP or HTTPS URL.")

    # CSV intake is intentionally strict: schema drift, bad values, duplicates, and jurisdiction mismatches are rejected before staging.
    figures, content_hash = read_import_csv(path)
    if actor.role == "state_auditor":
        if not actor.jurisdiction_code:
            raise PermissionError("This state auditor has no assigned state.")
        out_of_scope = [
            str(row["jurisdiction_code"])
            for row in figures
            if not app.jurisdiction_is_in_scope(
                str(row["jurisdiction_code"]), actor.jurisdiction_code
            )
        ]
        if out_of_scope:
            raise PermissionError(
                "State auditors may only stage records for their assigned state and districts."
            )
    batch_id = str(uuid.uuid4())
    imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scopes = sorted(
        {f"{row['jurisdiction_code']}:{row['fiscal_year']}" for row in figures}
    )

    with app.database_connection() as connection:
        existing_batch = connection.execute(
            "SELECT id, status FROM import_batches WHERE content_sha256 = ?",
            (content_hash,),
        ).fetchone()
        if existing_batch:
            raise ValueError(
                f"This exact CSV was already imported as batch {existing_batch['id']} "
                f"(status: {existing_batch['status']})."
            )
        connection.execute(
            """
            INSERT INTO import_batches (
                id, source_document, source_url, content_sha256, imported_at,
                status, corrections, staged_by, row_count, scope_json
            ) VALUES (?, ?, ?, ?, ?, 'staged', ?, ?, ?, ?)
            """,
            (
                batch_id, source_document, source_url, content_hash, imported_at,
                corrections.strip() or "None declared.", staged_by, len(figures),
                json.dumps(scopes),
            ),
        )
        connection.executemany(
            """
            INSERT INTO staged_budget_figures (
                import_batch_id, line_number, jurisdiction_code, fiscal_year,
                budget_stage, department_name_original, sector_name_original,
                sector_category_slug, amount, spent_amount, currency_code, amount_unit,
                source_page_or_table, source_locator, correction_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    batch_id, row["line_number"], row["jurisdiction_code"],
                    row["fiscal_year"], row["budget_stage"],
                    row["department_name_original"], row["sector_name_original"],
                    row["sector_category_slug"], row["amount"], row["spent_amount"],
                    row["currency_code"], row["amount_unit"],
                    row["source_page_or_table"], row["source_locator"],
                    row["correction_notes"],
                )
                for row in figures
            ],
        )
        app.append_audit_event(
            connection,
            actor=staged_by,
            action=AUDIT_ACTIONS["stage"],
            entity_type="import_batch",
            entity_id=batch_id,
            details={
                "row_count": len(figures),
                "content_sha256": content_hash,
                "source_document": source_document,
                "scope": scopes,
            },
        )
    return batch_id, len(figures)


def get_batch(batch_id: str) -> sqlite3.Row:
    with app.database_connection() as connection:
        batch = connection.execute(
            "SELECT * FROM import_batches WHERE id = ?", (batch_id,)
        ).fetchone()
    if batch is None:
        raise ValueError(f"Unknown import batch: {batch_id}.")
    return batch


def review_import(batch_id: str, *, actor: Principal, review_notes: str) -> None:
    reviewer = _audit_actor(actor, "review")
    notes = review_notes.strip()
    if not reviewer or not notes:
        raise ValueError("reviewed_by and non-empty review_notes are required.")
    with app.database_connection() as connection:
        batch = connection.execute(
            "SELECT status, staged_by FROM import_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise ValueError(f"Unknown import batch: {batch_id}.")
        if batch["status"] != "staged":
            raise ValueError(f"Only staged batches can be reviewed (current status: {batch['status']}).")
        if reviewer.casefold() == batch["staged_by"].casefold():
            raise ValueError("The reviewer must be different from the person who staged the batch.")
        connection.execute(
            """
            UPDATE import_batches
            SET status = 'reviewed', reviewed_by = ?, review_notes = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (
                reviewer, notes,
                datetime.now(timezone.utc).isoformat(timespec="seconds"), batch_id,
            ),
        )
        app.append_audit_event(
            connection,
            actor=reviewer,
            action=AUDIT_ACTIONS["review"],
            entity_type="import_batch",
            entity_id=batch_id,
            details={"review_notes": notes},
        )


def publish_import(batch_id: str, *, actor: Principal, confirm_batch: str) -> int:
    if confirm_batch != batch_id:
        raise ValueError("Publishing requires --confirm-batch with the exact batch ID.")
    publisher = _audit_actor(actor, "publish")
    with app.database_connection() as connection:
        batch = connection.execute(
            "SELECT * FROM import_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise ValueError(f"Unknown import batch: {batch_id}.")
        if batch["status"] != "reviewed":
            raise ValueError(
                f"Only reviewed batches can be published (current status: {batch['status']})."
            )
        staged_rows = connection.execute(
            "SELECT * FROM staged_budget_figures WHERE import_batch_id = ? ORDER BY line_number",
            (batch_id,),
        ).fetchall()
        if len(staged_rows) != batch["row_count"]:
            raise ValueError("Staged row count does not match the recorded import batch.")

        for row in staged_rows:
            duplicate = connection.execute(
                """
                SELECT id FROM budget_figures
                WHERE jurisdiction_code = ? AND fiscal_year = ? AND budget_stage = ?
                  AND COALESCE(department_name_original, '') = COALESCE(?, '')
                  AND COALESCE(sector_name_original, '') = COALESCE(?, '')
                  AND COALESCE(sector_category_slug, '') = COALESCE(?, '')
                  AND amount = ? AND currency_code = ? AND amount_unit = ?
                  AND source NOT LIKE 'Illustrative demo dataset%'
                LIMIT 1
                """,
                (
                    row["jurisdiction_code"], row["fiscal_year"], row["budget_stage"],
                    row["department_name_original"], row["sector_name_original"],
                    row["sector_category_slug"], row["amount"], row["currency_code"],
                    row["amount_unit"],
                ),
            ).fetchone()
            if duplicate:
                raise ValueError(
                    f"Duplicate published figure found for staged CSV line {row['line_number']}."
                )

        source = f"{SOURCE_LABEL}: {batch['source_document']}"
        connection.executemany(
            """
            INSERT INTO budget_figures (
                jurisdiction_code, fiscal_year, budget_stage,
                department_name_original, sector_name_original,
                sector_category_slug, amount, spent_amount, currency_code, amount_unit,
                source, source_locator, import_batch_id, source_page_or_table,
                correction_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["jurisdiction_code"], row["fiscal_year"],
                    row["budget_stage"], row["department_name_original"],
                    row["sector_name_original"], row["sector_category_slug"],
                    row["amount"], row["spent_amount"], row["currency_code"], row["amount_unit"],
                    source, row["source_locator"], batch_id,
                    row["source_page_or_table"], row["correction_notes"] or None,
                )
                for row in staged_rows
            ],
        )
        connection.execute(
            """
            UPDATE import_batches SET status = 'published', published_at = ?
            WHERE id = ? AND status = 'reviewed'
            """,
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), batch_id),
        )
        connection.execute(
            "DELETE FROM staged_budget_figures WHERE import_batch_id = ?", (batch_id,)
        )
        app.append_audit_event(
            connection,
            actor=publisher,
            action=AUDIT_ACTIONS["publish"],
            entity_type="import_batch",
            entity_id=batch_id,
            details={"row_count": len(staged_rows)},
        )
    return len(staged_rows)


def correct_import_row(
    batch_id: str,
    line_number: int,
    *,
    changes: dict[str, str],
    correction_note: str,
    actor: Principal,
) -> None:
    corrector = _audit_actor(actor, "correct")
    note = correction_note.strip()
    allowed_fields = {
        "jurisdiction_code", "fiscal_year", "budget_stage",
        "amount", "department_name_original", "sector_name_original",
        "spent_amount",
        "sector_category_slug", "currency_code", "amount_unit",
        "source_page_or_table", "source_locator",
    }
    if not changes or set(changes) - allowed_fields:
        raise ValueError("Provide one or more supported staged-row fields to correct.")
    if not note:
        raise ValueError("A non-empty correction note is required.")

    normalized: dict[str, object] = {}
    for field, value in changes.items():
        value = value.strip()
        if field == "jurisdiction_code":
            jurisdiction = app.JURISDICTION_BY_CODE.get(value)
            if jurisdiction is None or jurisdiction[1] not in {"state", "union_territory", "district"}:
                raise ValueError("Corrected jurisdiction must be a known state, Union Territory, or district code.")
            normalized[field] = value
        elif field == "fiscal_year":
            if value not in app.FISCAL_YEARS:
                raise ValueError("Unsupported corrected fiscal year.")
            normalized[field] = value
        elif field == "budget_stage":
            if value not in app.BUDGET_STAGES:
                raise ValueError("Unsupported corrected budget stage.")
            normalized[field] = value
        elif field in {"amount", "spent_amount"}:
            if field == "spent_amount" and not value:
                normalized[field] = None
                continue
            try:
                amount = Decimal(value)
            except InvalidOperation as error:
                label = "Spent amount" if field == "spent_amount" else "Corrected amount"
                raise ValueError(f"{label} must be a finite non-negative number.") from error
            if not amount.is_finite() or amount < 0:
                label = "Spent amount" if field == "spent_amount" else "Corrected amount"
                raise ValueError(f"{label} must be a finite non-negative number.")
            normalized[field] = str(amount)
        elif field == "currency_code":
            currency = value.upper()
            if len(currency) != 3 or not currency.isalpha():
                raise ValueError("Currency must be a three-letter code.")
            normalized[field] = currency
        elif field == "sector_category_slug":
            if value and value not in {slug for slug, _ in app.SECTORS}:
                raise ValueError("Unknown standardized sector category.")
            normalized[field] = value or None
        elif field == "department_name_original":
            normalized[field] = value or None
        elif field == "sector_name_original":
            normalized[field] = value or None
        else:
            if not value:
                raise ValueError(f"{field} cannot be empty.")
            normalized[field] = value

    sector_name = normalized.get("sector_name_original")
    sector_slug = normalized.get("sector_category_slug")
    with app.database_connection() as connection:
        batch = connection.execute(
            "SELECT status FROM import_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise ValueError(f"Unknown import batch: {batch_id}.")
        if batch["status"] != "staged":
            raise ValueError("Only staged batches can be corrected; restage a revised CSV after review.")
        row = connection.execute(
            """
            SELECT * FROM staged_budget_figures
            WHERE import_batch_id = ? AND line_number = ?
            """,
            (batch_id, line_number),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown CSV line {line_number} in batch {batch_id}.")
        if actor.role == "state_auditor":
            assigned_state = actor.jurisdiction_code
            current_code = str(row["jurisdiction_code"])
            corrected_code = str(normalized.get("jurisdiction_code", current_code))
            if (
                not assigned_state
                or not app.jurisdiction_is_in_scope(current_code, assigned_state)
                or not app.jurisdiction_is_in_scope(corrected_code, assigned_state)
            ):
                raise PermissionError("State auditors may only correct records in their assigned state.")
        effective_sector_name = (
            sector_name if "sector_name_original" in normalized else row["sector_name_original"]
        )
        effective_sector_slug = (
            sector_slug if "sector_category_slug" in normalized else row["sector_category_slug"]
        )
        if bool(effective_sector_name) != bool(effective_sector_slug):
            raise ValueError("Original sector name and standardized sector must both be present or both be blank.")
        before = {field: row[field] for field in normalized}
        assignments = [f"{field} = ?" for field in normalized]
        values = list(normalized.values())
        correction_notes = row["correction_notes"].strip()
        combined_notes = f"{correction_notes}\n{note}" if correction_notes else note
        assignments.append("correction_notes = ?")
        values.append(combined_notes)
        values.extend((batch_id, line_number))
        connection.execute(
            f"UPDATE staged_budget_figures SET {', '.join(assignments)} "
            "WHERE import_batch_id = ? AND line_number = ?",
            values,
        )
        if {"jurisdiction_code", "fiscal_year"} & normalized.keys():
            corrected_scopes = connection.execute(
                """
                SELECT DISTINCT jurisdiction_code, fiscal_year
                FROM staged_budget_figures
                WHERE import_batch_id = ?
                ORDER BY jurisdiction_code, fiscal_year
                """,
                (batch_id,),
            ).fetchall()
            scopes = sorted(f"{scope['jurisdiction_code']}:{scope['fiscal_year']}" for scope in corrected_scopes)
            connection.execute(
                "UPDATE import_batches SET scope_json = ? WHERE id = ?",
                (json.dumps(scopes), batch_id),
            )
        app.append_audit_event(
            connection,
            actor=corrector,
            action=AUDIT_ACTIONS["correct"],
            entity_type="import_batch_row",
            entity_id=f"{batch_id}:{line_number}",
            details={"before": before, "after": normalized, "correction_note": note},
        )


def list_batches(*, actor: Principal) -> list[sqlite3.Row]:
    _audit_actor(actor, "list")
    with app.database_connection() as connection:
        batches = connection.execute(
            """
            SELECT id, source_document, source_url, imported_at, status, row_count,
                   staged_by, reviewed_by, reviewed_at, published_at, scope_json
            FROM import_batches ORDER BY imported_at DESC
            """
        ).fetchall()
    if actor.role != "state_auditor":
        return batches
    return [
        batch for batch in batches
        if actor.jurisdiction_code
        and all(
            app.jurisdiction_is_in_scope(scope.split(":", 1)[0], actor.jurisdiction_code)
            for scope in json.loads(batch["scope_json"])
        )
    ]


def print_batch(batch_id: str, *, actor: Principal) -> None:
    _audit_actor(actor, "show")
    with app.database_connection() as connection:
        batch = connection.execute(
            "SELECT * FROM import_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise ValueError(f"Unknown import batch: {batch_id}.")
        if actor.role == "state_auditor" and (
            not actor.jurisdiction_code
            or not all(
                app.jurisdiction_is_in_scope(scope.split(":", 1)[0], actor.jurisdiction_code)
                for scope in json.loads(batch["scope_json"])
            )
        ):
            raise PermissionError("State auditors may only inspect batches for their assigned state.")
        rows = connection.execute(
            """
            SELECT staged.line_number, jurisdiction.name AS jurisdiction_name,
                   staged.jurisdiction_code, staged.fiscal_year, staged.budget_stage,
                   staged.department_name_original, staged.sector_name_original,
                   staged.amount, staged.spent_amount, staged.currency_code, staged.amount_unit,
                   staged.source_page_or_table, staged.source_locator,
                   staged.correction_notes
            FROM staged_budget_figures AS staged
            JOIN jurisdictions AS jurisdiction ON jurisdiction.code = staged.jurisdiction_code
            WHERE staged.import_batch_id = ? ORDER BY staged.line_number
            """,
            (batch_id,),
        ).fetchall()
    print(json.dumps(dict(batch), ensure_ascii=False, indent=2))
    if rows:
        print("Staged rows (not yet published):")
        for row in rows:
            print(json.dumps(dict(row), ensure_ascii=False))


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Authenticate administrators and manage staged budget datasets."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_user = subparsers.add_parser(
        "create-user", help="Create the first admin account or add an account as an admin."
    )
    create_user.add_argument("new_username")
    create_user.add_argument("--role", choices=sorted(VALID_ROLES), required=True)
    create_user.add_argument("--jurisdiction-code")
    create_user.add_argument(
        "--admin-user",
        help="Existing administrator username; omit only when bootstrapping the first admin.",
    )

    stage = subparsers.add_parser("stage", help="Validate and stage a UTF-8 CSV dataset.")
    stage.add_argument("csv_file", type=Path)
    stage.add_argument("--source-document", required=True)
    stage.add_argument("--source-url", default="")
    stage.add_argument("--username", required=True)
    stage.add_argument("--corrections", default="None declared.")

    show = subparsers.add_parser("show", help="Inspect staged rows and their provenance.")
    show.add_argument("batch_id")
    show.add_argument("--username", required=True)

    review = subparsers.add_parser("review", help="Record independent review and approve a staged batch.")
    review.add_argument("batch_id")
    review.add_argument("--username", required=True)
    review.add_argument("--notes", required=True)

    publish = subparsers.add_parser("publish", help="Publish a reviewed batch to the budget tables.")
    publish.add_argument("batch_id")
    publish.add_argument("--username", required=True)
    publish.add_argument("--confirm-batch", required=True)

    correct = subparsers.add_parser("correct", help="Correct a staged CSV row and record the reason.")
    correct.add_argument("batch_id")
    correct.add_argument("line_number", type=int)
    correct.add_argument("--username", required=True)
    correct.add_argument("--correction-note", required=True)
    for option, field in (
        ("--jurisdiction-code", "jurisdiction_code"),
        ("--fiscal-year", "fiscal_year"),
        ("--budget-stage", "budget_stage"),
        ("--amount", "amount"),
        ("--spent-amount", "spent_amount"),
        ("--department", "department_name_original"),
        ("--sector-name", "sector_name_original"),
        ("--sector-category", "sector_category_slug"),
        ("--currency", "currency_code"),
        ("--unit", "amount_unit"),
        ("--source-page-or-table", "source_page_or_table"),
        ("--source-locator", "source_locator"),
    ):
        correct.add_argument(option, dest=field)

    list_command = subparsers.add_parser("list", help="List staged, reviewed, and published datasets.")
    list_command.add_argument("--username", required=True)
    for command, help_text in (
        ("users", "List administrative users and roles."),
        ("backup", "Create an online, integrity-checked SQLite backup."),
        ("verify-audit", "Verify the append-only audit hash chain."),
    ):
        account_command = subparsers.add_parser(command, help=help_text)
        account_command.add_argument("--username", required=True)
    audit = subparsers.add_parser("audit", help="Print recent administrative audit events.")
    audit.add_argument("--username", required=True)
    audit.add_argument("--limit", type=int, default=100)

    user_role = subparsers.add_parser("set-role", help="Change an administrative user's role.")
    user_role.add_argument("target_username")
    user_role.add_argument("--role", choices=sorted(VALID_ROLES), required=True)
    user_role.add_argument("--username", required=True)
    disable = subparsers.add_parser("disable-user", help="Disable an administrative account.")
    disable.add_argument("target_username")
    disable.add_argument("--username", required=True)
    return parser


def _login(username: str) -> Principal:
    return authenticate_user(username, getpass.getpass(f"Password for {username}: "))


def main(argv: list[str] | None = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        app.initialize_database()
        if args.command == "create-user":
            with app.database_connection() as connection:
                existing_users = connection.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
            actor = _login(args.admin_user) if existing_users and args.admin_user else None
            if existing_users and actor is None:
                raise PermissionError("An existing --admin-user is required to create accounts.")
            password = getpass.getpass(f"New password for {args.new_username}: ")
            confirmation = getpass.getpass("Confirm password: ")
            if not hmac.compare_digest(password, confirmation):
                raise ValueError("Passwords do not match.")
            create_admin_user(
                args.new_username,
                password,
                role=args.role,
                actor=actor,
                jurisdiction_code=args.jurisdiction_code,
            )
            print(f"Created {args.role} account {args.new_username}.")
        elif args.command == "stage":
            actor = _login(args.username)
            batch_id, row_count = stage_import(
                args.csv_file,
                source_document=args.source_document,
                source_url=args.source_url,
                actor=actor,
                corrections=args.corrections,
            )
            print(f"Validated and staged {row_count} rows. Batch ID: {batch_id}")
            print("Rows are not published. Inspect them with the 'show' command.")
        elif args.command == "show":
            print_batch(args.batch_id, actor=_login(args.username))
        elif args.command == "review":
            review_import(args.batch_id, actor=_login(args.username), review_notes=args.notes)
            print(f"Batch {args.batch_id} reviewed. Publishing still requires an explicit command.")
        elif args.command == "publish":
            count = publish_import(
                args.batch_id,
                actor=_login(args.username),
                confirm_batch=args.confirm_batch,
            )
            print(f"Published {count} rows from batch {args.batch_id}.")
        elif args.command == "list":
            print(json.dumps(
                [dict(batch) for batch in list_batches(actor=_login(args.username))],
                ensure_ascii=False,
                indent=2,
            ))
        elif args.command == "correct":
            changes = {
                field: value
                for field in (
                    "jurisdiction_code", "fiscal_year", "budget_stage", "amount", "spent_amount",
                    "department_name_original", "sector_name_original",
                    "sector_category_slug", "currency_code", "amount_unit",
                    "source_page_or_table", "source_locator",
                )
                if (value := getattr(args, field)) is not None
            }
            correct_import_row(
                args.batch_id,
                args.line_number,
                changes=changes,
                correction_note=args.correction_note,
                actor=_login(args.username),
            )
            print(f"Corrected staged batch {args.batch_id}, CSV line {args.line_number}.")
        elif args.command == "users":
            print(json.dumps(
                [dict(user) for user in list_users(actor=_login(args.username))],
                ensure_ascii=False,
                indent=2,
            ))
        elif args.command == "set-role":
            set_user_role(args.target_username, args.role, actor=_login(args.username))
            print(f"Updated role for {args.target_username}.")
        elif args.command == "disable-user":
            disable_user(args.target_username, actor=_login(args.username))
            print(f"Disabled account {args.target_username}.")
        elif args.command == "backup":
            actor = _login(args.username)
            _audit_actor(actor, "backup")
            print(f"Created database backup: {app.create_database_backup(actor=actor.username)}")
        elif args.command == "audit":
            events = list_audit_events(actor=_login(args.username), limit=args.limit)
            print(json.dumps([dict(event) for event in events], ensure_ascii=False, indent=2))
        elif args.command == "verify-audit":
            actor = _login(args.username)
            _audit_actor(actor, "audit")
            result = app.verify_audit_log()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result["valid"]:
                return 2
    except (OSError, ValueError, PermissionError, csv.Error, sqlite3.Error) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

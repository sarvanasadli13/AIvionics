"""Authentication: bcrypt password verification against `app_user` (PLAN 4.2).

Roles live in a table, not an `is_admin` flag, so a third role costs a row
rather than a schema change.

Every authentication outcome is written to the hash-chained audit log. Page
navigation deliberately is **not** — see `AUDITED_ACTIONS` below.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import bcrypt

from .. import audit
from . import store

BCRYPT_ROUNDS = 12
MAX_PW_BYTES = 72          # bcrypt truncates beyond this; reject rather than truncate

SETUP_USERNAME = "admin"
SETUP_PASSWORD = "aivionics-setup"   # one-time; must_change_pw is set on seed

ROLES = {
    "admin": "users,roles,ingest,models,settings,audit,read,print",
    "engineer": "read,print,notes",
}

# Actions written to the audit log.
#
# Page navigation is *excluded* on purpose. A per-user record of which screen
# an engineer opened and when is exactly the "system suitable for performance
# monitoring" that triggers BetrVG §87(1)(6) works-council involvement
# (standing rule 6) — and it buys nothing forensically, because the acts that
# matter to airworthiness (who signed in, what locator was printed) are all
# captured below.
AUDITED_ACTIONS = ("login", "logout", "login_failed", "print", "password_change")

# Throttle repeated failures. In-memory: a desktop app restart clears it,
# which is acceptable — this defends a human at a keyboard, not a botnet.
_FAILURES: dict[str, list[float]] = {}
LOCKOUT_AFTER = 5
LOCKOUT_SECONDS = 30.0


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    role: str
    must_change_pw: bool


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    user: User | None = None
    reason: str = ""


def hash_password(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PW_BYTES:
        raise ValueError(f"password exceeds {MAX_PW_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS))


def check_password(password: str, pwhash: bytes) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PW_BYTES:
        return False
    if isinstance(pwhash, str):
        pwhash = pwhash.encode("utf-8")
    try:
        return bcrypt.checkpw(raw, pwhash)
    except ValueError:
        return False


def seed(con: sqlite3.Connection) -> None:
    """Create the roles and, on a virgin database, the setup admin account.

    Idempotent. Never resets or re-enables an existing account.
    """
    store.ensure_ui_tables(con)
    for name, perms in ROLES.items():
        con.execute("INSERT OR IGNORE INTO role(name,permissions) VALUES(?,?)",
                    (name, perms))
    have_users = con.execute("SELECT COUNT(*) FROM app_user").fetchone()[0]
    if not have_users:
        role_id = con.execute("SELECT id FROM role WHERE name='admin'").fetchone()[0]
        con.execute(
            "INSERT INTO app_user(username,pwhash,display_name,role_id,active,"
            "must_change_pw) VALUES(?,?,?,?,1,1)",
            (SETUP_USERNAME, hash_password(SETUP_PASSWORD), "Setup Administrator",
             role_id))
    con.commit()


def _locked_out(username: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _FAILURES.get(username, []) if now - t < LOCKOUT_SECONDS]
    _FAILURES[username] = recent
    return len(recent) >= LOCKOUT_AFTER


def _record_failure(username: str) -> None:
    _FAILURES.setdefault(username, []).append(time.monotonic())


def reset_throttle(username: str | None = None) -> None:
    if username is None:
        _FAILURES.clear()
    else:
        _FAILURES.pop(username, None)


def authenticate(con: sqlite3.Connection, username: str, password: str) -> LoginResult:
    """Verify credentials and write the outcome to the audit chain.

    The failure reason returned to the UI is deliberately identical for an
    unknown user and a wrong password.
    """
    username = (username or "").strip()
    if _locked_out(username):
        audit.log(con, "login_failed", entity="app_user", entity_id=username,
                  payload={"reason": "throttled"})
        return LoginResult(False, reason="Too many attempts — wait 30 seconds.")

    row = con.execute(
        "SELECT u.id, u.username, u.pwhash, u.display_name, r.name, u.active, "
        "       COALESCE(u.must_change_pw, 0) "
        "FROM app_user u JOIN role r ON r.id = u.role_id WHERE u.username=?",
        (username,)).fetchone()

    if row is None or not check_password(password, row[2]):
        _record_failure(username)
        audit.log(con, "login_failed", user_id=row[0] if row else None,
                  entity="app_user", entity_id=username)
        return LoginResult(False, reason="Username or password is not correct.")

    if not row[5]:
        audit.log(con, "login_failed", user_id=row[0], entity="app_user",
                  entity_id=username, payload={"reason": "inactive"})
        return LoginResult(False, reason="This account is disabled.")

    reset_throttle(username)
    user = User(id=row[0], username=row[1], display_name=row[3] or row[1],
                role=row[4], must_change_pw=bool(row[6]))
    audit.log(con, "login", user_id=user.id, entity="app_user", entity_id=user.username)
    return LoginResult(True, user=user)


def logout(con: sqlite3.Connection, user: User | None) -> None:
    audit.log(con, "logout", user_id=user.id if user else None, entity="app_user",
              entity_id=user.username if user else None)


def change_password(con: sqlite3.Connection, user: User, new_password: str) -> User:
    """Set a new password and clear the must-change flag.

    Raises ValueError on a password that fails the minimum policy, so the
    caller cannot accidentally store a weak one.
    """
    if len(new_password) < 10:
        raise ValueError("Password must be at least 10 characters.")
    if new_password == SETUP_PASSWORD:
        raise ValueError("Choose a password other than the setup password.")
    con.execute("UPDATE app_user SET pwhash=?, must_change_pw=0 WHERE id=?",
                (hash_password(new_password), user.id))
    con.commit()
    audit.log(con, "password_change", user_id=user.id, entity="app_user",
              entity_id=user.username)
    return User(user.id, user.username, user.display_name, user.role, False)


def signature(user: User | None) -> str:
    """`S. Asadli` style signature for the print locator block."""
    if user is None:
        return "unknown"
    name = (user.display_name or user.username).strip()
    parts = name.split()
    if len(parts) >= 2:
        return f"{parts[0][0].upper()}. {' '.join(parts[1:])}"
    return name


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")

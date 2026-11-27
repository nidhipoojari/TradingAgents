"""F1: who approved a paper order, as shown by a sign-in, not by a checkbox or the OS user.

An ``Identity`` is issued only by a successful password sign-in: a username and
password checked against a local users file of scrypt hashes
(``TRADINGAGENTS_DASHBOARD_USERS``; make an entry with
``python -m tradingagents.execution.identity hash-password``), with repeated
failures locked out for a while. The dashboard serves localhost only, so no
external identity provider is used.

Identities are signed with a key that exists only in this process and lapse
after ``SESSION_TTL_SECONDS``, so a hand-built one is refused by
``verify_identity`` and therefore by order approval and submission. Restarting
the dashboard makes a new key: every earlier sign-in and pending approval is void.

Paper orders require a verified identity unless ``TRADINGAGENTS_REQUIRE_ORDER_AUTH``
is set to false, in which case orders are recorded as ``auth_method: none``.

Sessions are server-side (Phase 3 hardening): the browser session holds a random
token, the ``SessionStore`` (SQLite, mode 0600) keeps only its hash, and every check
reads it: absolute expiry (``SESSION_TTL_SECONDS``), idle expiry
(``IDLE_TIMEOUT_SECONDS``), revocation at sign-out, and a per-start instance ID so
a restart still voids every session. Failed sign-ins are rate-limited in the same
store (per user and overall), so the limit survives restarts. Approving an order
needs the password again within ``STEP_UP_SECONDS`` (``confirm_password``), and the
approval is bound to the session, which the broker re-checks at submission.

Limit of what this proves: the dashboard's session belongs to someone who signed
in. Code running inside the dashboard's own process could still mint an identity
by calling the sign-in functions; this is protection against an unauthenticated
browser, not against code already running on the server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

AUTH_REQUIRED_ENV = "TRADINGAGENTS_REQUIRE_ORDER_AUTH"
USERS_FILE_ENV = "TRADINGAGENTS_DASHBOARD_USERS"
AUTHENTICATED_METHODS = frozenset({"password"})
SESSION_TTL_SECONDS = 8 * 3600
IDLE_TIMEOUT_SECONDS = 30 * 60
STEP_UP_SECONDS = 5 * 60
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60
STORE_WINDOW_SECONDS = 15 * 60        # persistent limits: failures counted over this window
MAX_FAILURES_PER_USER = 5
MAX_FAILURES_OVERALL = 30
AUTH_DB_ENV = "TRADINGAGENTS_AUTH_DB"
_INSTANCE = secrets.token_hex(8)      # this server start; a restart voids every session
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
_KEY = secrets.token_bytes(32)


class AuthError(ValueError):
    """The sign-in failed; the message is safe to show (it never says which part was wrong)."""


@dataclass(frozen=True)
class Identity:
    subject: str
    method: str
    authenticated_at: str
    session: str = field(default="", repr=False)     # the server-side session token, if any
    confirmed_at: str = ""                           # last password confirmation (step-up)
    # The same two moments on this process's monotonic clock. Wall-clock times are kept (they also count a
    # suspended machine's sleep); these stop a backward clock step from extending a sign-in or a confirmation.
    # Comparable only within this process, as the identity itself is (its signing key exists only here).
    authenticated_mono: float = 0.0
    confirmed_mono: float = 0.0
    signature: str = field(default="", repr=False)


def _now() -> datetime:
    return datetime.now(UTC)


def _monotonic() -> float:
    return time.monotonic()


def _within(wall_since: datetime, mono_since: float, limit_seconds: float) -> bool:
    """Both clocks agree that no more than ``limit_seconds`` have passed and the start is not in the future.

    The stricter clock wins, so a clock step in either direction can shorten a window but never extend it."""
    wall_age = (_now() - wall_since).total_seconds()
    mono_age = _monotonic() - mono_since
    return 0 <= wall_age <= limit_seconds and 0 <= mono_age <= limit_seconds


def auth_required() -> bool:
    return (os.environ.get(AUTH_REQUIRED_ENV) or "true").strip().lower() not in {"0", "false", "no", "off"}


def _sign(identity: Identity) -> str:
    payload = json.dumps({k: v for k, v in asdict(identity).items() if k != "signature"}, sort_keys=True)
    return hmac.new(_KEY, payload.encode(), hashlib.sha256).hexdigest()


def _issue(subject: str, method: str, session: str = "") -> Identity:
    now, mono = _now().isoformat(timespec="seconds"), _monotonic()
    identity = Identity(subject=subject.strip(), method=method, authenticated_at=now, session=session,
                        confirmed_at=now, authenticated_mono=mono, confirmed_mono=mono)
    return replace(identity, signature=_sign(identity))


def session_ref(token: str) -> str:
    """What is stored and bound into approvals: a hash, never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest() if token else ""


def confirmed_recently(identity: Identity, seconds: int = STEP_UP_SECONDS) -> bool:
    try:
        confirmed = datetime.fromisoformat(identity.confirmed_at or identity.authenticated_at)
    except (TypeError, ValueError):
        return False
    return _within(confirmed, identity.confirmed_mono, seconds + 5)


def verify_identity(identity, store: SessionStore | None = None) -> list[str]:
    """Why ``identity`` is not a current, signed sign-in; empty when it is.

    With ``store``, its server-side session must also be active (not expired, idle,
    revoked, or from before a restart); checking it counts as activity.
    """
    if not isinstance(identity, Identity):
        return ["Sign in to approve paper orders."]
    if not identity.signature or not hmac.compare_digest(identity.signature, _sign(identity)):
        return ["The sign-in could not be verified; sign in again."]
    if identity.method not in AUTHENTICATED_METHODS or not identity.subject:
        return ["The sign-in has no authenticated user; sign in again."]
    try:
        authenticated = datetime.fromisoformat(identity.authenticated_at)
    except ValueError:
        return ["The sign-in time cannot be read; sign in again."]
    # A future sign-in time (the clock stepped back) is allowed a minute's slack, then refused; the monotonic
    # age, which no clock step changes, must also be within the lifetime.
    wall_age = (_now() - authenticated).total_seconds()
    mono_age = _monotonic() - identity.authenticated_mono
    if wall_age > SESSION_TTL_SECONDS or mono_age > SESSION_TTL_SECONDS or mono_age < 0:
        return ["The sign-in has expired; sign in again."]
    if wall_age < -60:
        return ["The sign-in time is in the future (the clock changed); sign in again."]
    if store is not None:
        if not identity.session:
            return ["The sign-in has no server-side session; sign in again."]
        return store.check(identity.session)
    return []


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("use a password of at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}$".format(**_SCRYPT) + \
        f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=len(expected),
                                n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def load_users(path: str | Path | None = None) -> dict[str, str]:
    """``{username: scrypt hash}`` from the users file; empty when none is configured."""
    path = path or os.environ.get(USERS_FILE_ENV)
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthError(f"the users file {path} cannot be read: {type(exc).__name__}") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise AuthError(f"the users file {path} must map usernames to password hashes")
    return data


class LoginThrottle:
    """Lock a username out for a while after repeated failed sign-ins."""

    def __init__(self, max_failures: int = MAX_FAILURES, lockout_seconds: float = LOCKOUT_SECONDS):
        self.max_failures, self.lockout_seconds = max_failures, lockout_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if time.monotonic() - t < self.lockout_seconds]
            self._failures[key] = recent
            if len(recent) >= self.max_failures:
                raise AuthError("Too many failed sign-ins; wait a minute and try again.")

    def failed(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.monotonic())

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


_THROTTLE = LoginThrottle()
_DUMMY_HASH = None


def _check_password_for(username: str, password: str, users: Mapping[str, str]) -> bool:
    global _DUMMY_HASH
    stored = users.get((username or "").strip())
    if stored is None:
        # Spend the same time on an unknown user as on a wrong password.
        _DUMMY_HASH = _DUMMY_HASH or hash_password(secrets.token_urlsafe(16))
        check_password(password or "", _DUMMY_HASH)
        return False
    return check_password(password or "", stored)


def sign_in_with_password(username: str, password: str, users: Mapping[str, str] | None = None,
                          throttle: LoginThrottle | None = None, store: SessionStore | None = None) -> Identity:
    """Check the password; with ``store``, rate-limit there and open a server-side session."""
    throttle = throttle or _THROTTLE
    key = (username or "").strip().lower()
    throttle.check(key)
    if store is not None:
        store.check_rate(key)
    users = load_users() if users is None else users
    if not users:
        raise AuthError(f"Password sign-in is not configured; set {USERS_FILE_ENV}.")
    if not _check_password_for(username, password, users):
        throttle.failed(key)
        if store is not None:
            store.record_attempt(key, ok=False)
        raise AuthError("The username or password is not right.")
    throttle.succeeded(key)
    if store is None:
        return _issue(username, "password")
    store.record_attempt(key, ok=True)
    return _issue(username, "password", session=store.open(username.strip(), "password"))


def confirm_password(identity: Identity, password: str, users: Mapping[str, str] | None = None,
                     store: SessionStore | None = None) -> Identity:
    """Step-up: the signed-in person enters the password again to approve an order.

    Returns the identity with a fresh ``confirmed_at``; the session must still be
    active, and failures count against the same rate limits as sign-in.
    """
    problems = verify_identity(identity, store)
    if problems:
        raise AuthError(problems[0])
    key = identity.subject.lower()
    _THROTTLE.check(key)
    if store is not None:
        store.check_rate(key)
    users = load_users() if users is None else users
    if not _check_password_for(identity.subject, password, users):
        _THROTTLE.failed(key)
        if store is not None:
            store.record_attempt(key, ok=False)
        raise AuthError("That password is not right; the order was not approved.")
    confirmed = replace(identity, confirmed_at=_now().isoformat(timespec="seconds"), confirmed_mono=_monotonic(),
                        signature="")
    return replace(confirmed, signature=_sign(confirmed))


# Each open session's monotonic (created, last seen), in this process; see SessionStore.check_ref.
_SESSION_MONO: dict[str, list[float]] = {}
_SESSION_MONO_LOCK = threading.Lock()


class SessionStore:
    """Server-side sessions and persistent sign-in limits, in one SQLite file (mode 0600)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (ref TEXT PRIMARY KEY, subject TEXT, method TEXT, "
                       "instance TEXT, created REAL, last_seen REAL, revoked INTEGER DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS attempts (key TEXT, at REAL, ok INTEGER)")
        os.chmod(self.path, 0o600)

    @classmethod
    def default(cls) -> SessionStore:
        path = os.environ.get(AUTH_DB_ENV)
        if not path:
            from tradingagents.default_config import DEFAULT_CONFIG

            path = Path(DEFAULT_CONFIG["results_dir"]) / "dashboard_auth.sqlite"
        return cls(path)

    def _db(self):
        import sqlite3

        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    @staticmethod
    def _ts() -> float:
        return _now().timestamp()

    # ---------------------------------------------------------- sessions
    def open(self, subject: str, method: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self._ts()
        with _SESSION_MONO_LOCK:
            _SESSION_MONO[session_ref(token)] = [_monotonic(), _monotonic()]
        with self._db() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, 0)",
                       (session_ref(token), subject, method, _INSTANCE, now, now))
        return token

    def check(self, token: str, *, touch: bool = True) -> list[str]:
        return self.check_ref(session_ref(token), touch=touch)

    def check_ref(self, ref: str, *, touch: bool = True) -> list[str]:
        now = self._ts()
        with self._db() as db:
            row = db.execute("SELECT instance, created, last_seen, revoked FROM sessions WHERE ref = ?",
                             (ref,)).fetchone()
            if row is None:
                return ["The session is unknown; sign in again."]
            instance, created, last_seen, revoked = row
            if revoked:
                return ["The session was signed out; sign in again."]
            if instance != _INSTANCE:
                return ["The dashboard restarted since this sign-in; sign in again."]
            with _SESSION_MONO_LOCK:
                mono = _SESSION_MONO.get(ref)
            if mono is None:        # opened by no store in this process (sessions never outlive their process)
                return ["The session is unknown to this dashboard start; sign in again."]
            mono_now = _monotonic()
            if now - created > SESSION_TTL_SECONDS or mono_now - mono[0] > SESSION_TTL_SECONDS:
                return ["The session has expired; sign in again."]
            if now - last_seen > IDLE_TIMEOUT_SECONDS or mono_now - mono[1] > IDLE_TIMEOUT_SECONDS:
                return ["The session was idle too long; sign in again."]
            if now < created - 60:
                return ["The session time is in the future (the clock changed); sign in again."]
            if touch:
                db.execute("UPDATE sessions SET last_seen = ? WHERE ref = ?", (now, ref))
                with _SESSION_MONO_LOCK:
                    mono[1] = mono_now
        return []

    def revoke(self, token: str) -> None:
        with self._db() as db:
            db.execute("UPDATE sessions SET revoked = 1 WHERE ref = ?", (session_ref(token),))

    # ---------------------------------------------------------- persistent limits
    def check_rate(self, key: str) -> None:
        since = self._ts() - STORE_WINDOW_SECONDS
        with self._db() as db:
            db.execute("DELETE FROM attempts WHERE at < ?", (self._ts() - 86400,))
            mine = db.execute("SELECT COUNT(*) FROM attempts WHERE key = ? AND ok = 0 AND at >= ?",
                              (key, since)).fetchone()[0]
            overall = db.execute("SELECT COUNT(*) FROM attempts WHERE ok = 0 AND at >= ?", (since,)).fetchone()[0]
        if mine >= MAX_FAILURES_PER_USER or overall >= MAX_FAILURES_OVERALL:
            raise AuthError("Too many failed sign-ins; wait 15 minutes and try again.")

    def record_attempt(self, key: str, ok: bool) -> None:
        with self._db() as db:
            if ok:
                db.execute("DELETE FROM attempts WHERE key = ? AND ok = 0", (key,))
            db.execute("INSERT INTO attempts VALUES (?, ?, ?)", (key, self._ts(), int(ok)))


def add_user(path: str | Path, username: str, password: str) -> None:
    """Add or replace one user in the users file (created with mode 0600; other users kept)."""
    username = (username or "").strip()
    if not username or any(ch.isspace() for ch in username):
        raise ValueError("the username must be one word")
    path = Path(path)
    users = load_users(path) if path.exists() else {}
    users[username] = hash_password(password)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(users, handle, indent=2, sort_keys=True)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def main(argv: list[str] | None = None) -> None:
    import getpass

    usage = ("usage: python -m tradingagents.execution.identity hash-password\n"
             "       python -m tradingagents.execution.identity add-user USERNAME --file PATH")
    args = argv if argv is not None else sys.argv[1:]
    if args == ["hash-password"]:
        password = getpass.getpass("Password (12+ characters): ")
        if password != getpass.getpass("Again: "):
            sys.exit("The passwords differ.")
        print(hash_password(password))
        return
    if len(args) == 4 and args[0] == "add-user" and args[2] == "--file":
        password = getpass.getpass(f"Password for {args[1]} (12+ characters): ")
        if password != getpass.getpass("Again: "):
            sys.exit("The passwords differ.")
        try:
            add_user(args[3], args[1], password)
        except (ValueError, AuthError) as exc:
            sys.exit(str(exc))
        print(f"Saved {args[1]} to {args[3]} (mode 0600). Set {USERS_FILE_ENV}={args[3]}")
        return
    sys.exit(usage)


if __name__ == "__main__":
    main()

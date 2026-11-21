"""Session tokens, cookies and CSRF tokens (security-privacy 3).

- The cookie holds a random 256-bit token; `web_session.id` stores only its
  SHA-256, so a database reader cannot hijack sessions.
- Cookies use the `__Host-` prefix (browser-enforced: Secure, Path=/, no
  Domain) plus HttpOnly and SameSite=Strict, and carry no Max-Age: the
  server enforces idle and absolute timeouts.
- CSRF (synchronizer token, per session, stateless): HMAC-SHA256 of the
  session token under a KEK-derived key. It changes whenever the session
  rotates and needs no storage. A database reader cannot forge it (it has
  only the token hash), and it is accepted as a form field or, for HTMX, the
  `X-CSRF-Token` header.
- The login form has no session yet; it uses a double-submit cookie
  (`__Host-ag_login_csrf`) compared with the form field.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Final

from starlette.responses import Response

SESSION_COOKIE: Final = "__Host-ag_session"
LOGIN_CSRF_COOKIE: Final = "__Host-ag_login_csrf"
CSRF_FIELD: Final = "csrf_token"
CSRF_HEADER: Final = "X-CSRF-Token"
_TOKEN_BYTES: Final = 32
_MAX_TOKEN_LEN: Final = 128


def new_token() -> str:
    """A random 256-bit token, URL-safe base64 without padding."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def session_key(token: str) -> bytes:
    """The `web_session.id` of a cookie token."""
    return hashlib.sha256(token.encode("ascii", "replace")).digest()


def plausible_token(value: str | None) -> bool:
    """Cheap shape check before touching the database."""
    return value is not None and 0 < len(value) <= _MAX_TOKEN_LEN and value.isascii()


def csrf_token_for(csrf_key: bytes, session_token: str) -> str:
    """The CSRF token bound to one session."""
    mac = hmac.new(csrf_key, b"csrf:" + session_token.encode("ascii"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode("ascii")


def tokens_equal(expected: str, given: str | None) -> bool:
    """Constant-time comparison; a missing value never matches."""
    if not given:
        return False
    return hmac.compare_digest(expected.encode("ascii"), given.encode("ascii", "replace"))


def set_host_cookie(response: Response, name: str, value: str) -> None:
    """Set a `__Host-` cookie with the required flags and no lifetime."""
    response.set_cookie(name, value, path="/", secure=True, httponly=True, samesite="strict")


def clear_host_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="strict")

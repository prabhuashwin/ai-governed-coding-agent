"""Opaque identities: keep people's names and email addresses out of the append-only audit log.

The audit log is hash-chained, so an entry cannot be edited or removed without breaking verification. A "forget
user" request therefore cannot be met by deleting from it. Instead, with `[privacy] opaque_identities = true`, gca
only ever stores opaque platform user ids for people (requester, approver, actor, by). The platform (e.g. Koru)
keeps the id -> person mapping; deleting that mapping makes every gca record about the person unattributable,
and the audit chain stays intact.

What this does not cover: free text the caller sends (title, description, acceptance criteria, notes, conditions,
reasons) is recorded as given. Keep personal data out of it.

ServiceNow people are ServiceNow `sys_id`s (32 hex characters), a different id space from the platform's user
ids, and are checked against that shape instead (`sources/servicenow.py`).
"""

from __future__ import annotations

import re

from .config import Config


class IdentityError(ValueError):
    """An identity that is not an opaque user id while opaque identities are required."""


def check_identity(cfg: Config, value: str | None, field: str) -> str:
    """The identity, stripped; in opaque-identity mode it must match `[privacy] identity_pattern`.

    The offending value is never echoed back: a refused email address must not end up in a stored response."""
    v = (value or "").strip()
    if not cfg.privacy.opaque_identities:
        return v
    pattern = cfg.privacy.identity_pattern
    if not pattern:  # only reachable if the setting was changed after validation: refuse everyone, never accept
        raise IdentityError("opaque identities are on but no [privacy] identity_pattern is set")
    if not v:
        raise IdentityError(f"{field} is required: an opaque user id (opaque identities are on)")
    if not re.fullmatch(pattern, v):
        raise IdentityError(f"{field} must be an opaque user id matching {pattern!r}, not a name, user name or "
                            "email address ([privacy] opaque_identities = true)")
    return v

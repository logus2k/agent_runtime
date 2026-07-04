"""Multi-tenancy authorization (documents/multi_tenancy.md §3, §5).

A **principal** is the authenticated user (OIDC ``sub``). It reaches the farm's admin API
via serve.py, which sets the ``X-Patron-User`` header from the edge proxy's verified
identity. This module is the single place that:

  * resolves the calling principal from a request (with a dev/default fallback), and
  * decides access: ``can_access(principal, owner)`` — owner match OR superuser.

Trust boundary: the farm is internal/localhost-bound; the only ingress is proxy → serve.py
→ farm. When ``settings.internal_auth_token`` is set, the ``X-Patron-User`` header is
trusted ONLY if the request also carries a matching ``X-Internal-Auth`` (defends against a
spoofed identity from a non-proxy origin). Empty token (dev) trusts the header / falls back
to the default principal so single-user keeps working.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request

from .config import settings

USER_HEADER = "X-Patron-User"
EMAIL_HEADER = "X-Patron-Email"
INTERNAL_HEADER = "X-Internal-Auth"


def principal(request: Request) -> str:
    """The calling principal. Reads ``X-Patron-User`` (trusted per the boundary above);
    falls back to ``settings.default_principal`` when absent (dev / direct / legacy)."""
    token = settings.internal_auth_token
    header_user = request.headers.get(USER_HEADER)
    if header_user:
        # If a shared secret is configured, the identity header is only trusted with it.
        if token and request.headers.get(INTERNAL_HEADER) != token:
            return settings.default_principal
        return header_user
    return settings.default_principal


def principal_email(request: Request) -> Optional[str]:
    return request.headers.get(EMAIL_HEADER)


def is_admin(p: str) -> bool:
    return p in settings.admin_principal_set()


def effective_owner(owner: Optional[str]) -> str:
    """A record with no owner (legacy) is owned by the default principal."""
    return owner or settings.default_principal


def can_access(p: str, owner: Optional[str]) -> bool:
    return is_admin(p) or p == effective_owner(owner)


def require_access(request: Request, owner: Optional[str]) -> str:
    """Authorize the caller for a resource owned by ``owner``; return the principal or 403."""
    p = principal(request)
    if not can_access(p, owner):
        raise HTTPException(status_code=403, detail="not authorized for this resource")
    return p

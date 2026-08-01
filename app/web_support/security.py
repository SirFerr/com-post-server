import secrets

from fastapi import HTTPException, Request

from ..models import Role, User


def csrf(request: Request, value: str) -> None:
    expected = request.cookies.get("compost_csrf", "")
    if not value or not secrets.compare_digest(value, expected):
        raise HTTPException(403, "Invalid form token")


def generate_csrf() -> str:
    return secrets.token_urlsafe(24)


def require_role(actor: User, *roles: Role) -> None:
    if actor.role not in roles:
        raise HTTPException(403, "Недостаточно прав")

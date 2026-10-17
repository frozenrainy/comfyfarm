"""What the API modules share: who is calling, are they an admin, which groups may they use."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import HTTPException

from ..core import access
from ..identity import current_roles


@dataclass
class Ctx:
    router: object
    settings: object

    @property
    def dev(self) -> bool:
        return self.settings.dev_identity is not None

    def identity(self, user: str | None) -> str:
        """The user named by the gateway (header kept only if the gateway vouched, see identity.py), or the development
        identity on a loopback-only deployment."""
        if user:
            return user
        if not self.dev:
            raise HTTPException(401, "not authenticated")
        return self.settings.dev_identity

    def is_admin(self, who: str) -> bool:
        return "admin" in current_roles.get() or bool(self.dev and who in self.settings.dev_admins)

    def admin(self, user: str | None) -> str:
        who = self.identity(user)
        if not self.is_admin(who):
            raise HTTPException(403, "admin only")
        return who

    async def allowed(self, c, who: str) -> list[int] | None:
        """Groups `who` may use (None = all), D-34."""
        return await access.allowed_groups(c, who, admin=self.is_admin(who), dev=self.dev)


Identity = Callable[[str | None], str]

"""Identity service facade shared by inbound adapters and worker composition."""

from dataclasses import dataclass

from src.application.identity.service import IdentityService
from src.domain.identity.models import Principal
from src.domain.identity.ports import PrincipalAuthenticator


@dataclass(frozen=True, slots=True)
class IdentityServices:
    service: IdentityService
    authenticator: PrincipalAuthenticator | None = None

    async def authenticate(self, access_token: str) -> Principal:
        authenticator = self.authenticator or self.service
        return await authenticator.authenticate(access_token)

    async def resolve_principal(self, tenant_id: str, user_id: str) -> Principal:
        authenticator = self.authenticator or self.service
        return await authenticator.resolve_principal(tenant_id, user_id)

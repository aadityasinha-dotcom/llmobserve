"""Request and response models for sign-in, the current user, and API keys."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid")


class GoogleSignInRequest(BaseModel):
    """What the dashboard forwards after Google redirects back to it.

    `extra="forbid"`, unlike the ingest schemas: this is not a versioned SDK
    contract that must tolerate newer clients, it is our own dashboard, and an
    unexpected field here is a bug worth failing loudly on.
    """

    model_config = _STRICT

    code: str = Field(min_length=1, max_length=2048)
    # RFC 7636: 43-128 characters.
    code_verifier: str = Field(min_length=43, max_length=128)
    redirect_uri: str = Field(min_length=1, max_length=2048)
    nonce: str = Field(min_length=16, max_length=256)


class UserOut(BaseModel):
    id: UUID
    email: str
    name: str | None = None
    avatar_url: str | None = None


class ProjectOut(BaseModel):
    id: UUID
    name: str
    role: Literal["owner", "member"]


class SessionOut(BaseModel):
    session_token: str
    expires_at: datetime
    user: UserOut
    # True on the sign-in that created the account, so the dashboard can send a
    # new user to key setup rather than to an empty trace list.
    created: bool


class MeOut(BaseModel):
    user: UserOut
    projects: list[ProjectOut]


Scope = Literal["ingest", "read"]


class ApiKeyCreate(BaseModel):
    model_config = _STRICT

    label: str | None = Field(default=None, max_length=255)
    # No default: choosing what a key may do is the point of the form.
    scopes: list[Scope] = Field(min_length=1, max_length=2)


class ApiKeyOut(BaseModel):
    id: UUID
    key_prefix: str | None
    label: str | None
    scopes: list[str]
    created_at: datetime
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    # Returned exactly once. Only its hash is stored, so if it is lost the only
    # remedy is to revoke it and create another.
    api_key: str

from __future__ import annotations

import secrets
import webbrowser
from enum import Enum
from typing import Callable

from pydantic import BaseModel

from .secrets import SecretStore


class AuthorizationStatus(str, Enum):
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"
    AUTHORIZED = "AUTHORIZED"
    FAILED = "FAILED"


class AuthorizationChallenge(BaseModel):
    provider: str
    browser_url: str
    state: str
    secret_ref: str
    requires_user_action: bool = True


class AuthorizationResult(BaseModel):
    status: AuthorizationStatus
    provider: str
    secret_ref: str | None = None
    message: str
    requires_user_action: bool = False


class FirstAuthorizationFlow:
    """Provider-neutral browser authorization with credentials kept in SecretStore."""

    def __init__(
        self,
        provider: str,
        authorization_url: str,
        secret_store: SecretStore,
        *,
        browser_opener: Callable[[str], bool] = webbrowser.open,
    ) -> None:
        self.provider = provider
        self.authorization_url = authorization_url
        self.secret_store = secret_store
        self.browser_opener = browser_opener
        self._state: str | None = None

    def begin(self, *, secret_ref: str) -> AuthorizationChallenge:
        state = secrets.token_urlsafe(24)
        self._state = state
        separator = "&" if "?" in self.authorization_url else "?"
        url = f"{self.authorization_url}{separator}state={state}"
        self.browser_opener(url)
        return AuthorizationChallenge(
            provider=self.provider,
            browser_url=url,
            state=state,
            secret_ref=secret_ref,
        )

    def complete(self, *, state: str, credential: str, secret_ref: str) -> AuthorizationResult:
        if not credential or not self._state or not secrets.compare_digest(state, self._state):
            return AuthorizationResult(
                status=AuthorizationStatus.FAILED,
                provider=self.provider,
                message="Authorization could not be verified; start again.",
                requires_user_action=True,
            )
        self.secret_store.set(secret_ref, credential)
        self._state = None
        return AuthorizationResult(
            status=AuthorizationStatus.AUTHORIZED,
            provider=self.provider,
            secret_ref=secret_ref,
            message="Authorization completed.",
        )

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncContextManager, AsyncIterator, Callable, Protocol
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx2
from mcp.client.auth.oauth2 import OAuthClientInformationFull, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata, OAuthToken

from .secrets import SecretStore


class AsyncHttpClient(Protocol):
    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> Any: ...

    async def post(self, url: str, *, data: dict[str, str], follow_redirects: bool = False) -> Any: ...


Http_Client_Factory = Callable[[Any], AsyncContextManager[AsyncHttpClient]]


@dataclass
class DevSpaceAuthConnection:
    """Stable, GUI-consumable authorization state with no credentials."""

    status: str
    message: str
    secret_ref: str
    requires_user_action: bool = False


class SecretTokenStorage:
    """MCP OAuth token storage backed by the Bridge SecretStore."""

    def __init__(self, secret_store: SecretStore, *, secret_ref: str) -> None:
        self.secret_store = secret_store
        self.secret_ref = secret_ref

    async def get_tokens(self) -> OAuthToken | None:
        raw = self.secret_store.get(self.secret_ref)
        if not raw:
            return None
        return OAuthToken.model_validate_json(raw)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.secret_store.set(self.secret_ref, tokens.model_dump_json(exclude_none=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self.secret_store.get(f"{self.secret_ref}-client")
        if not raw:
            return None
        return OAuthClientInformationFull.model_validate_json(raw)

    async def set_client_info(self, client_info: Any) -> None:
        payload = json.dumps(client_info.model_dump(mode="json", exclude_none=True))
        self.secret_store.set(f"{self.secret_ref}-client", payload)


class DevSpaceLocalOAuthDriver:
    """Authorize the local Supervisor client against loopback DevSpace.

    MCP Python SDK performs discovery, dynamic registration, PKCE, bearer
    injection, and refresh. The only DevSpace-specific step is the upstream
    owner-password approval form; its response is parsed without logging the
    token or returned authorization code.
    """

    def __init__(
        self,
        *,
        owner_secret_ref: str = "devspace-owner-token",
        oauth_secret_ref: str = "devspace-oauth",
        redirect_uri: str = "http://127.0.0.1/codex-supervisor-callback",
        http_client_factory: Http_Client_Factory | None = None,
    ) -> None:
        self.owner_secret_ref = owner_secret_ref
        self.oauth_secret_ref = oauth_secret_ref
        self.redirect_uri = redirect_uri
        self.http_client_factory = http_client_factory or _http_client_with_auth

    async def submit_owner_approval(
        self,
        *,
        client: AsyncHttpClient,
        authorization_url: str,
        owner_token: str,
    ) -> AuthorizationCodeResult:
        authorization_fields = dict(
            parse_qsl(urlparse(authorization_url).query, keep_blank_values=True)
        )
        authorization_fields["owner_token"] = owner_token
        response = await client.post(
            authorization_url,
            data=authorization_fields,
            follow_redirects=False,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            raise RuntimeError("Local workspace authorization was not accepted")
        location = response.headers.get("location")
        if not location:
            raise RuntimeError("Local workspace authorization response was incomplete")
        query = parse_qs(urlparse(location).query)
        code = query.get("code", [None])[0]
        if not code:
            raise RuntimeError("Local workspace authorization did not complete")
        return AuthorizationCodeResult(
            code=code,
            state=query.get("state", [None])[0],
            iss=query.get("iss", [None])[0],
        )

    @asynccontextmanager
    async def http_transport(
        self,
        *,
        mcp_url: str,
        secret_store: SecretStore,
    ) -> AsyncIterator[Any]:
        owner_token = secret_store.get(self.owner_secret_ref)
        if not owner_token or len(owner_token) < 16:
            raise RuntimeError("Local workspace credentials are not ready")
        authorization_result: AuthorizationCodeResult | None = None

        async def redirect_handler(authorization_url: str) -> None:
            nonlocal authorization_result
            async with httpx2.AsyncClient(follow_redirects=False) as client:
                authorization_result = await self.submit_owner_approval(
                    client=client,
                    authorization_url=authorization_url,
                    owner_token=owner_token,
                )

        async def callback_handler() -> AuthorizationCodeResult:
            if authorization_result is None:
                raise RuntimeError("Local workspace authorization did not complete")
            return authorization_result

        provider = OAuthClientProvider(
            mcp_url,
            OAuthClientMetadata.model_validate(
                {
                    "client_name": "Codex Supervisor Bridge",
                    "redirect_uris": [self.redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_method": "none",
                    "scope": "devspace",
                }
            ),
            SecretTokenStorage(secret_store, secret_ref=self.oauth_secret_ref),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        async with httpx2.AsyncClient(auth=provider, follow_redirects=False) as http_client:
            async with streamable_http_client(mcp_url, http_client=http_client) as transport:
                yield transport

    async def authorize(
        self,
        *,
        mcp_url: str,
        secret_store: SecretStore,
        http_client_factory: Http_Client_Factory | None = None,
    ) -> DevSpaceAuthConnection:
        owner_token = secret_store.get(self.owner_secret_ref)
        if not owner_token or len(owner_token) < 16:
            return DevSpaceAuthConnection(
                status="NEEDS_REPAIR",
                message="Local workspace credentials need to be prepared.",
                secret_ref=self.owner_secret_ref,
                requires_user_action=False,
            )

        authorization_result: AuthorizationCodeResult | None = None

        async def redirect_handler(authorization_url: str) -> None:
            nonlocal authorization_result
            async with self.http_client_factory(None) as client:
                authorization_result = await self.submit_owner_approval(
                    client=client,
                    authorization_url=authorization_url,
                    owner_token=owner_token,
                )

        async def callback_handler() -> AuthorizationCodeResult:
            if authorization_result is None:
                raise RuntimeError("Local workspace authorization did not complete")
            return authorization_result

        provider = OAuthClientProvider(
            mcp_url,
            OAuthClientMetadata.model_validate(
                {
                    "client_name": "Codex Supervisor Bridge",
                    "redirect_uris": [self.redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_method": "none",
                    "scope": "devspace",
                }
            ),
            SecretTokenStorage(secret_store, secret_ref=self.oauth_secret_ref),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        async with (http_client_factory or self.http_client_factory)(provider) as client:
            response = await client.get(mcp_url, headers={"Accept": "application/json, text/event-stream"})
        if response.status_code >= 400:
            raise RuntimeError("Local workspace connection could not be authorized")
        return DevSpaceAuthConnection(
            status="AUTHORIZED",
            message="Local workspace is authorized.",
            secret_ref=self.oauth_secret_ref,
        )


def _http_client_with_auth(auth: Any) -> AsyncContextManager[AsyncHttpClient]:
    return httpx2.AsyncClient(auth=auth, follow_redirects=False)


def redact_oauth_payload(value: Any) -> Any:
    """Recursively remove all OAuth credential fields from diagnostics."""
    if isinstance(value, dict):
        return {
            key: redact_oauth_payload(item)
            for key, item in value.items()
            if key
            not in {
                "access_token",
                "refresh_token",
                "code",
                "code_verifier",
                "owner_token",
                "client_secret",
            }
        }
    if isinstance(value, list):
        return [redact_oauth_payload(item) for item in value]
    return value

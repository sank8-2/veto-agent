"""The four requests that make up this flow: authorize, token, userinfo, jwks."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .config import Config


class OidcError(RuntimeError):
    """Auth0 refused something, and this is what it said."""


def authorize_url(config: Config, state: str, challenge: str) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": config.scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{config.base_url}/authorize?{query}"


def logout_url(config: Config) -> str:
    query = urllib.parse.urlencode(
        {"client_id": config.client_id, "returnTo": _origin(config.redirect_uri)}
    )
    return f"{config.base_url}/v2/logout?{query}"


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def exchange_code(
    config: Config,
    code: str,
    verifier: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Trade the authorization code for tokens, proving PKCE with the verifier."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "redirect_uri": config.redirect_uri,
            "code_verifier": verifier,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{config.base_url}/oauth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with opener(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise OidcError(f"token exchange failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OidcError(f"could not reach Auth0: {exc}") from exc
    if "id_token" not in payload:
        raise OidcError("Auth0 returned no id_token")
    if not payload.get("access_token"):
        raise OidcError("Auth0 returned no access_token")
    return payload


def userinfo(
    config: Config,
    access_token: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Ask Auth0 who this access token belongs to.

    This is the first-party API's whole authentication story: no shared secret,
    no trusted header — the token the assistant presents is checked against the
    issuer that minted it.
    """
    request = urllib.request.Request(
        f"{config.base_url}/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with opener(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OidcError(f"/userinfo rejected the token ({exc.code})") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OidcError(f"could not reach Auth0: {exc}") from exc
    if not payload.get("sub"):
        raise OidcError("/userinfo returned no subject")
    return payload


class IdTokenVerifier:
    """Verify the ID token's signature against the tenant's JWKS, then its claims."""

    def __init__(self, config: Config, jwks_client=None) -> None:
        self.config = config
        self._jwks = jwks_client

    def _client(self):
        if self._jwks is None:
            from jwt import PyJWKClient  # imported here so tests need no network

            self._jwks = PyJWKClient(f"{self.config.base_url}/.well-known/jwks.json")
        return self._jwks

    def verify(self, token: str) -> dict[str, Any]:
        import jwt  # imported here so the app can start without the dependency

        try:
            signing_key = self._client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.config.client_id,
                issuer=self.config.issuer,
            )
        except Exception as exc:  # pyjwt raises several unrelated types
            raise OidcError(f"the ID token did not verify: {exc}") from exc
        if not claims.get("sub"):
            raise OidcError("the ID token has no subject")
        return claims

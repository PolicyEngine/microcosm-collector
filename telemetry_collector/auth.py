"""Hugging Face identity validation for collector token exchange."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class HuggingFaceAuthenticationError(RuntimeError):
    """The presented Hugging Face credential could not be authenticated."""


@dataclass(frozen=True)
class HuggingFacePrincipal:
    """Identity returned by Hugging Face for one bearer credential."""

    user_id: str
    username: str
    organizations: tuple[str, ...]


class HuggingFaceAuthenticator:
    """Validate opaque Hugging Face tokens using the live whoami endpoint."""

    def __init__(
        self,
        *,
        endpoint: str = "https://huggingface.co/api/whoami-v2",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds

    def authenticate(self, token: str, required_org: str) -> HuggingFacePrincipal:
        try:
            response = httpx.get(
                self._endpoint,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout_seconds,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise HuggingFaceAuthenticationError(
                "Hugging Face identity verification is unavailable."
            ) from error
        if response.status_code in {401, 403}:
            raise HuggingFaceAuthenticationError(
                "The Hugging Face credential is invalid or expired."
            )
        if response.status_code != 200:
            raise HuggingFaceAuthenticationError(
                "Hugging Face identity verification failed."
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise HuggingFaceAuthenticationError(
                "Hugging Face returned an invalid identity response."
            ) from error
        if not isinstance(payload, dict):
            raise HuggingFaceAuthenticationError(
                "Hugging Face returned an invalid identity response."
            )
        user_id = payload.get("id")
        username = payload.get("name")
        if not isinstance(user_id, str) or not user_id:
            raise HuggingFaceAuthenticationError(
                "Hugging Face did not identify the credential owner."
            )
        if not isinstance(username, str) or not username:
            raise HuggingFaceAuthenticationError(
                "Hugging Face did not identify the credential owner."
            )
        organizations = tuple(
            name
            for entry in payload.get("orgs", [])
            if (name := _organization_name(entry)) is not None
        )
        return HuggingFacePrincipal(
            user_id=user_id,
            username=username,
            organizations=organizations,
        )


def _organization_name(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        name = value.get("name")
        return name if isinstance(name, str) and name else None
    return None

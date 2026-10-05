import httpx
import pytest

from telemetry_collector.auth import (
    HuggingFaceAuthenticationError,
    HuggingFaceAuthenticationUnavailableError,
    HuggingFaceAuthenticator,
)


def test_whoami_resolves_organization_membership(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_get(url, *, headers, timeout, follow_redirects):
        calls.append((url, headers))
        return httpx.Response(
            200,
            json={
                "id": "user-id",
                "name": "builder",
                "orgs": [{"name": "policyengine"}, {"name": "another-org"}],
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    principal = HuggingFaceAuthenticator().authenticate(
        "hf-opaque-secret", "policyengine"
    )

    assert principal.user_id == "user-id"
    assert principal.organizations == ("policyengine", "another-org")
    assert calls == [
        (
            "https://huggingface.co/api/whoami-v2",
            {"Authorization": "Bearer hf-opaque-secret"},
        )
    ]


def test_invalid_hugging_face_token_is_not_echoed(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(401, json={"error": "bad token"}),
    )

    with pytest.raises(HuggingFaceAuthenticationError) as captured:
        HuggingFaceAuthenticator().authenticate("hf-private-value", "policyengine")

    assert "hf-private-value" not in str(captured.value)


def test_hugging_face_outage_is_distinct_from_an_invalid_token(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(503, json={"error": "unavailable"}),
    )

    with pytest.raises(HuggingFaceAuthenticationUnavailableError):
        HuggingFaceAuthenticator().authenticate("hf-private-value", "policyengine")

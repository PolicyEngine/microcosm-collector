"""The collector reads Hugging Face only with its own read token."""

import pytest

from telemetry_collector.huggingface_read import (
    GENERIC_TOKEN_VARIABLES,
    TOKEN_VARIABLE,
    HuggingFaceReadConfigurationError,
    HuggingFaceReadSettings,
)

# Not shaped like a real Hugging Face token, so secret scanners ignore it.
EXAMPLE_TOKEN = "hf_test_collector_read_token"


def test_the_token_comes_from_the_collectors_own_variable() -> None:
    settings = HuggingFaceReadSettings.from_environment({TOKEN_VARIABLE: EXAMPLE_TOKEN})

    assert TOKEN_VARIABLE == "COLLECTOR_HF_READ_TOKEN"
    assert settings.token == EXAMPLE_TOKEN


def test_the_process_environment_is_read_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for generic_variable in GENERIC_TOKEN_VARIABLES:
        monkeypatch.delenv(generic_variable, raising=False)
    monkeypatch.setenv(TOKEN_VARIABLE, EXAMPLE_TOKEN)

    assert HuggingFaceReadSettings.from_environment().token == EXAMPLE_TOKEN


def test_surrounding_whitespace_from_the_secret_is_removed() -> None:
    settings = HuggingFaceReadSettings.from_environment(
        {TOKEN_VARIABLE: f"  {EXAMPLE_TOKEN}\n"}
    )

    assert settings.token == EXAMPLE_TOKEN


@pytest.mark.parametrize("configured_value", [None, "", "   \n"])
def test_a_missing_token_names_the_variable_to_mount(
    configured_value: str | None,
) -> None:
    environment = {} if configured_value is None else {TOKEN_VARIABLE: configured_value}

    with pytest.raises(HuggingFaceReadConfigurationError) as raised:
        HuggingFaceReadSettings.from_environment(environment)

    assert TOKEN_VARIABLE in str(raised.value)
    assert "Secret Manager" in str(raised.value)


@pytest.mark.parametrize(
    "malformed_token",
    ["not-a-hugging-face-token", "hf_two words", "hf_\ttabbed", "hf_"],
)
def test_a_malformed_token_is_refused_without_repeating_it(
    malformed_token: str,
) -> None:
    with pytest.raises(HuggingFaceReadConfigurationError) as raised:
        HuggingFaceReadSettings.from_environment({TOKEN_VARIABLE: malformed_token})

    assert TOKEN_VARIABLE in str(raised.value)
    assert malformed_token.strip() not in str(raised.value)


@pytest.mark.parametrize("generic_variable", GENERIC_TOKEN_VARIABLES)
def test_a_generic_hugging_face_token_in_the_environment_is_refused(
    generic_variable: str,
) -> None:
    generic_value = "hf_test_generic_write_token"

    with pytest.raises(HuggingFaceReadConfigurationError) as raised:
        HuggingFaceReadSettings.from_environment(
            {TOKEN_VARIABLE: EXAMPLE_TOKEN, generic_variable: generic_value}
        )

    assert generic_variable in str(raised.value)
    assert generic_value not in str(raised.value)
    assert EXAMPLE_TOKEN not in str(raised.value)


def test_the_generic_variables_are_the_names_hugging_face_tools_read() -> None:
    assert set(GENERIC_TOKEN_VARIABLES) == {
        "HF_TOKEN",
        "HUGGINGFACE_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
    }


def test_an_empty_generic_variable_is_not_a_token() -> None:
    settings = HuggingFaceReadSettings.from_environment(
        {TOKEN_VARIABLE: EXAMPLE_TOKEN, "HF_TOKEN": ""}
    )

    assert settings.token == EXAMPLE_TOKEN


def test_the_token_never_appears_in_the_settings_representation() -> None:
    settings = HuggingFaceReadSettings.from_environment({TOKEN_VARIABLE: EXAMPLE_TOKEN})

    assert EXAMPLE_TOKEN not in repr(settings)
    assert EXAMPLE_TOKEN not in str(settings)

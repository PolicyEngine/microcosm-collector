"""Read access to Hugging Face for the collector's worker jobs.

Only the collector's worker jobs read Hugging Face, for backfills, old
releases and back-building; the API service never holds a Hugging Face
credential. They read with one purpose-specific, fine-grained read token,
which Google Secret Manager holds and Cloud Run mounts into each job that
needs it as ``COLLECTOR_HF_READ_TOKEN``.

Code that reads Hugging Face builds its settings with
``HuggingFaceReadSettings.from_environment()`` when it starts, so a job
without the mount fails at once with a message naming the variable. The
generic variables that Hugging Face's own tools read are refused, so a job
can never fall back to a credential with a different scope.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

TOKEN_VARIABLE: Final = "COLLECTOR_HF_READ_TOKEN"
#: Read without being asked by huggingface_hub and other Hugging Face tools.
GENERIC_TOKEN_VARIABLES: Final = (
    "HF_TOKEN",
    "HUGGINGFACE_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
)
TOKEN_PREFIX: Final = "hf_"


class HuggingFaceReadConfigurationError(RuntimeError):
    """The collector's Hugging Face read token is missing or unusable.

    Messages name variables only, never a credential's value.
    """


@dataclass(frozen=True)
class HuggingFaceReadSettings:
    """The collector's Hugging Face read token, read once when a job starts."""

    token: str = field(repr=False)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> HuggingFaceReadSettings:
        """Read the token, refusing a missing, malformed or generic one."""

        values = os.environ if environment is None else environment
        generic_variables = [
            name for name in GENERIC_TOKEN_VARIABLES if values.get(name, "").strip()
        ]
        if generic_variables:
            raise HuggingFaceReadConfigurationError(
                f"{', '.join(generic_variables)} must not be set: the collector "
                f"reads Hugging Face only with {TOKEN_VARIABLE}."
            )
        token = values.get(TOKEN_VARIABLE, "").strip()
        if not token:
            raise HuggingFaceReadConfigurationError(
                f"{TOKEN_VARIABLE} is required to read Hugging Face. Mount the "
                "collector's read token from Secret Manager into this job."
            )
        if (
            not token.startswith(TOKEN_PREFIX)
            or len(token) == len(TOKEN_PREFIX)
            or any(character.isspace() for character in token)
        ):
            raise HuggingFaceReadConfigurationError(
                f"{TOKEN_VARIABLE} is not a Hugging Face access token."
            )
        return cls(token=token)

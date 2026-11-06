"""Read access to Hugging Face for the collector's worker jobs.

Only the collector's worker jobs read Hugging Face, for backfills, old
releases and back-building; the API service never holds a Hugging Face
credential. They read with one purpose-specific, fine-grained read token,
which Google Secret Manager holds and Cloud Run mounts into each job that
needs it as ``COLLECTOR_HF_READ_TOKEN``. The token is scoped to the datasets
in ``HUGGINGFACE_DATASETS`` that are not public (``token_scope()``).

Code that reads Hugging Face builds its reader with
``HuggingFaceReader.from_environment()`` when it starts, so a job without the
mount fails at once with a message naming the variable. The generic variables
that Hugging Face's own tools read are refused, so a job can never fall back
to a credential with a different scope.

The reader sends the token only to Hugging Face: it never follows an API
redirect or a file-list page to another host, and httpx drops the
``Authorization`` header when a download is redirected to another host.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Any, Final
from urllib.parse import quote

import httpx

TOKEN_VARIABLE: Final = "COLLECTOR_HF_READ_TOKEN"
#: Read without being asked by huggingface_hub and other Hugging Face tools.
GENERIC_TOKEN_VARIABLES: Final = (
    "HF_TOKEN",
    "HUGGINGFACE_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
)
TOKEN_PREFIX: Final = "hf_"
HUGGINGFACE_ENDPOINT: Final = "https://huggingface.co"
REQUEST_TIMEOUT_SECONDS: Final = 30.0
#: Files read whole are records and manifests; anything larger is refused.
MAX_FILE_BYTES: Final = 64 * 1024 * 1024
_DATASET_NAME: Final = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
)
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


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


# The datasets the collector reads ---------------------------------------------


class DatasetPurpose(StrEnum):
    """What a Hugging Face dataset holds for the collector."""

    #: Published release builds of one country's datasets.
    RELEASES = "releases"
    #: Builds staged before release, and their run files.
    STAGING = "staging"


@dataclass(frozen=True)
class HuggingFaceDataset:
    """One Hugging Face dataset the collector's worker jobs read."""

    name: str
    purpose: DatasetPurpose
    #: Public datasets are readable without the token, so it is not scoped to them.
    public: bool = False
    #: The daily check applies the releases.json rules to this dataset. Set it
    #: only once the dataset publishes builds side by side with releases.json
    #: and its latest*.json pointer files are deleted.
    checks_releases_record: bool = False


#: The calibration dashboard's registered countries, without its fixtures.
#: Adding a dataset here also means adding it to the token's scope.
HUGGINGFACE_DATASETS: Final[tuple[HuggingFaceDataset, ...]] = (
    HuggingFaceDataset(
        "policyengine/populace-us", DatasetPurpose.RELEASES, public=True
    ),
    HuggingFaceDataset("policyengine/populace-uk-private", DatasetPurpose.RELEASES),
    HuggingFaceDataset("policyengine/populace-be-private", DatasetPurpose.RELEASES),
    HuggingFaceDataset("policyengine/populace-us-staging", DatasetPurpose.STAGING),
    HuggingFaceDataset("policyengine/populace-uk-staging", DatasetPurpose.STAGING),
)


def token_scope() -> list[str]:
    """The datasets the read token must be allowed to read."""

    return [dataset.name for dataset in HUGGINGFACE_DATASETS if not dataset.public]


# Reading ----------------------------------------------------------------------


class HuggingFaceReadError(RuntimeError):
    """A read that Hugging Face did not answer with HTTP 200.

    ``status_code`` is ``None`` when Hugging Face could not be reached, and 200
    when it answered with something the reader could not use.
    """

    def __init__(self, dataset: str, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.dataset = dataset
        self.status_code = status_code


@dataclass(frozen=True)
class DatasetFile:
    """One file in a dataset's file list."""

    path: str
    size: int
    #: The git blob ID Hugging Face reports as the file's ``oid``.
    blob_id: str
    #: For a file stored with LFS, the SHA-256 of its content.
    lfs_sha256: str | None


def _check_dataset_name(dataset: str) -> None:
    if not _DATASET_NAME.fullmatch(dataset):
        raise ValueError("A Hugging Face dataset name is owner/name.")


class HuggingFaceReader:
    """Reads datasets from Hugging Face with the collector's read token."""

    def __init__(
        self,
        settings: HuggingFaceReadSettings,
        *,
        client: httpx.Client | None = None,
        endpoint: str = HUGGINGFACE_ENDPOINT,
    ) -> None:
        self._settings = settings
        self._endpoint = httpx.URL(endpoint.rstrip("/"))
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._owns_client = client is None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> HuggingFaceReader:
        """A reader with the token from ``COLLECTOR_HF_READ_TOKEN``."""

        return cls(HuggingFaceReadSettings.from_environment(environment), client=client)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HuggingFaceReader:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def dataset_info(self, dataset: str) -> dict[str, Any]:
        """The dataset's information at ``main``."""

        _check_dataset_name(dataset)
        response = self._get(
            dataset, self._url(f"/api/datasets/{dataset}"), "dataset information"
        )
        information = _json(dataset, response, "dataset information")
        if not isinstance(information, dict):
            raise _unusable(dataset, "dataset information")
        return information

    def head_commit(self, dataset: str) -> str:
        """The commit ``main`` points at, to pin later reads to one state."""

        commit = self.dataset_info(dataset).get("sha")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise _unusable(dataset, "dataset information")
        return commit

    def list_files(self, dataset: str, revision: str) -> list[DatasetFile]:
        """Every file in the dataset at ``revision``, read page by page."""

        _check_dataset_name(dataset)
        page_url: httpx.URL | None = self._url(
            f"/api/datasets/{dataset}/tree/{quote(revision, safe='')}"
        )
        parameters: dict[str, str] | None = {"recursive": "true"}
        files: list[DatasetFile] = []
        while page_url is not None:
            response = self._get(dataset, page_url, "file list", parameters)
            entries = _json(dataset, response, "file list")
            if not isinstance(entries, list):
                raise _unusable(dataset, "file list")
            files.extend(
                _dataset_file(dataset, entry)
                for entry in entries
                if isinstance(entry, dict) and entry.get("type") != "directory"
            )
            next_page = response.links.get("next", {}).get("url")
            page_url = None
            if next_page:
                page_url = httpx.URL(next_page)
                if not self._is_hugging_face(page_url):
                    raise HuggingFaceReadError(
                        dataset,
                        response.status_code,
                        f"The file list of {dataset} continues outside Hugging "
                        "Face; the reader does not follow it.",
                    )
            # The next page's address carries its own query.
            parameters = None
        return files

    def read_file(self, dataset: str, path: str, revision: str) -> bytes:
        """One file's bytes at ``revision``, up to ``MAX_FILE_BYTES``."""

        _check_dataset_name(dataset)
        url = self._url(
            f"/datasets/{dataset}/resolve/{quote(revision, safe='')}/"
            f"{quote(path, safe='/')}"
        )
        try:
            with self._client.stream(
                "GET", url, headers=self._authorization(), follow_redirects=True
            ) as response:
                if response.status_code != 200:
                    raise _status_error(dataset, response.status_code, f"file {path}")
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_FILE_BYTES:
                        raise HuggingFaceReadError(
                            dataset,
                            response.status_code,
                            f"{path} in {dataset} is larger than "
                            f"{MAX_FILE_BYTES} bytes.",
                        )
        except httpx.HTTPError as error:
            raise _unreachable(dataset, f"file {path}") from error
        return bytes(content)

    # Requests -------------------------------------------------------------

    def _url(self, path: str) -> httpx.URL:
        return self._endpoint.copy_with(raw_path=path.encode())

    def _is_hugging_face(self, url: httpx.URL) -> bool:
        return (url.scheme, url.host, url.port) == (
            self._endpoint.scheme,
            self._endpoint.host,
            self._endpoint.port,
        )

    def _authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.token}"}

    def _get(
        self,
        dataset: str,
        url: httpx.URL,
        description: str,
        parameters: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.get(
                url,
                params=parameters,
                headers=self._authorization(),
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise _unreachable(dataset, description) from error
        if response.status_code != 200:
            raise _status_error(dataset, response.status_code, description)
        return response


def _status_error(
    dataset: str, status_code: int, description: str
) -> HuggingFaceReadError:
    return HuggingFaceReadError(
        dataset,
        status_code,
        f"Hugging Face answered HTTP {status_code} for the {description} of {dataset}.",
    )


def _unreachable(dataset: str, description: str) -> HuggingFaceReadError:
    return HuggingFaceReadError(
        dataset,
        None,
        f"Hugging Face could not be reached for the {description} of {dataset}.",
    )


def _unusable(dataset: str, description: str) -> HuggingFaceReadError:
    return HuggingFaceReadError(
        dataset,
        200,
        f"Hugging Face returned an unusable {description} for {dataset}.",
    )


def _json(dataset: str, response: httpx.Response, description: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise _unusable(dataset, description) from None


def _dataset_file(dataset: str, entry: dict[str, Any]) -> DatasetFile:
    path = entry.get("path")
    size = entry.get("size")
    blob_id = entry.get("oid")
    lfs = entry.get("lfs")
    lfs_sha256 = lfs.get("oid") if isinstance(lfs, dict) else None
    if (
        not isinstance(path, str)
        or not path
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not isinstance(blob_id, str)
        or (
            lfs is not None
            and not (isinstance(lfs_sha256, str) and _SHA256.fullmatch(lfs_sha256))
        )
    ):
        raise _unusable(dataset, "file list")
    return DatasetFile(path=path, size=size, blob_id=blob_id, lfs_sha256=lfs_sha256)

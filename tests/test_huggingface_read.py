"""The collector reads Hugging Face datasets over HTTP with its own read token."""

import hashlib
import re

import httpx
import pytest

from telemetry_collector.huggingface_read import (
    HUGGINGFACE_DATASETS,
    MAX_FILE_BYTES,
    TOKEN_VARIABLE,
    DatasetFile,
    DatasetPurpose,
    HuggingFaceReadConfigurationError,
    HuggingFaceReader,
    HuggingFaceReadError,
    HuggingFaceReadSettings,
    token_scope,
)
from tests.huggingface_fakes import CONTENT_HOST, FakeHuggingFace, git_blob_id

EXAMPLE_TOKEN = "hf_test_collector_read_token"
UK_RELEASES = "policyengine/populace-uk-private"
MANIFEST_PATH = "releases/microcosm-uk-2024-25-national/build-1/release_manifest.json"
DATASET_PATH = "releases/microcosm-uk-2024-25-national/build-1/microcosm_uk_2024_25.h5"


@pytest.fixture
def hub() -> FakeHuggingFace:
    return FakeHuggingFace(token=EXAMPLE_TOKEN)


def reader_for(hub: FakeHuggingFace) -> HuggingFaceReader:
    return HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN), client=hub.client()
    )


# The datasets the collector reads -------------------------------------------


def test_the_collector_reads_the_registered_release_and_staging_datasets() -> None:
    datasets = {dataset.name: dataset.purpose for dataset in HUGGINGFACE_DATASETS}

    assert datasets == {
        "policyengine/populace-us": DatasetPurpose.RELEASES,
        "policyengine/populace-uk-private": DatasetPurpose.RELEASES,
        "policyengine/populace-be-private": DatasetPurpose.RELEASES,
        "policyengine/populace-us-staging": DatasetPurpose.STAGING,
        "policyengine/populace-uk-staging": DatasetPurpose.STAGING,
    }
    assert len(HUGGINGFACE_DATASETS) == len(datasets)


def test_the_token_is_scoped_to_every_dataset_except_the_public_us_releases() -> None:
    assert token_scope() == [
        "policyengine/populace-uk-private",
        "policyengine/populace-be-private",
        "policyengine/populace-us-staging",
        "policyengine/populace-uk-staging",
    ]


# Requests -------------------------------------------------------------------


def test_dataset_information_is_read_with_the_collectors_token(
    hub: FakeHuggingFace,
) -> None:
    hub.add_dataset(UK_RELEASES, head_commit="a" * 40)

    information = reader_for(hub).dataset_info(UK_RELEASES)

    assert information["sha"] == "a" * 40
    (request,) = hub.requests
    assert str(request.url) == f"https://huggingface.co/api/datasets/{UK_RELEASES}"
    assert request.headers["Authorization"] == f"Bearer {EXAMPLE_TOKEN}"
    assert EXAMPLE_TOKEN not in str(request.url)


def test_the_head_commit_is_the_commit_main_points_at(hub: FakeHuggingFace) -> None:
    hub.add_dataset(UK_RELEASES, head_commit="b" * 40)

    assert reader_for(hub).head_commit(UK_RELEASES) == "b" * 40


@pytest.mark.parametrize("status_code", [401, 403, 404, 429, 500, 503])
def test_any_answer_but_200_is_an_error_naming_the_dataset_and_status(
    hub: FakeHuggingFace, status_code: int
) -> None:
    hub.add_dataset(UK_RELEASES).forced_status = status_code

    with pytest.raises(HuggingFaceReadError) as raised:
        reader_for(hub).dataset_info(UK_RELEASES)

    assert raised.value.dataset == UK_RELEASES
    assert raised.value.status_code == status_code
    assert f"HTTP {status_code}" in str(raised.value)
    assert EXAMPLE_TOKEN not in str(raised.value)


def test_an_api_redirect_is_reported_rather_than_followed(
    hub: FakeHuggingFace,
) -> None:
    hub.add_dataset(UK_RELEASES).forced_status = 307

    with pytest.raises(HuggingFaceReadError) as raised:
        reader_for(hub).dataset_info(UK_RELEASES)

    assert raised.value.status_code == 307
    assert len(hub.requests) == 1


def test_an_unreachable_hugging_face_is_an_error_without_a_status() -> None:
    def refuse_connection(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN),
        client=httpx.Client(transport=httpx.MockTransport(refuse_connection)),
    )

    with pytest.raises(HuggingFaceReadError) as raised:
        reader.dataset_info(UK_RELEASES)

    assert raised.value.status_code is None
    assert raised.value.dataset == UK_RELEASES


def test_a_dataset_name_that_is_not_owner_and_name_is_refused(
    hub: FakeHuggingFace,
) -> None:
    with pytest.raises(ValueError):
        reader_for(hub).dataset_info("policyengine/populace-uk-private/../other")
    assert hub.requests == []


# File lists -----------------------------------------------------------------


def test_the_file_list_holds_every_file_with_its_lfs_sha256(
    hub: FakeHuggingFace,
) -> None:
    manifest = b'{"release": "microcosm-uk-2024-25-national"}'
    dataset_bytes = b"rows" * 100
    hub.add_dataset(
        UK_RELEASES,
        {"releases.json": b"{}", MANIFEST_PATH: manifest, DATASET_PATH: dataset_bytes},
        lfs_paths={DATASET_PATH},
        head_commit="c" * 40,
    )

    files = reader_for(hub).list_files(UK_RELEASES, "c" * 40)

    assert sorted(files, key=lambda listed_file: listed_file.path) == [
        DatasetFile(
            path="releases.json", size=2, blob_id=git_blob_id(b"{}"), lfs_sha256=None
        ),
        DatasetFile(
            path=DATASET_PATH,
            size=len(dataset_bytes),
            blob_id=git_blob_id(dataset_bytes),
            lfs_sha256=hashlib.sha256(dataset_bytes).hexdigest(),
        ),
        DatasetFile(
            path=MANIFEST_PATH,
            size=len(manifest),
            blob_id=git_blob_id(manifest),
            lfs_sha256=None,
        ),
    ]
    assert all(
        request.url.path == f"/api/datasets/{UK_RELEASES}/tree/{'c' * 40}"
        for request in hub.requests
    )


def test_every_page_of_the_file_list_is_read() -> None:
    hub = FakeHuggingFace(token=EXAMPLE_TOKEN, page_size=2)
    names = [f"releases/r/b/file-{index}.json" for index in range(5)]
    hub.add_dataset(UK_RELEASES, {name: b"{}" for name in names})

    files = reader_for(hub).list_files(UK_RELEASES, "main")

    assert sorted(listed_file.path for listed_file in files) == names
    assert len(hub.requests) > 2
    assert all(
        request.headers["Authorization"] == f"Bearer {EXAMPLE_TOKEN}"
        for request in hub.requests
    )


def test_a_next_page_outside_hugging_face_is_never_requested() -> None:
    other_host_requests = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host != "huggingface.co":
            other_host_requests.append(request)
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[],
            headers={"Link": '<https://elsewhere.example.test/page2>; rel="next"'},
        )

    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN),
        client=httpx.Client(transport=httpx.MockTransport(answer)),
    )

    with pytest.raises(HuggingFaceReadError) as raised:
        reader.list_files(UK_RELEASES, "main")

    assert other_host_requests == []
    assert "outside Hugging Face" in str(raised.value)


def test_a_malformed_file_list_is_an_error() -> None:
    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"not": "a list"})
            )
        ),
    )

    with pytest.raises(HuggingFaceReadError) as raised:
        reader.list_files(UK_RELEASES, "main")

    assert raised.value.status_code == 200


# Files ----------------------------------------------------------------------


def test_a_file_is_read_at_the_requested_revision(hub: FakeHuggingFace) -> None:
    hub.add_dataset(UK_RELEASES, {MANIFEST_PATH: b"manifest"}, head_commit="d" * 40)

    content = reader_for(hub).read_file(UK_RELEASES, MANIFEST_PATH, "d" * 40)

    assert content == b"manifest"
    (request,) = hub.requests
    assert request.url.path == (
        f"/datasets/{UK_RELEASES}/resolve/{'d' * 40}/{MANIFEST_PATH}"
    )


def test_a_redirected_download_does_not_carry_the_token_to_another_host(
    hub: FakeHuggingFace,
) -> None:
    hub.add_dataset(UK_RELEASES, {DATASET_PATH: b"rows"}, lfs_paths={DATASET_PATH})

    content = reader_for(hub).read_file(UK_RELEASES, DATASET_PATH, "main")

    assert content == b"rows"
    (content_request,) = hub.requests_to(httpx.URL(CONTENT_HOST).host)
    assert "Authorization" not in content_request.headers


def test_a_missing_file_is_an_error_with_its_status(hub: FakeHuggingFace) -> None:
    hub.add_dataset(UK_RELEASES, {})

    with pytest.raises(HuggingFaceReadError) as raised:
        reader_for(hub).read_file(UK_RELEASES, "releases.json", "main")

    assert raised.value.status_code == 404


def test_a_file_larger_than_the_read_limit_is_refused() -> None:
    oversized = httpx.ByteStream(b"x" * (MAX_FILE_BYTES + 1))
    reader = HuggingFaceReader(
        HuggingFaceReadSettings(token=EXAMPLE_TOKEN),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=oversized)
            )
        ),
    )

    with pytest.raises(HuggingFaceReadError) as raised:
        reader.read_file(UK_RELEASES, "releases.json", "main")

    assert re.search(r"larger than \d+ bytes", str(raised.value))


# Configuration --------------------------------------------------------------


def test_a_reader_from_the_environment_requires_the_token(
    hub: FakeHuggingFace,
) -> None:
    with pytest.raises(HuggingFaceReadConfigurationError):
        HuggingFaceReader.from_environment({}, client=hub.client())

    reader = HuggingFaceReader.from_environment(
        {TOKEN_VARIABLE: EXAMPLE_TOKEN}, client=hub.client()
    )
    hub.add_dataset(UK_RELEASES)
    assert reader.dataset_info(UK_RELEASES)["private"] is True

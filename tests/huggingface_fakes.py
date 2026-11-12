"""An in-process stand-in for the Hugging Face read endpoints the collector uses.

It answers through ``httpx.MockTransport``, so no test reaches the network. It
serves dataset information, recursive file lists (paginated with a ``Link``
header, as Hugging Face does), one folder's entries, with ``expand=true`` each
with its ``lastCommit``, and file downloads, and redirects downloads of LFS
files to a separate content host, as Hugging Face does. A dataset can hold
other files at other commits (``FakeDataset.revisions``), for reads pinned to
a commit that is not its head. A 404 carries Hugging Face's ``X-Error-Code``:
``RepoNotFound``, ``RevisionNotFound`` or ``EntryNotFound``. A download of one
file can be answered with a status of its own (``FakeDataset.file_answers``),
while the dataset's other reads succeed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlencode

import httpx

ENDPOINT = "https://huggingface.co"
CONTENT_HOST = "https://cdn-lfs.example.test"
LFS_POINTER_BYTES = 134
#: The date of every last commit a test doesn't set.
DEFAULT_COMMIT_DATE = "2026-10-23T00:42:24.000Z"


@dataclass
class FakeDataset:
    files: dict[str, bytes]
    lfs_paths: set[str] = field(default_factory=set)
    private: bool = True
    head_commit: str = "0" * 40
    #: Answer every request for this dataset with this status instead.
    forced_status: int | None = None
    #: Answer a download of these files with this status and, when given,
    #: this ``X-Error-Code`` instead; every other read is answered as usual.
    file_answers: dict[str, tuple[int, str | None]] = field(default_factory=dict)
    #: Report these SHA-256 values for LFS files instead of their content's.
    reported_lfs_sha256: dict[str, str] = field(default_factory=dict)
    #: The last commit (ID and date) of each file or folder, by path, for
    #: listings with expand=true; ``None`` leaves ``lastCommit`` out. A path
    #: not listed gets the head commit and ``DEFAULT_COMMIT_DATE``.
    last_commits: dict[str, tuple[str, str] | None] = field(default_factory=dict)
    #: The files at other commits, by commit. ``main`` and the head commit
    #: hold ``files``.
    revisions: dict[str, dict[str, bytes]] = field(default_factory=dict)

    def files_at(self, revision: str) -> dict[str, bytes] | None:
        if revision in {"main", self.head_commit}:
            return self.files
        return self.revisions.get(revision)


def not_found(error_code: str) -> httpx.Response:
    return httpx.Response(
        404, json={"error": error_code}, headers={"X-Error-Code": error_code}
    )


class FakeHuggingFace:
    """Records every request and answers like Hugging Face's read endpoints."""

    def __init__(self, *, token: str, page_size: int = 1000) -> None:
        self.token = token
        self.page_size = page_size
        self.datasets: dict[str, FakeDataset] = {}
        self.requests: list[httpx.Request] = []

    def add_dataset(
        self,
        name: str,
        files: dict[str, bytes] | None = None,
        *,
        lfs_paths: set[str] | None = None,
        private: bool = True,
        head_commit: str | None = None,
    ) -> FakeDataset:
        dataset = FakeDataset(
            files=dict(files or {}),
            lfs_paths=set(lfs_paths or set()),
            private=private,
            head_commit=head_commit
            or hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest(),
        )
        self.datasets[name] = dataset
        return dataset

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def requests_to(self, host: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == host]

    def downloaded_paths(self) -> list[str]:
        """The path of every file requested from a ``resolve`` address."""

        paths = []
        for request in self.requests:
            path = unquote(request.url.raw_path.decode().split("?", 1)[0])
            parts = path.removeprefix("/datasets/").split("/")
            if path.startswith("/datasets/") and len(parts) >= 5:
                paths.append("/".join(parts[4:]))
        return paths

    # Routing --------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if f"{request.url.scheme}://{request.url.host}" == CONTENT_HOST:
            return self._content(request)
        path = unquote(request.url.raw_path.decode().split("?", 1)[0])
        if path.startswith("/api/datasets/"):
            parts = path.removeprefix("/api/datasets/").split("/")
            name = "/".join(parts[:2])
            if len(parts) == 2:
                return self._guarded(request, name, self._information)
            if len(parts) >= 4 and parts[2] == "tree":
                folder = "/".join(parts[4:])
                return self._guarded(
                    request,
                    name,
                    lambda dataset: self._tree(request, name, parts[3], folder),
                )
        if path.startswith("/datasets/"):
            parts = path.removeprefix("/datasets/").split("/")
            if len(parts) >= 5 and parts[2] == "resolve":
                name = "/".join(parts[:2])
                return self._guarded(
                    request,
                    name,
                    lambda dataset: self._resolve(
                        name, dataset, parts[3], "/".join(parts[4:])
                    ),
                )
        return httpx.Response(404, json={"error": "Not found"})

    def _guarded(self, request, name, respond) -> httpx.Response:
        dataset = self.datasets.get(name)
        if dataset is None:
            return not_found("RepoNotFound")
        if dataset.forced_status is not None:
            return httpx.Response(dataset.forced_status, json={"error": "forced"})
        authorized = request.headers.get("Authorization") == f"Bearer {self.token}"
        if dataset.private and not authorized:
            return httpx.Response(401, json={"error": "Invalid credentials"})
        return respond(dataset)

    # Endpoints ------------------------------------------------------------

    def _information(self, dataset: FakeDataset) -> httpx.Response:
        return httpx.Response(
            200, json={"sha": dataset.head_commit, "private": dataset.private}
        )

    def _tree(self, request: httpx.Request, name: str, revision: str, folder: str):
        dataset = self.datasets[name]
        files = dataset.files_at(revision)
        if files is None:
            return not_found("RevisionNotFound")
        query = parse_qs(request.url.query.decode())
        if query.get("recursive") == ["true"] and not folder:
            entries = self._entries(dataset, files)
            listing_query = {"recursive": "true"}
            listing_path = f"{ENDPOINT}/api/datasets/{name}/tree/{revision}"
        elif folder and "recursive" not in query:
            expand = query.get("expand") == ["true"]
            entries = self._folder_entries(dataset, files, folder, expand=expand)
            if not entries:
                return not_found("EntryNotFound")
            listing_query = {"expand": "true"} if expand else {}
            listing_path = f"{ENDPOINT}/api/datasets/{name}/tree/{revision}/{folder}"
        else:
            return httpx.Response(
                400, json={"error": "expected recursive=true or a folder"}
            )
        offset = int(query.get("cursor", ["0"])[0])
        page = entries[offset : offset + self.page_size]
        headers = {}
        if offset + self.page_size < len(entries):
            next_query = urlencode({**listing_query, "cursor": offset + self.page_size})
            headers["Link"] = f'<{listing_path}?{next_query}>; rel="next"'
        return httpx.Response(200, content=json.dumps(page), headers=headers)

    def _file_entry(self, dataset: FakeDataset, file_path: str, content: bytes) -> dict:
        entry: dict = {
            "type": "file",
            "oid": git_blob_id(content),
            "size": len(content),
            "path": file_path,
        }
        if file_path in dataset.lfs_paths:
            entry["lfs"] = {
                "oid": dataset.reported_lfs_sha256.get(
                    file_path, hashlib.sha256(content).hexdigest()
                ),
                "size": len(content),
                "pointerSize": LFS_POINTER_BYTES,
            }
        return entry

    def _folder_entries(
        self,
        dataset: FakeDataset,
        files: dict[str, bytes],
        folder: str,
        *,
        expand: bool,
    ) -> list[dict]:
        """The entries directly inside ``folder``, with expand each with its last commit."""

        prefix = f"{folder}/"
        children = sorted(
            {
                file_path.removeprefix(prefix).split("/", 1)[0]
                for file_path in files
                if file_path.startswith(prefix)
            }
        )
        entries: list[dict] = []
        for child in children:
            child_path = prefix + child
            if child_path in files:
                entry = self._file_entry(dataset, child_path, files[child_path])
            else:
                entry = {
                    "type": "directory",
                    "oid": "f" * 40,
                    "size": 0,
                    "path": child_path,
                }
            last_commit = dataset.last_commits.get(
                child_path, (dataset.head_commit, DEFAULT_COMMIT_DATE)
            )
            if expand and last_commit is not None:
                commit, date = last_commit
                entry["lastCommit"] = {
                    "id": commit,
                    "title": f"Change {child}",
                    "date": date,
                }
            entries.append(entry)
        return entries

    def _entries(self, dataset: FakeDataset, files: dict[str, bytes]) -> list[dict]:
        folders = sorted(
            {
                "/".join(file_path.split("/")[:depth])
                for file_path in files
                for depth in range(1, file_path.count("/") + 1)
            }
        )
        entries: list[dict] = [
            {"type": "directory", "oid": "f" * 40, "size": 0, "path": folder}
            for folder in folders
        ]
        for file_path, content in sorted(files.items()):
            entries.append(self._file_entry(dataset, file_path, content))
        return entries

    def _resolve(self, name, dataset, revision, file_path) -> httpx.Response:
        if file_path in dataset.file_answers:
            status_code, error_code = dataset.file_answers[file_path]
            return httpx.Response(
                status_code,
                json={"error": error_code or "forced"},
                headers={"X-Error-Code": error_code} if error_code else {},
            )
        files = dataset.files_at(revision)
        if files is None:
            return not_found("RevisionNotFound")
        if file_path not in files:
            return not_found("EntryNotFound")
        if file_path in dataset.lfs_paths:
            digest = hashlib.sha256(files[file_path]).hexdigest()
            return httpx.Response(
                302, headers={"Location": f"{CONTENT_HOST}/{name}/{digest}"}
            )
        return httpx.Response(200, content=files[file_path])

    def _content(self, request: httpx.Request) -> httpx.Response:
        name = "/".join(request.url.path.strip("/").split("/")[:2])
        digest = request.url.path.rsplit("/", 1)[-1]
        dataset = self.datasets.get(name)
        every_revision = [dataset.files, *dataset.revisions.values()] if dataset else []
        for files in every_revision:
            for content in files.values():
                if hashlib.sha256(content).hexdigest() == digest:
                    return httpx.Response(200, content=content)
        return httpx.Response(404)


def git_blob_id(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()

"""An in-process stand-in for the Hugging Face read endpoints the collector uses.

It answers through ``httpx.MockTransport``, so no test reaches the network. It
serves dataset information, recursive file lists (paginated with a ``Link``
header, as Hugging Face does), one folder's entries with ``expand=true``
(each folder entry with its ``lastCommit``) and file downloads, and redirects
downloads of LFS files to a separate content host, as Hugging Face does.
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


@dataclass
class FakeDataset:
    files: dict[str, bytes]
    lfs_paths: set[str] = field(default_factory=set)
    private: bool = True
    head_commit: str = "0" * 40
    #: Answer every request for this dataset with this status instead.
    forced_status: int | None = None
    #: The last commit of each folder, by path, for listings with expand=true.
    folder_commits: dict[str, str] = field(default_factory=dict)


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
            return httpx.Response(404, json={"error": "Dataset not found"})
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

    def _check_revision(self, dataset: FakeDataset, revision: str) -> bool:
        return revision in {"main", dataset.head_commit}

    def _tree(self, request: httpx.Request, name: str, revision: str, folder: str):
        dataset = self.datasets[name]
        if not self._check_revision(dataset, revision):
            return httpx.Response(404, json={"error": "Revision not found"})
        query = parse_qs(request.url.query.decode())
        if query.get("recursive") == ["true"] and not folder:
            entries = self._entries(dataset)
            listing_query = {"recursive": "true"}
            listing_path = f"{ENDPOINT}/api/datasets/{name}/tree/{revision}"
        elif query.get("expand") == ["true"] and folder:
            entries = self._folder_entries(dataset, folder)
            if not entries:
                return httpx.Response(404, json={"error": "Entry not found"})
            listing_query = {"expand": "true"}
            listing_path = f"{ENDPOINT}/api/datasets/{name}/tree/{revision}/{folder}"
        else:
            return httpx.Response(
                400, json={"error": "expected recursive=true or a folder with expand"}
            )
        offset = int(query.get("cursor", ["0"])[0])
        page = entries[offset : offset + self.page_size]
        headers = {}
        if offset + self.page_size < len(entries):
            next_query = urlencode({**listing_query, "cursor": offset + self.page_size})
            headers["Link"] = f'<{listing_path}?{next_query}>; rel="next"'
        return httpx.Response(200, content=json.dumps(page), headers=headers)

    def _folder_entries(self, dataset: FakeDataset, folder: str) -> list[dict]:
        """The entries directly inside ``folder``, each folder with its last commit."""

        prefix = f"{folder}/"
        children = sorted(
            {
                file_path.removeprefix(prefix).split("/", 1)[0]
                for file_path in dataset.files
                if file_path.startswith(prefix)
            }
        )
        entries: list[dict] = []
        for child in children:
            child_path = prefix + child
            if child_path in dataset.files:
                content = dataset.files[child_path]
                entries.append(
                    {
                        "type": "file",
                        "oid": git_blob_id(content),
                        "size": len(content),
                        "path": child_path,
                    }
                )
                continue
            entries.append(
                {
                    "type": "directory",
                    "oid": "f" * 40,
                    "size": 0,
                    "path": child_path,
                    "lastCommit": {
                        "id": dataset.folder_commits.get(
                            child_path, dataset.head_commit
                        ),
                        "title": f"Stage {child}",
                        "date": "2026-10-23T00:42:24.000Z",
                    },
                }
            )
        return entries

    def _entries(self, dataset: FakeDataset) -> list[dict]:
        folders = sorted(
            {
                "/".join(file_path.split("/")[:depth])
                for file_path in dataset.files
                for depth in range(1, file_path.count("/") + 1)
            }
        )
        entries: list[dict] = [
            {"type": "directory", "oid": "f" * 40, "size": 0, "path": folder}
            for folder in folders
        ]
        for file_path, content in sorted(dataset.files.items()):
            entry = {
                "type": "file",
                "oid": git_blob_id(content),
                "size": len(content),
                "path": file_path,
            }
            if file_path in dataset.lfs_paths:
                entry["lfs"] = {
                    "oid": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "pointerSize": LFS_POINTER_BYTES,
                }
            entries.append(entry)
        return entries

    def _resolve(self, name, dataset, revision, file_path) -> httpx.Response:
        if not self._check_revision(dataset, revision):
            return httpx.Response(404, json={"error": "Revision not found"})
        if file_path not in dataset.files:
            return httpx.Response(404, json={"error": "Entry not found"})
        if file_path in dataset.lfs_paths:
            digest = hashlib.sha256(dataset.files[file_path]).hexdigest()
            return httpx.Response(
                302, headers={"Location": f"{CONTENT_HOST}/{name}/{digest}"}
            )
        return httpx.Response(200, content=dataset.files[file_path])

    def _content(self, request: httpx.Request) -> httpx.Response:
        name = "/".join(request.url.path.strip("/").split("/")[:2])
        digest = request.url.path.rsplit("/", 1)[-1]
        dataset = self.datasets.get(name)
        for content in dataset.files.values() if dataset else []:
            if hashlib.sha256(content).hexdigest() == digest:
                return httpx.Response(200, content=content)
        return httpx.Response(404)


def git_blob_id(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()

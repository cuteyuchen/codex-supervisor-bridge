from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from codex_supervisor_bridge.bootstrap.archive import (
    UnsafeArchiveError,
    extract_tar_safe,
    extract_zip_safe,
)
from codex_supervisor_bridge.bootstrap.download import DownloadError, HttpsDownloader


def _zip_with_member(tmp_path: Path, name: str, payload: bytes) -> Path:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        info = zipfile.ZipInfo(name)
        handle.writestr(info, payload)
    return archive


def test_zip_extraction_rejects_path_traversal(tmp_path: Path) -> None:
    archive = _zip_with_member(tmp_path, "../escape.txt", b"bad")
    with pytest.raises(UnsafeArchiveError, match="escapes"):
        extract_zip_safe(archive, tmp_path / "out")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_zip_extraction_rejects_absolute_and_drive_names(tmp_path: Path) -> None:
    for name in ("/etc/passwd", "C:/Windows/evil.txt"):
        archive = _zip_with_member(tmp_path, name, b"bad")
        with pytest.raises(UnsafeArchiveError):
            extract_zip_safe(archive, tmp_path / "out")


def test_zip_extraction_rejects_symlink_member(tmp_path: Path) -> None:
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        info = zipfile.ZipInfo("link.txt")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        handle.writestr(info, "target")
    with pytest.raises(UnsafeArchiveError, match="symlink"):
        extract_zip_safe(archive, tmp_path / "out")


def test_tar_extraction_rejects_links_devices_and_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.tgz"
    with tarfile.open(archive, "w:gz") as handle:
        info = tarfile.TarInfo("../escape")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        handle.addfile(info)
    with pytest.raises(UnsafeArchiveError, match="link"):
        extract_tar_safe(archive, tmp_path / "out")


def test_zip_extraction_writes_regular_members(tmp_path: Path) -> None:
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("root/file.txt", "hello")
    destination = tmp_path / "out"
    extract_zip_safe(archive, destination)
    assert (destination / "root" / "file.txt").read_text(encoding="utf-8") == "hello"


def test_https_downloader_requires_https_and_verifies_digest() -> None:
    payload = b"node-binary"

    class FakeResponse:
        def __init__(self) -> None:
            self.stream = io.BytesIO(payload)

        def read(self, size: int) -> bytes:
            return self.stream.read(size)

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    def urlopen(request: object, timeout: object = None, context: object = None) -> FakeResponse:
        del request, timeout, context
        return FakeResponse()

    downloader = HttpsDownloader(
        urlopen=urlopen,
        max_bytes=1024,
        retries=1,
    )
    target = Path(".") / "node.tar.gz"
    try:
        result = downloader.download("https://example.invalid/node.tar.gz", target)
        downloaded = result.read_bytes()
        assert downloaded == payload
        assert hashlib.sha256(downloaded).hexdigest() == hashlib.sha256(payload).hexdigest()
    finally:
        target.unlink(missing_ok=True)

    with pytest.raises(DownloadError, match="HTTPS"):
        downloader.download("http://example.invalid/node.tar.gz", target)


def test_https_downloader_cleans_partial_and_bounded_retries(tmp_path: Path) -> None:
    calls = 0

    def failing_urlopen(
        request: object,
        timeout: object = None,
        context: object = None,
    ) -> io.BytesIO:
        del request, timeout, context
        nonlocal calls
        calls += 1
        raise TimeoutError("timeout")

    downloader = HttpsDownloader(
        urlopen=failing_urlopen,
        retries=2,
    )
    target = tmp_path / "artifact.zip"
    with pytest.raises(DownloadError):
        downloader.download("https://example.invalid/artifact.zip", target)
    assert calls == 2
    assert list(tmp_path.iterdir()) == []

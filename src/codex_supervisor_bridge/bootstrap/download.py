from __future__ import annotations

import hashlib
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import BinaryIO, Callable

from .physical import PhysicalPathGuard


class DownloadError(RuntimeError):
    """A bounded HTTPS download failed after retries."""


class NoDowngradeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject any redirect that downgrades HTTPS to a non-HTTPS scheme."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urljoin(req.full_url, newurl)
        if urllib.parse.urlsplit(target).scheme.lower() != "https":
            raise DownloadError(
                f"redirect downgrade blocked; {target!r} is not HTTPS"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpsDownloader:
    """Streaming HTTPS downloader with bounded time/size and retry semantics.

    The downloader never accepts plain HTTP, always verifies TLS by default,
    rejects HTTPS -> HTTP redirect downgrades at every hop, streams to a
    temporary file, computes SHA256 while streaming, removes the partial file
    on failure, and never emits credentials.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_bytes: int = 512 * 1024 * 1024,
        retries: int = 3,
        chunk_size: int = 1024 * 1024,
        urlopen: Callable[..., object] | None = None,
        ssl_context: ssl.SSLContext | None = None,
        path_guard: PhysicalPathGuard | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retries = max(1, retries)
        self.chunk_size = max(1024, chunk_size)
        self.path_guard = path_guard or PhysicalPathGuard()
        self._ssl_context = ssl_context or ssl.create_default_context()
        if urlopen is None:
            opener = urllib.request.build_opener(
                NoDowngradeRedirectHandler(),
                urllib.request.HTTPSHandler(context=self._ssl_context),
            )
            self._urlopen = _opener_urlopen(opener)
        else:
            self._urlopen = urlopen

    def download(self, url: str, target: Path) -> Path:
        """Download ``url`` into ``target`` atomically and return its path."""
        if not url.lower().startswith("https://"):
            raise DownloadError("downloads must use HTTPS; HTTP downgrade is forbidden")
        target = Path(target)
        self.path_guard.ensure_directory(target.parent, role="components")
        self.path_guard.before_write(target, role="components")
        last_error: str | None = None
        for attempt in range(1, self.retries + 1):
            descriptor, temporary = self.path_guard.create_temp_file(
                target.parent,
                prefix=f"{target.name}.{os.getpid()}.{attempt}.",
                suffix=".tmp",
                role="components",
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    self._download_once(url, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                self.path_guard.verify_root(temporary, role="components")
                self.path_guard.replace(temporary, target, role="components")
                return target
            except Exception as exc:  # noqa: BLE001 - bounded retry
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    continue
                raise DownloadError(last_error or "download failed") from exc
            finally:
                self.path_guard.remove(temporary, role="components")
        raise DownloadError(last_error or "download failed")

    def _download_once(self, url: str, handle: BinaryIO) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "CodexSupervisorBridge/1"})
        response = self._urlopen(request, timeout=self.timeout, context=self._ssl_context)
        digest = hashlib.sha256()
        total = 0
        try:
            with response:
                while True:
                    chunk = response.read(self.chunk_size)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise DownloadError(
                            f"artifact exceeds the {self.max_bytes}-byte size limit"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise DownloadError(f"{type(exc).__name__}: {exc}") from exc

def _opener_urlopen(opener: urllib.request.OpenerDirector):
    def open_url(
        request: urllib.request.Request,
        timeout: object = None,
        context: object = None,
    ) -> object:
        del context
        return opener.open(request, timeout=timeout)

    return open_url

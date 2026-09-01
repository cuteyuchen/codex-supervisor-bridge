from __future__ import annotations

import ctypes
import ctypes.wintypes
import platform
from pathlib import Path
from typing import Protocol

from .physical import PhysicalPathGuard


class SecretStore(Protocol):
    def set(self, name: str, value: str) -> None: ...

    def get(self, name: str) -> str | None: ...

    def delete(self, name: str) -> None: ...


class MemorySecretStore:
    """Safe fake store for tests and ephemeral development sessions."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self._values[name] = value

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def delete(self, name: str) -> None:
        self._values.pop(name, None)


class WindowsDpapiSecretStore:
    """Small DPAPI-backed store; unavailable on non-Windows hosts."""

    def __init__(
        self,
        directory: str | Path,
        *,
        path_guard: PhysicalPathGuard | None = None,
    ) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("Windows DPAPI secret storage is only available on Windows")
        self.directory = Path(directory)
        self.path_guard = path_guard or PhysicalPathGuard()

    def set(self, name: str, value: str) -> None:
        self.path_guard.ensure_directory(self.directory, role="path")
        path = _secret_path(self.directory, name)
        self.path_guard.before_write(path, role="path")
        encrypted = _protect(value.encode("utf-8"))
        self.path_guard.write_bytes(path, encrypted, role="path")

    def get(self, name: str) -> str | None:
        path = _secret_path(self.directory, name)
        if path.exists():
            self.path_guard.verify_root(path, role="path")
        if not path.exists():
            return None
        return _unprotect(path.read_bytes()).decode("utf-8")

    def delete(self, name: str) -> None:
        path = _secret_path(self.directory, name)
        if path.exists():
            self.path_guard.remove(path, role="path")


def _secret_path(directory: Path, name: str) -> Path:
    if not name or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in name):
        raise ValueError("secret name contains unsupported characters")
    return directory / f"{name}.dpapi"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect(data: bytes) -> bytes:
    in_buffer = ctypes.create_string_buffer(data)
    blob = _Blob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out = _Blob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(ctypes.byref(blob), "Codex Supervisor Bridge", None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI encryption failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _unprotect(data: bytes) -> bytes:
    in_buffer = ctypes.create_string_buffer(data)
    blob = _Blob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out = _Blob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(ctypes.byref(blob), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI decryption failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)

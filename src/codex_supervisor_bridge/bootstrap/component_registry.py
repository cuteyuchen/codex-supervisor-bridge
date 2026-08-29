from __future__ import annotations

import platform
from pathlib import Path
from typing import Mapping

from .installer import ComponentInstaller, ComponentManifest, InstallPlan

BUILTIN_MANIFESTS: dict[str, ComponentManifest] = {
    "openai-tunnel-client": ComponentManifest(
        name="openai-tunnel-client",
        display_name="OpenAI Secure MCP Tunnel",
        version="0.0.13",
        source=(
            "https://github.com/openai/tunnel-client/releases/download/v0.0.13/"
            "tunnel-client-v0.0.13-windows-amd64.zip"
        ),
        source_ref="v0.0.13",
        checksum_sha256="17113162b353906bbb884c3ed7620facba5cc72b5fdc94fd54fd7208c7166edb",
        checksum_source=(
            "https://github.com/openai/tunnel-client/releases/download/v0.0.13/"
            "SHA256SUMS.txt"
        ),
        checksum_entry="tunnel-client-v0.0.13-windows-amd64.zip",
        archive_kind="zip",
        archive_root=None,
        entrypoint="tunnel-client.exe",
        version_args=["--version"],
        version_contains="0.0.13",
        install_commands=[],
        requires_node=False,
    ),
    "nodejs": ComponentManifest(
        name="nodejs",
        display_name="Node.js",
        version="24.20.0",
        source="https://nodejs.org/dist/v24.20.0/node-v24.20.0-win-x64.zip",
        source_ref="v24.20.0",
        checksum_sha256="6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba",
        archive_kind="zip",
        archive_root="node-v24.20.0-win-x64",
        entrypoint="node.exe",
        install_commands=[],
        requires_node=False,
    ),
    "devspace": ComponentManifest(
        name="devspace",
        display_name="Local workspace",
        version="1.0.8",
        source="https://registry.npmjs.org/@waishnav/devspace/-/devspace-1.0.8.tgz",
        source_ref="1.0.8",
        checksum_sha256="59578f71855c160826682fa0409ca885287fee79f549b57128d8962348ff6488",
        archive_kind="tgz",
        archive_root="package",
        entrypoint="dist/cli.js",
        install_commands=[
            ["npm", "install", "--omit=dev"],
        ],
        requires_node=True,
    ),
    "local-codex-bridge": ComponentManifest(
        name="local-codex-bridge",
        display_name="Codex control",
        version="2.1.3",
        source=(
            "https://github.com/zoeynine/Local-Codex-Bridge/archive/"
            "4ffed814f615316ade8967189a2e1772488d33c2.tar.gz"
        ),
        source_ref="4ffed814f615316ade8967189a2e1772488d33c2",
        commit_sha="4ffed814f615316ade8967189a2e1772488d33c2",
        checksum_sha256=None,
        archive_kind="tgz",
        entrypoint="dist/src/index.js",
        install_commands=[
            ["npm", "ci"],
            ["npm", "run", "typecheck"],
            ["npm", "run", "build"],
        ],
        requires_node=True,
    ),
}


class ManagedComponentRegistry:
    """Built-in trusted registry for app-managed component installation.

    Normal users never supply manifests, URLs, checksums, or install commands.
    The registry owns the pinned versions and their verification strategy.
    """

    def __init__(
        self,
        manifests: Mapping[str, ComponentManifest] | None = None,
    ) -> None:
        self._manifests = dict(manifests or BUILTIN_MANIFESTS)

    def names(self) -> list[str]:
        return sorted(self._manifests)

    def manifests(self) -> dict[str, ComponentManifest]:
        return {name: self.manifest(name) for name in self._manifests}

    def manifest(self, name: str) -> ComponentManifest:
        try:
            manifest = self._manifests[name]
        except KeyError as exc:
            raise ValueError(f"unknown managed component: {name}") from exc
        if name != "openai-tunnel-client" or platform.system() != "Windows":
            return manifest
        machine = platform.machine().lower()
        if machine not in {"arm64", "aarch64"}:
            return manifest
        artifact = "tunnel-client-v0.0.13-windows-arm64.zip"
        checksum = "ec7c33cb06fabbbc04aa4803304b647f8542922b8b1489c961b3ebfc283ddcb0"
        return manifest.model_copy(
            update={
                "source": manifest.source.replace(
                    "tunnel-client-v0.0.13-windows-amd64.zip", artifact
                ),
                "checksum_sha256": checksum,
                "checksum_entry": artifact,
            }
        )

    def plan(
        self,
        name: str,
        installer: ComponentInstaller,
    ) -> InstallPlan:
        return installer.plan(self.manifest(name))

    def install_plan(
        self,
        name: str,
        components_root: str | Path,
        *,
        max_retries: int = 3,
    ) -> InstallPlan:
        installer = ComponentInstaller(
            components_root,
            max_retries=max_retries,
            trusted_manifests=self.manifests(),
        )
        return self.plan(name, installer)

    def verification_strategy(self, name: str) -> str:
        manifest = self.manifest(name)
        if manifest.commit_sha:
            return (
                f"pinned {manifest.version} at commit {manifest.commit_sha}; "
                "the upstream archive is unsigned, so the immutable GitHub "
                "commit archive plus build and protocol health is the "
                "verification strategy"
            )
        if manifest.checksum_sha256:
            sidecar = " and official SHA256SUMS.txt" if manifest.checksum_source else ""
            return (
                f"pinned {manifest.version}; SHA256 verified from the official "
                f"distribution{sidecar} before promotion"
            )
        return (
            f"pinned {manifest.version}; upstream publishes no official SHA256 "
            "for its generated archive, so the pinned tag is verified during "
            "the Windows Gate by build and protocol health"
        )

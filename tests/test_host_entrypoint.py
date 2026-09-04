from __future__ import annotations

import os
from types import SimpleNamespace

import codex_supervisor_bridge.bootstrap.host as host_module
import codex_supervisor_bridge.mcp.server as server_module


def test_host_entrypoint_delegates_with_authority_marker(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakeHost:
        host_identity_path = "C:/managed/runtime/host-identity.json"

        def prepare_app_data(self) -> SimpleNamespace:
            return SimpleNamespace(
                identity=SimpleNamespace(host_instance_id="csb-host-test")
            )

    def fake_server(argv: list[str]) -> None:
        observed["argv"] = argv
        observed["authority"] = os.environ.get(host_module.SUPERVISOR_HOST_AUTHORITY_ENV)
        observed["instance"] = os.environ.get(host_module.SUPERVISOR_HOST_INSTANCE_ENV)
        observed["identity_path"] = os.environ.get(host_module.SUPERVISOR_HOST_IDENTITY_PATH_ENV)

    monkeypatch.setattr(host_module, "StandaloneSupervisorHost", FakeHost)
    monkeypatch.setattr(server_module, "main", fake_server)
    monkeypatch.delenv(host_module.SUPERVISOR_HOST_AUTHORITY_ENV, raising=False)
    monkeypatch.delenv(host_module.SUPERVISOR_HOST_INSTANCE_ENV, raising=False)
    monkeypatch.delenv(host_module.SUPERVISOR_HOST_IDENTITY_PATH_ENV, raising=False)

    host_module.main(["--transport", "stdio"])

    assert observed == {
        "argv": ["--transport", "stdio"],
        "authority": host_module.SUPERVISOR_HOST_AUTHORITY_VALUE,
        "instance": "csb-host-test",
        "identity_path": "C:/managed/runtime/host-identity.json",
    }
    assert host_module.SUPERVISOR_HOST_AUTHORITY_ENV not in os.environ


def test_direct_mcp_server_entrypoint_redirects_to_standalone_host(monkeypatch) -> None:
    forwarded: list[str] = []

    def fake_host(argv: list[str]) -> None:
        forwarded.extend(argv)

    monkeypatch.setattr(host_module, "main", fake_host)
    monkeypatch.delenv(host_module.SUPERVISOR_HOST_AUTHORITY_ENV, raising=False)

    server_module.main(["--transport", "stdio"])

    assert forwarded == ["--transport", "stdio"]

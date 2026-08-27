from __future__ import annotations


def create_mcp_server(*args: object, **kwargs: object):
    """Lazily import the server so ``python -m ...mcp.server`` stays quiet."""
    from .server import create_mcp_server as _create_mcp_server

    return _create_mcp_server(*args, **kwargs)


__all__ = ["create_mcp_server"]

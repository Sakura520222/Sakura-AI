"""Trusted runtime construction for the standalone sandboxd entrypoint.

The ASGI app still accepts an injected runtime for unit tests.  The real
server/CLI path must use this factory so a missing or mutable Docker
configuration cannot silently select ``UnavailableRuntimeAdapter``.
"""

from __future__ import annotations

from .config import SandboxdConfig, is_immutable_image_reference
from .docker_runtime import DockerRuntimeAdapter
from .runtime import RuntimeAdapter


def create_runtime(config: SandboxdConfig) -> RuntimeAdapter:
    """Instantiate the only production runtime supported by sandboxd.

    ``SandboxdConfig`` performs shape validation; this second admission gate
    checks the fields required to safely bind a real Docker adapter.  A local
    source image ID (``sha256:<64 hex>``) is immutable and accepted, while a
    mutable tag remains rejected even when it is supplied through a trusted
    CLI invocation.
    """

    if config.runtime_name != "docker":
        raise ValueError("sandboxd runtime must be explicitly configured as docker")
    if not config.workspace_root:
        raise ValueError("sandboxd Docker runtime requires workspace_root")
    if not config.instance_id:
        raise ValueError("sandboxd Docker runtime requires stable instance_id")
    if not config.runner_image_digest or not is_immutable_image_reference(
        config.runner_image_digest
    ):
        raise ValueError("sandboxd Docker runtime requires an immutable runner image reference")
    return DockerRuntimeAdapter(config)


# These names make the factory easy to discover for embedding callers while
# keeping one implementation and one validation path.
build_runtime = create_runtime
create_runtime_adapter = create_runtime


__all__ = ["build_runtime", "create_runtime", "create_runtime_adapter"]

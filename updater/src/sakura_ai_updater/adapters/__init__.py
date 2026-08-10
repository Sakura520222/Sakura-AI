"""Deployment adapters used by the host updater.

The adapter package intentionally contains only host-side primitives.  Web
containers never import this package and no Docker socket is exposed to them.
"""

from .image import (
    HealthCheckError,
    HealthCheckTimeout,
    HealthCheckVersionMismatch,
    ImageAdapter,
    ImageAdapterError,
    ImageCommandError,
    ImagePreflightError,
    atomic_update_deployment_env,
    write_deployment_env,
)

__all__ = [
    "HealthCheckError",
    "HealthCheckTimeout",
    "HealthCheckVersionMismatch",
    "ImageAdapter",
    "ImageAdapterError",
    "ImageCommandError",
    "ImagePreflightError",
    "atomic_update_deployment_env",
    "write_deployment_env",
]

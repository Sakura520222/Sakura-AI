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
    ImageDeploymentAdapter,
    ImagePreflightError,
    atomic_update_deployment_env,
    capture_deployment_snapshot,
    restore_deployment_snapshot,
    write_deployment_env,
    write_deployment_env_values,
)

__all__ = [
    "HealthCheckError",
    "HealthCheckTimeout",
    "HealthCheckVersionMismatch",
    "ImageAdapter",
    "ImageAdapterError",
    "ImageCommandError",
    "ImageDeploymentAdapter",
    "ImagePreflightError",
    "atomic_update_deployment_env",
    "capture_deployment_snapshot",
    "restore_deployment_snapshot",
    "write_deployment_env",
    "write_deployment_env_values",
]

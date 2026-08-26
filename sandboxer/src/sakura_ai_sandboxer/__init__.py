"""Independent host-side Agent sandbox daemon.

This package intentionally has no import dependency on ``updater``.  The
daemon owns a separate protocol, socket, lifecycle and runtime adapter.
"""

from __future__ import annotations

PROTOCOL_VERSION = 1
__version__ = "0.1.0"

__all__ = ["PROTOCOL_VERSION", "__version__"]

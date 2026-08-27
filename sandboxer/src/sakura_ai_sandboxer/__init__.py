"""Independent host-side Agent sandbox daemon.

This package intentionally has no import dependency on ``updater``.  The
daemon owns a separate protocol, socket, lifecycle and runtime adapter.
"""

from __future__ import annotations

# v2 makes the egress capability explicit on both request and health
# responses.  There is no silent v1 compatibility because a v1 caller could
# otherwise omit the network boundary and accidentally receive a different
# security contract.
PROTOCOL_VERSION = 2
__version__ = "0.1.0"

__all__ = ["PROTOCOL_VERSION", "__version__"]

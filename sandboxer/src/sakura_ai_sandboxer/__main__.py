"""CLI for the standalone ``sakura-ai-sandboxd`` process."""

from __future__ import annotations

import argparse

from . import __version__
from .config import (
    DEFAULT_CLEANUP_MARGIN_SECONDS,
    DEFAULT_EGRESS_NETWORK,
    DEFAULT_LEDGER_CAPACITY,
    DEFAULT_LEDGER_TTL_SECONDS,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_TIMEOUT_SECONDS,
    DEFAULT_RUNNER_IMAGE,
    DEFAULT_SOCKET_GROUP,
    DEFAULT_SOCKET_MODE,
    DEFAULT_SOCKET_OWNER,
    DEFAULT_SOCKET_PATH,
    DEFAULT_TIMEOUT_SECONDS,
    SandboxdConfig,
)
from .server import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sakura AI independent Agent sandbox daemon")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--socket", default=DEFAULT_SOCKET_PATH)
    parser.add_argument("--socket-root", default=None)
    parser.add_argument("--socket-owner", type=int, default=DEFAULT_SOCKET_OWNER)
    parser.add_argument("--socket-group", type=int, default=DEFAULT_SOCKET_GROUP)
    parser.add_argument(
        "--socket-mode",
        type=lambda value: int(value, 8),
        default=DEFAULT_SOCKET_MODE,
        help="UDS permission mode; production is fixed at 0660",
    )
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument("--instance-id", default=None)
    # The real CLI is never allowed to fall back to the unavailable adapter.
    # Unit tests inject a fake runtime directly into ``create_app`` instead.
    parser.add_argument("--runtime", default="docker")
    parser.add_argument("--runner-image", default=None)
    parser.add_argument("--runner-image-digest", default=None)
    parser.add_argument("--docker-binary", default="docker")
    parser.add_argument("--oci-runtime", default=None)
    parser.add_argument(
        "--egress-network",
        dest="egress_network",
        default=DEFAULT_EGRESS_NETWORK,
        help=(
            "server-owned Docker network for the constrained egress capability; "
            "default 'bridge'; the Backend cannot override this value."
        ),
    )
    parser.add_argument("--max-concurrent", type=int, default=DEFAULT_MAX_CONCURRENT)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-timeout-seconds", type=float, default=DEFAULT_MAX_TIMEOUT_SECONDS)
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
    parser.add_argument("--max-response-bytes", type=int, default=None)
    parser.add_argument(
        "--cleanup-margin-seconds",
        type=float,
        default=DEFAULT_CLEANUP_MARGIN_SECONDS,
    )
    parser.add_argument(
        "--request-ledger-capacity",
        type=int,
        default=DEFAULT_LEDGER_CAPACITY,
    )
    parser.add_argument(
        "--request-ledger-ttl-seconds",
        type=float,
        default=DEFAULT_LEDGER_TTL_SECONDS,
    )
    parser.add_argument("--shutdown-timeout-seconds", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    serve(
        SandboxdConfig(
            socket_path=args.socket,
            max_concurrent=args.max_concurrent,
            timeout_seconds=args.timeout_seconds,
            max_timeout_seconds=args.max_timeout_seconds,
            max_output_bytes=args.max_output_bytes,
            max_response_bytes=args.max_response_bytes,
            cleanup_margin_seconds=args.cleanup_margin_seconds,
            request_ledger_capacity=args.request_ledger_capacity,
            request_ledger_ttl_seconds=args.request_ledger_ttl_seconds,
            shutdown_timeout_seconds=args.shutdown_timeout_seconds,
            socket_root=args.socket_root,
            socket_owner=args.socket_owner,
            socket_group=args.socket_group,
            socket_mode=args.socket_mode,
            state_dir=args.state_dir,
            workspace_root=args.workspace_root,
            instance_id=args.instance_id,
            runtime_name=args.runtime,
            runner_image=args.runner_image or DEFAULT_RUNNER_IMAGE,
            runner_image_digest=args.runner_image_digest,
            docker_binary=args.docker_binary,
            oci_runtime=args.oci_runtime,
            egress_network=args.egress_network,
        )
    )


if __name__ == "__main__":
    main()

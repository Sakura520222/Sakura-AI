from __future__ import annotations

import pytest
from sakura_ai_sandboxer.config import DEFAULT_EGRESS_NETWORK, SandboxdConfig


def test_egress_network_defaults_to_docker_bridge():
    config = SandboxdConfig()
    assert DEFAULT_EGRESS_NETWORK == "bridge"
    assert config.egress_network == "bridge"


@pytest.mark.parametrize(
    "network",
    ["host", "container:other", "ns:/run/netns/x", "--network=host", "bad/name", ""],
)
def test_egress_network_rejects_runtime_namespace_and_argv_fragments(network: str):
    with pytest.raises(ValueError, match="egress_network"):
        SandboxdConfig(egress_network=network)

"""精确验证两个 compose 的 web service 含 group_add 9472 + read-only /run/sakura-ai bind mount。

不全文 grep 字符串——解析 YAML 后精确定位 services.web 并断言结构（spec §7.1 安全增强）。
"""

import pytest
import yaml

COMPOSE_FILES = [
    "docker/docker-compose.yml",
    "docker/docker-compose.prod.yml",
]


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_web_service_has_group_add_9472(compose_file):
    with open(compose_file, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    web = config["services"]["web"]
    assert "9472" in [str(v) for v in web.get("group_add", [])], \
        f"{compose_file}: web service missing group_add 9472"


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_web_service_has_readonly_run_mount(compose_file):
    with open(compose_file, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    web = config["services"]["web"]
    mounts = web.get("volumes", [])
    # 查找 target=/run/sakura-ai 的 mount（long syntax dict 形式）
    run_mounts = [
        v for v in mounts
        if isinstance(v, dict) and v.get("target") == "/run/sakura-ai"
    ]
    assert len(run_mounts) == 1, f"{compose_file}: expected exactly one /run/sakura-ai mount"
    mount = run_mounts[0]
    assert mount["type"] == "bind"
    assert mount["source"] == "/run/sakura-ai"
    assert mount["read_only"] is True


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_no_docker_sock_mounted(compose_file):
    """安全 invariant：任何 service 不得挂 /var/run/docker.sock。"""
    with open(compose_file, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    for service_name, service_config in config.get("services", {}).items():
        for volume in service_config.get("volumes", []):
            if isinstance(volume, dict):
                src = volume.get("source", "")
                tgt = volume.get("target", "")
            elif isinstance(volume, str):
                parts = volume.split(":")
                src = parts[0] if parts else ""
                tgt = parts[1] if len(parts) > 1 else ""
            else:
                continue
            assert "docker.sock" not in src, \
                f"{compose_file} service {service_name}: docker.sock source mount forbidden"
            assert "docker.sock" not in tgt, \
                f"{compose_file} service {service_name}: docker.sock target mount forbidden"

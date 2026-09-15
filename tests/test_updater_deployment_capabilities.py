"""Web and shell consumers reject legacy/partial deployment success."""

import json
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient
from sakura_ai_updater.contract import DEPLOYMENT_CAPABILITIES, compatibility
from sakura_ai_updater.ipc import create_app

from backend.services.updater_client import UpdaterActionError, UpdaterClient
from backend.webui.routes.version import build_version_info

CAPABILITIES = sorted(DEPLOYMENT_CAPABILITIES)


@pytest.mark.parametrize("caps", [None, [], ["three-image-transaction-v1"], "three-image-transaction-v1", [None]])
@pytest.mark.asyncio
async def test_protocol_one_does_not_authorize_any_update_action(monkeypatch, caps):
    seen = []

    def transport(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"protocol_version": 1, "updater_version": "0.2.0",
                                         "capabilities": caps, "data": {}})

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(transport))
    client = UpdaterClient(socket_path="unused")
    for action in (client.check, client.preflight, client.update):
        with pytest.raises(UpdaterActionError) as exc:
            await action()
        assert exc.value.status_code == 409
        assert exc.value.body["missing_capabilities"]
        assert "updater reinstall" in exc.value.body["detail"]
    assert seen == [("GET", "/v1/status")] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("verified,caps,allowed", [(False, CAPABILITIES, False), (True, [], False), (True, CAPABILITIES, True)])
async def test_web_never_projects_unverified_job_success(monkeypatch, verified, caps, allowed):
    payload = {"protocol_version": 1, "updater_version": "0.3.0", "capabilities": caps,
               "data": {"state": "success", "deployment_verified": verified}}
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    client = UpdaterClient(socket_path="unused")
    if allowed:
        assert await client.get_job("test") == payload
    else:
        with pytest.raises(UpdaterActionError, match="deployment_success_unverified"):
            await client.get_job("test")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["success", "complete"])
async def test_legacy_complete_state_is_also_treated_as_success_claim(monkeypatch, state):
    payload = {"protocol_version": 1, "updater_version": "0.3.0", "capabilities": CAPABILITIES,
               "data": {"state": state, "deployment_verified": False}}
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    client = UpdaterClient(socket_path="unused")
    with pytest.raises(UpdaterActionError, match="deployment_success_unverified"):
        await client.get_job("test")


@pytest.mark.parametrize("caps,compatible", [([], False), (CAPABILITIES, True), (CAPABILITIES + ["future-feature"], True)])
def test_ui_exposes_contract_compatibility_and_build_identity(caps, compatible):
    identity = {"protocol_version": 1, "updater_version": "0.3.0", "capabilities": caps,
                "build_revision": "a" * 40, "build_release": "v3.2.0", "data": {"update_ready": True}}
    info = build_version_info("image", {}, identity)
    assert info["updater_connected"] is True
    assert info["update_supported"] is compatible
    assert info["update_ready"] is compatible
    assert info["updater_compatibility"]["compatible"] is compatible
    assert info["updater_build_revision"] == "a" * 40
    assert info["updater_build_release"] == "v3.2.0"


def test_status_health_and_cli_publish_same_machine_identity(tmp_path):
    from sakura_ai_updater import __version__

    assert __version__ == "0.3.0"
    client = TestClient(create_app(str(tmp_path / "state.json")))
    identities = [client.get(path).json() for path in ("/v1/status", "/v1/health")]
    cli = subprocess.run([sys.executable, "-m", "sakura_ai_updater", "--identity"],
                         capture_output=True, text=True, check=True)
    identities.append(json.loads(cli.stdout))
    for identity in identities:
        assert compatibility(identity)["compatible"] is True
        assert identity["updater_version"] == __version__
        assert "build_revision" in identity and "build_release" in identity
    for identity in identities[1:]:
        assert {k: v for k, v in identity.items() if k != "data"} == {k: v for k, v in identities[0].items() if k != "data"}


def test_shell_rejects_live_legacy_daemon_before_submission():
    payload = json.dumps({"protocol_version": 1, "updater_version": "0.2.0", "data": {}})
    script = """
export _START_SH_SOURCED=1
source ./start.sh
updater_daemon_is_running() { return 0; }
updater_ipc_get() { printf '%s' "$IDENTITY"; }
curl() { echo UNSAFE_POST; return 0; }
updater_submit_image_transaction stable ignored
"""
    import os

    result = subprocess.run(["bash"], input=script, capture_output=True, text=True,
                            env={**os.environ, "IDENTITY": payload}, check=False)
    assert result.returncode != 0
    assert "UNSAFE_POST" not in result.stdout
    assert "updater reinstall" in result.stderr

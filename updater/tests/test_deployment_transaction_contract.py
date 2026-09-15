"""Issue 570: exercise the real orchestrator, adapter and identity reader together.

Only registry/Docker/health transport is simulated. Env writes, snapshot journals,
preflight, drift decisions, rollback and job persistence are production code.
"""

import json

import pytest
from sakura_ai_updater.adapters.image import ImageAdapter, ImageAdapterError
from sakura_ai_updater.contract import REPOSITORIES
from sakura_ai_updater.deployment import DeploymentStateProvider, _read_env_file
from sakura_ai_updater.jobs import JobOrchestrator
from sakura_ai_updater.registry import DevelopmentSandboxPair, RegistryClient

REVISION = "a" * 40
OLD_REVISION = "b" * 40
VERSION = "3.2.0"
TAG = f"dev-20260914000000-v{VERSION}-{REVISION}"
TARGET = {"channel": "development", "version": VERSION, "revision": REVISION,
          "tag": TAG, "digest": "sha256:" + "a" * 64}
NEW = {
    "web": REPOSITORIES["web"] + ":" + TAG + "@" + TARGET["digest"],
    "sandboxd": REPOSITORIES["sandboxd"] + "@sha256:" + "c" * 64,
    "runner": REPOSITORIES["runner"] + "@sha256:" + "d" * 64,
}
OLD = {
    "web": REPOSITORIES["web"] + ":dev-20260913000000-v3.2.0-" + OLD_REVISION + "@sha256:" + "e" * 64,
    "sandboxd": REPOSITORIES["sandboxd"] + "@sha256:" + "f" * 64,
    "runner": REPOSITORIES["runner"] + "@sha256:" + "b" * 64,
}


class Transport:
    def __init__(self, path, initial):
        self.path = path
        self.running = dict(initial)
        self.fail = None
        self.failed_once = False
        self.commands = []
        self.pulled = []
        self.bad_label = None
        self.persist(initial)

    def persist(self, values):
        self.path.write_text(
            "# preserve administrator config\nUNRELATED=keep\nCOMPOSE_PROJECT_NAME=sakura-ai\nSAKURA_DEPLOY_MODE=image\n"
            "SAKURA_DEPLOY_CHANNEL=development\nSAKURA_SANDBOX_INSTANCE_ID=sandbox-1234567890abcdef\n"
            f"SAKURA_AI_IMAGE={values['web']}\nSAKURA_SANDBOXD_IMAGE_DIGEST={values['sandboxd']}\n"
            f"SAKURA_AGENT_RUNNER_IMAGE_DIGEST={values['runner']}\n"
        )

    def selected(self):
        env = _read_env_file(str(self.path))
        return {"web": env["SAKURA_AI_IMAGE"], "sandboxd": env.get("SAKURA_SANDBOXD_IMAGE_DIGEST"),
                "runner": env.get("SAKURA_AGENT_RUNNER_IMAGE_DIGEST")}

    def metadata(self, ref):
        if ref == "sha256:" + "0" * 64:
            ref = self.running["web"]
        if ref == "sha256:" + "1" * 64:
            ref = self.running["sandboxd"]
        component = next(c for c, repo in REPOSITORIES.items() if ref.startswith(repo + (":" if c == "web" else "@")))
        labels = {
            "org.opencontainers.image.revision": REVISION if ref == NEW[component] else OLD_REVISION,
            "org.opencontainers.image.version": VERSION,
            "com.sakura-ai.build.channel": "development",
            "com.sakura-ai.component": "agent-runner" if component == "runner" else component,
        }
        if self.bad_label == component:
            labels["org.opencontainers.image.revision"] = "9" * 40
        return {"RepoDigests": [REPOSITORIES[component] + "@" + ref.rsplit("@", 1)[1]], "Config": {"Labels": labels}}

    async def health(self, provider=None, timeout=5):
        return {"version": VERSION, "build": {"channel": "development",
                "revision": REVISION if self.running["web"] == NEW["web"] else OLD_REVISION}}

    async def command(self, argv, **kwargs):
        self.commands.append(tuple(argv))
        if argv[:3] == ["docker", "manifest", "inspect"]:
            return "{}", ""
        if argv[:2] == ["docker", "pull"]:
            assert self.selected() != NEW  # no premature commit, even during mixed repair
            self.pulled.append(argv[-1])
            if self.fail == "pull:" + argv[-1]:
                raise ImageAdapterError("pull failure")
            return "", ""
        if argv[:3] == ["docker", "image", "inspect"]:
            return json.dumps(self.metadata(argv[-1])), ""
        if argv[:2] == ["docker", "inspect"]:
            if argv[-1] == "sakura-ai":
                return "sha256:" + "0" * 64, ""
            return json.dumps({"Config": {"Image": self.running["sandboxd"],
                "Cmd": ["--runner-image-digest", self.running["runner"]],
                "Labels": {"ai.sakura.managed-by": "sandboxd-daemon", "ai.sakura.instance-id": "sandbox-1234567890abcdef",
                           "ai.sakura.runner-image-digest": self.running["runner"]}},
                "Image": "sha256:" + "1" * 64, "State": {"Running": True}}), ""
        if argv[0] == "bash":
            selected = self.selected()
            if self.fail == "rollback" and self.failed_once:
                raise ImageAdapterError("rollback failure")
            self.running.update({k: selected[k] for k in ("sandboxd", "runner")})
            if self.fail == "activation" and not self.failed_once:
                self.failed_once = True
                raise ImageAdapterError("activation failure")
            return "", ""
        if argv[:2] == ["docker", "compose"]:
            assert set(NEW.values()) <= set(self.pulled)
            self.running["web"] = self.selected()["web"]
            if self.fail == "partial":
                self.running["runner"] = OLD["runner"]
            return "", ""
        if argv[0] == "curl":
            if self.fail in {"health", "rollback"}:
                self.failed_once = True
                raise ImageAdapterError("health failure")
            return json.dumps({"protocol_version": 2, "data": {"ready": True, "runtime": "docker",
                "instance_id": "sandbox-1234567890abcdef", "runner_image_digest": self.running["runner"]}}), ""
        raise AssertionError(argv)


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    def factory(initial=None):
        directory = tmp_path / str(len(list(tmp_path.iterdir())))
        directory.mkdir()
        env = directory / "deployment.env"
        transport = Transport(env, initial or {**OLD, "web": NEW["web"]})
        adapter = ImageAdapter(str(directory / "docker/compose.yml"), str(env))
        monkeypatch.setattr(adapter, "_run_command", transport.command)
        monkeypatch.setattr(adapter, "_project_start_script", lambda: directory / "start.sh")

        async def command(provider, argv):
            return await transport.command(argv)

        monkeypatch.setattr(DeploymentStateProvider, "_run_docker_command", command)
        monkeypatch.setattr(DeploymentStateProvider, "_health_payload", transport.health)
        deployment = DeploymentStateProvider(str(env))

        async def disk(threshold):
            return True, 10000000000

        monkeypatch.setattr(deployment, "disk_space_sufficient", disk)

        async def health_check(target):
            payload = await transport.health()
            if isinstance(target, dict):
                assert payload["version"] == target["version"]
                assert payload["build"]["revision"] == target["revision"]

        monkeypatch.setattr(adapter, "health_check", health_check)

        async def verify(client, target):
            return target

        monkeypatch.setattr(RegistryClient, "verify_target", verify)

        class Release:
            async def resolve_development_sandbox_pair(self, target):
                return DevelopmentSandboxPair(REVISION, NEW["sandboxd"], NEW["runner"])

        orchestrator = JobOrchestrator(str(directory / "state.json"), adapter, Release(), deployment, disk_space_threshold=1)
        return orchestrator, transport
    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["web", "sandboxd", "runner", "mixed", "all"])
async def test_each_digest_drift_reconciles_and_only_complete_deployment_is_success(transaction, component):
    initial = dict(NEW)
    if component == "mixed":
        initial.update(sandboxd=OLD["sandboxd"], runner=OLD["runner"])
    elif component != "all":
        initial[component] = OLD[component]
    orchestrator, transport = transaction(initial)
    result = await orchestrator.preflight(TARGET)
    assert result["can_update"] is (component != "all")
    assert result["reconcile_required"] is (component != "all")
    if component == "all":
        assert result["drift"] == []
        assert not transport.pulled
        return
    job = await orchestrator.wait_for_job(await orchestrator.submit_update(TARGET))
    assert job.state == "success", job.error
    assert job.deployment_verified is True
    assert transport.selected() == NEW == transport.running
    assert "UNRELATED=keep" in transport.path.read_text()
    assert not (await orchestrator.preflight(TARGET))["can_update"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pull:web", "pull:sandboxd", "pull:runner", "activation", "health", "partial", "rollback"])
async def test_transaction_failure_never_reports_partial_success(transaction, failure):
    orchestrator, transport = transaction()
    before = transport.path.read_bytes()
    old_runtime = dict(transport.running)
    transport.fail = "pull:" + NEW[failure.split(":")[1]] if failure.startswith("pull:") else failure
    job = await orchestrator.wait_for_job(await orchestrator.submit_update(TARGET))
    assert job.state == "failed"
    assert job.deployment_verified is False
    assert transport.path.read_bytes() == before
    if failure == "rollback":
        assert "rollback failed" in job.error
        assert list(transport.path.parent.glob("*transaction*"))
    elif failure != "partial":
        assert transport.running == old_runtime
    if failure.startswith("pull:"):
        assert not job.activation_started
        assert not any(argv[0] == "bash" for argv in transport.commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["web", "sandboxd", "runner"])
async def test_inconsistent_pulled_revision_fails_before_deployment_write(transaction, component):
    orchestrator, transport = transaction()
    before = transport.path.read_bytes()
    transport.bad_label = component
    job = await orchestrator.wait_for_job(await orchestrator.submit_update(TARGET))
    assert job.state == "failed"
    assert not job.activation_started
    assert transport.path.read_bytes() == before


@pytest.mark.asyncio
async def test_incomplete_persisted_pair_recovers_rollback_anchor_and_self_heals(transaction):
    orchestrator, transport = transaction()
    transport.path.write_text("\n".join(line for line in transport.path.read_text().splitlines()
                                         if not line.startswith("SAKURA_AGENT_RUNNER_IMAGE_DIGEST=")) + "\n")
    result = await orchestrator.preflight(TARGET)
    assert result["can_update"] is True
    assert "runner.deployment_digest" in result["drift"]
    job = await orchestrator.wait_for_job(await orchestrator.submit_update(TARGET))
    assert job.state == "success", job.error
    assert transport.selected() == NEW == transport.running

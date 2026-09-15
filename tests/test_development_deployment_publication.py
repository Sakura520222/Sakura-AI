"""Publication join gate: no development head can represent unfinished builds."""

import json

import pytest
from sakura_ai_updater.contract import MANIFEST_ANNOTATION, REPOSITORIES
from sakura_ai_updater.registry import RegistryClient, RegistryTargetError

from scripts.publish_development_deployment import publish

REVISION = "a" * 40
TAG = "dev-20260914000000-v3.2.0-" + REVISION
DIGESTS = {"web": "sha256:" + "a" * 64, "sandboxd": "sha256:" + "b" * 64, "runner": "sha256:" + "c" * 64}


class Registry:
    def __init__(self, failure=None):
        self.failure = failure
        self.events = []
        self.published = None
        self.source = {"manifests": [{"digest": "sha256:" + "e" * 64}]}

    def _token_sync(self, repository):
        return "token"

    def _image_reference_sync(self, repository, digest, token, **expected):
        component = next(key for key, value in REPOSITORIES.items() if value == repository)
        self.events.append("verify:" + component)
        assert expected["expected_revision"] == REVISION
        if component == self.failure:
            raise RegistryTargetError("component not published")
        return digest

    def _manifest_response_sync(self, repository, ref, token):
        if ref == DIGESTS["web"]:
            return self.source, {}
        if self.published is None:
            raise RegistryTargetError("manifest absent", status_code=404)
        return self.published, {"docker-content-digest": "sha256:" + "d" * 64}

    _manifest_digest = staticmethod(RegistryClient._manifest_digest)

    def command(self, *argv):
        if argv[:3] == ("docker", "buildx", "imagetools"):
            self.events.append("publish:manifest")
            assert all("verify:" + c in self.events for c in DIGESTS)
            value = argv[argv.index("--annotation") + 1].split("=", 1)[1]
            self.published = {**self.source, "annotations": {MANIFEST_ANNOTATION: value}}
            if self.failure == "manifest":
                self.published["annotations"] = {}
        elif argv[0] == "git":
            return ("b" * 40 if self.failure == "old-head" else REVISION) + "\trefs/heads/develop"
        elif argv[:2] == ("crane", "copy"):
            self.events.append("advance:edge")
            assert self.published is not None
            assert "@sha256:" in argv[2]
        else:
            raise AssertionError(argv)
        return ""


def attempt(registry):
    return publish(tag=TAG, web_digest=DIGESTS["web"], sandboxd_digest=DIGESTS["sandboxd"],
                   runner_digest=DIGESTS["runner"], run=registry.command, client=registry)


@pytest.mark.parametrize("failure", ["web", "sandboxd", "runner", "manifest", "old-head"])
def test_no_head_move_until_all_components_and_manifest_verified(failure):
    registry = Registry(failure)
    with pytest.raises(ValueError):
        attempt(registry)
    assert "advance:edge" not in registry.events
    if failure in DIGESTS:
        assert "publish:manifest" not in registry.events


def test_manifest_publishes_before_head_and_rerun_preserves_immutable_target():
    registry = Registry()
    assert attempt(registry) == "sha256:" + "d" * 64
    assert registry.events == ["verify:web", "verify:sandboxd", "verify:runner", "publish:manifest", "advance:edge"]
    registry.events.clear()
    attempt(registry)
    assert "publish:manifest" not in registry.events
    manifest = json.loads(registry.published["annotations"][MANIFEST_ANNOTATION])
    assert manifest["sandboxd_image"].endswith(DIGESTS["sandboxd"])
    assert manifest["runner_image"].endswith(DIGESTS["runner"])


def test_rerun_cannot_overwrite_a_different_deployment_contract():
    registry = Registry()
    attempt(registry)
    registry.events.clear()
    manifest = json.loads(registry.published["annotations"][MANIFEST_ANNOTATION])
    manifest["runner_image"] = REPOSITORIES["runner"] + "@sha256:" + "f" * 64
    registry.published["annotations"][MANIFEST_ANNOTATION] = json.dumps(manifest)
    with pytest.raises(ValueError, match="different contract"):
        attempt(registry)
    assert "advance:edge" not in registry.events


@pytest.mark.parametrize("mutation", [None, "missing-pair", "revision", "canonical", "digest"])
def test_standalone_bootstrap_consumes_the_same_complete_manifest(monkeypatch, capsys, tmp_path, mutation):
    """Execute the actual embedded bootstrap parser with registry I/O replaced."""
    import hashlib
    import io
    import runpy
    import sys
    from pathlib import Path
    from urllib import request

    manifest = {"schema_version": 1, "channel": "development", "version": "3.2.0",
                "revision": REVISION, "tag": TAG,
                "sandboxd_image": REPOSITORIES["sandboxd"] + "@" + DIGESTS["sandboxd"],
                "runner_image": REPOSITORIES["runner"] + "@" + DIGESTS["runner"]}
    if mutation == "missing-pair":
        del manifest["runner_image"]
    if mutation == "revision":
        manifest["revision"] = "b" * 40
    raw = json.dumps({"annotations": {MANIFEST_ANNOTATION: json.dumps(manifest)}}).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()

    class Response(io.BytesIO):
        headers = {"Docker-Content-Digest": "bad" if mutation == "digest" else digest}

    def urlopen(req, timeout):
        if "/token?" in req.full_url:
            return Response(b'{"token":"test"}')
        if mutation == "canonical" and req.full_url.endswith(TAG):
            return Response(b"{}")
        return Response(raw)

    monkeypatch.setattr(request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", ["bootstrap", "edge"])
    shell = Path("start.sh").read_text()
    source = shell.split("<<'PYMANIFEST'\n", 1)[1].split("\nPYMANIFEST", 1)[0]
    parser = tmp_path / "bootstrap_parser.py"
    parser.write_text(source)
    if mutation:
        with pytest.raises(SystemExit):
            runpy.run_path(str(parser), run_name="__main__")
        assert capsys.readouterr().out == ""
    else:
        runpy.run_path(str(parser), run_name="__main__")
        assert capsys.readouterr().out.splitlines() == [REPOSITORIES["web"] + ":" + TAG + "@" + digest,
            manifest["sandboxd_image"], manifest["runner_image"]]

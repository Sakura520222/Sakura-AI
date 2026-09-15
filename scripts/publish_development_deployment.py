"""Publish one complete development deployment, then advance its channel head.

Run with PYTHONPATH=updater/src. Builds have already pushed all components by
immutable digest. The Web OCI index itself is the deployment manifest, so its
atomic registry tag update also commits the sandbox pair. Requires Buildx and
crane authenticated to GHCR. Never builds or retags individual components.
"""

import json
import os
import subprocess

from sakura_ai_updater.contract import (
    MANIFEST_ANNOTATION,
    REPOSITORIES,
    parse_deployment_manifest,
)
from sakura_ai_updater.registry import (
    RegistryClient,
    RegistryTargetError,
    parse_development_target,
)


def command(*argv):
    return subprocess.check_output(argv, text=True).strip()


def publish(*, tag, web_digest, sandboxd_digest, runner_digest, run=command, client=None):
    client = client or RegistryClient()
    # The exact canonical tag validates version, revision and every digest.
    version, revision = tag.split("-v", 1)[1].split("-", 1)
    target = parse_development_target({
        "channel": "development", "tag": tag, "version": version,
        "revision": revision, "digest": web_digest,
    })
    manifest = {
        "schema_version": 1, "channel": target.channel, "version": version,
        "revision": revision, "tag": tag,
        "sandboxd_image": REPOSITORIES["sandboxd"] + "@" + sandboxd_digest,
        "runner_image": REPOSITORIES["runner"] + "@" + runner_digest,
    }
    annotation = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    parse_deployment_manifest({"annotations": {MANIFEST_ANNOTATION: annotation}},
                              channel=target.channel, version=version, revision=revision)
    # Missing image, wrong component/revision or any platform failure prevents
    # both manifest publication and the head move.
    for component, digest in (("web", web_digest), ("sandboxd", sandboxd_digest), ("runner", runner_digest)):
        repository = REPOSITORIES[component]
        token = client._token_sync(repository)
        actual = client._image_reference_sync(
            repository, digest, token, expected_revision=revision,
            expected_component="agent-runner" if component == "runner" else component,
            expected_channel=target.channel, expected_version=version,
        )
        if actual != digest:
            raise ValueError("published component digest mismatch")
    repository = REPOSITORIES["web"]
    token = client._token_sync(repository)
    source, _ = client._manifest_response_sync(repository, web_digest, token)
    try:
        existing, _ = client._manifest_response_sync(repository, tag, token)
    except RegistryTargetError as exc:
        if exc.status_code != 404:
            raise
        run("docker", "buildx", "imagetools", "create", "--annotation",
            "index:" + MANIFEST_ANNOTATION + "=" + annotation,
            "--tag", repository + ":" + tag, repository + "@" + web_digest)
    else:
        existing_contract = parse_deployment_manifest(existing, channel=target.channel, version=version, revision=revision)
        if existing_contract != manifest or existing.get("manifests") != source.get("manifests"):
            raise ValueError("immutable deployment tag already exists with a different contract")
    published, headers = client._manifest_response_sync(repository, tag, token)
    if (parse_deployment_manifest(published, channel=target.channel, version=version, revision=revision) != manifest
        or published.get("manifests") != source.get("manifests")):
        raise ValueError("published deployment verification failed")
    digest = client._manifest_digest(headers)
    if run("git", "ls-remote", "origin", "refs/heads/develop").split()[0] != revision:
        raise ValueError("source is no longer develop head; refusing channel rollback")
    run("crane", "copy", repository + "@" + digest, repository + ":edge")
    return digest


if __name__ == "__main__":
    print(publish(tag=os.environ["DEPLOYMENT_TAG"], web_digest=os.environ["WEB_DIGEST"],
                  sandboxd_digest=os.environ["SANDBOXD_DIGEST"], runner_digest=os.environ["RUNNER_DIGEST"]))

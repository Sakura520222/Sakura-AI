import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import {
  deploymentMatches,
  targetForImage,
  transientUpdateError,
  updateRequest,
  type ImageEntry,
  type UpdateJob,
} from "./state";

const image: ImageEntry = {
  channel: "development",
  canonical_tag: "dev-20261030-abcdef",
  version: "3.2.6",
  revision: "a".repeat(40),
  digest: `sha256:${"b".repeat(64)}`,
  selectable: true,
  tags: ["edge", "dev-20261030-abcdef"],
};
const job: UpdateJob = {
  job_id: "fixture",
  state: "success",
  deployment_verified: true,
  target_version: "3.2.6",
  target_channel: "development",
  target_revision: image.revision,
  target_digest: image.digest,
};
const health = {
  version: "3.2.6",
  build: {
    channel: "development" as const,
    revision: image.revision,
    digest: image.digest,
  },
};

describe("immutable updater targets and verification", () => {
  it("preserves legacy version-only updates without accepting partial channel identities", () => {
    expect(
      updateRequest({ version: "3.2.6", image: "legacy-image" }, false),
    ).toEqual({ target_version: "3.2.6" });
    expect(() =>
      updateRequest({ version: "3.2.6", channel: "development" }, true),
    ).toThrow("incomplete_target");
  });
  it("uses the canonical tag and does not turn an alias into an update target", () => {
    expect(targetForImage(image)).toEqual({
      channel: "development",
      version: "3.2.6",
      tag: image.canonical_tag,
      revision: image.revision,
      digest: image.digest,
    });
    expect(targetForImage({ ...image, selectable: false })).toBeNull();
    expect(targetForImage({ ...image, revision: undefined })).toBeNull();
    expect(targetForImage({ ...image, canonical_tag: "" })).toBeNull();
  });
  it("requires both verified deployment and matching running identity", () => {
    expect(deploymentMatches(job, health)).toBe(true);
    expect(
      deploymentMatches({ ...job, deployment_verified: false }, health),
    ).toBe(false);
    expect(deploymentMatches({ ...job, state: "rolled_back" }, health)).toBe(
      false,
    );
    expect(
      deploymentMatches(job, {
        ...health,
        build: { ...health.build, digest: `sha256:${"c".repeat(64)}` },
      }),
    ).toBe(false);
    expect(
      deploymentMatches(job, {
        ...health,
        build: { ...health.build, revision: "c".repeat(40) },
      }),
    ).toBe(false);
  });
  it("retries transport interruptions without hiding permanent updater failures", () => {
    expect(transientUpdateError(new TypeError("fetch failed"))).toBe(true);
    expect(transientUpdateError(new ApiError(503, null))).toBe(true);
    expect(
      transientUpdateError(
        new ApiError(503, { error: "registry_unavailable" }),
      ),
    ).toBe(false);
    expect(
      transientUpdateError(new ApiError(404, { error: "job_not_found" })),
    ).toBe(false);
    expect(transientUpdateError(new ApiError(401, {}))).toBe(false);
  });
});

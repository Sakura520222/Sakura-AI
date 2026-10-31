import { ApiError } from "../../api/client";

export interface Target {
  channel: "stable" | "development";
  version: string;
  tag: string;
  digest: string;
  revision?: string;
}
export interface ImageEntry extends Partial<Target> {
  canonical_tag: string;
  tags: string[];
  selectable: boolean;
  target?: Target;
  reason?: string;
}
export interface Catalog {
  stale: boolean;
  stale_reason?: string;
  images: ImageEntry[];
  heads: Partial<Record<Target["channel"], Target>>;
}
export interface Check {
  name: string;
  passed: boolean;
  detail?: string;
}
export interface Readiness {
  can_update?: boolean;
  update_ready?: boolean;
  update_available?: boolean;
  target?: Partial<Target> & { version: string; image?: string };
  checks?: Check[];
  readiness?: Record<string, boolean> | Check[];
}
export interface VersionInfo {
  current_version: string;
  build: Partial<Target>;
  deployment_type: string;
  update_supported: boolean;
  update_unsupported_reason: string | null;
  update_available: boolean | null;
  latest_version: string | null;
  updater_connected: boolean;
  updater_version: string | null;
  updater_compatibility?: { compatible: boolean; reason?: string };
  has_active_job: boolean;
  active_job_id: string | null;
  last_checked: string | null;
}
export interface UpdateJob {
  job_id: string;
  state: string;
  step?: string;
  deployment_verified?: boolean;
  target_version?: string;
  target_channel?: Target["channel"];
  target_digest?: string;
  target_revision?: string;
  error_code?: string;
  progress?: number;
}
export interface Envelope<T> {
  data: T;
}
export function unwrap<T>(value: T | Envelope<T>): T {
  return value && typeof value === "object" && "data" in value
    ? value.data
    : (value as T);
}

export function targetForImage(image: ImageEntry): Target | null {
  if (
    !image.selectable ||
    !["stable", "development"].includes(image.channel ?? "") ||
    !image.version ||
    !image.canonical_tag ||
    !image.digest
  )
    return null;
  if (image.channel === "development" && !image.revision) return null;
  return {
    channel: image.channel as Target["channel"],
    version: image.version,
    tag: image.canonical_tag,
    digest: image.digest,
    ...(image.revision ? { revision: image.revision } : {}),
  };
}

export function updateRequest(
  target: NonNullable<Readiness["target"]>,
  confirmed: boolean,
) {
  if (!target.channel) return { target_version: target.version };
  if (
    !["stable", "development"].includes(target.channel) ||
    !target.tag ||
    !target.digest ||
    (target.channel === "development" && !target.revision)
  )
    throw new Error("incomplete_target");
  return { target, confirm_channel_switch: confirmed };
}

export function jobTarget(job: UpdateJob): Partial<Target> {
  return {
    version: job.target_version,
    channel: job.target_channel,
    digest: job.target_digest,
    revision: job.target_revision,
  };
}

export function deploymentMatches(
  job: UpdateJob,
  health: { version?: string; build?: Partial<Target> },
): boolean {
  if (
    !["success", "complete"].includes(job.state) ||
    job.deployment_verified !== true ||
    !job.target_version ||
    job.target_version !== health.version
  )
    return false;
  if (job.target_channel && job.target_channel !== health.build?.channel)
    return false;
  if (job.target_digest && job.target_digest !== health.build?.digest)
    return false;
  if (
    job.target_channel === "development" &&
    (!job.target_revision || job.target_revision !== health.build?.revision)
  )
    return false;
  return true;
}

export function transientUpdateError(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  if (
    !(error instanceof ApiError) ||
    ![500, 502, 503, 504].includes(error.status)
  )
    return false;
  const code =
    error.body && typeof error.body === "object" && "error" in error.body
      ? error.body.error
      : null;
  return ![
    "job_not_found",
    "protocol_error",
    "updater_protocol_error",
    "updater_not_ready",
    "registry_unavailable",
    "release_unavailable",
    "channel_head_unavailable",
    "stale_catalog",
  ].includes(String(code));
}

export function readinessChecks(value: Readiness): Check[] {
  if (Array.isArray(value.checks)) return value.checks;
  if (Array.isArray(value.readiness)) return value.readiness;
  return Object.entries(value.readiness ?? {}).map(([name, passed]) => ({
    name,
    passed,
  }));
}

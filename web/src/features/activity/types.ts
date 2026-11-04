export interface ActivitySession {
  session_id: number;
  session_kind: string;
  resource_type: string | null;
  resource_number: number | null;
  repo_full_name: string | null;
  status: string;
  last_active_at: string | null;
  created_at: string | null;
}
export interface Artifact {
  artifact_id: number;
  artifact_kind: string;
  can_request: boolean;
  capture_error?: string | null;
}
export interface Entry {
  id: string;
  type: string;
  order_key: string;
  created_at: string | null;
  role?: string;
  source_role?: string;
  thread_id?: number | null;
  invocation_id?: number | null;
  content?: string | null;
  content_visibility?: string;
  current_context?: boolean;
  name?: string;
  status?: string;
  attempt?: Record<string, unknown>;
  artifacts?: Artifact[];
  operation_type?: string;
  trigger_reason?: string;
  before_revision_id?: number;
  after_revision_id?: number;
  thinking_mode?: string;
  effort?: string;
  tokens?: number | null;
  message_kind?: string;
}
export interface Conversation {
  entries: Entry[];
  before_cursor: string | null;
  events_cursor: string;
  projection_version: number;
  session_id: number;
  last_scanned_sequence: number;
}
export interface Updates {
  entries: Entry[];
  cursor: string;
  last_scanned_sequence: number;
  projection_version: number;
}
export interface Snapshot {
  session: ActivitySession;
  current_phase: string | null;
  usage_totals?: Record<string, number | null>;
  invocations?: Record<string, unknown>[];
  context_operations?: Record<string, unknown>[];
}

export function mergeEntries(existing: Entry[], incoming: Entry[]): Entry[] {
  const entries = new Map(existing.map((entry) => [entry.id, entry]));
  incoming.forEach((entry) => entries.set(entry.id, entry));
  return [...entries.values()].sort((a, b) => {
    const left = a.order_key.split(":").map(BigInt),
      right = b.order_key.split(":").map(BigInt);
    for (let index = 0; index < Math.min(left.length, right.length); index++) {
      if (left[index] !== right[index])
        return left[index] < right[index] ? -1 : 1;
    }
    return a.id.localeCompare(b.id);
  });
}

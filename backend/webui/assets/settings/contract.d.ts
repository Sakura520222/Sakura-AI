/** Stable projection shared with future WebUI 2.0 Settings consumers. */
export type Json =
  null | boolean | number | string | Json[] | { [key: string]: Json };
export interface ConfigField {
  id: string;
  key: string;
  path: string[];
  category:
    | "personal"
    | "ai"
    | "review"
    | "issue"
    | "agent"
    | "labels"
    | "system"
    | "backup";
  group: string;
  type: "boolean" | "integer" | "number" | "string" | "array" | "object";
  label: string;
  description: string;
  /** Presentation-only JSON schema for an existing atomic value. */
  editorSchema: Record<string, unknown> | null;
  optionLabels: Record<string, string>;
  scope: "global" | "user";
  permission: "super_admin" | "authenticated";
  sensitive: boolean;
  writable: boolean;
  resettable: boolean;
  requiresRestart: boolean;
  advanced: boolean;
  constraints: {
    enum?: Json[];
    ge?: number;
    gt?: number;
    le?: number;
    lt?: number;
    min_length?: number;
    max_length?: number;
  };
  value: Json;
  default: Json;
  inheritedValue: Json;
  source: "builtin" | "environment" | "deployment" | "database" | "user";
  version: string;
  hasValue: boolean;
  overridden: boolean;
  effect: "live" | "restart_required" | "pending";
  submission: "automatic" | "confirmed";
}
export interface FieldMutation {
  field: string;
  version: string;
  value?: Json;
  confirmed?: boolean;
}
export interface MutationResult {
  field: ConfigField & { persisted: true };
}
export interface ConfigFailure {
  error: { code: string; field: string; detail: string };
}

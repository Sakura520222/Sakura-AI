import type {
  ConfigField as Descriptor,
  Json,
} from "../../api/configuration";

export type { Json };
export type Category = Descriptor["category"];
export type ConfigField = Descriptor & { searchText?: string };
export interface SettingsResponse {
  fields: ConfigField[];
  messages: Record<string, string>;
  groups: Record<string, string>;
  uiMessages?: Record<string, string>;
  syncIntervalMs: number;
  csrf: string;
}
export interface Account {
  id: string;
  name: string;
  provider_id: string;
  enabled: boolean;
  default_model: string;
  models: string[];
}
export interface Schema {
  $ref?: string;
  $defs?: Record<string, Schema>;
  type?: string;
  default?: Json;
  const?: Json;
  enum?: Json[];
  anyOf?: Schema[];
  properties?: Record<string, Schema>;
  required?: string[];
  additionalProperties?: boolean | Schema;
  items?: Schema;
  minItems?: number;
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  pattern?: string;
  format?: string;
  "x-fixedKeys"?: string[];
  "x-keyLabel"?: string;
}

import { useId, useState } from "react";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import type { Account, ConfigField, Json, Schema } from "./types";
import { initialValue } from "./state";

type Translate = (key: string) => string;
interface Props {
  value: Json;
  schema: Schema;
  label: string;
  onChange: (value: Json) => void;
  message: Translate;
  disabled?: boolean;
}

export function StructuredEditor({
  value,
  schema,
  label,
  onChange,
  message,
  disabled = false,
}: Props) {
  const id = useId();
  const [newKey, setNewKey] = useState("");
  const [newType, setNewType] = useState("string");
  const [keyError, setKeyError] = useState(false);
  if ("const" in schema)
    return <output aria-label={label}>{String(schema.const)}</output>;
  const choices = schema.anyOf;
  if (choices?.some((item) => item.type === "null")) {
    const concrete = choices.find((item) => item.type !== "null") ?? {};
    return (
      <div className="settings-structured">
        <label className="settings-checkbox">
          <input
            type="checkbox"
            checked={value !== null}
            disabled={disabled}
            onChange={(event) =>
              onChange(event.target.checked ? initialValue(concrete) : null)
            }
          />
          {message("editor_type_object")}
        </label>
        {value !== null && (
          <StructuredEditor
            {...{ value, label, onChange, message, disabled }}
            schema={concrete}
          />
        )}
      </div>
    );
  }
  if (schema.enum)
    return (
      <select
        id={id}
        aria-label={label}
        value={JSON.stringify(value)}
        disabled={disabled}
        onChange={(event) => onChange(JSON.parse(event.target.value) as Json)}
      >
        {schema.enum.map((option) => (
          <option key={JSON.stringify(option)} value={JSON.stringify(option)}>
            {message(`option.${String(option)}`) === `option.${String(option)}`
              ? String(option)
              : message(`option.${String(option)}`)}
          </option>
        ))}
      </select>
    );
  const kind =
    schema.type ??
    (Array.isArray(value) ? "array" : value === null ? "string" : typeof value);
  if (kind === "array") {
    const items = Array.isArray(value) ? value : [];
    const move = (index: number, direction: number) => {
      const next = [...items];
      [next[index], next[index + direction]] = [
        next[index + direction],
        next[index],
      ];
      onChange(next);
    };
    return (
      <div className="settings-structured" role="group" aria-label={label}>
        {!items.length && (
          <p className="settings-hint">{message("editor_empty")}</p>
        )}
        {items.map((item, index) => (
          <div className="settings-structured-item" key={index}>
            <div className="settings-structured-heading">
              <span>
                {label} {index + 1}
              </span>
              <div className="settings-icon-actions">
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`${message("editor_move_up")}: ${index + 1}`}
                  disabled={disabled || index === 0}
                  onClick={() => move(index, -1)}
                >
                  <ArrowUp size={16} />
                </button>
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`${message("editor_move_down")}: ${index + 1}`}
                  disabled={disabled || index === items.length - 1}
                  onClick={() => move(index, 1)}
                >
                  <ArrowDown size={16} />
                </button>
                <button
                  type="button"
                  className="icon-button"
                  aria-label={`${message("editor_remove")}: ${index + 1}`}
                  disabled={disabled}
                  onClick={() => onChange(items.filter((_, i) => i !== index))}
                >
                  <Trash2 size={16} />
                </button>
              </div>
            </div>
            <StructuredEditor
              value={item}
              schema={schema.items ?? {}}
              label={`${label} ${index + 1}`}
              message={message}
              disabled={disabled}
              onChange={(child) =>
                onChange(items.map((old, i) => (i === index ? child : old)))
              }
            />
          </div>
        ))}
        <button
          type="button"
          className="button button-secondary"
          disabled={disabled}
          onClick={() =>
            onChange([
              ...items,
              initialValue(schema.items ?? { type: "string" }),
            ])
          }
        >
          <Plus size={16} />
          {message("editor_add")}
        </button>
      </div>
    );
  }
  if (kind === "object") {
    const object: Record<string, Json> =
      value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const properties = schema.properties ?? {};
    const optional = Object.keys(properties).filter((key) => !(key in object));
    const permitsNew = schema.additionalProperties !== false;
    const childLabel = (key: string) => {
      const translated = message(`field.${key}`);
      return translated === `field.${key}` ? key : translated;
    };
    const addKey = () => {
      const key = newKey.trim();
      if (
        !key ||
        key in object ||
        key === "__proto__" ||
        key === "constructor" ||
        key === "prototype"
      ) {
        setKeyError(true);
        return;
      }
      const childSchema =
        properties[key] ??
        (typeof schema.additionalProperties === "object"
          ? schema.additionalProperties
          : { type: newType });
      onChange({ ...object, [key]: initialValue(childSchema) });
      setNewKey("");
      setKeyError(false);
    };
    return (
      <div className="settings-structured" role="group" aria-label={label}>
        {Object.entries(object).map(([key, item]) => {
          const childSchema =
            properties[key] ??
            (typeof schema.additionalProperties === "object"
              ? schema.additionalProperties
              : {});
          const removable =
            !schema.required?.includes(key) &&
            !schema["x-fixedKeys"]?.includes(key);
          return (
            <div className="settings-property" key={key}>
              <div className="settings-structured-heading">
                <span>{childLabel(key)}</span>
                {removable && (
                  <button
                    type="button"
                    className="icon-button"
                    disabled={disabled}
                    aria-label={`${message("editor_remove")}: ${childLabel(key)}`}
                    onClick={() => {
                      const next = { ...object };
                      delete next[key];
                      onChange(next);
                    }}
                  >
                    <Trash2 size={15} />
                  </button>
                )}
              </div>
              <StructuredEditor
                value={item}
                schema={childSchema}
                label={`${label}: ${childLabel(key)}`}
                message={message}
                disabled={disabled}
                onChange={(child) => onChange({ ...object, [key]: child })}
              />
            </div>
          );
        })}
        {!!optional.length && (
          <select
            aria-label={message("editor_add")}
            value=""
            disabled={disabled}
            onChange={(event) => {
              if (event.target.value)
                onChange({
                  ...object,
                  [event.target.value]: initialValue(
                    properties[event.target.value],
                  ),
                });
            }}
          >
            <option value="">{message("editor_add")}</option>
            {optional.map((key) => (
              <option key={key} value={key}>
                {childLabel(key)}
              </option>
            ))}
          </select>
        )}
        {permitsNew && (
          <div className="settings-add-row">
            <input
              value={newKey}
              disabled={disabled}
              aria-label={`${message(schema["x-keyLabel"] ?? "editor_key")}: ${label}`}
              placeholder={message(schema["x-keyLabel"] ?? "editor_key")}
              aria-invalid={keyError}
              onChange={(event) => {
                setNewKey(event.target.value);
                setKeyError(false);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  addKey();
                }
              }}
            />
            {typeof schema.additionalProperties !== "object" && (
              <select
                aria-label={message("editor_value_type")}
                value={newType}
                disabled={disabled}
                onChange={(event) => setNewType(event.target.value)}
              >
                {["string", "number", "boolean", "array", "object"].map(
                  (type) => (
                    <option key={type} value={type}>
                      {message(`editor_type_${type}`)}
                    </option>
                  ),
                )}
              </select>
            )}
            <button
              type="button"
              className="button button-secondary"
              aria-label={`${message("editor_add")}: ${label}`}
              disabled={disabled || !newKey.trim()}
              onClick={addKey}
            >
              <Plus size={16} />
              {message("editor_add")}
            </button>
          </div>
        )}
        {keyError && (
          <p role="alert" className="settings-error">
            {message("editor_duplicate_key")}
          </p>
        )}
      </div>
    );
  }
  if (kind === "boolean")
    return (
      <input
        type="checkbox"
        role="switch"
        aria-label={label}
        disabled={disabled}
        checked={value === true}
        onChange={(event) => onChange(event.target.checked)}
      />
    );
  const multiline =
    schema.format === "multiline" ||
    (typeof value === "string" && (value.includes("\n") || value.length > 160));
  if (multiline)
    return (
      <textarea
        aria-label={label}
        rows={5}
        disabled={disabled}
        value={typeof value === "string" ? value : ""}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  const numeric = kind === "integer" || kind === "number";
  return (
    <input
      aria-label={label}
      type={
        numeric ? "number" : schema.format === "password" ? "password" : "text"
      }
      autoComplete={schema.format === "password" ? "new-password" : undefined}
      disabled={disabled}
      value={
        typeof value === "string" || typeof value === "number" ? value : ""
      }
      step={kind === "integer" ? 1 : "any"}
      min={schema.minimum}
      max={schema.maximum}
      minLength={schema.minLength}
      maxLength={schema.maxLength}
      pattern={schema.pattern}
      onChange={(event) =>
        onChange(
          numeric && event.target.value !== ""
            ? Number(event.target.value)
            : event.target.value,
        )
      }
    />
  );
}

interface Assignment {
  account?: string;
  provider?: string;
  model?: string;
}
interface Binding {
  primary: Assignment;
  fallback: Assignment[];
}
export function BindingEditor({
  field,
  value,
  accounts,
  onChange,
  message,
  disabled,
}: {
  field: ConfigField;
  value: Json;
  accounts: Account[];
  onChange: (value: Json) => void;
  message: Translate;
  disabled: boolean;
}) {
  const id = useId();
  const binding = (value ?? {
    primary: {},
    fallback: [],
  }) as unknown as Binding;
  const update = (next: Binding) => onChange(next as unknown as Json);
  const assignment = (entry: Assignment, index: number) => {
    const accountId = entry.account ?? entry.provider ?? "";
    const account = accounts.find((item) => item.id === accountId);
    const set = (next: Assignment) =>
      update(
        index === -1
          ? { ...binding, primary: next }
          : {
              ...binding,
              fallback: binding.fallback.map((item, i) =>
                i === index ? next : item,
              ),
            },
      );
    return (
      <div className="settings-assignment">
        <label>
          {message("account")}
          <select
            aria-label={`${message("account")}: ${index === -1 ? message("primary_assignment") : index + 1}`}
            value={accountId}
            disabled={disabled}
            onChange={(event) =>
              set({
                account: event.target.value,
                model:
                  event.target.value === "main"
                    ? "follow"
                    : (accounts.find((item) => item.id === event.target.value)
                        ?.default_model ?? ""),
              })
            }
          >
            <option value="">{message("select_account")}</option>
            {field.id !== "binding/main" && (
              <option value="main">{message("follow_main")}</option>
            )}
            {accounts.map((item) => (
              <option key={item.id} value={item.id} disabled={!item.enabled}>
                {item.name}
                {!item.enabled ? ` · ${message("account_disabled")}` : ""}
              </option>
            ))}
            {accountId && accountId !== "main" && !account && (
              <option value={accountId} disabled>
                {accountId} · {message("account_unavailable")}
              </option>
            )}
          </select>
        </label>
        <label>
          {message("model_id")}
          <input
            aria-label={`${message("model_id")}: ${index === -1 ? message("primary_assignment") : index + 1}`}
            value={
              accountId === "main"
                ? message("follow_main")
                : (entry.model ?? "")
            }
            disabled={disabled || accountId === "main"}
            list={`${id}-${index}`}
            onChange={(event) =>
              set({ ...entry, account: accountId, model: event.target.value })
            }
          />
          <datalist id={`${id}-${index}`}>
            {[...new Set([account?.default_model, ...(account?.models ?? [])])]
              .filter(Boolean)
              .map((model) => (
                <option key={model} value={model} />
              ))}
          </datalist>
        </label>
      </div>
    );
  };
  return (
    <div className="settings-structured" role="group" aria-label={field.label}>
      <h4>{message("primary_assignment")}</h4>
      {assignment(binding.primary ?? {}, -1)}
      <h4>{message("fallback")}</h4>
      <p className="settings-hint">{message("fallback_help")}</p>
      {(binding.fallback ?? []).map((entry, index) => (
        <div className="settings-structured-item" key={index}>
          <div className="settings-structured-heading">
            <span>
              {message("fallback_item")} {index + 1}
            </span>
            <div className="settings-icon-actions">
              {[-1, 1].map((direction) => (
                <button
                  type="button"
                  className="icon-button"
                  key={direction}
                  disabled={
                    disabled ||
                    index + direction < 0 ||
                    index + direction >= binding.fallback.length
                  }
                  aria-label={`${message(direction === -1 ? "editor_move_up" : "editor_move_down")}: ${index + 1}`}
                  onClick={() => {
                    const next = [...binding.fallback];
                    [next[index], next[index + direction]] = [
                      next[index + direction],
                      next[index],
                    ];
                    update({ ...binding, fallback: next });
                  }}
                >
                  {direction === -1 ? (
                    <ArrowUp size={16} />
                  ) : (
                    <ArrowDown size={16} />
                  )}
                </button>
              ))}
              <button
                type="button"
                className="icon-button"
                disabled={disabled}
                aria-label={`${message("remove_fallback")}: ${index + 1}`}
                onClick={() =>
                  update({
                    ...binding,
                    fallback: binding.fallback.filter((_, i) => i !== index),
                  })
                }
              >
                <Trash2 size={16} />
              </button>
            </div>
          </div>
          {assignment(entry, index)}
        </div>
      ))}
      <button
        type="button"
        className="button button-secondary"
        disabled={disabled}
        onClick={() =>
          update({
            ...binding,
            fallback: [...(binding.fallback ?? []), { account: "", model: "" }],
          })
        }
      >
        <Plus size={16} />
        {message("add_fallback")}
      </button>
    </div>
  );
}

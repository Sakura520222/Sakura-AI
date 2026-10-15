/** Small typed form editor shared by Settings and the dedicated plugin page. */
let editorSequence = 0;
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
};
const kind = (value) =>
  value === null
    ? "null"
    : Array.isArray(value)
      ? "array"
      : typeof value === "number"
        ? "number"
        : typeof value;
const copy = (value) => structuredClone(value);
const invalid = (code = "editor_incomplete", input) => {
  input?.setAttribute("aria-invalid", "true");
  const error = new Error(code);
  error.input = input;
  throw error;
};

export function createValueEditor({
  value,
  schema = {},
  t,
  label = "",
  disabled = false,
}) {
  const element = node("div", null, "settings-value-editor");
  const prefix = `value-editor-${editorSequence++}`;
  element.setAttribute("role", "group");
  element.setAttribute("aria-label", label);
  const definitions = schema.$defs || {};
  let sequence = 0;
  const resolve = (spec) =>
    spec?.$ref
      ? {
          ...definitions[spec.$ref.split("/").at(-1)],
          ...spec,
          $ref: undefined,
        }
      : spec && typeof spec === "object"
        ? spec
        : {};
  const text = (key) => {
    const translated = t(`field.${key}`);
    return translated === `field.${key}` ? key : translated;
  };
  const optionText = (value) => {
    const key = `option.${value}`,
      translated = t(key);
    return translated === key ? String(value) : translated;
  };
  const emit = () =>
    element.dispatchEvent(new Event("input", { bubbles: true }));
  const action = (key, run, name = "") => {
    const button = node("button", t(key), "settings-editor-button");
    button.type = "button";
    button.disabled = disabled;
    if (name) button.setAttribute("aria-label", `${t(key)}: ${name}`);
    button.addEventListener("click", () => {
      run();
      emit();
    });
    return button;
  };
  const initial = (raw) => {
    const spec = resolve(raw);
    if ("default" in spec) return copy(spec.default);
    if ("const" in spec) return spec.const;
    if (spec.enum) return spec.enum[0];
    if (spec.anyOf)
      return initial(
        spec.anyOf.find((s) => s.type !== "null") || spec.anyOf[0],
      );
    if (spec.type === "object")
      return Object.fromEntries(
        Object.entries(spec.properties || {})
          .filter(
            ([key, property]) =>
              spec.required?.includes(key) || "default" in resolve(property),
          )
          .map(([key, property]) => [key, initial(property)]),
      );
    return (
      {
        array: [],
        boolean: false,
        integer: spec.minimum || 0,
        number: spec.minimum || 0,
        null: null,
      }[spec.type] ?? ""
    );
  };

  function build(current, raw, path, name) {
    let spec = resolve(raw);
    const container = node("div", null, "settings-editor-value");
    container.dataset.editorPath = path;
    if (spec.description)
      container.append(node("p", spec.description, "settings-editor-hint"));
    const choices =
      spec.anyOf ||
      (!spec.type && !spec.enum && !("const" in spec)
        ? ["string", "number", "boolean", "array", "object", "null"].map(
            (type) => ({ type }),
          )
        : null);
    if (choices) {
      const select = node("select"),
        body = node("div");
      select.setAttribute("aria-label", `${name}: ${t("editor_value_type")}`);
      select.disabled = disabled;
      choices.forEach((choice, index) => {
        const option = node(
          "option",
          resolve(choice)["x-label"] ||
            t(`editor_type_${resolve(choice).type || "object"}`),
        );
        option.value = String(index);
        select.append(option);
      });
      const selected = Math.max(
        0,
        choices.findIndex(
          (s) =>
            resolve(s).type === kind(current) ||
            (resolve(s).type === "integer" && Number.isInteger(current)),
        ),
      );
      select.value = String(selected);
      let child = build(current, choices[selected], path, name);
      body.append(child.element);
      let active = selected;
      const choose = () => {
        if (active === Number(select.value)) return;
        active = Number(select.value);
        child = build(
          initial(choices[Number(select.value)]),
          choices[Number(select.value)],
          path,
          name,
        );
        body.replaceChildren(child.element);
      };
      select.addEventListener("input", choose);
      select.addEventListener("change", choose);
      container.append(select, body);
      return { element: container, read: () => child.read() };
    }
    const type = spec.type || kind(current);
    if (type === "null") {
      container.append(
        node("span", t("editor_not_set"), "settings-editor-hint"),
      );
      return { element: container, read: () => null };
    }
    if (type === "array") {
      const list = node("div", null, "settings-editor-list");
      const rows = [];
      const items = resolve(spec.items || {});
      const update = () => {
        rows.forEach((row, index) => {
          row.up.disabled = disabled || index === 0;
          row.down.disabled = disabled || index === rows.length - 1;
        });
        empty.hidden = rows.length > 0;
      };
      const add = (entry, opened = false) => {
        let pending = opened && entry === "";
        const object = kind(entry) === "object" || items.type === "object";
        const rowElement = node(
          object ? "details" : "div",
          null,
          "settings-editor-row",
        );
        if (object) {
          rowElement.open = opened;
          const summary = node(
            "summary",
            entry?.name ||
              entry?.id ||
              `${t("editor_item")} ${rows.length + 1}`,
          );
          rowElement.append(summary);
          rowElement.addEventListener("input", () => {
            try {
              const v = child.read();
              summary.textContent =
                v.name ||
                v.id ||
                `${t("editor_item")} ${rows.indexOf(row) + 1}`;
            } catch {
              /* Keep the draft title. */
            }
          });
        }
        const child = build(entry, items, `${path}/${rows.length}`, name);
        const actions = node("div", null, "settings-editor-row-actions");
        const move = (delta) => {
          const index = rows.indexOf(row),
            other = rows[index + delta];
          if (!other) return;
          [rows[index], rows[index + delta]] = [other, row];
          if (delta < 0) list.insertBefore(rowElement, other.element);
          else list.insertBefore(other.element, rowElement);
          update();
        };
        const row = {
          element: rowElement,
          child: {
            ...child,
            read: () => {
              const value = child.read();
              if (pending && !value.trim()) invalid();
              pending = false;
              return value;
            },
          },
          up: action("editor_move_up", () => move(-1), name),
          down: action("editor_move_down", () => move(1), name),
        };
        actions.append(
          row.up,
          row.down,
          action(
            "editor_remove",
            () => {
              const index = rows.indexOf(row);
              rows.splice(index, 1);
              rowElement.remove();
              update();
              (
                rows[index]?.element.querySelector(
                  "input,select,textarea,button",
                ) || addButton
              ).focus();
            },
            name,
          ),
        );
        rowElement.append(child.element, actions);
        rows.push(row);
        list.append(rowElement);
        update();
      };
      const empty = node("p", t("editor_empty"), "settings-editor-hint");
      const addButton = action(
        "editor_add",
        () => {
          add(initial(items), true);
          rows.at(-1).element.querySelector("input,select,textarea")?.focus();
        },
        name,
      );
      for (const entry of current || []) add(entry);
      container.append(empty, list, addButton);
      update();
      return {
        element: container,
        read: () => {
          if (spec.minItems != null && rows.length < spec.minItems) invalid();
          return rows.map((row) => row.child.read());
        },
      };
    }
    if (type === "object") {
      const properties = spec.properties || {},
        rows = [],
        list = node("div", null, "settings-editor-object");
      const add = (key, entry, property, dynamic = false, opened = false) => {
        const rowElement = node(
          dynamic ? "details" : "div",
          null,
          "settings-editor-property",
        );
        const keyInput = dynamic ? node("input") : null;
        let summary;
        if (dynamic) {
          rowElement.open = opened;
          summary = node("summary", key || t("editor_new_entry"));
          rowElement.append(summary);
          const keyLabel = node("label", t(spec["x-keyLabel"] || "editor_key"));
          keyInput.value = key;
          keyInput.required = true;
          keyInput.disabled = disabled || spec["x-fixedKeys"]?.includes(key);
          keyInput.setAttribute(
            "aria-label",
            `${name}: ${t(spec["x-keyLabel"] || "editor_key")}`,
          );
          keyInput.addEventListener("input", () => {
            summary.textContent = keyInput.value || t("editor_new_entry");
          });
          keyLabel.append(keyInput);
          rowElement.append(keyLabel);
        }
        const label = dynamic ? null : node("label", text(key));
        const child = build(
          entry,
          property,
          `${path}/${key}`,
          dynamic ? key || name : text(key),
        );
        if (label) {
          const input = child.element.matches("input,select,textarea")
            ? child.element
            : child.element.querySelector("input,select,textarea");
          if (input) {
            input.id ||= `${prefix}-${sequence++}`;
            label.htmlFor = input.id;
          }
          rowElement.append(label);
        }
        rowElement.append(child.element);
        const row = { element: rowElement, key, keyInput, child };
        if (
          (dynamic && !spec["x-fixedKeys"]?.includes(key)) ||
          (!dynamic && !spec.required?.includes(key))
        ) {
          rowElement.append(
            action(
              dynamic ? "editor_remove" : "editor_unset",
              () => {
                rows.splice(rows.indexOf(row), 1);
                rowElement.remove();
                updateOptions();
              },
              dynamic ? key || name : text(key),
            ),
          );
        }
        rows.push(row);
        list.append(rowElement);
        return row;
      };
      const optional = node("select");
      optional.setAttribute("aria-label", `${name}: ${t("editor_add_field")}`);
      optional.disabled = disabled;
      const updateOptions = () => {
        optional.replaceChildren();
        for (const key of Object.keys(properties).filter(
          (key) => !rows.some((row) => row.key === key),
        )) {
          const option = node("option", text(key));
          option.value = key;
          optional.append(option);
        }
        optional.hidden = optional.options.length === 0;
        configure.hidden = optional.hidden;
      };
      const configure = action(
        "editor_add_field",
        () => {
          const key = optional.value;
          if (key) add(key, initial(properties[key]), properties[key]);
          updateOptions();
        },
        name,
      );
      for (const [key, entry] of Object.entries(current || {})) {
        add(
          key,
          entry,
          properties[key] || spec.additionalProperties || {},
          !(key in properties),
        );
      }
      for (const key of spec.required || []) {
        if (!rows.some((row) => row.key === key) && properties[key])
          add(key, initial(properties[key]), properties[key]);
      }
      container.append(list);
      if (Object.keys(properties).some((key) => !spec.required?.includes(key)))
        container.append(optional, configure);
      if (spec.additionalProperties !== false) {
        container.append(
          action(
            "editor_add_entry",
            () => {
              const row = add(
                "",
                initial(spec.additionalProperties || {}),
                spec.additionalProperties || {},
                true,
                true,
              );
              row.keyInput.focus();
            },
            name,
          ),
        );
      }
      updateOptions();
      return {
        element: container,
        read: () => {
          const entries = [],
            keys = new Set();
          for (const row of rows) {
            const key = row.keyInput?.value ?? row.key;
            if (!key.trim()) invalid("editor_incomplete", row.keyInput);
            if (keys.has(key)) invalid("editor_duplicate_key", row.keyInput);
            row.keyInput?.setAttribute("aria-invalid", "false");
            keys.add(key);
            entries.push([key, row.child.read()]);
          }
          return Object.fromEntries(entries);
        },
      };
    }
    let input;
    if (spec.enum || "const" in spec) {
      input = node("select");
      for (const value of spec.enum || [spec.const]) {
        const option = node("option", optionText(value));
        option.value = String(value);
        input.append(option);
      }
      input.value = String(current);
    } else if (type === "boolean") {
      input = node("input");
      input.type = "checkbox";
      input.checked = Boolean(current);
    } else {
      input = node(
        spec.format === "multiline" ||
          (typeof current === "string" &&
            (current.includes("\n") || current.length > 160))
          ? "textarea"
          : "input",
      );
      if (input.tagName === "INPUT")
        input.type =
          spec.format === "password"
            ? "password"
            : ["number", "integer"].includes(type)
              ? "number"
              : "text";
      input.value = current ?? "";
      if (["number", "integer"].includes(type)) {
        input.step = type === "integer" ? "1" : "any";
        input.required = true;
        if (spec.minimum != null) input.min = spec.minimum;
        if (spec.maximum != null) input.max = spec.maximum;
      }
      if (spec.minLength != null) input.minLength = spec.minLength;
      if (spec.maxLength != null) input.maxLength = spec.maxLength;
      if (spec.format === "password") input.autocomplete = "new-password";
    }
    input.disabled = disabled || "const" in spec;
    input.setAttribute("aria-label", name);
    input.dataset.editorPath = path;
    return {
      element: input,
      read: () => {
        if (
          !input.checkValidity() ||
          (spec.pattern && !new RegExp(spec.pattern).test(input.value)) ||
          (spec.minLength && input.value.length < spec.minLength)
        )
          invalid("editor_incomplete", input);
        input.setAttribute("aria-invalid", "false");
        if (type === "boolean") return input.checked;
        if (["number", "integer"].includes(type)) {
          const value = Number(input.value);
          if (
            !input.value.trim() ||
            !Number.isFinite(value) ||
            (type === "integer" && !Number.isSafeInteger(value)) ||
            (spec.exclusiveMinimum != null && value <= spec.exclusiveMinimum)
          )
            invalid("invalid_type", input);
          return value;
        }
        return input.value;
      },
    };
  }
  let root;
  const write = (value) => {
    if (root) {
      try {
        if (JSON.stringify(root.read()) === JSON.stringify(value)) return;
      } catch {
        /* A reset replaces invalid drafts. */
      }
    }
    root = build(value, schema, "", label);
    element.replaceChildren(root.element);
  };
  write(value);
  return { element, read: () => root.read(), write };
}

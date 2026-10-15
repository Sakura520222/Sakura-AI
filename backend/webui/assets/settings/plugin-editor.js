import { createValueEditor } from "./value-editor.js";

const data = JSON.parse(
  document.getElementById("plugin-editor-bootstrap").textContent,
);
const t = (key) => data.messages[key] || key;
const form = document.getElementById("plugin-settings-form");
const target = document.getElementById("plugin-form-editor");
const raw = document.getElementById("plugin-json");
const rawSection = document.getElementById("plugin-raw-editor");
const toggle = document.getElementById("plugin-editor-mode");
const error = document.getElementById("plugin-editor-error");
let rawMode = false;
const editor = createValueEditor({
  value: data.value,
  schema: data.schema,
  t,
  label: data.label,
});
target.append(editor.element);
const showError = (failure) => {
  error.hidden = false;
  error.textContent = t(failure.message || "editor_incomplete");
  if (failure.input) {
    for (
      let parent = failure.input.parentElement;
      parent;
      parent = parent.parentElement
    )
      if (parent.tagName === "DETAILS") parent.open = true;
    failure.input.focus();
  }
};
const readRaw = () => {
  try {
    const value = JSON.parse(raw.value);
    if (!value || typeof value !== "object" || Array.isArray(value))
      throw new Error();
    return value;
  } catch {
    throw new Error("invalid_json");
  }
};
toggle.addEventListener("click", () => {
  try {
    if (rawMode) editor.write(readRaw());
    else raw.value = JSON.stringify(editor.read(), null, 2);
    rawMode = !rawMode;
    rawSection.hidden = !rawMode;
    target.hidden = rawMode;
    toggle.textContent = t(rawMode ? "editor_form" : "editor_raw");
    toggle.setAttribute("aria-expanded", String(rawMode));
    error.hidden = true;
  } catch (failure) {
    showError(failure);
  }
});
form.addEventListener("submit", (event) => {
  try {
    raw.value = JSON.stringify(rawMode ? readRaw() : editor.read());
    error.hidden = true;
  } catch (failure) {
    event.preventDefault();
    showError(failure);
  }
});

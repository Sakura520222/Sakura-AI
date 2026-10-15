import { SettingsClient } from "./client.js";
import { FieldController } from "./field-controller.js";
import { createValueEditor } from "./value-editor.js";
import { createBindingEditor } from "./binding-editor.js";

const root = document.getElementById("configuration-center");
const bootstrap = JSON.parse(
  document.getElementById("settings-bootstrap").textContent,
);
const client = new SettingsClient(bootstrap.csrf);
const controllers = new Map();
const editors = new Map();
let refreshGeneration = 0,
  modelGeneration = 0,
  languageGeneration = 0,
  runtimeTimer = null,
  syncInterval = 2000,
  allowNavigation = false;
const retryRuntime = () => {
  clearTimeout(runtimeTimer);
  runtimeTimer = setTimeout(refresh, syncInterval);
};
let language = bootstrap.language,
  messages = bootstrap.messages,
  groupLabels = {},
  searchIndex = [],
  modelSelection = null,
  accounts = [];
const category = root.dataset.category;
const t = (key) => messages[key] || key;
const el = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
};
const button = (text, action) => {
  const node = el("button", text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
};
const confirmAction = async (message) =>
  window.Sakura?.confirm
    ? window.Sakura.confirm({ message })
    : window.confirm(message);
const params = () => ({ category, language });
const showError = (error) => {
  const node = document.getElementById("settings-error");
  node.hidden = false;
  node.textContent = t(error.code || "network");
  node.append(
    button(t("retry"), async () => {
      try {
        if (controllers.size) await refresh();
        else await load();
        node.hidden = true;
      } catch (failure) {
        showError(failure);
      }
    }),
  );
};
const dirty = () =>
  [...controllers.values()].some((c) => c.dirty || c.inflight);

function readInput(input, field) {
  if (input.settingsEditor) return input.settingsEditor.read();
  if (field.type === "boolean") return input.checked;
  if (field.type === "integer" || field.type === "number") {
    if (!input.value.trim() || !input.checkValidity())
      throw new Error("invalid_type");
    return Number(input.value);
  }
  return input.value;
}
function writeInput(input, field, value) {
  if (input.settingsEditor) input.settingsEditor.write(value);
  else if (field.type === "boolean") input.checked = Boolean(value);
  else
    input.value = field.sensitive
      ? ""
      : value == null
        ? ""
        : typeof value === "object"
          ? JSON.stringify(value, null, 2)
          : String(value);
}

function renderField(field, parent) {
  const article = el("div", null, "settings-field");
  article.id = field.id;
  article.dataset.field = field.id;
  const explanation = el("div");
  const label = el("label", field.label);
  label.id = `label-${field.id}`;
  label.htmlFor = `input-${field.id}`;
  const description = el("p", field.description);
  description.id = `description-${field.id}`;
  explanation.append(label, description, el("code", field.id, "settings-key"));
  const source = el("details", null, "settings-source"),
    summary = el("summary"),
    sourceBody = el("pre");
  source.append(summary, sourceBody);
  explanation.append(source);
  const controls = el("div", null, "settings-controls");
  let input;
  if (
    field.id.startsWith("binding/") ||
    ["array", "object"].includes(field.type)
  ) {
    const editor = field.id.startsWith("binding/")
      ? createBindingEditor({ field, accounts, t, label: field.label })
      : createValueEditor({
          value: field.value,
          schema: field.editorSchema,
          t,
          label: field.label,
          disabled: !field.writable,
        });
    input = editor.element;
    input.settingsEditor = editor;
    editors.set(field.id, editor);
    input.setAttribute("aria-labelledby", label.id);
    label.addEventListener("click", () =>
      input.querySelector("input,select,textarea")?.focus(),
    );
    article.classList.add("settings-field-structured");
  } else if (field.constraints.enum) {
    input = el("select");
    for (const option of field.constraints.enum) {
      const node = el(
        "option",
        field.optionLabels?.[String(option)] || String(option),
      );
      if (field.key === "output_language" && option === "") {
        node.dataset.centerText = "follow_request_language";
        node.textContent = t("follow_request_language");
      }
      node.value = String(option);
      input.append(node);
    }
  } else if (field.type === "boolean") {
    input = el("input");
    input.type = "checkbox";
  } else if (
    /prompt|template/.test(field.path.at(-1) || "") ||
    (typeof field.value === "string" &&
      (field.value.includes("\n") || field.value.length > 160))
  )
    input = el("textarea");
  else {
    input = el("input");
    input.type = field.sensitive
      ? "password"
      : ["integer", "number"].includes(field.type)
        ? "number"
        : "text";
  }
  input.id = label.htmlFor;
  input.name = field.key;
  input.setAttribute("aria-describedby", description.id);
  input.disabled = !field.writable;
  if (!field.writable) description.textContent = t("deployment_owned");
  if (field.sensitive) {
    input.autocomplete = "new-password";
    input.placeholder = t(field.hasValue ? "credential" : "credential_empty");
  }
  if (input.type === "number") {
    input.step = field.type === "integer" ? "1" : "any";
    input.required = true;
  }
  if (field.constraints.ge != null) input.min = field.constraints.ge;
  if (field.constraints.le != null) input.max = field.constraints.le;
  writeInput(input, field, field.value);
  const status = el("span", null, "settings-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const actions = el("div", null, "settings-actions");
  const retry = button(t("retry"), async () => {
    try {
      if (controller.error?.status === 403) await client.read({ search: true });
      await controller.retry();
    } catch (error) {
      controller.error = error;
      controller.notify();
    }
  });
  const reload = button(t("reload_field"), async () => {
    if (!(await confirmAction(t("confirm_discard")))) return;
    try {
      const data = await client.read(
        field.id.startsWith("model/")
          ? { ...params(), ...modelSelection }
          : params(),
      );
      const server = data.fields.find((item) => item.id === field.id);
      controller.acceptServer(server);
      writeInput(input, server, server.value);
    } catch (error) {
      showError(error);
    }
  });
  const reset = button(t("reset"), async () => {
    if (
      (["object", "array"].includes(field.type) || controller.dirty) &&
      !(await confirmAction(t("confirm_reset")))
    )
      return;
    await controller.reset();
  });
  reset.setAttribute("aria-label", `${t("reset")}: ${field.label}`);
  const submit = button(t("confirmed"), async () => {
    if (await confirmAction(t("confirm_secret")))
      await controller.flush({ confirmed: true });
  });
  actions.append(status, retry, reload);
  if (field.resettable) actions.append(reset);
  if (field.sensitive && field.writable) actions.append(submit);
  controls.append(input, actions);
  article.append(explanation, controls);
  parent.append(article);
  const controller = new FieldController(field, {
    save: (payload, resetting) =>
      client.serialize(async () => {
        refreshGeneration++;
        const data = await client.save(payload(), resetting);
        // Refresh version tokens for sibling leaves before the next queued write.
        try {
          const fresh = await client.read(
            field.id.startsWith("model/")
              ? { ...params(), ...modelSelection }
              : params(),
          );
          for (const item of fresh.fields)
            controllers.get(item.id)?.reconcile(item);
        } catch (error) {
          showError(error);
        } // The mutation itself is already confirmed.
        if (field.id === "personal/theme") applyTheme(data.field.value);
        if (field.id === "personal/language")
          queueMicrotask(() => changeLanguage(data.field.value));
        try {
          channel?.postMessage({ changed: true });
        } catch {
          /* A page leaving the back/forward cache refreshes on focus. */
        }
        document.dispatchEvent(
          new CustomEvent("configuration-saved", {
            detail: { field: field.id },
          }),
        );
        refreshGeneration++;
        if (data.field.effect === "pending") retryRuntime();
        return data.field;
      }),
    changed: (state) => {
      retry.textContent = t("retry");
      reload.textContent = t("reload_field");
      reset.textContent = t("reset");
      submit.textContent = t("confirmed");
      status.dataset.state = state.state;
      status.textContent = state.error
        ? `${t(state.error.local ? "unsaved" : "failed")}: ${t(state.error.code || "network")}${state.error.detail ? " " + state.error.detail : ""}`
        : t(state.state);
      input.setAttribute("aria-invalid", state.error ? "true" : "false");
      retry.hidden = !state.error || state.conflict || state.error.local;
      reload.hidden = !state.conflict;
      reset.disabled =
        state.inflight ||
        (!state.field.overridden && !state.dirty) ||
        state.conflict;
      submit.disabled = state.inflight || !state.dirty || state.conflict;
      summary.textContent = `${t("source")}: ${t(state.field.source)}`;
      sourceBody.textContent = field.sensitive
        ? t(state.field.hasValue ? "credential" : "credential_empty")
        : `${t("default")}: ${JSON.stringify(state.field.default)}\n${t("inherited")}: ${JSON.stringify(state.field.inheritedValue)}`;
      if (!state.dirty && !state.composing)
        writeInput(input, state.field, state.saved);
    },
  });
  controllers.set(field.id, controller);
  controller.notify();
  const edit = (event) => {
    try {
      const value =
        field.sensitive && !input.value ? null : readInput(input, field);
      if (value === null && field.type === "object" && field.default === null) {
        void controller.reset();
        return;
      }
      controller.edit(
        value,
        event.type === "change" &&
          (field.type === "boolean" ||
            event.target?.tagName === "SELECT" ||
            event.target?.type === "checkbox"),
      );
    } catch (error) {
      controller.invalidate(error.message);
    }
  };
  input.addEventListener("input", edit);
  input.addEventListener("change", edit);
  input.addEventListener("compositionstart", () =>
    controller.composition(true),
  );
  input.addEventListener("compositionend", () => {
    controller.composing = false;
    edit({ type: "input" });
  });
  input.addEventListener("focusout", (event) => {
    if (input.contains(event.relatedTarget)) return;
    if (!controller.error) void controller.flush();
  });
  if (field.key === "star_aid_github_app_slug") {
    actions.append(
      button(t("fetch_slug"), async () => {
        const clientId =
          document.querySelector("[name=star_aid_github_app_client_id]")
            ?.value || "";
        try {
          const response = await fetch("/system-config/github-app-slug", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": bootstrap.csrf,
            },
            body: JSON.stringify({ clientId }),
          });
          const data = await response.json();
          if (!response.ok || !data.success)
            throw { code: "invalid_value", detail: data.message };
          input.value = data.slug;
          input.dispatchEvent(new Event("input"));
        } catch (error) {
          showError(error);
        }
      }),
    );
  }
  if (["database_url", "redis_url"].includes(field.key)) {
    actions.append(
      button(t("test_connection"), async () => {
        try {
          const response = await fetch("/system-config/test-connection", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRF-Token": bootstrap.csrf,
            },
            body: JSON.stringify({
              type: field.key === "database_url" ? "database" : "redis",
              url: input.value,
              use_saved: !input.value,
            }),
          });
          const data = await response.json();
          status.textContent =
            data.message || t(response.ok ? "saved" : "failed");
        } catch (error) {
          showError(error);
        }
      }),
    );
  }
}

function renderFields(fields, target) {
  target.replaceChildren();
  target.setAttribute("aria-busy", "false");
  if (!fields.length) {
    target.append(el("p", t("empty")));
    return;
  }
  const grouped = Map.groupBy(fields, (field) => field.group);
  for (const [name, items] of grouped) {
    const group = el("details", null, "settings-group");
    group.open = items.some((item) => !item.advanced);
    group.dataset.group = name;
    group.append(el("summary", groupLabels[name] || t(name) || name));
    target.append(group);
    let advanced = null;
    for (const item of items) {
      if (item.advanced) {
        if (!advanced) {
          advanced = el("details");
          advanced.append(el("summary", t("advanced")));
          group.append(advanced);
        }
        renderField(item, advanced);
      } else renderField(item, group);
    }
  }
  highlight();
}
function highlight() {
  let id;
  try {
    id = decodeURIComponent(location.hash.slice(1));
  } catch {
    return;
  }
  const aliases = {
    "section-basic": "review_basic",
    "section-web-search": "web_search",
    "section-issue": "issue_analysis",
    "section-agent-team": "agent_team",
    "section-strategy-strategies": "strategy.strategies",
    "section-strategy-filters": "strategy.file_filters",
    "section-strategy-context": "strategy.context_enhancement",
    "section-strategy-policy": "strategy.review_policy",
    "section-strategy-issue-analysis": "strategy.issue_analysis",
    "section-strategy-depgraph": "strategy.pr_dependency_graph",
    "section-strategy-pr-summary": "strategy.pr_summary",
    "section-strategy-scan": "strategy.scan",
    "section-label-recommendation": "label.recommendation",
    "section-label-conflict": "label.conflict_rules",
    "section-label-definitions": "label.definitions",
  };
  const group =
    aliases[id] ||
    (id.startsWith("section-group-")
      ? id.slice(14)
      : id.startsWith("group:")
        ? id.slice(6)
        : null);
  if (group) {
    const candidate = searchIndex.find((field) => field.group === group);
    if (candidate && candidate.category !== category) {
      location.replace(
        `/settings-center/${candidate.category}#${encodeURIComponent("group:" + group)}`,
      );
      return;
    }
  }
  const target = group
    ? document.querySelector(`[data-group="${CSS.escape(group)}"]`)
    : document.getElementById(id);
  if (!target) return;
  if (target.tagName === "DETAILS") target.open = true;
  for (let parent = target.parentElement; parent; parent = parent.parentElement)
    if (parent.tagName === "DETAILS") parent.open = true;
  target.classList.add("highlight");
  target.scrollIntoView({ block: "center" });
  target
    .querySelector(
      "input:not([disabled]),select:not([disabled]),textarea:not([disabled])",
    )
    ?.focus({ preventScroll: true });
}
function applyTheme(theme) {
  window.dispatchEvent(new CustomEvent("sakura-theme", { detail: theme }));
  localStorage.setItem("theme", theme);
  document.documentElement.classList.toggle(
    "dark",
    theme === "dark" ||
      (theme === "system" &&
        matchMedia("(prefers-color-scheme: dark)").matches),
  );
  document.documentElement.classList.toggle(
    "light",
    !document.documentElement.classList.contains("dark"),
  );
}
async function load() {
  const data = await client.read(params());
  messages = data.messages;
  groupLabels = data.groups;
  syncInterval = data.syncIntervalMs;
  if (category === "ai") {
    const response = await fetch("/api/v1/config/ai/accounts");
    accounts = (await response.json()).data?.accounts || [];
  }
  const theme = data.fields.find((field) => field.id === "personal/theme");
  if (theme) applyTheme(theme.value);
  renderFields(data.fields, document.getElementById("settings-fields"));
  const index = await client.read({ language, search: true });
  searchIndex = index.fields;
  highlight();
  if (category === "ai") await modelEditor();
  if (data.fields.some((field) => field.effect === "pending")) retryRuntime();
}
async function changeLanguage(value) {
  const generation = ++languageGeneration;
  language = value;
  const data = await client.read(params());
  if (generation !== languageGeneration) return;
  messages = data.messages;
  groupLabels = data.groups;
  document.documentElement.lang = value;
  document
    .querySelectorAll("[data-center-text]")
    .forEach((node) => (node.textContent = t(node.dataset.centerText)));
  document
    .querySelectorAll("[data-category-link]")
    .forEach(
      (node) => (node.textContent = t(`category.${node.dataset.categoryLink}`)),
    );
  document.getElementById("settings-category-title").textContent = t(
    `category.${category}`,
  );
  document.getElementById("settings-search").placeholder =
    t("search_placeholder");
  document.querySelectorAll("[data-setting-i18n]").forEach((node) => {
    const text = data.uiMessages?.[node.dataset.settingI18n];
    if (text) node.textContent = text;
  });
  document.querySelectorAll("[data-group]").forEach((node) => {
    node.querySelector(":scope > summary").textContent =
      groupLabels[node.dataset.group] || t(node.dataset.group);
  });
  for (const field of data.fields) {
    const controller = controllers.get(field.id);
    if (!controller) continue;
    controller.field.label = field.label;
    const label = document.getElementById(`label-${field.id}`);
    if (label) label.textContent = field.label;
    const input = document.getElementById(`input-${field.id}`);
    for (const option of input?.options || []) {
      option.textContent = field.optionLabels?.[option.value] || option.value;
    }
    const description = document.getElementById(`description-${field.id}`);
    if (description) description.textContent = field.description;
    controller.notify();
  }
  searchIndex = (await client.read({ language, search: true })).fields;
}
async function refresh() {
  if (document.hidden) return;
  const generation = ++refreshGeneration;
  try {
    const data = await client.read(params());
    if (generation !== refreshGeneration) return;
    for (const field of data.fields)
      controllers.get(field.id)?.reconcile(field);
    if (data.fields.some((field) => field.effect === "pending")) retryRuntime();
  } catch {
    /* A refresh never discards a draft or marks a mutation successful. */
  }
}
async function modelEditor() {
  const section = el("details", null, "settings-group");
  section.append(el("summary", t("model")));
  const controls = el("div", null, "settings-field"),
    provider = el("select"),
    model = el("input"),
    output = el("div");
  provider.setAttribute("aria-label", t("provider"));
  model.setAttribute("aria-label", t("model_id"));
  model.placeholder = t("model_id");
  const response = await fetch("/api/v1/config/ai/catalog", {
    credentials: "same-origin",
  });
  const catalog = await response.json();
  for (const item of catalog.data?.providers || []) {
    const option = el("option", item.label);
    option.value = item.id;
    provider.append(option);
  }
  const open = button(t("load_model"), async () => {
    if (
      [...controllers].some(
        ([id, c]) => id.startsWith("model/") && (c.dirty || c.inflight),
      )
    ) {
      showError({ code: "leave" });
      return;
    }
    try {
      const generation = ++modelGeneration;
      const selection = { provider: provider.value, model: model.value };
      const data = await client.read({ ...params(), ...selection });
      if (generation !== modelGeneration) return;
      modelSelection = selection;
      for (const id of controllers.keys())
        if (id.startsWith("model/")) controllers.delete(id);
      renderFields(data.fields, output);
    } catch (error) {
      showError(error);
    }
  });
  controls.append(provider, model, open);
  section.append(controls, output);
  document.getElementById("settings-fields").append(section);
}
const search = document.getElementById("settings-search");
search.value = new URLSearchParams(location.search).get("q") || "";
function searchSettings() {
  const query = search.value.trim().toLocaleLowerCase(),
    results = document.getElementById("settings-results");
  results.replaceChildren();
  results.hidden = !query;
  const url = new URL(location.href);
  query
    ? url.searchParams.set("q", search.value)
    : url.searchParams.delete("q");
  history.replaceState(null, "", url);
  if (!query) return;
  const matches = searchIndex.filter((field) =>
    field.searchText.toLocaleLowerCase().includes(query),
  );
  if (!matches.length) results.append(el("p", t("no_results")));
  for (const field of matches) {
    const link = el("a", `${t(`category.${field.category}`)} · ${field.label}`);
    link.href = `/settings-center/${field.category}?q=${encodeURIComponent(search.value)}#${encodeURIComponent(field.id)}`;
    results.append(link);
  }
}
search.addEventListener("input", searchSettings);
const channel =
  typeof BroadcastChannel === "function"
    ? new BroadcastChannel("sakura-settings")
    : null;
if (channel) channel.onmessage = refresh;
document.addEventListener("configuration-accounts-changed", async () => {
  try {
    const response = await fetch("/api/v1/config/ai/accounts", {
      credentials: "same-origin",
    });
    accounts = (await response.json()).data?.accounts || [];
    for (const editor of editors.values()) editor.setAccounts?.(accounts);
  } catch (error) {
    showError(error);
  }
});
window.addEventListener("focus", refresh);
window.addEventListener("hashchange", highlight);
window.addEventListener("beforeunload", (event) => {
  if (dirty() && !allowNavigation) {
    event.preventDefault();
    event.returnValue = "";
  }
});
window.addEventListener("pagehide", () => {
  clearTimeout(runtimeTimer);
  channel?.close();
});
window.addEventListener("pageshow", () => {
  allowNavigation = false;
});
document.addEventListener("click", async (event) => {
  const link = event.target.closest("a[href]");
  if (
    !link ||
    event.button !== 0 ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey ||
    link.target === "_blank" ||
    !dirty() ||
    (link.hash &&
      link.pathname === location.pathname &&
      link.search === location.search)
  )
    return;
  event.preventDefault();
  if (await confirmAction(t("leave"))) {
    allowNavigation = true;
    location.href = link.href;
  }
});
load().then(searchSettings).catch(showError);

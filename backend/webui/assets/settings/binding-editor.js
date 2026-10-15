/** Account/model rows edit one versioned role binding, including its ordered fallbacks. */
import { createEditorLocalizer } from "./value-editor.js";

export function createBindingEditor({ field, accounts, t, label }) {
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const element = el("div", null, "settings-binding-editor");
  const localizer = createEditorLocalizer(element);
  const localized = (tag, read, className) =>
    localizer.text(el(tag, null, className), read);
  element.setAttribute("role", "group");
  localizer.attribute(element, "aria-label", () => label);
  const followAllowed = field.id !== "binding/main";
  let sequence = 0,
    primary,
    fallbacks = [];
  const list = el("div", null, "settings-fallback-list");
  const emit = () =>
    element.dispatchEvent(new Event("input", { bubbles: true }));
  const action = (key, run) => {
    const node = localized("button", () => t(key), "settings-editor-button");
    node.type = "button";
    node.addEventListener("click", () => {
      run();
      emit();
    });
    return node;
  };
  const update = () => {
    empty.hidden = fallbacks.length > 0;
    fallbacks.forEach((row, index) => {
      row.title.textContent = `${t("fallback_item")} ${index + 1}`;
      row.up.disabled = index === 0;
      row.down.disabled = index === fallbacks.length - 1;
    });
  };
  function assignment(entry, fallback = false) {
    const row = el("div", null, "settings-assignment");
    row.dataset.assignment = fallback ? "fallback" : "primary";
    const title = fallback
        ? el("h4")
        : localized("h4", () => t("primary_assignment")),
      account = el("select"),
      model = el("input"),
      datalist = el("datalist");
    datalist.id = `binding-models-${field.id}-${sequence++}`;
    model.setAttribute("list", datalist.id);
    localizer.attribute(model, "placeholder", () => t("model_id"));
    let initialized = false;
    const options = () => {
      const selected = initialized
        ? account.value
        : entry.account || entry.provider || "";
      initialized = true;
      account.replaceChildren();
      const add = (id, text, disabled = false) => {
        const option = el("option", text);
        option.value = id;
        option.disabled = disabled;
        account.append(option);
      };
      add("", t("select_account"));
      if (followAllowed) add("main", t("follow_main"));
      for (const item of accounts)
        add(
          item.id,
          `${item.name}${item.enabled === false ? ` · ${t("account_disabled")}` : ""}`,
          item.enabled === false,
        );
      if (
        selected &&
        ![...account.options].some((option) => option.value === selected)
      )
        add(selected, `${selected} · ${t("account_unavailable")}`, true);
      account.value = selected;
      datalist.replaceChildren();
      const item = accounts.find((item) => item.id === selected);
      for (const id of new Set(
        [item?.default_model, ...(item?.models || [])].filter(Boolean),
      )) {
        const option = el("option");
        option.value = id;
        datalist.append(option);
      }
      model.disabled = selected === "main";
      if (selected === "main") model.value = t("follow_main");
    };
    const accountLabel = localized("label", () => t("account")),
      modelLabel = localized("label", () => t("model_id"));
    localizer.attribute(account, "aria-label", () => t("account"));
    localizer.attribute(model, "aria-label", () => t("model_id"));
    accountLabel.append(account);
    modelLabel.append(model);
    model.value = entry.model || "";
    options();
    account.addEventListener("change", () => {
      model.value =
        account.value === "main"
          ? "follow"
          : accounts.find((item) => item.id === account.value)?.default_model ||
            "";
      options();
    });
    const fields = el("div", null, "settings-assignment-fields");
    fields.append(accountLabel, modelLabel, datalist);
    row.append(title, fields);
    const result = {
      element: row,
      title,
      account,
      model,
      options,
      read: () => ({
        account: account.value,
        model: account.value === "main" ? "follow" : model.value,
      }),
    };
    if (fallback) {
      const actions = el("div", null, "settings-editor-row-actions");
      const move = (delta) => {
        const index = fallbacks.indexOf(result),
          other = fallbacks[index + delta];
        if (!other) return;
        [fallbacks[index], fallbacks[index + delta]] = [other, result];
        if (delta < 0) list.insertBefore(row, other.element);
        else list.insertBefore(other.element, row);
        update();
      };
      result.up = action("editor_move_up", () => move(-1));
      result.down = action("editor_move_down", () => move(1));
      actions.append(
        result.up,
        result.down,
        action("remove_fallback", () => {
          const index = fallbacks.indexOf(result);
          fallbacks.splice(index, 1);
          row.remove();
          update();
          (fallbacks[index]?.account || add).focus();
        }),
      );
      row.append(actions);
    }
    return result;
  }
  const empty = localized(
    "p",
    () => t("fallback_empty"),
    "settings-editor-hint",
  );
  const add = action("add_fallback", () => {
    const row = assignment({}, true);
    fallbacks.push(row);
    list.append(row.element);
    update();
    row.account.focus();
  });
  const snapshot = () => ({
    primary: primary.read(),
    fallback: fallbacks.map((row) => row.read()),
  });
  const write = (next) => {
    if (primary && JSON.stringify(snapshot()) === JSON.stringify(next)) return;
    primary = assignment(next?.primary || {});
    primary.account.dataset.bindingAccount = field.id;
    fallbacks = (next?.fallback || []).map((entry) => assignment(entry, true));
    list.replaceChildren(...fallbacks.map((row) => row.element));
    element.replaceChildren(
      primary.element,
      localized("h4", () => t("fallback")),
      localized("p", () => t("fallback_help"), "settings-editor-hint"),
      empty,
      list,
      add,
    );
    update();
  };
  write(field.value);
  return {
    element,
    write,
    relabel: (nextLabel = label) => {
      label = nextLabel;
      localizer.refresh();
      primary.options();
      fallbacks.forEach((row) => row.options());
      update();
    },
    setAccounts: (next) => {
      accounts = next;
      primary.options();
      fallbacks.forEach((row) => row.options());
    },
    read: () => {
      const result = snapshot();
      for (const row of [primary, ...fallbacks]) {
        const valid = row.account.value && row.model.value.trim();
        row.account.setAttribute(
          "aria-invalid",
          !row.account.value ? "true" : "false",
        );
        row.model.setAttribute(
          "aria-invalid",
          !row.model.value.trim() ? "true" : "false",
        );
        if (!valid) throw new Error("binding_incomplete");
      }
      return result;
    },
  };
}

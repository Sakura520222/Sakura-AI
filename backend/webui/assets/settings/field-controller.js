/** Framework-independent field state machine; reusable from React Settings. */
const canonical = (value) =>
  JSON.stringify(value, (_key, node) =>
    node && typeof node === "object" && !Array.isArray(node)
      ? Object.fromEntries(
          Object.entries(node).sort(([a], [b]) => a.localeCompare(b)),
        )
      : node,
  );
const same = (a, b) => canonical(a) === canonical(b);
export class FieldController {
  constructor(
    field,
    {
      save,
      changed = () => {},
      schedule = setTimeout,
      cancel = clearTimeout,
      debounce = 650,
    },
  ) {
    this.field = field;
    this.saved = field.value;
    this.draft = field.value;
    this.save = save;
    this.changed = changed;
    this.schedule = (...args) => schedule(...args);
    this.cancel = (...args) => cancel(...args);
    this.debounce = debounce;
    this.state = field.effect !== "live" ? field.effect : "unchanged";
    this.timer = null;
    this.composing = false;
    this.inflight = false;
    this.generation = 0;
    this.error = null;
    this.conflict = false;
    this.resetting = false;
  }
  get dirty() {
    return this.resetting || !same(this.draft, this.saved);
  }
  notify() {
    this.changed(this);
  }
  edit(value, immediate = false) {
    this.draft = value;
    this.generation++;
    this.error = null;
    this.resetting = false;
    this.cancel(this.timer);
    this.timer = null;
    this.state = this.dirty ? "unsaved" : "unchanged";
    this.notify();
    if (this.composing || this.field.sensitive || this.conflict) return;
    if (immediate) void this.flush();
    else this.timer = this.schedule(() => this.flush(), this.debounce);
  }
  composition(active) {
    this.composing = active;
    if (active) this.cancel(this.timer);
    else this.edit(this.draft);
  }
  async flush({ confirmed = false } = {}) {
    this.cancel(this.timer);
    this.timer = null;
    if (
      this.composing ||
      this.inflight ||
      this.conflict ||
      !this.dirty ||
      (this.field.sensitive && !confirmed)
    )
      return;
    const generation = this.generation,
      value = this.draft,
      reset = this.resetting;
    this.inflight = true;
    this.state = "saving";
    this.notify();
    try {
      const response = await this.save(
        () => ({
          field: this.field.id,
          value,
          version: this.field.version,
          confirmed,
        }),
        reset,
      );
      this.field = response;
      this.saved = response.value;
      this.error = null;
      if (generation === this.generation) {
        this.draft = response.value;
        this.resetting = false;
      }
      this.state = this.dirty
        ? "unsaved"
        : response.effect === "live"
          ? "saved"
          : response.effect;
    } catch (error) {
      this.error = error;
      this.conflict = error.status === 409;
      this.state = "failed";
    } finally {
      this.inflight = false;
      this.notify();
      if (!this.error && this.dirty && !this.composing && !this.field.sensitive)
        void this.flush();
    }
  }
  async reset() {
    if (!this.field.resettable || this.inflight) return;
    this.generation++;
    this.resetting = true;
    await this.flush();
  }
  reconcile(field) {
    if (this.inflight) {
      // A sibling may be queued behind the write that produced this
      // refresh. Its baseline is unchanged, so rebase only its token.
      if (same(this.saved, field.value)) this.field.version = field.version;
      return;
    }
    if (this.dirty && !same(this.saved, field.value)) {
      this.conflict = true;
      this.state = "failed";
      this.error = { code: "conflict", status: 409 };
    } else {
      this.field = field;
      if (!this.dirty) {
        this.saved = field.value;
        this.draft = field.value;
        if (field.effect !== "live") this.state = field.effect;
        else if (this.state === "pending") this.state = "saved";
      }
    }
    this.notify();
  }
  acceptServer(field) {
    this.cancel(this.timer);
    this.field = field;
    this.saved = field.value;
    this.draft = field.value;
    this.resetting = false;
    this.conflict = false;
    this.error = null;
    this.state = "unchanged";
    this.notify();
  }
  retry() {
    this.error = null;
    return this.flush({ confirmed: this.field.sensitive });
  }
}

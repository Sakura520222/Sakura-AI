/** Versioned Settings API client. No HTML, redirect, or toast parsing. */
export class SettingsClient {
  constructor(csrf, transport = fetch) {
    this.csrf = csrf;
    this.transport = (...args) => transport(...args);
    this.queue = Promise.resolve();
  }
  async request(path = "", options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await this.transport(`/api/v1/config-center${path}`, {
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
        ...options,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": this.csrf,
          ...options.headers,
        },
      });
      const data = await response.json();
      if (!response.ok) {
        const error = new Error(data.error?.code || `http_${response.status}`);
        error.code = data.error?.code || error.message;
        error.detail = data.error?.detail || "";
        error.status = response.status;
        throw error;
      }
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }
  serialize(work) {
    const next = this.queue.then(work, work);
    this.queue = next.catch(() => {});
    return next;
  }
  async read(params) {
    const data = await this.request(`?${new URLSearchParams(params)}`);
    if (data.csrf) this.csrf = data.csrf;
    return data;
  }
  save(payload, reset = false) {
    return this.request(reset ? "/reset" : "", {
      method: reset ? "POST" : "PATCH",
      body: JSON.stringify(payload),
    });
  }
}

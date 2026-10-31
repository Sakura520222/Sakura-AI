export type ConnectionState =
  "connecting" | "connected" | "reconnecting" | "closed";
export interface StreamOptions {
  events: Record<string, (data: unknown, event: MessageEvent) => void>;
  onState?: (state: ConnectionState) => void;
  createSource?: (url: string) => EventSource;
}

/** EventSource owns reconnection and Last-Event-ID; disposal closes every listener. */
export function connectStream(url: string, options: StreamOptions): () => void {
  if (
    !url.startsWith("/") ||
    url.startsWith("//") ||
    url.includes("\\") ||
    new URL(url, window.location.origin).origin !== window.location.origin
  )
    throw new Error("A same-origin stream path is required");
  let closed = false;
  const source =
    options.createSource?.(url) ??
    new EventSource(url, { withCredentials: true });
  const listeners = Object.entries(options.events).map(([name, handler]) => {
    const listener = (event: MessageEvent) => {
      if (closed) return;
      try {
        handler(JSON.parse(event.data), event);
      } catch {
        /* Ignore a malformed event; the next projection refresh repairs state. */
      }
    };
    source.addEventListener(name, listener as EventListener);
    return [name, listener] as const;
  });
  options.onState?.("connecting");
  source.onopen = () => {
    if (!closed) options.onState?.("connected");
  };
  source.onerror = () => {
    if (!closed)
      options.onState?.(
        source.readyState === EventSource.CLOSED ? "closed" : "reconnecting",
      );
  };
  const expired = () => dispose();
  window.addEventListener("sakura:session-expired", expired);
  function dispose() {
    if (closed) return;
    closed = true;
    listeners.forEach(([name, listener]) =>
      source.removeEventListener(name, listener as EventListener),
    );
    source.onopen = null;
    source.onerror = null;
    source.close();
    window.removeEventListener("sakura:session-expired", expired);
    options.onState?.("closed");
  }
  return dispose;
}

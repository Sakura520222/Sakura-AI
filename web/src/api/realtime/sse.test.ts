import { afterEach, describe, expect, it, vi } from "vitest";
import { connectStream } from "./sse";

class FakeSource extends EventTarget {
  readyState = 0;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  close = vi.fn();
}
afterEach(() => vi.unstubAllGlobals());
describe("shared realtime lifecycle", () => {
  it("reconnects through the browser and closes all listeners on disposal", () => {
    vi.stubGlobal("EventSource", { CLOSED: 2 });
    const source = new FakeSource(),
      onState = vi.fn(),
      event = vi.fn();
    const dispose = connectStream("/api/v1/activity/sessions/1/stream", {
      events: { update: event },
      onState,
      createSource: () => source as unknown as EventSource,
    });
    source.onopen?.();
    source.dispatchEvent(
      new MessageEvent("update", { data: '{"sequence":1}' }),
    );
    expect(event).toHaveBeenCalledWith(
      { sequence: 1 },
      expect.any(MessageEvent),
    );
    source.onerror?.();
    expect(onState).toHaveBeenLastCalledWith("reconnecting");
    dispose();
    dispose();
    expect(source.close).toHaveBeenCalledOnce();
    source.dispatchEvent(
      new MessageEvent("update", { data: '{"sequence":2}' }),
    );
    expect(event).toHaveBeenCalledOnce();
  });
  it("closes on session expiry and ignores malformed input", () => {
    const source = new FakeSource(),
      event = vi.fn();
    connectStream("/api/v1/activity/sessions/1/stream", {
      events: { update: event },
      createSource: () => source as unknown as EventSource,
    });
    source.dispatchEvent(new MessageEvent("update", { data: "not json" }));
    expect(event).not.toHaveBeenCalled();
    window.dispatchEvent(new Event("sakura:session-expired"));
    expect(source.close).toHaveBeenCalledOnce();
  });
});

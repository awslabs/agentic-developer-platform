/** Consume provider transport without publishing partial model output as evidence.
 * A completed Responses event owns the full output and usage. A broken stream
 * is an unknown outcome and must not be automatically replayed.
 */
export async function readModelResponse(response: Response, signal: AbortSignal): Promise<unknown> {
  if (!response.ok || !response.body) throw new Error("Model response unavailable");
  const reader = response.body.getReader();
  const abort = () => { void reader.cancel(signal.reason).catch(() => {}); };
  signal.addEventListener("abort", abort, { once: true });
  const decoder = new TextDecoder("utf-8", { fatal: true });
  const streaming = response.headers.get("content-type")?.split(";", 1)[0]?.trim() === "text/event-stream";
  let buffer = "", event = "";
  let data: string[] = [];
  let completed: unknown;
  const line = (value: string) => {
    if (value === "") {
      if (data.length) {
        const payload = data.join("\n");
        if (payload !== "[DONE]") {
          const value = JSON.parse(payload);
          const type = event || value.type;
          if (event && value.type && event !== value.type) throw new Error("Model stream event mismatch");
          if (["error", "response.failed", "response.incomplete"].includes(type)) throw new Error("Model stream did not complete");
          if (type === "response.completed") {
            if (!value.response || value.response.status !== "completed") throw new Error("Model stream did not complete");
            completed = value.response;
          }
        }
      }
      data = []; event = "";
    } else if (!value.startsWith(":")) {
      const separator = value.indexOf(":");
      const name = separator < 0 ? value : value.slice(0, separator);
      const content = separator < 0 ? "" : value.slice(separator + 1).replace(/^ /, "");
      if (name === "event") event = content;
      if (name === "data") data.push(content);
    }
  };
  const consume = (final = false) => {
    while (completed === undefined) {
      const index = buffer.search(/[\r\n]/);
      if (index < 0 || (!final && buffer[index] === "\r" && index === buffer.length - 1)) break;
      const width = buffer[index] === "\r" && buffer[index + 1] === "\n" ? 2 : 1;
      const value = buffer.slice(0, index);
      buffer = buffer.slice(index + width);
      line(value);
    }
  };
  try {
    for (;;) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      signal.throwIfAborted();
      buffer += decoder.decode(value, { stream: !done });
      if (streaming) consume(done);
      if (completed !== undefined) return completed;
      if (done) break;
    }
    if (streaming) throw new Error("Model stream ended without completed response");
    // Some compatible providers return JSON even for a streaming request.
    return JSON.parse(buffer);
  } finally {
    signal.removeEventListener("abort", abort);
    await reader.cancel();
  }
}

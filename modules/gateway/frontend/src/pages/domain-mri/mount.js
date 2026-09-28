import { taskRequest, downloadReport } from "./task-client.js";
export function mountDomainMRI(root, fetcher, storageKey) {
  const lifetime = new AbortController();
  let disposed = false,
    reportURL,
    reportHTML;
  const storage = {
    setItem: (key, value) => sessionStorage.setItem(storageKey + key, value),
    getItem: (key) => sessionStorage.getItem(storageKey + key),
    removeItem: (key) => sessionStorage.removeItem(storageKey + key),
  };
  const $ = (id) => root.getElementById(id);
  let run,
    stream,
    poller,
    startTime,
    ended = false,
    seen = new Set(),
    count = 0,
    pendingId;
  const terminal = new Set(["completed", "failed", "cancelled", "expired"]);
  const labels = {
    accepted: "Investigation accepted",
    queued: "Waiting for an investigation worker",
    running: "Investigation in progress",
    completed: "Investigation complete",
    failed: "Investigation stopped",
    cancelled: "Investigation cancelled",
    expired: "Investigation expired",
    submission_unknown: "Confirming submission",
    waiting_for_input: "Investigator needs additional input",
    cancel_requested: "Stopping investigation",
  };
  async function api(path, body, headers = {}) {
    const r = await fetcher(
      path,
      body
        ? {
            method: "POST",
            headers: { "Content-Type": "application/json", ...headers },
            body: JSON.stringify(body),
          }
        : {},
    );
    const data = await r.json();
    if (!r.ok) {
      const e = Error(
        (typeof data.detail === "string"
          ? data.detail
          : data.message || data.error) || "Request temporarily unavailable.",
      );
      e.id = data.id;
      throw e;
    }
    return data;
  }
  function error(message) {
    if (disposed) return;
    $("error").textContent = message;
    $("error").hidden = !message;
  }
  function addEvent(event) {
    const key = event.event_id || event.sequence || JSON.stringify(event);
    if (seen.has(key)) return;
    seen.add(key);
    const d = event.data || {};
    let message = d.message || labels[d.status];
    if (!message && event.type === "history.gap")
      message =
        "Some earlier events are unavailable. The final report includes the recorded investigation timeline.";
    if (!message) return;
    run.events ??= [];
    run.events.push(event);
    run.events = run.events.slice(-250);
    storage.setItem("mri-run", JSON.stringify(run));
    const li = document.createElement("li"),
      time = document.createElement("time"),
      p = document.createElement("p");
    time.textContent = new Date(
      event.timestamp || Date.now(),
    ).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    p.textContent = message;
    li.append(time, p);
    $("timeline").append(li);
    if (d.message) count++;
    $("count").textContent = String(count);
    li.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  function setState(status) {
    $("state").textContent = status.replaceAll("_", " ").toUpperCase();
    $("activity-title").textContent =
      labels[status] || "Investigation in progress";
    $("working-text").textContent =
      status === "queued" || status === "accepted"
        ? "Waiting for the worker to start"
        : "Waiting for the next observation";
  }
  async function refresh() {
    if (!run || disposed) return;
    try {
      const s = await api("/v1/tasks/" + run.id);
      if (disposed) return;
      startTime = Date.parse(s.created_at) || run.started || Date.now();
      const end = s.completed_at || s.result?.committed_at;
      if (end) {
        const seconds = Math.max(
          0,
          Math.floor((Date.parse(end) - startTime) / 1000),
        );
        $("elapsed").textContent =
          `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
      }
      s.domain = run.domain;
      $("subject").textContent = s.domain;
      $("domain").value = s.domain;
      setState(s.status);
      $("case-id").textContent = s.task_id || "";
      $("case-id").hidden = !s.task_id;
      if (s.status === "submission_unknown") {
        pendingId = run.id;
        error(
          "Submission is not confirmed. Retry the same submission; it will not create a duplicate Task.",
        );
        ended = true;
        finish();
        $("submit").textContent = "Retry submission ↗";
        return;
      }
      if (s.status === "waiting_for_input")
        error(
          "This investigation requested additional input. Stop it and start a new investigation with a more specific domain.",
        );
      if (terminal.has(s.status)) {
        pendingId = null;
        ended = true;
        finish();
        $("connection").textContent = "FINISHED";
        $("connection").className = "connection";
        if (s.status === "completed") await showReport(s);
        else
          error(
            s.error?.message ||
              "This investigation ended without a final report. You can start another.",
          );
      }
    } catch (e) {
      if (disposed) return;
      $("connection").textContent = "RECONNECTING";
      error(e.message);
    }
  }
  function finish() {
    stream?.close();
    clearInterval(poller);
    $("working").hidden = true;
    $("cancel").hidden = true;
    $("submit").disabled = false;
    $("domain").disabled = false;

    if (ended) $("submit").innerHTML = "Investigate again <span>↗</span>";
  }
  async function showReport(s) {
    error("");
    $("result").hidden = false;
    const summary =
      s.result?.report?.summary || "The investigation report is available.";
    $("summary").textContent = summary.split("\n\n")[0];
    $("result-note").textContent =
      "A standalone report with evidence, citations and coverage limits.";
    if (!reportURL) {
      const blob = await downloadReport(fetcher, run.id, s, lifetime.signal);
      reportHTML = await blob.text();
      if (disposed) return;
      reportURL = URL.createObjectURL(blob);
    }
    // Inline the verified document inside the sandbox: the site CSP blocks Blob frames.
    $("report").srcdoc = reportHTML;
    $("download").href = reportURL;
    $("download").download = run.domain + "-report.html";
    $("open-report").href = reportURL;
  }
  function connect() {
    stream?.close();
    const controller = new AbortController();
    stream = { close: () => controller.abort() };
    let cursor = run.cursor || "";
    (async () => {
      while (!controller.signal.aborted && !ended && !disposed) {
        try {
          const response = await fetcher("/v1/tasks/" + run.id + "/events", {
            signal: controller.signal,
            headers: cursor ? { "Last-Event-ID": cursor } : {},
          });
          if (!response.ok || !response.body) throw Error("Stream unavailable");
          $("connection").textContent = "CONNECTED";
          $("connection").className = "connection live";
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          try {
            while (!controller.signal.aborted) {
              const { value, done } = await reader.read();
              if (done) break;
              buffer += decoder
                .decode(value, { stream: true })
                .replaceAll("\r", "");
              let boundary;
              while ((boundary = buffer.indexOf("\n\n")) >= 0) {
                const frame = buffer.slice(0, boundary);
                buffer = buffer.slice(boundary + 2);
                const lines = frame.split("\n");
                const id = lines
                  .find((line) => line.startsWith("id:"))
                  ?.slice(3)
                  .trim();
                const type = lines
                  .find((line) => line.startsWith("event:"))
                  ?.slice(6)
                  .trim();
                const data = lines
                  .filter((line) => line.startsWith("data:"))
                  .map((line) => line.slice(5).trim())
                  .join("\n");
                if (type === "event" && data) {
                  try {
                    addEvent(JSON.parse(data));
                  } catch {
                    /* Ignore malformed frames. */
                  }
                  if (id) {
                    cursor = id;
                    run.cursor = id;
                    storage.setItem("mri-run", JSON.stringify(run));
                  }
                }
              }
            }
          } finally {
            await reader.cancel().catch(() => {});
          }
        } catch {
          /* Polling continues while the event stream reconnects. */
        }
        if (!ended && !disposed && !controller.signal.aborted) {
          $("connection").textContent = "RECONNECTING";
          await new Promise((resolve) => {
            const timer = setTimeout(resolve, 2000);
            controller.signal.addEventListener(
              "abort",
              () => {
                clearTimeout(timer);
                resolve();
              },
              { once: true },
            );
          });
        }
      }
    })();
  }
  async function openRun(value) {
    if (disposed) return;
    if (reportURL) URL.revokeObjectURL(reportURL);
    reportURL = null;
    const previous = value.events || [];
    run = { ...value, events: [] };
    pendingId = null;
    storage.setItem("mri-run", JSON.stringify(value));
    ended = false;
    seen = new Set();
    count = 0;
    $("timeline").replaceChildren();
    $("timeline").hidden = false;
    $("empty").hidden = true;
    $("result").hidden = true;
    $("working").hidden = false;
    $("subject").textContent = value.domain;
    $("domain").value = value.domain;
    $("execution").textContent = "Live investigation";
    $("case-note").textContent = "Public domain · desktop browser";
    $("submit").disabled = true;
    $("domain").disabled = true;

    $("cancel").hidden = false;
    $("cancel").disabled = false;
    $("cancel").textContent = "Stop investigation";
    setState("accepted");
    startTime = Date.now();
    for (const event of previous) addEvent(event);
    await refresh();
    if (!ended) {
      connect();
      clearInterval(poller);
      poller = setInterval(refresh, 4000);
    }
  }
  async function submit() {
    error("");
    $("submit").disabled = true;

    const domain = $("domain").value.trim();
    const id = pendingId || crypto.randomUUID();
    pendingId = id;
    storage.setItem("mri-pending", JSON.stringify({ id, domain }));
    try {
      const task = await api("/v1/tasks", taskRequest(domain), {
        "Idempotency-Key": "domain-mri-" + id,
      });
      const result = { id: task.task_id, domain, started: Date.now() };
      storage.removeItem("mri-pending");
      await openRun(result);
    } catch (e) {
      if (disposed) return;
      $("submit").disabled = false;

      error(e.message);
      if (e.id) {
        await openRun({ id: e.id, domain });
      } else {
        $("submit").textContent = "Retry submission ↗";
      }
    }
  }
  $("domain-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submit();
  });
  $("domain").addEventListener("input", () => {
    pendingId = null;
  });
  $("cancel").onclick = async () => {
    try {
      $("cancel").disabled = true;
      await api("/v1/tasks/" + run.id + "/cancel", {
        schema_version: "1.0",
        command_id: crypto.randomUUID(),
        reason: "Stopped from Domain MRI",
      });
      $("cancel").textContent = "Stopping…";
      await refresh();
    } catch (e) {
      if (disposed) return;
      error(e.message);
      $("cancel").disabled = false;
    }
  };
  const clockTimer = setInterval(() => {
    if (startTime && !ended) {
      const seconds = Math.floor((Date.now() - startTime) / 1000);
      $("elapsed").textContent =
        `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
    }
  }, 1000);
  try {
    const saved = JSON.parse(storage.getItem("mri-run"));
    const pending = JSON.parse(storage.getItem("mri-pending"));
    if (pending) {
      pendingId = pending.id;
      $("domain").value = pending.domain;
      error(
        "A previous submission was interrupted. Retry to recover the same investigation.",
      );
      $("submit").textContent = "Retry submission ↗";
    } else if (saved) openRun(saved);
  } catch {
    storage.removeItem("mri-run");
    storage.removeItem("mri-pending");
  }

  return () => {
    disposed = true;
    ended = true;
    lifetime.abort();
    stream?.close();
    clearInterval(poller);
    clearInterval(clockTimer);
    if (reportURL) URL.revokeObjectURL(reportURL);
  };
}

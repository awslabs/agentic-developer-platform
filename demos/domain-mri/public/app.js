const $ = (id) => document.getElementById(id);
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
  awaiting_input: "Investigator needs additional input",
};
async function api(path, body) {
  const r = await fetch(
    path,
    body
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {},
  );
  const data = await r.json();
  if (!r.ok) {
    const e = Error(data.error || "Request temporarily unavailable.");
    e.id = data.id;
    throw e;
  }
  return data;
}
function error(message) {
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
  sessionStorage.setItem("mri-run", JSON.stringify(run));
  const li = document.createElement("li"),
    time = document.createElement("time"),
    p = document.createElement("p");
  time.textContent = new Date(event.timestamp || Date.now()).toLocaleTimeString(
    [],
    { hour: "2-digit", minute: "2-digit", second: "2-digit" },
  );
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
  if (!run) return;
  try {
    const s = await api("/api/runs/" + run.id);
    startTime = s.started;
    const end = s.completed_at || s.result?.committed_at;
    if (end) {
      const seconds = Math.max(
        0,
        Math.floor((Date.parse(end) - startTime) / 1000),
      );
      $("elapsed").textContent =
        `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
    }
    run.domain = s.domain;
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
    if (s.status === "awaiting_input")
      error(
        "This investigation requested additional input. Stop it and start a new investigation with a more specific domain.",
      );
    if (terminal.has(s.status)) {
      pendingId = null;
      ended = true;
      finish();
      $("connection").textContent = run.replay ? "REPLAY COMPLETE" : "FINISHED";
      $("connection").className = "connection";
      if (s.status === "completed") showReport(s);
      else
        error(
          s.error?.message ||
            "This investigation ended without a final report. You can start another.",
        );
    }
  } catch (e) {
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
  $("replay").disabled = false;
  if (ended) $("submit").innerHTML = "Investigate again <span>↗</span>";
}
function showReport(s) {
  error("");
  $("result").hidden = false;
  const summary =
    s.result?.report?.summary || "The investigation report is available.";
  $("summary").textContent = summary.split("\n\n")[0];
  $("result-note").textContent = run.replay
    ? "Recorded WICAR investigation · 27 September 2026 · 5 Opus 5 turns · $0.493213 model usage"
    : "A standalone report with evidence, citations and coverage limits.";
  const url = "/api/runs/" + run.id + "/report";
  $("report").src = url;
  $("download").href = url + "?download=1";
  $("open-report").href = url;
}
function connect() {
  stream?.close();
  stream = new EventSource("/api/runs/" + run.id + "/events");
  stream.onopen = () => {
    if (!ended) {
      $("connection").textContent = run.replay
        ? "RECORDED REPLAY"
        : "CONNECTED";
      $("connection").className = "connection live";
    }
  };
  stream.addEventListener("event", (e) => {
    try {
      addEvent(JSON.parse(e.data));
    } catch {}
    refresh();
  });
  stream.addEventListener("reconnect", () => {
    $("connection").textContent = "RECONNECTING";
  });
  stream.onerror = () => {
    if (!ended) {
      $("connection").textContent = "RECONNECTING";
      $("connection").className = "connection";
    }
  };
}
async function openRun(value) {
  const previous = value.events || [];
  run = { ...value, events: [] };
  pendingId = null;
  sessionStorage.setItem("mri-run", JSON.stringify(value));
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
  $("execution").textContent = value.replay
    ? "Recorded replay"
    : "Live investigation";
  $("case-note").textContent = value.replay
    ? "Playback of a completed run · not a new scan"
    : "Public domain · desktop browser";
  $("submit").disabled = true;
  $("domain").disabled = true;
  $("replay").disabled = true;
  $("cancel").hidden = value.replay;
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
async function submit(replay = false) {
  error("");
  $("submit").disabled = true;
  $("replay").disabled = true;
  const domain = replay ? "malware.wicar.org" : $("domain").value;
  const id = pendingId || crypto.randomUUID();
  pendingId = id;
  sessionStorage.setItem("mri-pending", JSON.stringify({ id, domain, replay }));
  try {
    const result = await api("/api/runs", { domain, request_id: id, replay });
    sessionStorage.removeItem("mri-pending");
    await openRun(result);
  } catch (e) {
    $("submit").disabled = false;
    $("replay").disabled = false;
    error(e.message);
    if (e.id) {
      await openRun({ id: e.id, domain, replay: false });
    } else {
      $("submit").textContent = "Retry submission ↗";
    }
  }
}
$("domain-form").addEventListener("submit", (e) => {
  e.preventDefault();
  submit();
});
$("replay").onclick = () => {
  pendingId = null;
  ended = true;
  submit(true);
};
$("domain").addEventListener("input", () => {
  pendingId = null;
});
$("cancel").onclick = async () => {
  try {
    $("cancel").disabled = true;
    await api("/api/runs/" + run.id + "/cancel", {});
    $("cancel").textContent = "Stopping…";
    await refresh();
  } catch (e) {
    error(e.message);
    $("cancel").disabled = false;
  }
};
setInterval(() => {
  if (startTime && !ended) {
    const seconds = Math.floor((Date.now() - startTime) / 1000);
    $("elapsed").textContent =
      `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
  }
}, 1000);
try {
  const saved = JSON.parse(sessionStorage.getItem("mri-run"));
  const pending = JSON.parse(sessionStorage.getItem("mri-pending"));
  if (pending) {
    pendingId = pending.id;
    $("domain").value = pending.domain;
    error(
      "A previous submission was interrupted. Retry to recover the same investigation.",
    );
    $("submit").textContent = "Retry submission ↗";
  } else if (saved) openRun(saved);
} catch {
  sessionStorage.removeItem("mri-run");
  sessionStorage.removeItem("mri-pending");
}

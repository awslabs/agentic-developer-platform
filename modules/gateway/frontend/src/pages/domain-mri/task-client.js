// Direct Task API client. Authentication is supplied by the existing Cognito session.
export function taskRequest(value) {
  const url = new URL(value.includes("://") ? value : "https://" + value);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.port ||
    url.pathname !== "/" ||
    url.search ||
    url.hash ||
    !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/i.test(
      url.hostname,
    ) ||
    /\.(local|internal|localhost)$/i.test(url.hostname)
  ) {
    throw Error("Enter a public domain only, without a path or credentials.");
  }
  return {
    schema_version: "1.0",
    persona: "agent-task-cyber",
    instructions: `Assess https://${url.hostname} using the URL-analysis skill, Common Crawl and isolated live browsing. Determine what the site serves and whether observations indicate malicious activity, deliberate security testing, or uncertainty. Read one relevant archived capture if available. Open a host-scoped browser, capture and inspect a screenshot, inspect network requests, and close the browser. Do not execute downloaded files or activate exploit/test links. Batch independent operations and finish within eight model turns. Submit a concise evidence-grounded report with at most four findings, an explicit Verdict and confidence, historical versus live observations, and clear coverage limitations. Qualify conclusions when capture is incomplete; do not infer absence of activity from missing evidence. Cite exact Task evidence references. The host creates the offline HTML report and recorded action timeline; do not invent steps or write HTML. Omit top-level evidence_refs metadata so the host derives it. Do not ask for input.`,
    inputs: { url: `https://${url.hostname}`, browser_scope: "host" },
    acceptance_criteria: [
      "Assess the domain using actual tool evidence; separate observations from uncertainty.",
      "Capture screenshot/network evidence and close the browser session.",
      "Publish a downloadable offline HTML report with citations and limitations.",
    ],
    external_reference: "domain-mri-client",
  };
}

export async function downloadReport(fetcher, taskId, snapshot, signal) {
  for (const id of snapshot.result?.artifact_ids || []) {
    const response = await fetcher(
      `/v1/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(id)}`,
      { signal },
    );
    if (!response.ok)
      throw Error("Report download unavailable. Refresh to retry.");
    if (!response.headers.get("content-type")?.startsWith("text/html")) {
      await response.body?.cancel();
      continue;
    }
    const data = await response.arrayBuffer();
    const hash = [
      ...new Uint8Array(await crypto.subtle.digest("SHA-256", data)),
    ]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
    if (
      data.byteLength > 16 * 1024 * 1024 ||
      hash !== response.headers.get("x-adp-content-sha256")
    )
      throw Error("Report integrity check failed.");
    // Retain restrictions when opened as a Blob or downloaded, without relying
    // on HTTP headers. The embedded viewer also has an empty sandbox attribute.
    const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">`;
    return new Blob([csp, data], { type: "text/html" });
  }
  throw Error("The Task completed without an HTML report.");
}

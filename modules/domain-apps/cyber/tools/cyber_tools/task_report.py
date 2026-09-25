"""Offline cyber reports rendered from validated findings and host tool receipts."""

import base64
import hashlib
import html
import re


def esc(value):
    return html.escape(str(value), quote=True)


def render_report(*, report, context):
    summary = report.get("summary", "No verdict supplied.")
    # Only promote an explicit leading verdict; never infer safety from prose.
    match = re.match(
        r"^\s*Verdict\s*:\s*(no malicious behavior observed|malicious|suspicious|inconclusive)(?=\s|[.,;:!]|$)",
        summary,
        re.IGNORECASE,
    )
    verdict = match.group(1).upper() if match else "VERDICT NOT SPECIFIED"
    tone = {
        "MALICIOUS": "danger",
        "SUSPICIOUS": "warning",
        "NO MALICIOUS BEHAVIOR OBSERVED": "clear",
    }.get(verdict, "neutral")
    steps = context.get("steps", [])
    refs = {}
    for step in steps:
        aid = (step.get("artifact") or {}).get("artifact_id")
        if aid and aid not in refs:
            refs[aid] = {"label": f"E{len(refs) + 1}", "tool": step["tool"]}
    for ref in report.get("evidence_refs", []):
        aid = ref.get("ref")
        if aid not in refs:
            refs[aid] = {
                "label": f"E{len(refs) + 1}",
                "tool": ref.get("source", "input"),
            }

    def citations(ids):
        return " ".join(
            f'<a class="cite" href="#e-{esc(refs[aid]["label"])}">{esc(refs[aid]["label"])}</a>'
            for aid in ids
            if aid in refs
        )

    def finding(item):
        return f'<li><p>{esc(item["statement"])}</p><div class="muted">{esc(item.get("confidence", "unspecified"))} confidence {citations(item.get("evidence_refs", []))}</div></li>'

    def source_section(prefix, title, intro):
        source_steps = [s for s in steps if s["tool"].startswith(prefix)]
        source_ids = {
            (s.get("artifact") or {}).get("artifact_id") for s in source_steps
        }
        findings = [
            f
            for f in report.get("findings", [])
            if source_ids.intersection(f.get("evidence_refs", []))
        ]
        blocks = [
            f'<section id="{"archive" if prefix.endswith("common_crawl") else "browser"}"><div class="eyebrow">EVIDENCE SOURCE</div><h2>{title}</h2><p class="muted">{intro}</p>'
        ]
        if not source_steps:
            blocks.append(
                '<p class="notice">This source was not investigated in this task.</p>'
            )
        else:
            blocks.append(
                '<ul class="findings">' + "".join(map(finding, findings)) + "</ul>"
                if findings
                else "<p>No final findings were attributed to this source. Recorded tool outcomes appear below.</p>"
            )
        for step in source_steps:
            result = step.get("result") or {}
            aid = (step.get("artifact") or {}).get("artifact_id")
            citation = citations([aid])
            if step["operation_status"] != "confirmed":
                blocks.append(
                    f'<p class="notice">{esc(step["tool"])}: {esc(step["operation_status"])}. This is not confirmed evidence.</p>'
                )
                continue
            if result.get("captures"):
                captures = result["captures"][:30]
                blocks.append(
                    f'<details><summary>{len(captures)} historical captures returned {citation}</summary><div class="table-wrap"><table><thead><tr><th>Capture time (UTC)</th><th>URL</th><th>HTTP</th></tr></thead><tbody>'
                )
                for capture in captures:
                    blocks.append(
                        f'<tr><td>{esc(capture.get("fetch_time", "Unknown"))}</td><td class="url">{esc(capture.get("url", ""))}</td><td>{esc(capture.get("fetch_status", "Unknown"))}</td></tr>'
                    )
                blocks.append("</tbody></table></div></details>")
            image = result.get("image") or {}
            if image.get("media_type") in {"image/jpeg", "image/png"}:
                try:
                    raw = base64.b64decode(image.get("data", ""), validate=True)
                    magic = (
                        b"\xff\xd8\xff"
                        if image["media_type"] == "image/jpeg"
                        else b"\x89PNG\r\n\x1a\n"
                    )
                    if not 0 < len(raw) <= 12000 or not raw.startswith(magic):
                        raise ValueError("Invalid image")
                    blocks.append(
                        f'<figure><img alt="Captured browser viewport" src="data:{image["media_type"]};base64,{base64.b64encode(raw).decode()}"><figcaption>Browser screenshot preview · {esc(step["finished_at"])} {citation}<br>SHA-256: <code>{hashlib.sha256(raw).hexdigest()}</code>. Full-resolution evidence remains in the Task artifacts.</figcaption></figure>'
                    )
                except (ValueError, TypeError):
                    blocks.append(
                        '<p class="notice">Screenshot could not be embedded; consult the original evidence artifact.</p>'
                    )
            if result.get("section") in {
                "network",
                "forms",
                "scripts",
                "frames",
                "dom",
            }:
                text = result.get("text") or result.get("reason", "Section unavailable")
                blocks.append(
                    f"<details><summary>{esc(result['section'].title())} evidence {citation}</summary><pre>{esc(text[:6000])}</pre></details>"
                )
            if result.get("reason") or result.get("limitations"):
                notes = [result["reason"]] if result.get("reason") else []
                notes += result.get("limitations", [])
                blocks.append(
                    '<p class="muted">' + " ".join(esc(x) for x in notes) + "</p>"
                )
        blocks.append("</section>")
        return "".join(blocks)

    subject = context.get("inputs", {}).get("url") or "Cyber investigation"
    timeline = []
    for index, step in enumerate(steps, 1):
        payload = step.get("payload", {})
        details = " · ".join(
            f"{key}: {payload[key]}"
            for key in (
                "url",
                "match",
                "profile",
                "scope",
                "action",
                "section",
                "capture_id",
            )
            if key in payload
        )
        result = step.get("result") or {}
        status = (
            result.get("cleanup_status")
            or result.get("status")
            or step["operation_status"]
        )
        if step["operation_status"] != "confirmed":
            status = step["operation_status"]
        timeline.append(
            f'<tr><td>{index}</td><td>{esc(step["started_at"])}<br><span class="muted">to {esc(step["finished_at"])}</span></td><td><strong>{esc(step["tool"].removeprefix("cyber.").replace("_", " "))}</strong><br>{esc(details)}</td><td>{esc(status)} {citations([(step.get("artifact") or {}).get("artifact_id")])}</td></tr>'
        )
    limitations = list(report.get("uncertainties", []))
    if any(s["tool"].startswith("cyber.common_crawl") for s in steps):
        limitations.append(
            "Common Crawl is historical evidence from selected crawl partitions. A missing capture does not establish safety or domain age."
        )
    if any(s["tool"].startswith("cyber.browser") for s in steps):
        limitations.append(
            "Live observations cover the recorded time, browser profile and visited pages. They do not establish that every path, download or conditional behavior is safe. Screenshots are bounded viewport previews; network records are metadata, not a full HAR."
        )
    if context.get("steps_truncated"):
        limitations.append(
            "The action log reached its 128-operation report bound; later operations are not displayed."
        )
    if not limitations:
        limitations.append(
            "No additional limitations were supplied by the investigator. This does not establish complete coverage."
        )
    css = """*{box-sizing:border-box}body{margin:0;background:#eef2f6;color:#172b3a;font:16px/1.65 system-ui,-apple-system,Segoe UI,sans-serif}main{max-width:1120px;margin:32px auto;padding:0 24px}header{background:#102c3b;color:#fff;padding:40px;border-radius:20px}h1{font-size:32px;line-height:1.2;overflow-wrap:anywhere}h2{font-size:25px;line-height:1.3;margin:4px 0 18px}h3{font-size:18px}.eyebrow{font-size:12px;font-weight:800;letter-spacing:2px;color:#36766f}header .eyebrow{color:#8ddbc8}.meta{font-size:13px;opacity:.8;overflow-wrap:anywhere}nav{display:flex;gap:20px;flex-wrap:wrap;padding:20px 0}a{color:#176656}section{background:#fff;border:1px solid #dbe3e9;border-radius:16px;padding:30px;margin:0 0 22px}#verdict{border-top:5px solid #267c70}.verdict-banner{padding:24px;border-radius:12px;margin-bottom:24px;background:#edf1f5;color:#263d50;border-left:6px solid currentColor}.verdict-label{font-size:12px;font-weight:800;letter-spacing:2px}.verdict-banner h2{font-size:clamp(26px,4vw,40px);line-height:1.15;margin:10px 0 0;overflow-wrap:anywhere}.verdict-banner.danger{background:#fff0ef;color:#a52020}.verdict-banner.warning{background:#fff4db;color:#805000}.verdict-banner.clear{background:#e8f5ee;color:#185c3d}.subject{font-size:23px;margin:8px 0 20px;overflow-wrap:anywhere}.verdict{font-size:17px}.muted,figcaption{color:#586a77;font-size:13px}.findings{padding-left:22px}.findings li{padding:8px 0;border-bottom:1px solid #edf0f3}.findings p{margin:0}.cite{font-size:12px;font-weight:700;background:#e7f3ef;padding:2px 6px;border-radius:5px;text-decoration:none}.notice{background:#fff4dd;padding:12px;border-radius:8px}table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;color:#586a77}td,th{padding:12px 9px;border-bottom:1px solid #dfe6ec;vertical-align:top}.url,code{overflow-wrap:anywhere;word-break:break-word}.table-wrap{overflow-x:auto}figure{margin:24px 0}img{max-width:100%;height:auto;display:block;border:1px solid #dae2e8;border-radius:10px}figcaption{margin-top:8px}details{margin:14px 0;padding:14px;background:#f6f8fa;border-radius:8px}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 ui-monospace,monospace}footer{color:#586a77;font-size:12px;padding:15px 0 35px}@media(max-width:640px){main{padding:0 12px}header,section{padding:22px}h1{font-size:26px}}@media print{body{background:#fff}main{max-width:none;margin:0;padding:0}section,header{break-inside:avoid;border-radius:0}nav{display:none}details{display:block}a{color:inherit}header{color:#172b3a;background:#fff;border-bottom:3px solid #267c70}}"""
    document = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
        "<title>DOMAIN MRI</title><style>",
        css,
        "</style></head><body><main>",
        f'<header><h1>DOMAIN MRI</h1><p class="subject">{esc(subject)}</p><div class="meta">Task {esc(context.get("task_id", ""))}<br>Investigation started {esc(context.get("started_at", ""))} · timestamps in UTC</div></header>',
        '<nav aria-label="Report sections"><a href="#verdict">Verdict</a><a href="#archive">Common Crawl</a><a href="#browser">Live browsing</a><a href="#steps">Investigation steps</a><a href="#coverage">Coverage</a><a href="#evidence">Evidence index</a></nav>',
        f'<section id="verdict"><div class="verdict-banner {tone}"><div class="verdict-label">VERDICT</div><h2>{esc(verdict)}</h2></div><h3>Reasoning and confidence</h3><p class="verdict">{esc(summary)}</p><h3>Evidence supporting the assessment</h3><ul class="findings">',
        "".join(map(finding, report.get("findings", []))),
        '</ul><p class="muted">The assessment reflects the recorded evidence and stated coverage. Confidence is shown per finding.</p></section>',
        source_section(
            "cyber.common_crawl",
            "What Common Crawl showed",
            "Historical captures, capture dates and archive retrieval findings. These describe past content, not the current site.",
        ),
        source_section(
            "cyber.browser",
            "What live browsing showed",
            "Direct observations from the browser session, including visual evidence and recorded requests.",
        ),
        '<section id="steps"><div class="eyebrow">RECORDED ACTIVITY</div><h2>Steps taken on the domain</h2><p class="muted">Recorded tool calls and their outcomes, in order. This is an action log, not private model reasoning. A confirmed receipt alone does not imply a successful investigation.</p><div class="table-wrap"><table><thead><tr><th>#</th><th>Time (UTC)</th><th>Action</th><th>Outcome / evidence</th></tr></thead><tbody>',
        "".join(timeline)
        or '<tr><td colspan="4">No tool operations were recorded.</td></tr>',
        "</tbody></table></div></section>",
        '<section id="coverage"><h2>Uncertainty and coverage limits</h2><ul>',
        "".join(f"<li>{esc(x)}</li>" for x in limitations),
        "</ul><h3>Recommended next steps</h3><ul>",
        "".join(f"<li>{esc(x)}</li>" for x in report.get("recommendations", []))
        or "<li>No additional actions were recommended.</li>",
        "</ul></section>",
        '<section id="evidence"><h2>Evidence index</h2><p class="muted">References identify the immutable Task artifacts. Original evidence remains protected by the Task API; this downloaded report can be shared as a standalone file.</p>',
        "".join(
            f'<p id="e-{esc(v["label"])}"><strong>{esc(v["label"])}</strong> · {esc(v["tool"])}<br><code>{esc(k)}</code></p>'
            for k, v in refs.items()
        ),
        "</section>",
        "<footer>Generated from the validated Task report and recorded tool receipts. No external scripts, fonts or image requests are required. After download, access is controlled by how the file is shared.</footer></main></body></html>",
    ]
    content = "".join(document).encode("utf-8")
    if len(content) > 512 * 1024:
        raise ValueError("HTML report exceeds its 512 KiB bound")
    return {"content_type": "text/html", "content": content}

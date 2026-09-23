"""Capture, extend and assess a durable researcher case through the browser broker.

The existing cyber agent supplies investigation choices and evidence-linked
assessments. This module does not instantiate a model or hold browser credentials.
"""

from __future__ import annotations

import argparse
import base64
import csv
import fcntl
import html
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from browser_client import BrowserBrokerError, capture_url
from browser_guard import DestinationRefused
from case_contract import (
    Assessment,
    SCHEMA_VERSION,
    content_digest,
    digest,
    redact_url,
    sanitize,
    utcnow,
)

MAX_PROBES = 4
CASE_FILE = "case.json"


def _write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def _inconclusive(reason: str) -> dict:
    return Assessment(
        verdict="inconclusive", assessor="collection-system", limitations=[reason]
    ).model_dump()


def _validate_input(url: str) -> None:
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or len(url) > 8192:
        raise ValueError("An absolute HTTP(S) URL is required")
    if p.username is not None or p.password is not None:
        raise ValueError("Credential-bearing URLs are refused")
    if any(
        re.search(r"token|secret|password|api.?key|session|signature|^code$", k, re.I)
        for k, _ in parse_qsl(p.query)
    ):
        raise ValueError("Credential-bearing or single-use URLs are refused")


def new_case(output: Path, url: str) -> dict:
    _validate_input(url)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    case = {
        "schema_version": SCHEMA_VERSION,
        "case_id": output.name,
        "created_at": utcnow(),
        "target_url": redact_url(url),
        "subject_sha256": digest(url),
        "probes": [],
        "observations": [],
        "assessment": _inconclusive("No browser evidence has been collected"),
    }
    save_case(output, case)
    return case


def add_probe(
    output: Path,
    url: str,
    *,
    profile="desktop",
    wait_seconds=0,
    reason="Initial browser observation",
    capture=capture_url,
) -> dict:
    """Persist intent before a bounded broker call; retain errors and earlier evidence."""
    _validate_input(url)
    if (
        profile not in {"desktop", "mobile"}
        or type(wait_seconds) is not int
        or not 0 <= wait_seconds <= 15
    ):
        raise ValueError("Invalid profile or wait budget")
    if not reason.strip() or len(reason) > 1500:
        raise ValueError("A short reason for the probe is required")
    with (output / ".case.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        verify_case(output)
        case = json.loads((output / CASE_FILE).read_text())
        if case["subject_sha256"] != digest(url):
            raise ValueError("A probe must use the exact original URL")
        if len(case["probes"]) >= MAX_PROBES:
            raise ValueError("Case probe budget exhausted")
        for prior in case["probes"]:
            if prior["status"] == "running":
                prior.update(status="failed", error="interrupted_before_result")
        probe = {
            "id": f"probe-{len(case['probes']) + 1:03d}",
            "profile": profile,
            "wait_seconds": wait_seconds,
            "reason": sanitize(reason),
            "started_at": utcnow(),
            "status": "running",
        }
        case["probes"].append(probe)
        case["assessment"] = _inconclusive("New evidence requires assessment")
        save_case(output, case)
        try:
            bundle = capture(url, profile=profile, wait_seconds=wait_seconds)
            if (
                bundle.get("schema_version") != SCHEMA_VERSION
                or bundle.get("subject_sha256") != case["subject_sha256"]
            ):
                raise ValueError(
                    "Broker returned an incompatible or mismatched subject"
                )
            observations = bundle.get("observations", [])
            if not 1 <= len(observations) <= 2:
                raise ValueError("Broker returned an invalid observation count")
            for index, raw in enumerate(observations):
                if raw.get("subject_sha256") != case["subject_sha256"] or raw.get(
                    "status"
                ) not in {"complete", "partial", "failed"}:
                    raise ValueError("Broker returned an invalid observation")
                if (
                    raw.get("profile") != profile
                    or raw.get("action") != ("initial" if index == 0 else "wait")
                    or (index == 1 and not wait_seconds)
                    or not isinstance(raw.get("captured_at"), str)
                    or raw.get("content_sha256") != content_digest(raw)
                ):
                    raise ValueError("Broker returned invalid capture provenance")
                if raw["status"] == "complete" and (
                    not raw.get("screenshot_base64")
                    or not raw.get("dom_snapshot")
                    or not raw.get("visible_text", "").strip()
                    or type(raw.get("http_status")) is not int
                    or not 200 <= raw["http_status"] < 400
                    or raw.get("errors")
                    or raw.get("blocked_requests")
                    or not raw.get("network_requests")
                    or bundle.get("cleanup_status") != "stopped"
                ):
                    raise ValueError(
                        "Broker claimed completeness without capture evidence"
                    )
                o = dict(raw)
                o["id"] = f"obs-{len(case['observations']) + 1:03d}"
                o["probe_id"] = probe["id"]
                encoded = o.pop("screenshot_base64", "")
                if encoded:
                    image = base64.b64decode(encoded, validate=True)
                    if (
                        not image.startswith(b"\x89PNG")
                        or len(image) > 5 * 1024 * 1024
                        or digest(image) != o.get("screenshot_sha256")
                    ):
                        raise ValueError("Screenshot integrity check failed")
                    o["screenshot"] = o["id"] + ".png"
                    (output / o["screenshot"]).write_bytes(image)
                # Keep raw markup out of rendered reports; save it as inert text.
                dom = o.pop("dom_snapshot", "")
                if dom:
                    name = o["id"] + "-dom.txt"
                    (output / name).write_text(dom)
                    o["dom_snapshot"] = name
                    o["dom_sha256"] = digest(dom)
                case["observations"].append(sanitize(o))
                save_case(output, case)
            probe["manifest"] = sanitize(
                {k: v for k, v in bundle.items() if k != "observations"}
            )
            if bundle.get("cleanup_status") != "stopped":
                raise ValueError("Broker did not confirm session cleanup")
            probe["status"] = (
                "complete"
                if all(o["status"] == "complete" for o in observations)
                else "partial"
            )
        except Exception as exc:
            probe.update(status="failed", error=type(exc).__name__)
            if isinstance(exc, DestinationRefused):
                probe.update(
                    reason_code=exc.reason_code, diagnostic=sanitize(exc.reason)
                )
            elif isinstance(exc, (ValueError, BrowserBrokerError)):
                probe["diagnostic"] = sanitize(str(exc))[:1000]
            case["assessment"] = _inconclusive(
                f"{probe['id']} failed: {type(exc).__name__}"
            )
        finally:
            probe["completed_at"] = utcnow()
            save_case(output, case)
        return case


def assess_case(output: Path, assessment: dict) -> dict:
    with (output / ".case.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        verify_case(output)
        case = json.loads((output / CASE_FILE).read_text())
        parsed = Assessment.model_validate(assessment)
        if case.get("case_kind") == "domain_investigation" and (
            not case.get("stop_reason")
            or (
                parsed.verdict != "inconclusive"
                and (
                    case.get("unconfirmed_browser_start", False)
                    or not case.get("sessions")
                    or any(s["cleanup_status"] != "stopped" for s in case["sessions"])
                )
            )
        ):
            raise ValueError(
                "Record the stopping reason; unconfirmed cleanup permits only inconclusive assessment"
            )
        parsed.validate_evidence(case["observations"])
        if parsed.verdict == "no_adverse_behavior_observed" and any(
            p["status"] != "complete" for p in case["probes"]
        ):
            raise ValueError("Incomplete probes cannot support clearance")
        case["assessment"] = sanitize(parsed.model_dump())
        case["assessed_at"] = utcnow()
        save_case(output, case)
        return case


def indicators(case: dict) -> list[dict]:
    """Observed infrastructure with provenance, not automatically malicious IOCs."""
    rows, seen = [], set()
    for o in case["observations"]:
        destinations = [
            (r.get("url", ""), "observed_request")
            for r in o.get("network_requests", [])
        ]
        destinations += [
            (f.get("action", ""), "form_destination_not_submitted")
            for f in o.get("forms", [])
        ]
        destinations += [
            (d.get("url", ""), "download_offer") for d in o.get("downloads", [])
        ]
        for url, role in destinations:
            try:
                host = urlsplit(url).hostname
            except ValueError:
                host = None
            if not host:
                continue
            for kind, value in (("url", url), ("domain", host)):
                key = (kind, value, role, o["id"])
                if key not in seen:
                    seen.add(key)
                    rows.append(
                        dict(
                            type=kind,
                            value=value,
                            role=role,
                            evidence_id=o["id"],
                            disposition="unassessed",
                        )
                    )
        for c in o.get("connections", []):
            if c.get("connected_ip"):
                rows.append(
                    dict(
                        type="ip",
                        value=c["connected_ip"],
                        role="connected_address",
                        evidence_id=o["id"],
                        disposition="unassessed",
                    )
                )
    return rows


def _escaped(value) -> str:
    return html.escape(str(value), quote=True)


def verify_case(output: Path) -> int:
    """Detect changed/missing case files. The local manifest is not a signature."""
    manifest = json.loads((output / "manifest.json").read_text())
    files = manifest.get("files", {})
    if manifest.get("schema_version") != SCHEMA_VERSION or CASE_FILE not in files:
        raise ValueError("Invalid evidence manifest")
    for name, info in files.items():
        path = output / name
        if Path(name).name != name or path.is_symlink() or not path.is_file():
            raise ValueError("Evidence manifest contains a missing or invalid file")
        data = path.read_bytes()
        if len(data) != info["bytes"] or digest(data) != info["sha256"]:
            raise ValueError("Evidence integrity check failed")
    return len(files)


def _md(value) -> str:
    return (
        _escaped(value)
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("|", "\\|")
    )


def _investigation_report(case):
    if case.get("case_kind") != "domain_investigation":
        return [], ""
    lines = [
        "",
        "## Investigation path and hypothesis updates",
        "",
        "Scope: " + _md(case.get("scope", "host")),
        "",
    ]
    items = []
    for probe in case["probes"]:
        decision = probe.get("decision", {})
        observations = [o for o in case["observations"] if o["probe_id"] == probe["id"]]
        refs = " ".join(f'<a href="#{o["id"]}">{o["id"]}</a>' for o in observations)
        question = decision.get("question", "Inspect the seed")
        action = probe.get("action", "capture")
        reason = decision.get("reason", "")
        lines += [
            f"### {probe['id']}: {_md(action)}",
            "",
            "Question: " + _md(question),
            "",
            "Reason: " + _md(reason),
            "",
        ]
        updates = []
        for review in case.get("reviews", []):
            if review["probe_id"] != probe["id"]:
                continue
            lines += [
                f"Hypothesis ({review['outcome']}): {_md(review['hypothesis'])}",
                "",
                _md(review["explanation"]),
                "",
                "Next question: " + _md(review["next_question"]),
                "",
            ]
            updates.append(
                f"<p><strong>{_escaped(review['outcome'])}:</strong> "
                f"{_escaped(review['hypothesis'])}</p><p>{_escaped(review['explanation'])}</p>"
                f"<p><small>Next question: {_escaped(review['next_question'])}</small></p>"
            )
        items.append(
            f"<li><h3>{_escaped(action)} {refs}</h3>"
            f"<p><strong>Question:</strong> {_escaped(question)}</p>"
            f"<p>{_escaped(reason)}</p>" + "".join(updates) + "</li>"
        )
    stop = case.get("stop_reason", "Investigation remains open")
    lines += ["Stopping reason: " + _md(stop), ""]
    leads = case.get("external_leads", [])
    if leads:
        lines += ["### External or unavailable leads", ""]
        lines += [f"- {_md(x['url'])} ({_md(x['observation_id'])})" for x in leads]
    lead_html = "".join(
        f"<li>{_escaped(x['url'])} <small>{_escaped(x['observation_id'])}</small></li>"
        for x in leads
    )
    markup = (
        "<h2>Investigation path and hypothesis updates</h2>"
        f"<p>Scope: {_escaped(case.get('scope', 'host'))}</p><ol>"
        + "".join(items)
        + f"</ol><p><strong>Stopping reason:</strong> {_escaped(stop)}</p>"
        + (
            f"<details><summary>External or unavailable leads ({len(leads)})</summary><ul>{lead_html}</ul></details>"
            if leads
            else ""
        )
    )
    return lines, markup


def save_case(output: Path, case: dict) -> None:
    """JSON is authoritative. Reports are regenerated from the same captured evidence."""
    _write_json(output / CASE_FILE, case)
    rows = indicators(case)
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=["type", "value", "role", "evidence_id", "disposition"]
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                k: "'" + v if v.startswith(("=", "+", "-", "@")) else v
                for k, v in row.items()
            }
        )
    (output / "indicators.csv").write_text(buffer.getvalue())
    a = case["assessment"]
    lines = [
        f"# URL research case: {a['verdict']}",
        "",
        f"Target: {_md(case['target_url'])}",
        "",
        "Observed infrastructure is unassessed until a finding supports its relevance.",
        "",
        "## Findings",
        "",
    ]
    cards = []
    for f in a["findings"]:
        citations = ", ".join(f"[{i}](#{i})" for i in f["evidence_ids"])
        lines.append(f"- {_md(f['statement'])} ({f['basis']}; {citations})")
    for o in case["observations"]:
        lines += [
            "",
            f'<a id="{o["id"]}"></a>',
            f"## {o['id']} — {o['profile']} / {o['action']}",
            "",
            f"{o['captured_at']} · {o['status']} · HTTP {o['http_status']}",
            "",
            _md(o.get("page_title", "")),
            "",
            f"Final URL: {_md(o.get('final_url', 'unavailable'))}",
            "",
        ]
        facts = (
            f"{len(o.get('network_requests', []))} network events · "
            f"{len(o.get('redirects', []))} redirect/navigation events · "
            f"{len(o.get('forms', []))} forms · "
            f"{len(o.get('downloads', []))} download offers"
        )
        lines += [facts, ""]
        form_rows = []
        for form in o.get("forms", []):
            fields = ", ".join(
                str(f.get("type", "unknown")) for f in form.get("fields", [])
            )
            destination = form.get("action", "")
            method = form.get("method", "")
            lines.append(
                f"- Form (not submitted): {_md(method)} {_md(destination)}; fields: {_md(fields)}"
            )
            form_rows.append(
                f"<li>{_escaped(method)} {_escaped(destination)}; field types: {_escaped(fields)}</li>"
            )
        picture = ""
        if o.get("screenshot"):
            lines.append(f"![{o['id']}]({o['screenshot']})")
            picture = f'<a href="{o["screenshot"]}"><img src="{o["screenshot"]}" alt="{o["id"]}"></a>'
        lines.append(_md(o.get("visible_text", "")[:2000]))
        lines += [
            "",
            f"Errors: {_md(', '.join(o.get('errors', [])) or 'none recorded')}",
        ]
        cards.append(
            f'<section id="{o["id"]}"><h2>{o["id"]}: {_escaped(o["profile"])} / {_escaped(o["action"])}</h2>'
            f"<p>{_escaped(o['captured_at'])} · {_escaped(o['status'])} · HTTP {o['http_status']}</p>"
            f"<p>Final URL: {_escaped(o.get('final_url', 'unavailable'))}</p><p>{facts}</p>"
            f"<h3>{_escaped(o.get('page_title', ''))}</h3>{picture}"
            + (
                "<h3>Forms (not submitted)</h3><ul>" + "".join(form_rows) + "</ul>"
                if form_rows
                else ""
            )
            + f"<pre>{_escaped(o.get('visible_text', ''))}</pre>"
            f"<p>Coverage errors: {_escaped(', '.join(o.get('errors', [])) or 'none recorded')}</p>"
            f"<details><summary>Structured observation</summary><pre>{_escaped(json.dumps(o, indent=2))}</pre></details></section>"
        )
    investigation_lines, investigation_html = _investigation_report(case)
    lines += investigation_lines
    lines += ["", "## Limitations", ""] + [f"- {_md(x)}" for x in a["limitations"]]
    limits = set(a["limitations"])
    for probe in case["probes"]:
        limits.update(probe.get("manifest", {}).get("limitations", []))
        if probe["status"] != "complete":
            limits.add(
                f"{probe['id']}: {probe['status']} ({probe.get('error', 'partial evidence')})"
            )
    lines += [f"- {_md(x)}" for x in sorted(limits - set(a["limitations"]))]
    lines += ["", "## Recommended actions", ""] + [
        f"- {_md(x)}" for x in a["recommended_actions"]
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    findings = "".join(
        f"<li>{_escaped(f['statement'])} <small>({_escaped(f['basis'])})</small> "
        + " ".join(f'<a href="#{i}">{i}</a>' for i in f["evidence_ids"])
        + "</li>"
        for f in a["findings"]
    )
    (output / "report.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'\">"
        "<title>URL research case</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px;background:#f6f8fa;color:#17212b;overflow-wrap:anywhere}"
        "section{background:white;padding:24px;margin:24px 0;border:1px solid #d0d7de;overflow-wrap:anywhere}img{max-width:100%;max-height:420px;border:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere}"
        "small{color:#57606a}</style>"
        f"<h1>{_escaped(a['verdict'])}</h1><p>{_escaped(case['target_url'])}</p>"
        '<p><a href="case.json">Case JSON</a> · <a href="indicators.csv">Observed indicators CSV</a></p>'
        f"<h2>Findings</h2><ul>{findings}</ul><details><summary>Investigation choices and provenance</summary><pre>{_escaped(json.dumps(case['probes'], indent=2))}</pre></details>"
        + investigation_html
        + "".join(cards)
        + "<h2>Limitations</h2><ul>"
        + "".join(f"<li>{_escaped(x)}</li>" for x in sorted(limits))
        + "</ul><h2>Recommended actions</h2><ul>"
        + "".join(f"<li>{_escaped(x)}</li>" for x in a["recommended_actions"])
        + "</ul></html>"
    )
    # Hash the actual persisted bytes. A manifest is integrity evidence, not a signature.
    files = {
        p.name: {"sha256": digest(p.read_bytes()), "bytes": p.stat().st_size}
        for p in sorted(output.iterdir())
        if p.is_file()
        and p.name != "manifest.json"
        and not p.name.startswith(".")
        and not p.name.endswith(".tmp")
    }
    _write_json(
        output / "manifest.json", {"schema_version": SCHEMA_VERSION, "files": files}
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("capture", "probe"):
        p = sub.add_parser(command)
        p.add_argument("url")
        p.add_argument(
            "--output" if command == "capture" else "--case",
            type=Path,
            required=True,
            dest="output",
        )
        p.add_argument("--profile", choices=["desktop", "mobile"], default="desktop")
        p.add_argument("--wait-seconds", type=int, default=0)
        p.add_argument(
            "--reason",
            default="Initial browser observation" if command == "capture" else None,
            required=command == "probe",
        )
    p = sub.add_parser("assess")
    p.add_argument("--case", type=Path, required=True, dest="output")
    p.add_argument("--assessment", type=Path, required=True)
    p = sub.add_parser("verify")
    p.add_argument("--case", type=Path, required=True, dest="output")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            with (args.output / ".case.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                count = verify_case(args.output)
            print(json.dumps({"verified_files": count}))
            return 0
        if args.command == "assess":
            case = assess_case(args.output, json.loads(args.assessment.read_text()))
        else:
            if args.command == "capture":
                new_case(args.output, args.url)
            case = add_probe(
                args.output,
                args.url,
                profile=args.profile,
                wait_seconds=args.wait_seconds,
                reason=args.reason,
            )
        print(
            json.dumps(
                {
                    "case": str(args.output),
                    "verdict": case["assessment"]["verdict"],
                    "observations": len(case["observations"]),
                }
            )
        )
        return 2 if case["assessment"]["verdict"] == "inconclusive" else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": sanitize(str(exc))}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""adp-cred CLI entry point.

Usage:
  python -m adp_cred list
  python -m adp_cred http GET https://api.github.com/user --service github
  python -m adp_cred materialize --service ssh-key-prod
  python -m adp_cred raw --service github --purpose "gh CLI auth"
"""

from __future__ import annotations

import json
import sys

from adp_cred.client import list_credentials, materialize, proxy_http, raw_read


def _usage() -> None:
    print(
        "Usage:\n"
        "  adp-cred list\n"
        "  adp-cred http METHOD URL --service SERVICE [--label LABEL] [--header KEY:VALUE ...]\n"
        "  adp-cred materialize --service SERVICE [--label LABEL]\n"
        "  adp-cred raw --service SERVICE [--label LABEL] [--purpose PURPOSE]\n",
        file=sys.stderr,
    )
    sys.exit(2)


def cmd_list() -> None:
    """List available credentials."""
    creds = list_credentials()
    print(json.dumps(creds, indent=2))


def cmd_http(args: list[str]) -> None:
    """Proxy an HTTP request."""
    if len(args) < 2:
        print("error: http requires METHOD and URL", file=sys.stderr)
        _usage()

    method = args[0].upper()
    url = args[1]
    service: str | None = None
    label: str | None = None
    headers: dict[str, str] = {}
    body: str | None = None

    i = 2
    while i < len(args):
        if args[i] == "--service" and i + 1 < len(args):
            service = args[i + 1]
            i += 2
        elif args[i] == "--label" and i + 1 < len(args):
            label = args[i + 1]
            i += 2
        elif args[i] == "--header" and i + 1 < len(args):
            k, _, v = args[i + 1].partition(":")
            headers[k.strip()] = v.strip()
            i += 2
        elif args[i] == "--body" and i + 1 < len(args):
            body = args[i + 1]
            i += 2
        else:
            print(f"error: unknown argument: {args[i]}", file=sys.stderr)
            _usage()

    if not service:
        print("error: --service is required for http", file=sys.stderr)
        _usage()

    result = proxy_http(method, url, service, label=label, headers=headers or None, body=body)
    print(json.dumps(result, indent=2))


def cmd_materialize(args: list[str]) -> None:
    """Materialize a file-type credential."""
    service: str | None = None
    label: str | None = None

    i = 0
    while i < len(args):
        if args[i] == "--service" and i + 1 < len(args):
            service = args[i + 1]
            i += 2
        elif args[i] == "--label" and i + 1 < len(args):
            label = args[i + 1]
            i += 2
        else:
            print(f"error: unknown argument: {args[i]}", file=sys.stderr)
            _usage()

    if not service:
        print("error: --service is required for materialize", file=sys.stderr)
        _usage()

    result = materialize(service, label=label)
    print(json.dumps(result, indent=2))


def cmd_raw(args: list[str]) -> None:
    """Read raw credential value — prints value to stdout."""
    service: str | None = None
    label: str | None = None
    purpose: str | None = None

    i = 0
    while i < len(args):
        if args[i] == "--service" and i + 1 < len(args):
            service = args[i + 1]
            i += 2
        elif args[i] == "--label" and i + 1 < len(args):
            label = args[i + 1]
            i += 2
        elif args[i] == "--purpose" and i + 1 < len(args):
            purpose = args[i + 1]
            i += 2
        else:
            print(f"error: unknown argument: {args[i]}", file=sys.stderr)
            _usage()

    if not service:
        print("error: --service is required for raw", file=sys.stderr)
        _usage()

    result = raw_read(service, label=label, purpose=purpose)
    # Print only the raw value to stdout (pipeable)
    print(result["value"], end="")


def main() -> None:
    if len(sys.argv) < 2:
        _usage()

    command = sys.argv[1]
    rest = sys.argv[2:]

    if command == "list":
        cmd_list()
    elif command == "http":
        cmd_http(rest)
    elif command == "materialize":
        cmd_materialize(rest)
    elif command == "raw":
        cmd_raw(rest)
    else:
        print(f"error: unknown command: {command}", file=sys.stderr)
        _usage()


if __name__ == "__main__":
    main()

"""Bounded IMDSv2 reachability probe. Emits no response bodies or tokens."""

import http.client
import socket
import sys


def reachable(host):
    conn = http.client.HTTPConnection(host, timeout=3)
    try:
        conn.request(
            "PUT",
            "/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        response = conn.getresponse()
        # An HTTP denial still proves the endpoint is reachable. In particular,
        # IMDSv2's unauthenticated 401 cannot establish network isolation.
        if response.status != 200:
            return True
        token = response.read(2049)
        if not token or len(token) > 2048:
            raise ValueError("invalid token response")
        token = token.decode("ascii")
        if any(c.isspace() for c in token):
            raise ValueError("invalid token response")
        conn.close()
        conn = http.client.HTTPConnection(host, timeout=3)
        try:
            conn.request(
                "GET",
                "/latest/meta-data/iam/security-credentials/",
                headers={"X-aws-ec2-metadata-token": token},
            )
            conn.getresponse()  # Discard the entire credential/role response.
        except (OSError, http.client.HTTPException):
            pass  # The successful token exchange already proved reachability.
        return True
    except (TimeoutError, socket.timeout, ConnectionRefusedError, ConnectionResetError):
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        status = "REACHABLE" if reachable(sys.argv[1]) else "UNREACHABLE"
    except Exception:
        print("ADP_IMDS_ERROR")
        sys.exit(2)
    print("ADP_IMDS_" + status)

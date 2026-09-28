"""Retire the superseded raw Bedrock chat harness when PMM enforcement starts."""
import json
import os
import re
import urllib.request

import botocore.auth
import botocore.awsrequest
import botocore.session


def require_legacy_chat_admission():
    # Deployment opts in with the current chat rollout; every actual raw call
    # rereads committed gateway posture. Environment telemetry is never policy.
    if os.environ.get('ADP_CHAT_MODEL_POLICY_ENABLED', 'false').lower() != 'true':
        return
    endpoint = os.environ.get('ADP_AGENT_CONTROL_ENDPOINT', '').rstrip('/')
    match = re.fullmatch(r'https://[a-z0-9]+\.execute-api\.([a-z0-9-]+)\.amazonaws\.com/[A-Za-z0-9_-]+/agent/internal/v1/agent', endpoint)
    try:
        if not match:
            raise ValueError()
        credentials = botocore.session.get_session().get_credentials().get_frozen_credentials()
        if not credentials.token:
            raise ValueError()
        url = endpoint + '/legacy-chat-preflight'
        signed = botocore.awsrequest.AWSRequest(method='POST', url=url, data=b'')
        botocore.auth.SigV4Auth(credentials, 'execute-api', match.group(1)).add_auth(signed)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(urllib.request.Request(url, data=b'', headers=dict(signed.headers), method='POST'), timeout=5) as response:
            raw = response.read(1025)
            if response.status != 200 or len(raw) > 1024:
                raise ValueError()
            result = json.loads(raw)
        if result.get('legacy_permitted') is not True or result.get('posture') not in {'disabled', 'report_only'}:
            raise ValueError()
    except Exception:
        raise RuntimeError('Legacy chat model authority refused; use the current chat worker') from None

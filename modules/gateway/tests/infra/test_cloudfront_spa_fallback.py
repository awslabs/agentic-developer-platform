"""Guards the CloudFront SPA-fallback arrangement introduced by issue #4386.

Background. The distribution used to carry a single distribution-wide
`custom_error_response` mapping any `403` to `200 /index.html`. It existed for SPA
deep links, because the S3 origin (OAC without `s3:ListBucket`) answers `403` for a
missing key. But `custom_error_response` is a distribution-level argument — it
cannot be scoped to a cache behavior or an origin — so it also rewrote genuine
authorization denials from the API origin. `POST /api/orchestration/gates/<id>/approve`
by a caller without `plan:approve` reached the browser as `200` plus SPA HTML: the
backend denied it and logged the denial, but every client, acceptance test, and
external monitor was told the request succeeded.

SPA routing now happens on viewer-request via `aws_cloudfront_function.spa_fallback`,
attached to the S3 default behavior only, so S3 is only asked for keys that exist and
the `403` never occurs.

These tests parse the Terraform source rather than a plan because the regression they
guard is a *textual* one — someone re-adding a block that looks harmless — and because
running `terraform plan` needs AWS credentials and the managed-policy data sources.
"""

import re
from pathlib import Path

_MODULE_DIR = Path(__file__).resolve().parents[2] / "infra" / "modules" / "cloudfront"
_MAIN_TF = _MODULE_DIR / "main.tf"
_SPA_FUNCTION_JS = _MODULE_DIR / "functions" / "spa-fallback.js"


def _strip_comments(hcl: str) -> str:
    """Drop `#` and `//` line comments so prose about a block never matches as code."""
    out = []
    for line in hcl.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def _extract_block(hcl: str, header: str) -> str:
    """Return the text of the brace-balanced block whose opening line contains `header`."""
    start = hcl.index(header)
    depth = 0
    for i in range(start, len(hcl)):
        if hcl[i] == "{":
            depth += 1
        elif hcl[i] == "}":
            depth -= 1
            if depth == 0:
                return hcl[start : i + 1]
    raise AssertionError(f"unbalanced braces after {header!r} in {_MAIN_TF}")


def _main_tf_code() -> str:
    return _strip_comments(_MAIN_TF.read_text())


class TestNoDistributionWide403Rewrite:
    """The masking rule must not come back."""

    def test_cloudfront_module_has_no_distribution_wide_403_rewrite(self):
        """No `custom_error_response` at all — the block cannot be scoped, so any
        instance of it risks rewriting API status codes."""
        code = _main_tf_code()
        assert "custom_error_response" not in code, (
            "A `custom_error_response` block is back in the CloudFront module. It is "
            "distribution-wide and cannot be scoped to the S3 origin, so it will also "
            "rewrite the API origin's status codes — that was issue #4386, where every "
            "403 permission denial reached clients as 200 + SPA HTML. Use the "
            "`spa_fallback` viewer-request function on the default behavior instead."
        )

    def test_no_error_code_403_mapped_to_response_code_200(self):
        """Narrower assertion on the exact shape, so the failure message points at the
        specific 403-to-200 mapping even if the block is spelled unusually."""
        code = _main_tf_code()
        has_403 = re.search(r"error_code\s*=\s*403", code)
        has_200 = re.search(r"response_code\s*=\s*200", code)
        assert not (has_403 and has_200), (
            "Found a 403 -> 200 error mapping in the CloudFront module (issue #4386). Authorization denials must reach the caller as 403."
        )


class TestSpaFallbackScoping:
    """The replacement must be attached to the S3 behavior and nowhere else."""

    def test_spa_fallback_function_resource_exists(self):
        code = _main_tf_code()
        assert 'resource "aws_cloudfront_function" "spa_fallback"' in code
        assert 'file("${path.module}/functions/spa-fallback.js")' in code, "spa_fallback should load its code from functions/spa-fallback.js"

    def test_spa_fallback_source_rewrites_to_index_html(self):
        js = _SPA_FUNCTION_JS.read_text()
        assert "function handler(event)" in js
        assert "/index.html" in js
        assert "request.uri" in js

    def test_spa_fallback_is_associated_with_the_s3_behavior_only(self):
        """Proves the fix is scoped: present on `default_cache_behavior`, absent from
        every `ordered_cache_behavior` (which are the /api/*, /.well-known/*, and
        /gitlab/* behaviors targeting the API and GitLab origins)."""
        code = _main_tf_code()

        default_behavior = _extract_block(code, "default_cache_behavior {")
        assert "aws_cloudfront_function.spa_fallback.arn" in default_behavior, (
            "spa_fallback must be attached to default_cache_behavior or SPA deep links will 403 instead of loading the app."
        )
        assert re.search(r'event_type\s*=\s*"viewer-request"', default_behavior), (
            "spa_fallback must run on viewer-request so S3 is asked for a key that exists"
        )

        # Every ordered_cache_behavior targets a non-S3 origin; none may carry the rewrite.
        header = 'dynamic "ordered_cache_behavior" {'
        matches = list(re.finditer(re.escape(header), code))
        assert matches, "expected the /api/*, /.well-known/* and /gitlab/* behaviors"
        for match in matches:
            block = _extract_block(code[match.start() :], header)
            assert "spa_fallback" not in block, (
                "spa_fallback is attached to an ordered_cache_behavior. API and GitLab behaviors must not rewrite URIs to /index.html (issue #4386)."
            )

    def test_api_behaviors_still_strip_api_prefix(self):
        """Regression guard for the edit that added the association: the pre-existing
        strip_api_prefix function must still be attached to the API behaviors, or
        every /api/* call breaks."""
        code = _main_tf_code()
        assert code.count("aws_cloudfront_function.strip_api_prefix.arn") == 2, (
            "Expected strip_api_prefix on exactly the /api/* and /.well-known/* behaviors; the count changed."
        )

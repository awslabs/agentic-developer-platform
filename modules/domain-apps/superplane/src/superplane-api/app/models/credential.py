"""Credential models — registry, vault assignments, audit log (sections 15.3, 15.7)."""

import html
import re
import unicodedata
import urllib.parse
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database import Base

# The ARN and secret-value rules below mirror `superplane_contracts.secrets`
# (`_ARN_PATTERN`, `_SECRET_VALUE_PATTERNS`) as plain values rather than importing them.
#
# This is the arrangement `app/services/provisioning.py` documents, but NOT for the
# reason an earlier version of both comments gave. They claimed `superplane_contracts`
# is "genuinely not importable at API runtime" because it lives above the pinned build
# context; that was inaccurate — it was merely unstaged, and issue #5053 staged it
# (`scripts/stage-domain-auth.sh`, with the Dockerfile failing by name if it is
# missing). Three modules in this tree were already importing it at runtime when that
# claim was written.
#
# The reason to keep mirroring HERE is narrower and still holds: `alembic/env.py`
# imports `app.models`, so an import in this file would make the contract package a
# requirement of the migration runner as well as the API. A migration that cannot run
# because a validation-rule package is missing is a worse failure than a duplicated
# regex, and the duplication is covered by a test that fails on drift.
#
# A mirror can drift, so `tests/test_models.py::TestTheMirroredSecretRulesMatchTheContract`
# compares these patterns against the contract's and asserts the one deliberate divergence
# (the `sk-` entry below) explicitly. Any *other* difference fails that test, so drift
# cannot leave this copy quietly enforcing a weaker rule than the contract it mirrors.

# Why these use lookarounds and not `\b`
#
# The contract's copies start with `\b`, which asserts a non-word/word transition. That is
# satisfied by a *separator*, so it is silently suppressed by a leading **word** character:
# `\barn:` does not match `_arn:aws:...` or `credarn:aws:...` at all. `_` is a word
# character, so a single underscore defeats the rule while `value.split("arn:")[1]` still
# recovers the complete address — and `secret_arn:aws:...` is exactly what the old column
# name joined to its old value produces during this very rename. The same suppression
# applies to a trailing character for the `...\b` end: `AKIA...EXAMPLE_` escaped too.
#
# `(?<![A-Za-z0-9])` / `(?![A-Za-z0-9])` assert the absence of an adjacent *alphanumeric*
# instead. Underscores, hyphens and any other punctuation therefore no longer hide a token,
# while the patterns still do not fire mid-word inside a longer alphanumeric run.
#
# That change alone was still not enough, and the reason is worth stating plainly: swapping
# `\b` for a negative lookbehind narrowed the hole from "any word character" to "any
# alphanumeric character" but did not close it. `xAKIAIOSFODNN7EXAMPLE` was still stored,
# and `stored[1:]` recovers the key. So for the shapes whose prefix is itself distinctive
# --- `AKIA`/`ASIA`, `gh[pousr]_`, `xox[abposr]-`, `Bearer` --- there is no left lookaround
# at all: a preceding character cannot make those sequences innocent, and dropping it was
# measured to cost nothing on a legitimate-handle corpus.
#
# `sk-` is the one shape that keeps its left lookaround, because `sk` is a common English
# word ending: without it, ordinary handles like `risk-<20+ chars>`, `task-...`, `desk-...`
# and `ask-...` are refused. There the boundary is load-bearing rather than decorative.

# Any ARN, in any partition (`arn:aws-cn:`, `arn:aws-us-gov:`) and for any service. A KMS
# or SSM-parameter ARN is the same class of pointer-into-the-account as a secretsmanager
# one, so the `arn:` scheme is matched rather than a service enumeration. Unanchored on the
# left: no legitimate opaque handle contains `arn:<partition>:<service>:<region>:`, so there
# is nothing to protect by requiring a boundary before it.
#
# The segment class admits `.` and `_` as well as `-`. A narrower `[a-z0-9-]` class meant
# `arn:aws:secrets_manager:us-east-1:1:secret:n` and the `secrets.manager` form did not
# match the structure at all, while still reading as a perfectly usable ARN. Widening the
# class was measured to add no false positives: a legitimate handle does not contain four
# colon-separated segments beginning with the literal `arn`.
_ARN_PATTERN = re.compile(r"arn:[a-z0-9._-]*:[a-z0-9._-]+:[a-z0-9._-]*:", re.IGNORECASE)

# Value shapes that are secret material wherever they appear. Mirrored from the contract,
# with the boundary strengthened as described above and one change to the `sk-` body.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # AWS access key id — AKIA (long-lived) and ASIA (temporary session). No boundary
    # assertion on either side, and this is the one shape where the *trailing* assertion
    # mattered: the body is a fixed `{16}`, so `AKIAIOSFODNN7EXAMPLEx` was stored and
    # `stored[:-1]` recovers the key. The open-ended bodies below (`{16,}`, `{10,}`,
    # `{20,}`) are greedy and simply absorb a trailing character, so their lookahead is
    # already a no-op against this evasion.
    #
    # Measured cost of dropping it, over 200k generated handles per format: 0 false
    # positives for ULID/Crockford-base32, 0 for uppercase hex, 0 for uuid4, and 5 for
    # arbitrary uppercase base32 (~1 in 40k) — a handle would have to contain a literal
    # `AKIA`/`ASIA` followed by exactly 16 more uppercase alphanumerics. That refusal is
    # loud and immediate at registration time, whereas the alternative is silently storing
    # a working access key id, so the trade is taken deliberately in that direction.
    # Case-sensitive deliberately, unlike the ARN pattern above. `akiaiosfodnn7example` is
    # not a working access key id -- AWS key ids are case-sensitive uppercase, and no
    # decode step recovers the uppercase form -- so matching case-insensitively guards
    # nothing. It is not free either: over 300k generated handles it refused
    # `u3pXKcVTL63jCtJRfakIaTObUB2FgWClh5Vftqnizabt`, where `akIaTObUB2FgWClh5Vft` is an
    # incidental mixed-case run. The trade that justifies the uppercase false refusals
    # below -- refuse loudly rather than silently store a live key -- does not carry here,
    # because there is no live key to store.
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    # PEM private key block of any flavour (RSA, EC, OPENSSH, PGP).
    re.compile(r"-----BEGIN(?: [A-Z]+)* PRIVATE KEY-----"),
    # GitHub tokens: personal, OAuth, user-to-server, server-to-server, refresh.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}(?![A-Za-z0-9])"),
    # Slack tokens. Requiring a 10+ unbroken alphanumeric run in the final segment is kept
    # rather than raised, because the repo's own corpus for this shape
    # (`tools/superplane-mcp/tests/test_no_secret_in_tool_result.py`) uses a 12-character
    # final segment, and a threshold above that would stop recognising the token the
    # outbound scrubber is tested against. The cost is that a `xox[abposr]-`-prefixed word
    # slug whose last segment is 10+ letters is refused as well. That is a deliberate
    # trade: a reference beginning with a Slack token prefix is not a shape any vault issues,
    # so the false refusal is reachable only by choosing a handle that impersonates one.
    re.compile(r"xox[abposr]-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{10,}(?![A-Za-z0-9])"),
    # A Bearer credential embedded in a header-ish string.
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{16,}=*", re.IGNORECASE),
    # Generic provider API keys, e.g. `sk-...` style. The contract's `[A-Za-z0-9]{20,}`
    # body stops at the first hyphen, so it matches no real `sk-ant-api03-...` key. Simply
    # admitting hyphens in the body overshoots the other way: it then matches any
    # hyphenated slug of 20+ characters containing `sk-`, so legitimate handles like
    # `sk-prod-nebius-credential-ref1` would be refused and the credential would be
    # unregisterable. Hyphenated *segments* are allowed, but a long unbroken alphanumeric
    # run is still required — which is the part a word slug does not have.
    re.compile(r"(?<![A-Za-z0-9])sk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{20,}(?![A-Za-z0-9])"),
    # Second `sk-` tier, unanchored on the left, closing the affix hole the tier above
    # cannot: `xsk-ant-api03-...` keeps its lookbehind satisfied by the `x`, so a one-
    # character prefix laundered a real provider key. Unanchoring the tier above instead
    # is not an option -- `sk` is a common English word ending, so it would refuse
    # `risk-<20+>`, `task-...`, `desk-...`, `ask-...`.
    #
    # Length is the only available tradeoff, because the prefix alone cannot:
    # `risk-01HQ8V3XK2WERTYUIOPASDFGH` and `xsk-ant-api03-<40>` are the same shape to a
    # regex. A 30+ unbroken run catches long keys but also refuses `risk-<30+>`
    # and `task-<30+>` handles. Shorter word slugs remain registerable; this is
    # a deliberate false-refusal cost of catching long prefixed provider keys.
    #
    # A laundered key whose unbroken run is 20-29 characters is still accepted.
    re.compile(r"sk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{30,}"),
)

# Interior whitespace, and escaped forms of the structure.
#
# The rules above were hardened three times against a leading or trailing affix. All three
# rounds tested *prefixes and suffixes*, and the matrices are prefix/suffix only — so none
# of them could see that the same laundering works from the **inside**, where no boundary
# assertion is involved at all. A single space placed anywhere inside a real ARN leaves
# every pattern above unmatched:
#
#     'arn:aws:secretsmanager:us-east-1 :123456789012:secret:nebius-AbCdEf'   was stored
#     re.sub(r"\s+", "", stored) == the real ARN                              -> True
#
# Exhaustive single-character insertion at every position of a real secret ARN found 32
# such accepted values, every one of them restored byte-for-byte by a whitespace strip.
# It is not confined to ARNs: `'AKIA IOSFODNN7EXAMPLE'` was stored and strips back to a
# live AWS access key id, so the secret-shape rules are bypassable the same way.
#
# Escaping is the same evasion one encoding layer up: the ARN pattern matches a literal
# `:`, so any escape of the structure walked past it while a consumer that decodes — a
# query string, a webhook payload, a K8s annotation, a JSON body — gets the exact address
# back. See `_JSON_UNICODE_ESCAPE_PATTERN` below for why the whole escape form is decoded
# rather than the single `%3A` sequence this rule originally handled.
#
# An ADP credential ID is an opaque printable handle: it has no legitimate interior
# whitespace and no legitimate escape sequences to decode. Measured cost
# of both rules over 480,000 generated legitimate handles (uuid4, uuid hex, ULID/Crockford
# base32, 44-char base62, `adp/cred/...` and `vault:secret/data/...` path forms, and the
# `risk-`/`task-`/`sk-prod-...` word-slug handles the earlier rounds were careful to keep
# registerable): **0 false refusals** for either. Neither costs anything real.
_INTERIOR_WHITESPACE_PATTERN = re.compile(r"\s")
_WHITESPACE_RUN_PATTERN = re.compile(r"\s+")

# Escape forms a realistic consumer decodes, applied before the ARN and secret rules read
# the string so an escaped address is refused for the accurate reason -- it is an ARN --
# rather than as a vaguer "suspicious encoding".
#
# Decoding ONLY `%3A` was the first version of this rule, and it was the same mistake the
# earlier rounds of this validator made three times over: it narrowed the hole to one
# character instead of closing the class. A consumer that percent-decodes does not decode
# selectively, so `%61rn:aws:...` (an encoded `a`) survived while `urllib.parse.unquote`
# restored the exact ARN. HTML entities and JSON `\uXXXX` escapes are the same evasion in
# the other two encodings a payload realistically passes through.
#
# So the whole escape form is decoded rather than one sequence of it. Measured cost over
# 330,000 generated legitimate handles: 0 false refusals -- an opaque handle has no
# legitimate reason to carry a percent-escape, an HTML entity or a unicode escape.
_JSON_UNICODE_ESCAPE_PATTERN = re.compile(r"\\u([0-9a-fA-F]{4})")

# A reference is an opaque printable handle. Zero-width and control characters have no
# legitimate place in one, and they are how an ARN gets past a naive prefix check: a
# leading U+200B makes `value.startswith("arn:")` false while the string still reads as an
# ARN to everything downstream that strips or ignores it.
_INVISIBLE_PATTERN = re.compile(
    "["
    "\x00-\x1f"  # C0 controls, including NUL/newline/tab
    "\x7f-\x9f"  # DEL and C1 controls
    "\u00ad"  # soft hyphen
    "\u200b-\u200f"  # zero-width space/joiners, LTR/RTL marks
    "\u2028\u2029"  # line/paragraph separators
    "\u202a-\u202e"  # bidi embedding/override
    "\u2060-\u2064"  # word joiner, invisible operators
    "\ufeff"  # zero-width no-break space (BOM)
    "]"
)


MAX_ADP_REFERENCE_LENGTH = 255
MAX_ADP_REFERENCE_COUNT = 64
_MAX_DECODE_ROUNDS = 129
_MAX_RECOVERABLE_FORMS = 2 * _MAX_DECODE_ROUNDS + 2
_MAX_RECOVERABLE_FORM_LENGTH = 4096


def validate_adp_credential_id(value: str) -> str:
    """Reject anything that is a secret ARN or a secret value rather than a reference.

    Issue #5046 (U13b). The schema rule is that a domain record holds an opaque ADP
    credential ID and never secret material or a copied address for it. A column rename
    alone does not enforce that: `adp_credential_id` is a string column, so an ARN fits in
    it just as well as it fit the old one, and the defect would return under a compliant
    column name. This is the check that makes the rename mean something.

    Deliberately a *shape* check and nothing more. It cannot establish that a well-formed
    reference is resolvable -- that ADP owns the credential, that account and KMS
    permissions allow reading it, or that rotation and revocation reach it. Those are
    separate facts verified only by the audited vault-owned migration (R7 acc. 6-7,
    deferred to U7). Passing this validator means "not a secret ARN", not "usable".

    Raises:
        ValueError: if the value is empty, contains invisible or control characters, embeds
            an ARN anywhere in the string, matches a known secret shape, or is too long to
            be a reference.

    The 255-character bound matches the registry and connection columns; 129 decode
    rounds cover every percent nesting that fits (127 layers plus a fixed-point check).
    At most 260 recoverable forms of 4096 characters each may be examined; exceeding
    either bound fails closed. Normalization never changes the stored reference.
    """
    if not isinstance(value, str):
        raise ValueError(
            "adp_credential_id must be a non-empty ADP credential reference; "
            "an empty reference resolves to nothing"
        )
    if len(value) > MAX_ADP_REFERENCE_LENGTH:
        raise ValueError(
            "adp_credential_id must be a short opaque reference, not secret material "
            f"(got {len(value)} characters). This record must never hold a "
            "credential value."
        )
    if not value.strip():
        raise ValueError(
            "adp_credential_id must be a non-empty ADP credential reference; "
            "an empty reference resolves to nothing"
        )

    candidate = value.strip()

    # Checked first, and kept as its own message: a PEM block reaching this column is the
    # clearest form of the defect this validator exists to stop, so it is worth naming as
    # key material specifically rather than as the generic "secret material" below. It also
    # has to precede the invisible-character rule, because a PEM block legitimately
    # contains newlines and would otherwise be refused for the wrong stated reason.
    if "PRIVATE KEY" in candidate.upper():
        raise ValueError(
            "adp_credential_id looks like key material, not a reference. This record "
            "must never hold a credential value."
        )

    # Checked before the ARN and secret rules, because those rules read the string: a
    # zero-width or control character inside the value is what lets an ARN past a check
    # that anchors on the start of the string, and it has no legitimate place in an opaque
    # handle regardless. Rejecting rather than stripping keeps the stored value exactly
    # what the submitter sent, so it cannot be normalized into something different.
    invisible = _INVISIBLE_PATTERN.search(candidate)
    if invisible is not None:
        raise ValueError(
            "adp_credential_id must not contain invisible or control characters (found "
            f"U+{ord(invisible.group()):04X}). An ADP credential ID is a printable opaque "
            "handle; such characters exist here only to disguise another value."
        )

    # Checked before the ARN and secret rules for the same reason as the rule above, and
    # closing the same class of evasion one level deeper. The patterns below match ASCII
    # `arn`, `:` and `[a-z0-9._-]`, so any confusable substitution walks straight past them
    # while `unicodedata.normalize("NFKC", stored)` reproduces the original **byte for
    # byte**: `arn:aws:secretsmanager:...` written with U+FF1A fullwidth colons, or with a
    # fullwidth `ａ`, was stored and then recovered exactly by anything that normalizes --
    # a JSON/YAML round-trip, a K8s label sanitizer, a non-Python client, or an operator
    # copying the value out of the UI. That is the ExternalSecret path this class exists to
    # close.
    #
    # Rejecting non-ASCII outright is the fix rather than normalizing before matching: an
    # ADP credential ID is an opaque handle, so it has no legitimate need for a character
    # outside ASCII, and refusing the whole class removes the confusable question at the
    # root instead of depending on one normal form catching every homoglyph. NFKC would
    # miss the Cyrillic and combining-mark forms, which are not NFKC-equivalent to ASCII.
    non_ascii = next((ch for ch in candidate if not ch.isascii()), None)
    if non_ascii is not None:
        raise ValueError(
            "adp_credential_id must be printable ASCII (found "
            f"U+{ord(non_ascii):04X}). An ADP credential ID is an opaque handle, so a "
            "non-ASCII character appears here only to disguise another value: a "
            "fullwidth or homoglyph character can normalize back into an exact ARN."
        )

    # The forms a consumer can recover from `candidate`, tested by the ARN and secret rules
    # below *in addition to* the literal value.
    #
    # Those rules match contiguous text, so a mutation placed **inside** a value defeats
    # them with no boundary involved -- which is why three rounds of prefix/suffix hardening
    # could not see it. Rather than only refusing the mutation, each recoverable form is run
    # through the same rules, so an interior-spaced ARN is still reported as an **ARN** and a
    # split access key id as **secret material**. The accurate reason is what an operator
    # acts on, and it is also what the existing tests assert.
    #
    # Only used for matching. The value is never rewritten, so nothing is normalized into
    # storage and the stored string stays exactly what the submitter sent.
    # The literal and each decoded form are tested with and without NFKC normalization
    # and whitespace removal. Unstripped forms still matter for `Bearer\s+...`.
    #
    # Decoded to a FIXED POINT, not a fixed number of layers. Applying the three decoders
    # once each left `%253A` accepted -- `unquote` turns it into `%3A`, which needs a second
    # pass -- and that is the identical "narrowed the hole, left the class open" mistake as
    # decoding only `%3A` was. A consumer that decodes twice recovers the exact ARN, so the
    # loop runs until the string stops changing.
    #
    # Normalization can itself reveal another escape marker (a decoded fullwidth percent
    # sign becomes `%`). Decode both normalized and unnormalized forms until neither
    # operation discovers a new form; refuse rather than accept on budget exhaustion.
    decoded_forms = {candidate}
    pending_forms = [candidate]
    for _ in range(_MAX_RECOVERABLE_FORMS):
        if not pending_forms:
            break
        form = pending_forms.pop()
        for next_form in (
            unicodedata.normalize("NFKC", form),
            urllib.parse.unquote(
                html.unescape(
                    _JSON_UNICODE_ESCAPE_PATTERN.sub(
                        lambda match: chr(int(match.group(1), 16)), form
                    )
                )
            ),
        ):
            if len(next_form) > _MAX_RECOVERABLE_FORM_LENGTH:
                raise ValueError("ADP credential reference contains excessive escaping")
            if next_form not in decoded_forms:
                decoded_forms.add(next_form)
                pending_forms.append(next_form)
    if pending_forms:
        raise ValueError("ADP credential reference contains excessive escaping")

    recoverable_forms = tuple(
        {
            form
            for base in decoded_forms
            for form in (base, _WHITESPACE_RUN_PATTERN.sub("", base))
        }
    )

    # A secret value must never reach this column. Length alone is not the signal: the
    # credentials most likely to be pasted here -- an AWS access key id, a GitHub PAT, a
    # Slack or provider API key -- are all shorter than the 255-char column, so a
    # length-only rule accepts precisely the values this record must never hold. The
    # recognizable shapes are matched explicitly, and length remains as the backstop for
    # material with no distinctive prefix (a PEM block, a long opaque blob).
    #
    # Ordered before the ARN rule on purpose, and the message deliberately quotes nothing.
    # The ARN branch below echoes the first 32 characters so an operator can see which
    # value was refused, which is safe for an ARN but would reflect a live secret back to
    # the caller in a 422 -- and into the logs -- for a value that contained both (e.g. a
    # pasted `"<access-key-id> <role-arn>"` pair). Whichever rule fires first decides what
    # is echoed, so the non-echoing rule has to win.
    # Tested against every recoverable form, so `'AKIA IOSFODNN7EXAMPLE'` -- which strips
    # back to a live access key id -- is refused here as secret material rather than
    # slipping through to a vaguer rule.
    for pattern in _SECRET_VALUE_PATTERNS:
        if any(pattern.search(form) for form in recoverable_forms):
            raise ValueError(
                "adp_credential_id looks like secret material, not a reference. This "
                "record must never hold a credential value; store the opaque ADP "
                "credential ID that the vault resolves instead."
            )

    # Any ARN, not just a secretsmanager one. `arn:aws-cn:`, `arn:aws-us-gov:` and a
    # KMS/SSM-parameter ARN are all addresses of resources this record must not name, so
    # matching the `arn:` scheme is simpler than enumerating services.
    #
    # Searched, not prefix-matched: `"cred arn:aws:secretsmanager:..."` carries the whole
    # address just as usefully as the bare ARN does, and a leading character defeats a
    # `startswith` test while leaving the ARN intact for anything that later trims it.
    #
    # Not a strict superset of the old `startswith("arn:")` test, and worth being precise
    # about rather than claiming otherwise: this requires the colon-separated structure, so
    # a bare fragment like `"arn:aws"` or `"arn:aws:secretsmanager"` now passes where the
    # prefix test refused it. Those carry no account id and no resource name, so they are
    # not the address this record must not hold -- whereas every *complete* ARN the prefix
    # test caught is still caught, along with the embedded and prefixed forms it missed.
    # Searched across the recoverable forms for the same reason as the secret rules above:
    # an interior space or an escaped character hides the structure from a literal match
    # while a consumer that strips or decodes recovers the address byte for byte.
    arn_match = next(
        (m for m in (_ARN_PATTERN.search(form) for form in recoverable_forms) if m),
        None,
    )
    if arn_match is not None:
        # Echoes only the *matched* ARN prefix, never the submitted string. Quoting
        # `candidate[:32]` here was a disclosure: rule order stops a *recognized* secret
        # from reaching this branch, but a credential with no distinctive prefix (a bare
        # 40-char AWS secret access key, a JWT) matches none of the shape patterns, so
        # `"<secret> <role-arn>"` reflected 32 characters of live secret into a 422 body
        # and into the logs. The matched span is structural -- through the 5th colon, so
        # no resource or secret name -- and is the part an operator needs to see.
        raise ValueError(
            f"adp_credential_id must not be an ARN (matched {arn_match.group()!r}). A "
            "copied secret ARN is a second reference to secret material living outside "
            "the ADP vault, so vault rotation and revocation would no longer reach it. "
            "Store the opaque ADP credential ID instead."
        )

    # Residual rule, deliberately placed AFTER the ARN and secret rules rather than before
    # them. Those rules now see through whitespace, so anything reaching this line is not a
    # recognized ARN or secret shape in any recoverable form -- and letting them run first is
    # what keeps an interior-spaced ARN reported as an ARN instead of as "whitespace", which
    # is the reason an operator can act on.
    #
    # It still has to exist: an opaque handle has no legitimate interior whitespace, and a
    # value containing it is either malformed or is breaking up material the shape patterns
    # do not recognize (a bare AWS secret access key, a JWT). Measured at 0 false refusals
    # over 480k generated legitimate handles, so refusing the class costs nothing.
    interior_ws = _INTERIOR_WHITESPACE_PATTERN.search(candidate)
    if interior_ws is not None:
        raise ValueError(
            "adp_credential_id must not contain whitespace (found "
            f"U+{ord(interior_ws.group()):04X}). An ADP credential ID is a single opaque "
            "handle; whitespace inside one exists only to break up another value, and a "
            "consumer that strips it recovers that value exactly."
        )

    return candidate


class CredentialRegistry(Base):
    """Org-level credential registry — stores an ADP credential reference only.

    Issue #5046 (U13b), R7 schema half. This record holds an **ADP credential ID**:
    an opaque handle that only the ADP vault can resolve back into usable credential
    material. It holds no secret value and no copied secret ARN.

    Why the previous shape was not sufficient, since "ARNs, never values" reads like it
    already was: a secret's ARN is its address in AWS Secrets Manager, so a copy of it
    is a second, independent route to the secret material living outside the vault. The
    vault could rotate that credential to a new value or revoke it entirely and nothing
    would reach the copy — `vault_sync.py` wrote this column straight into a workload
    cluster's ExternalSecret, so the cluster pulled the secret directly and the vault
    never saw it happen. Removing the address is what makes the vault the single owner.

    `kms_key_id` was dropped alongside it, not merely the ARN. That column existed only
    to decrypt a secret this record no longer reads; retaining the decryption key for
    material the domain must not resolve is the same leak in a different column.

    What this class does NOT establish: that any given `adp_credential_id` is actually
    resolvable — that ADP owns the credential, that account/KMS permissions let ADP read
    it, and that rotation and revocation work through the reference. A reference's shape
    and its resolvability are separate facts. Verifying the second is the audited
    vault-owned migration (R7 acc. 6-7), which is deferred live work under U7, and
    `alembic/versions/011_*.py` refuses to guess at it rather than assuming it.
    """

    __tablename__ = "credential_registry"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "adp_credential_id",
            name="uq_credential_registry_org_adp_credential",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # nebius, lambda, coreweave
    friendly_name: Mapped[str] = mapped_column(String(255), nullable=False)
    credential_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # api_key, service_account, oauth_token
    # The ADP vault's opaque handle for this credential. Deliberately NOT an ARN and not
    # a value: the vault resolves it, and this schema grants Superplane no read access to
    # ADP secrets. 255 chars matches the other opaque-identifier columns in this schema
    # (e.g. organizations.cognito_sub) and is well clear of an ADP credential ID; the old
    # 512 width existed to fit a full ARN, which is exactly what must no longer be stored.
    adp_credential_id: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="Active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @validates("adp_credential_id")
    def _check_adp_credential_id(self, _key: str, value: str) -> str:
        """Enforce the schema rule at assignment time, before any flush can persist it.

        Placed on the model rather than only in the request schema so the rule holds for
        every writer -- a router, a reconciler, a backfill script or a test fixture --
        instead of only for traffic that happens to arrive through the validated API.
        """
        return validate_adp_credential_id(value)


class ClusterVaultAssignment(Base):
    """Which credentials are assigned to which cluster."""

    __tablename__ = "cluster_vault_assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=False
    )
    credential_registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credential_registry.id"), nullable=False
    )
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="Pending")


class CredentialAuditLog(Base):
    """Audit trail for credential access."""

    __tablename__ = "credential_audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    credential_registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credential_registry.id"), nullable=False
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=True
    )
    accessed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

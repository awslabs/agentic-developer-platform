# Final observed rollout reconciliation

Conservative open workload findings: **59 unique Critical /377 unique High**,
versus59/389 at the reviewed frozen baseline. Open image/package occurrences are
**2861 versus3231**, a net reduction of370. These are package vulnerability
findings, including pending applicability review, not confirmed exploitability.

The10:50UTC capture has32 observed active digests, all mapped to verified immutable
scans. New observed images include ARC runner0C/105H, chat0C/0H, gateway0C/50H,
Context MCP0C/50H and LiteLLM0C/50H raw matches. The exact Debian zlib unaffected
review is rebound to installed matching binaries in the three Python images.
Old MCP/LiteLLM image references still attached to active pods remain counted.

The observed-only subset is56C/359H and2613 occurrences. It is NOT used as the
closure total: DeepWiki is still desired but has no verified current imageID, so
its248 previous findings remain open. The capture also retains9 missing runtime
imageID rows,34 incomplete owner chains and7 desired-template rows without active
observations. Missing/unreadable/pending execution is not zero exposure.

The main gateway rollout was independently verified earlier with authenticated
smoke (#6542); later workflow-built gateway/chat/ARC image digests were scanned
again. Context MCP and LiteLLM match our already published/scanned candidates,
with one ready updated replica each in the collected deployment state. Other
candidate images are not deducted as live fixes. The frozen all-scope64C/425H
baseline remains historical evidence and is not relabeled as a fresh all-scope
total. Scanner DB and unchanged-digest baseline scans remain frozen.

`summary.json` retains all exact references, dispositions and coverage gaps;
`scan-receipts.json` retains image/config and scanner hashes. `reconcile.py`
reproduces the join from local retained inputs and fails on an unscanned observed
image. No original baseline or raw scanner report was overwritten.

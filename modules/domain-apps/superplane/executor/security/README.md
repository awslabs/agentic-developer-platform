# Executor scan build input

The October 6 critical scan found that `ADP_SECURITY_EXECUTOR_PYTHON_IMAGE` still selected an obsolete Debian base. The executor and detached acceptance fixture now use the maintained Python 3.12.15 input identified in `python-base-review.json`. The recipe continues to require an explicit `PYTHON_IMAGE`; customer builds are not tied to a platform registry default.

The base has an independent build-input review and preserves the original executable paths and Python distribution inventory. Both resulting artifacts have zero raw critical findings and 63 raw high findings. The executor passes its nonroot, read-only tool/import/SQLite checks. The detached acceptance driver accepts its valid input and rejects an altered input. High findings and live deployment remain separate work.

The executor ECR lifecycle retains tagged build inputs so later application publications cannot delete a pinned dependency. Only untagged images expire after 14 days. Five Terraform mock tests pass, including preservation of the other repositories’ retention limits. No full infrastructure apply is required for the narrow lifecycle repair.

#!/usr/bin/env bash
# =============================================================================
# Bootstrap canary-benign corpus for YARA rule validation
# =============================================================================
# Issue #272: Creates a small corpus of known-benign files to validate YARA
# rules against. Any rule that fires on >5% of this corpus is quarantined.
#
# Usage:
#   ./bootstrap-canary-corpus.sh [--upload]
#
# With --upload, pushes the corpus to S3. Without it, just stages locally.
# =============================================================================
set -euo pipefail

BUCKET="${BUCKET:-adp-dev-cape-assets}"
PREFIX="yara-rules/canary-benign"
STAGING_DIR="/tmp/canary-benign-corpus"
AWS_REGION="${AWS_REGION:-us-east-1}"
UPLOAD=false

if [[ "${1:-}" == "--upload" ]]; then
  UPLOAD=true
fi

echo "=== Bootstrap canary-benign corpus ==="
echo "Staging dir: $STAGING_DIR"

rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

# ---------------------------------------------------------------------------
# Collect known-benign binaries from the local system
# ---------------------------------------------------------------------------
COLLECTED=0

collect_file() {
  local src="$1"
  local dest_name="$2"

  if [ -f "$src" ]; then
    cp -f "$src" "$STAGING_DIR/$dest_name"
    COLLECTED=$((COLLECTED + 1))
    echo "  [+] $dest_name ($src)"
  else
    echo "  [-] SKIP: $src not found"
  fi
}

echo ""
echo "Collecting Linux system binaries..."

collect_file /bin/ls                "linux-ls"
collect_file /bin/bash              "linux-bash"
collect_file /bin/cat               "linux-cat"
collect_file /bin/grep              "linux-grep"
collect_file /bin/find              "linux-find"
collect_file /bin/cp                "linux-cp"
collect_file /bin/mv                "linux-mv"
collect_file /bin/rm                "linux-rm"
collect_file /bin/chmod             "linux-chmod"
collect_file /bin/date              "linux-date"

echo ""
echo "Collecting standard utilities..."

collect_file /usr/bin/python3       "linux-python3"
collect_file /usr/bin/curl          "linux-curl"
collect_file /usr/bin/wget          "linux-wget"
collect_file /usr/bin/git           "linux-git"
collect_file /usr/bin/ssh           "linux-ssh"
collect_file /usr/bin/env           "linux-env"
collect_file /usr/bin/head          "linux-head"
collect_file /usr/bin/tail          "linux-tail"
collect_file /usr/bin/sort          "linux-sort"
collect_file /usr/bin/wc            "linux-wc"
collect_file /usr/bin/xargs         "linux-xargs"
collect_file /usr/bin/tee           "linux-tee"
collect_file /usr/bin/tr            "linux-tr"
collect_file /usr/bin/cut           "linux-cut"
collect_file /usr/bin/awk           "linux-awk"
collect_file /usr/bin/sed           "linux-sed"

# Libraries (known-benign shared objects)
collect_file /lib/x86_64-linux-gnu/libc.so.6    "linux-libc"
collect_file /lib/x86_64-linux-gnu/libm.so.6    "linux-libm"
collect_file /lib/x86_64-linux-gnu/libpthread.so.0 "linux-libpthread"

# If docker/kubectl/aws CLI available on this host
collect_file /usr/local/bin/docker   "linux-docker"
collect_file /usr/local/bin/kubectl  "linux-kubectl"
collect_file /usr/local/bin/aws      "linux-aws"

echo ""
echo "Collected: $COLLECTED files"

# ---------------------------------------------------------------------------
# Generate MANIFEST.json
# ---------------------------------------------------------------------------
echo ""
echo "Generating MANIFEST.json..."

MANIFEST="$STAGING_DIR/MANIFEST.json"

# Build JSON array of file entries
FILES_JSON="["
FIRST=true
for f in "$STAGING_DIR"/*; do
  [ -f "$f" ] || continue
  FNAME=$(basename "$f")
  [ "$FNAME" = "MANIFEST.json" ] && continue

  SHA=$(sha256sum "$f" | cut -d' ' -f1)
  SIZE=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null)

  if [ "$FIRST" = true ]; then
    FIRST=false
  else
    FILES_JSON+=","
  fi

  FILES_JSON+="{\"name\":\"$FNAME\",\"sha256\":\"$SHA\",\"size\":$SIZE,\"source\":\"local system binary\"}"
done
FILES_JSON+="]"

# Write manifest
cat > "$MANIFEST" << EOF
{
  "description": "Canary-benign corpus for YARA rule false-positive validation",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "file_count": $COLLECTED,
  "purpose": "Any YARA rule that matches >5% of these files is quarantined as too broad",
  "files": $FILES_JSON
}
EOF

echo "MANIFEST.json written ($COLLECTED entries)"

# ---------------------------------------------------------------------------
# Upload to S3 (optional)
# ---------------------------------------------------------------------------
if [ "$UPLOAD" = true ]; then
  echo ""
  echo "Uploading to s3://$BUCKET/$PREFIX/ ..."
  aws s3 sync "$STAGING_DIR/" "s3://$BUCKET/$PREFIX/" --region "$AWS_REGION"
  echo "Upload complete."
  echo ""
  echo "Verify:"
  echo "  aws s3 ls s3://$BUCKET/$PREFIX/ --region $AWS_REGION"
  echo "  aws s3 cp s3://$BUCKET/$PREFIX/MANIFEST.json - --region $AWS_REGION | jq .file_count"
else
  echo ""
  echo "Staged at: $STAGING_DIR"
  echo "To upload: $0 --upload"
  echo "Or: aws s3 sync $STAGING_DIR/ s3://$BUCKET/$PREFIX/ --region $AWS_REGION"
fi

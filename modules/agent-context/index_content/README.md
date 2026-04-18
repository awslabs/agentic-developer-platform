# Index Content

This folder controls what gets indexed into the Code Intelligence Platform (OpenViking + Sourcebot).

## How to add repos

1. Edit `repos.txt` -- add one `org/repo` per line
2. Push to main
3. GitHub Actions automatically triggers ingestion

## Format

- One repo per line: `org/repo`
- Comments start with `#`
- Empty lines are ignored

## What happens on push

When `repos.txt` changes on main:
1. The workflow diffs the change to find added/removed repos
2. Each new repo is sent to OpenViking via `add_resource`
3. Sourcebot config is updated with the full repo list
4. Sourcebot is restarted to pick up the new config
5. OpenViking processes repos asynchronously (AST extraction, L0/L1 summaries, embeddings)
6. Processing takes 1-2 hours for the full batch

## Manual full re-index

Go to Actions > "Ingest Content into Code Intelligence Platform" > Run workflow > check "Re-ingest all repos"

## Checking status

- Workflow runs: https://github.com/aws-innovate/projects/actions/workflows/ingest-content.yml
- Local check: `./scripts/check-index-status.sh`

## Files

| File | Description |
|------|-------------|
| `repos.txt` | GitHub repos to index (`org/repo` format) |
| `docs.txt` | (future) Documentation URLs to index |

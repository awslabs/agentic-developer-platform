# Codex Engine Smoke Test

Today's date: 2026-07-28

```python
def is_leap_year(year: int) -> bool: return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
```

## Engine evidence

Verbatim from Codex CLI session output:

- **model**: `openai.gpt-5.5`
- **provider**: `adp-gateway` (ADP Gateway → bedrock-mantle passthrough → `us-east-1`)
- **session id**: `019fa82e-a8b5-76e2-b4b4-f59f6a4f6afb`
- **tokens**: input=28536, output=473, reasoning=122
- **exit code**: 0 (success)
- **CLI version**: Codex 0.145.0
- **web_search**: disabled (PR #3903)

### Authorship

The `is_leap_year` function above was authored by Codex (`openai.gpt-5.5` on
Bedrock via ADP Gateway). The `## Engine evidence` section was backfilled by
the supervising Claude agent from the wrapper's session trailer.

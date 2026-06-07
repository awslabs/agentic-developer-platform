# gbrain Evaluation Experiment

**ExperimentId**: `gbrain-eval-2026-06`
**Duration**: 4 weeks (2026-06-09 to 2026-07-07)
**Owner**: Operations team
**Status**: Setup

## Hypothesis

ADP agent personas perform measurably better on repeated tasks when they can recall prior experiences via gbrain's synthesis + knowledge-graph layer, compared to current cold-start behavior.

## Success Criteria

| # | Criterion | How to measure | Target |
|---|---|---|---|
| 1 | Repeat-task time reduction | Run 5 previously-completed tasks with/without gbrain. Compare wall-clock time. | >= 20% reduction |
| 2 | Repeat-task error reduction | Count diagnostic retries + wrong approaches on repeated tasks. | >= 30% reduction |
| 3 | Cross-persona knowledge reuse | Search gbrain for cases where persona A's learning was retrieved by persona B. | >= 2 instances |
| 4 | Contradiction detection | Check dream cycle logs for flagged contradictions. | >= 1 genuine finding |
| 5 | Operational cost | Check AWS Cost Explorer filtered by ExperimentId tag. | <= $100/mo |

## Fail Criteria (Immediate Kill)

- gbrain MCP uptime < 95% (CloudWatch HealthyHostCount on internal ALB)
- Agent task failure directly caused by gbrain returning bad data
- Monthly cost > $200

## Evaluation Protocol

### Week 1-2: Baseline Collection

1. Deploy gbrain (this module)
2. Run smoke test to verify operation
3. Seed with existing learnings from `agent_learning/`
4. Enable gbrain for operations persona only (`GBRAIN_ENABLED=true`)
5. Monitor: uptime, response latency, dream cycle success

### Week 3-4: Controlled Comparison

1. Select 5 closed issues from `gh issue list --state closed --label agent-operations` (completed in past 30 days)
2. For each issue, create duplicate test issue with `[EXPERIMENT]` prefix
3. Run twice per issue:
   - **Trial A** (control): `GBRAIN_ENABLED=false`, agent completes from scratch
   - **Trial B** (treatment): `GBRAIN_ENABLED=true`, agent has gbrain recall
4. Record per trial: time-to-completion, turns used, errors encountered, retries, final quality score (manual review)
5. Compare A vs B across all 5 tasks

### Metrics Collection

- **Uptime**: CloudWatch `HealthyHostCount` metric on ECS service
- **Latency**: CloudWatch Logs Insights query on gbrain request duration
- **Cost**: AWS Cost Explorer, tag filter `ExperimentId=gbrain-eval-2026-06`
- **Quality**: Manual review of agent outputs (1-5 scale)
- **Dream cycle**: EventBridge invocation success + CloudWatch logs

## Decision Framework

After 4 weeks:

| Outcome | Action |
|---------|--------|
| All 5 criteria met | Promote gbrain to production module, integrate deeply with all personas |
| 3-4 criteria met | Extend experiment 2 weeks with targeted improvements |
| 1-2 criteria met | Analyze what worked, salvage learnings, teardown infrastructure |
| 0 criteria met or fail criteria triggered | Teardown, document why, close EPIC |

## Teardown Procedure

See `scripts/teardown.sh` — full automated cleanup.

Manual verification after teardown:
```bash
aws resourcegroupstaggingapi get-resources \
  --tag-filters Key=ExperimentId,Values=gbrain-eval-2026-06 \
  --query 'ResourceTagMappingList[].ResourceARN' --output text
# Expected: empty
```

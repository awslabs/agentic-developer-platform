# Story designs and implementation backlog

The [S14 provider expansion](stories/s14.md) adds SonarQube, Jenkins, Argo CD,
GitLab, PagerDuty, Linear and Slack after #9437 and before the live campaign. The 13-story tables below preserve the
original baseline; S14 is an explicit supplement, not a renumbering.

**Delivery amendment:** [The POC flow](delivery-flow/README.md) uses code-only handoffs and one coordinated release campaign. Its [file plans](delivery-flow/planned-files.md) and [44-pair acceptance map](delivery-flow/acceptance-mapping.json) preserve all original criteria. Implementation checkpoints are not per-story deployment barriers.

This is the complete design backlog for the existing 13 stories, not newly filed or dispatched work. The [canonical architecture](architecture.md) governs every design. All existing EC/AC/SCN IDs and live completion boundaries remain; S02/S10/S11 add three criterion/scenario pairs for optional deployment (44 ACs/44 SCNs total); proposed selectors are not executed tests. The requesting owner authorized design merge on 2026-10-27 with the [review amendments](review-amendments.md); S01 unresolved prerequisites still block affected execution.

## Dependency and handoff index

| Alias / existing issue | Owned outcome and exact supporting design | Epic criteria | Native prerequisites | Independent acceptance boundary |
|---|---|---|---|---|
| S01 [#8888](https://github.com/aws-e/adp/issues/8888) | [Scope, reuse and evaluation contract](stories/s01.md) | EC-01 | none | existing story completion boundary |
| S02 [#8889](https://github.com/aws-e/adp/issues/8889) | [Optional connector infrastructure and IAM collection runtime](stories/s02.md) | EC-02, EC-10 | #8888, #8901, #8919 | cell/IAM harness before provider/history |
| S03 [#8890](https://github.com/aws-e/adp/issues/8890) | [Profile-source collection and normalization](stories/s03.md) | EC-02 | #8888, #8889, #8903 | existing story completion boundary |
| S04 [#8891](https://github.com/aws-e/adp/issues/8891) | [Scoped immutable observation admission](stories/s04.md) | EC-03 | #8374, #8375, #8888, #8890 | existing story completion boundary |
| S05 [#8892](https://github.com/aws-e/adp/issues/8892) | [Exact requirement, test and delivery chain](stories/s05.md) | EC-04 | #8891 | existing story completion boundary |
| S06 [#8893](https://github.com/aws-e/adp/issues/8893) | [Real work/cost publication](stories/s06.md) | EC-05 | #8374, #8375, #8891, #8920 | existing story completion boundary |
| S07 [#8894](https://github.com/aws-e/adp/issues/8894) | [Operational, freshness and isolation proof](stories/s07.md) | EC-06, EC-10 | #8889, #8890, #8892, #8893 | existing story completion boundary |
| S08 [#8895](https://github.com/aws-e/adp/issues/8895) | [Reuse decisions and verified cleanup](stories/s08.md) | EC-07 | #8894 | existing story completion boundary |
| S09 [#8901](https://github.com/aws-e/adp/issues/8901) | [Pinned custom builds and immutable artifacts](stories/s09.md) | EC-02 | #8888 | baseline build/publication before functional consumers |
| S10 [#8902](https://github.com/aws-e/adp/issues/8902) | [Reused Go discovery through ADP authority](stories/s10.md) | EC-08 | #8888, #8889, #8901 | existing story completion boundary |
| S11 [#8903](https://github.com/aws-e/adp/issues/8903) | [Profile onboarding enrichment](stories/s11.md) | EC-09 | #8902 | existing story completion boundary |
| S12 [#8919](https://github.com/aws-e/adp/issues/8919) | [Cell and policy authority](stories/s12.md) | EC-10 | #8888 | controlled registry before S02 |
| S13 [#8920](https://github.com/aws-e/adp/issues/8920) | [Fresh source/evidence authority when serving](stories/s13.md) | EC-10 | #8374, #8375, #8891, #8919 | controlled admitted fixtures before S06 |

## Ordered implementation checkpoints

1. S01 resolves owners/registered scope, provider/policy/field maps and reproducible evaluation bounds. S09 source build and S12 controlled authority implementation can then proceed independently.
2. S09 first qualifies its artifact finalizer/ledger before explicitly selected publication. S02 adds separate optional infrastructure roots/state and the default-off/connector-only/full mode matrix; ordinary ADP installation does not invoke them. S02 extends the same finalizer for runtime resources before provisioning, applies the schema manifest and accepts cell routing/DB/TLS/IAM with a bounded real harness. This does not wait for S10 provider integration. S10 then reuses extracted provider code; S11 proves saved Profile UI/API/CLI without Insights.
3. S03 uses explicit grants, DML-only workers, fenced generations and durable immutable seals; normalized source oracles verify the handoff. S04 integrates the shared #8374/#8375 sink; those owners retain their functional/flow gates. #8373 remains required for historical catalog provenance, not a replacement by today’s head.
4. S05 exact chain and S13 controlled admitted-fixture reader run independently after S04. S06 real work/cost publication consumes S13. No edge from S13 acceptance back to S06 is introduced.
5. S07 exercises the complete live chain and 48-hour soak; S08 reviews three separate reuse decisions and independently verifies owned cleanup. None is accepted by a design-only PR. Independently, terminal failure/abort/expiry at any resource-owning stage triggers finalization without waiting for S07/S08; original failed/not-run outcomes remain.

## Shared ownership and dependency qualification

The native POC graph is preserved. External prerequisite edges are recorded in [dependency data](dependencies.json); dependency closure includes #8373/#8374/#8375 and I21/I22/I23. A dependency is satisfied by its named capability/evidence, not the open/closed state of an entire unrelated epic. No flow activation, sequencing label or dispatch is part of this package.

The architecture owner must reconcile #7295’s older store/gateway wording and #8239/#8375’s V13 handoff with the explicit live deferral. These are owner decisions at existing gates, not circular requirements to finish the full Insights epic before controlled POC coding.

## Completion evidence ledger

Each story retains stable existing AC/SCN IDs and original obligations. The historical review amendments and subsequent [optional deployment amendment](optional-deployment.md) list changed and newly added criteria. Historical baseline hashes remain in story-contracts.json. S01 owns the final matrix of `EC -> child AC -> scenario -> run/attempt -> requirement/source/code/image/policy revision -> independent evidence -> cleanup -> acceptance decision`. Required blocked/not-run rows remain incomplete. Public reports contain sanitized references only.

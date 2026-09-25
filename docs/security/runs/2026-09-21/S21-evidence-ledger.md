# S21 — Consolidated evidence ledger

Work package **S21**, issue [#5620](https://github.com/aws-e/adp/issues/5620), daily epic [#5599](https://github.com/aws-e/adp/issues/5599), AWS epic [#5677](https://github.com/aws-e/adp/issues/5677).

## 1. Current reconciliation snapshot

This revision refreshes the existing ledger's owner joins and issue/PR states. It supersedes the 2026-09-24 status narrative; that historical snapshot remains in git history at `209762ff0`. It does not replace original scan records or represent a new clean scan.

Snapshot: **2026-09-25T08:57:52.624237+00:00**; repository read: `209762ff0`. Machine-readable joins, full merge hashes and disposition checksum: [S21-reconciliation-status.json](evidence/S21-reconciliation-status.json).

**19/24 AWS + 17/21 daily = 36/45 package issues closed.** The nested AWS epic is counted once. Open packages: S12 #5611, S13 #5612, S18 #5617, S21 #5620, A03 #5655, A11 #5666, A12 #5668, A13 #5669, A14 #5670.

**Issue closed, code merged, validation recorded, deployed protection and final acceptance are separate states.** Package closure alone does not establish that an original finding is fixed or that an older ticket can close. The tables below report GitHub issue state; findings retain their original owners. A PR cross-reference alone is not acceptance evidence. The listed successor PRs identify concrete review work without claiming an exhaustive PR history.

## 2. Package state and reviewed change references

A merged reference carries its merge commit; OPEN references remain pending review/CI/merge. Existing scoped validation is retained in the individual package dispositions under this directory. This evidence-only update performs no new runtime validation or deployment.

| Package | Issue | Issue state | Reviewed/main or pending implementation references |
|---|---|---|---|
| S01 | #5600 | CLOSED | [#5725](https://github.com/aws-e/adp/pull/5725) merged `76c91c24` |
| S02 | #5601 | CLOSED | [#5783](https://github.com/aws-e/adp/pull/5783) merged `27a5b943` |
| S03 | #5602 | CLOSED | [#5724](https://github.com/aws-e/adp/pull/5724) merged `c742c087` |
| S04 | #5603 | CLOSED | [#5774](https://github.com/aws-e/adp/pull/5774) merged `ebde4b40` |
| S05 | #5604 | CLOSED | [#5704](https://github.com/aws-e/adp/pull/5704) merged `9614f881` |
| S06 | #5605 | CLOSED | [#5701](https://github.com/aws-e/adp/pull/5701) merged `ccff2bea` |
| S07 | #5606 | CLOSED | [#5706](https://github.com/aws-e/adp/pull/5706) merged `32c06c1a` |
| S08 | #5607 | CLOSED | [#5753](https://github.com/aws-e/adp/pull/5753) merged `4275564c` |
| S09 | #5608 | CLOSED | [#5765](https://github.com/aws-e/adp/pull/5765) merged `c2553b5f` |
| S10 | #5609 | CLOSED | [#5890](https://github.com/aws-e/adp/pull/5890) merged `d0b8089d`; [#6027](https://github.com/aws-e/adp/pull/6027) merged `2eee18c2` |
| S11 | #5610 | CLOSED | [#6081](https://github.com/aws-e/adp/pull/6081) merged `7f1c8d7c` |
| S12 | #5611 | OPEN | [#6062](https://github.com/aws-e/adp/pull/6062) merged `700bacb9`; [#6084](https://github.com/aws-e/adp/pull/6084) OPEN |
| S13 | #5612 | OPEN | [#6053](https://github.com/aws-e/adp/pull/6053) merged `85d8e6e6`; [#5959](https://github.com/aws-e/adp/pull/5959) OPEN |
| S14 | #5613 | CLOSED | [#5850](https://github.com/aws-e/adp/pull/5850) merged `51a78e34`; [#6066](https://github.com/aws-e/adp/pull/6066) merged `e0ba2fad` |
| S15 | #5614 | CLOSED | [#5851](https://github.com/aws-e/adp/pull/5851) merged `c180d10e`; [#6043](https://github.com/aws-e/adp/pull/6043) merged `79aa4134`; [#6060](https://github.com/aws-e/adp/pull/6060) merged `35953b36`; [#6068](https://github.com/aws-e/adp/pull/6068) merged `7ffef8f6`; [#6069](https://github.com/aws-e/adp/pull/6069) merged `1ca9ee50`; [#6079](https://github.com/aws-e/adp/pull/6079) merged `b361419c` |
| S16 | #5615 | CLOSED | [#5857](https://github.com/aws-e/adp/pull/5857) merged `0ea010af`; [#5987](https://github.com/aws-e/adp/pull/5987) merged `228b0185`; [#5995](https://github.com/aws-e/adp/pull/5995) merged `c614fca7` |
| S17 | #5616 | CLOSED | [#5758](https://github.com/aws-e/adp/pull/5758) merged `eed0fbe7` |
| S18 | #5617 | OPEN | [#6093](https://github.com/aws-e/adp/pull/6093) OPEN |
| S19 | #5618 | CLOSED | [#5756](https://github.com/aws-e/adp/pull/5756) merged `42fbe6cd` |
| S20 | #5619 | CLOSED | [#5702](https://github.com/aws-e/adp/pull/5702) merged `956a60da` |
| S21 | #5620 | OPEN | [#5957](https://github.com/aws-e/adp/pull/5957) merged `8f21f175`; [#6090](https://github.com/aws-e/adp/pull/6090) merged `209762ff`; [#6092](https://github.com/aws-e/adp/pull/6092) OPEN |
| A01 | #5653 | CLOSED | [#5737](https://github.com/aws-e/adp/pull/5737) merged `6455f8b5` |
| A02 | #5682 | CLOSED | [#5786](https://github.com/aws-e/adp/pull/5786) merged `3cb303b0` |
| A03 | #5655 | OPEN | [#6088](https://github.com/aws-e/adp/pull/6088) OPEN |
| A04 | #5683 | CLOSED | [#5739](https://github.com/aws-e/adp/pull/5739) merged `a5f3570c` |
| A05 | #5656 | CLOSED | [#5787](https://github.com/aws-e/adp/pull/5787) merged `058745bc` |
| A06 | #5658 | CLOSED | [#5790](https://github.com/aws-e/adp/pull/5790) merged `e6e8f322` |
| A07 | #5660 | CLOSED | [#5742](https://github.com/aws-e/adp/pull/5742) merged `a38cecbc` |
| A08 | #5662 | CLOSED | [#5721](https://github.com/aws-e/adp/pull/5721) merged `1786f856` |
| A09 | #5663 | CLOSED | [#5950](https://github.com/aws-e/adp/pull/5950) merged `207518e4`; [#6027](https://github.com/aws-e/adp/pull/6027) merged `2eee18c2` |
| A10 | #5664 | CLOSED | [#5848](https://github.com/aws-e/adp/pull/5848) merged `d23d60dc` |
| A11 | #5666 | OPEN | [#5959](https://github.com/aws-e/adp/pull/5959) OPEN |
| A12 | #5668 | OPEN | [#6087](https://github.com/aws-e/adp/pull/6087) OPEN |
| A13 | #5669 | OPEN | [#6093](https://github.com/aws-e/adp/pull/6093) OPEN |
| A14 | #5670 | OPEN | [#6085](https://github.com/aws-e/adp/pull/6085) OPEN |
| A15 | #5671 | CLOSED | [#5826](https://github.com/aws-e/adp/pull/5826) merged `20612976` |
| A16 | #5672 | CLOSED | [#5762](https://github.com/aws-e/adp/pull/5762) merged `69bd15f4` |
| A17 | #5673 | CLOSED | [#5824](https://github.com/aws-e/adp/pull/5824) merged `c4635f45` |
| A18 | #5674 | CLOSED | [#5767](https://github.com/aws-e/adp/pull/5767) merged `d2646593` |
| A19 | #5684 | CLOSED | [#5741](https://github.com/aws-e/adp/pull/5741) merged `aacfabfa` |
| A20 | #5675 | CLOSED | [#5768](https://github.com/aws-e/adp/pull/5768) merged `70c8d6b4` |
| A21 | #5685 | CLOSED | [#5755](https://github.com/aws-e/adp/pull/5755) merged `a6fcb67e` |
| A22 | #5676 | CLOSED | [#5785](https://github.com/aws-e/adp/pull/5785) merged `8d9e767e` |
| A23 | #5686 | CLOSED | [#5726](https://github.com/aws-e/adp/pull/5726) merged `e9ef11f3` |
| A24 | #5687 | CLOSED | [#5708](https://github.com/aws-e/adp/pull/5708) merged `4457a3b4` |

S13 shares work with A11 #5959; A13/S18 share #6093; A14 uses #6085. These references do not discharge either package's separate criteria. PR references for A13/S18 were corrected after the issue-state snapshot; their issue states remain unchanged. The original AWS dependency graph and the six zero-primary satellites remain unchanged: A02, A04, A19, A21, A23 and A24. All 24 package rows, including satellites, are preserved in the companion evidence file.

## 3. Release/scanner reconciliation and residual owners

| Evidence or residual | Current disposition | Accountable owner / remaining action |
|---|---|---|
| S01 controller, S02 API, executor exact artifacts | #6092 contains their reviewed digest promotion and preserved build provenance. S01/S02 scoped advisory results remain valid within their original scope; no whole-image clean claim. | S21 #5620: merge release integration and attach final scan/provenance. |
| S03 derived SkyPilot | #6092 records publication of exact OCI digest `sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec` to `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot`, with paired lock, runtime fixture and Terraform pin. The fixture differs from reviewed S03 facts only in final image reference. | S21 #5620: review/merge integration and final scan reconciliation. Existing local startup/auth/restart evidence remains scoped to S03. |
| Platform monitor | Its deployment release pin remains pending. Scanner artifact acceptance is tracked separately below. | S21 #5620: final **five-image scan and provenance** establish scanner artifact acceptance, including the monitor build. A monitor deployment release may remain pending if unsupported. |
| Three OpenSSH occurrences, indices 70/71/72 | `CVE-2020-15778` remains in openssh-client/server/sftp-server `1:10.0p1-7+deb13u4`; Debian native **Negligible**. **Not fixed or removed.** Startup without scp does not prove every provisioning path unreachable. | S21 #5620 residual adjudication, with S03 #5602 image ownership. Retain the vendor rating rationale and exposure limits; do not call package closure a vulnerability fix. |
| Five other CVSS-disagreement occurrences | Controller indices 0/38/44 and SkyPilot 445/446 have scoped replacement-image no-match evidence. S01 prose/inventory inconsistency for Helm is explicitly disclosed. | S21 #5620: retain [per-occurrence dispositions](S21-suppressed-and-cvss-dispositions.md); final scan remains pending. |
| 13 suppressed source occurrences | #6090 merged: eight server fetch sinks reject redirects; four browser fetches and one direct CLI exec retain individual context-based dispositions. No suppressions were added. | S21 #5620: retain all original selectors and [location evidence](S21-suppressed-and-cvss-dispositions.md). S04/S05 are supporting outbound-request owners. |
| Four S20 ECR `CKV_AWS_51` records | Source default repaired to IMMUTABLE in **#6092 (OPEN at snapshot)**. This changes the shared default; it is not a live apply or proof that existing repositories are immutable. | S21 #5620: merge source repair; stage live apply with publisher alias migration. Import newly created SkyPilot repository into owning control-plane state before the next apply. |
| Bandit YAML configuration | #6092 replaces `--ini` with `--configfile` and removes blanket B101/B311 skips. Existing pinned-tool fixture validation is recorded by the integration owner. | S21 #5620: merge configuration and consume final scanner output. S19 #5618 owns corrected reporting contracts. |
| Final scanner evidence | **PENDING.** No scan was started by this reconciliation update. | S21 #5620: the integration owner will attach exact source revision, all expected scanner artifacts/coverage, five image digests and provenance, and per-finding outcomes before completion. |

The prior ledger also retained rollout qualifications for A08 (deployed-agent acceptance), A22 (CA provisioning before the TLS default), A23 (CLI artifact publication), A17 (event retention), A18 (effective Checkov policy), A24 (cfn-nag evidence), and S17 (sandbox/image/CI prerequisites). This snapshot does not independently revalidate or discharge those qualifications. Their original owners must reconcile the latest evidence before a live-protection claim; issue closure is insufficient. Historical details remain in the previous ledger and package dispositions.

## 4. Retained 860 S20 dispositions

The [original disposition file](triage/s20-dispositions.json) is retained byte-for-byte: **234 Semgrep + 626 Checkov = 860 unique records, zero unaccounted**. Its SHA-256 is recorded in the companion evidence. These are unrated source records, not evidence of safety. The 33 source-rated occurrences, 860 dispositions, 47 AWS findings and 16 older mappings are overlapping inventories and must not be added as distinct vulnerabilities.

Original S20 owner counts remain 464 S20-reviewed, 27 S12, 27 S14, 4 S17, 4 S21 and 335 proposed follow-on records. The original verdicts include 335 needs-followon and 175 accepted-risk across three tiers. Retention is not renewed risk acceptance, evidence that every proposed follow-on is still unfiled, or a claim that later remediation never occurred. Current package state is in §2; later per-record evidence must confirm or overturn the retained verdict rather than silently delete the record. S21 #5620 retains reconciliation accountability for proposed follow-ons and accepted-risk decisions not yet joined to later evidence. S12/S14/S17 remain accountable for their routed records even where their package issue is closed.

## 5. Required per-finding joins

These are the existing reviewed joins, refreshed against live issue states. Original finding IDs, source selectors, legacy owners and composite contributions are unchanged. Source plan: `7b0004eda47a82c0d33a5f78f699ca83fe5053c2:doc/ai-dlc-engine/security-agent-35547077368/work-packages.json`; source inventory: `3193c78b167f583eed5026c582ab58794c88c34e:docs/security/runs/2026-09-21/findings.json`.

### A. All 47 AWS finding IDs → original owner

The state column is **owner issue state**, not finding resolution. Dependencies are the original plan, not a claim that each is still open. Zero-primary satellites appear in §2 and the original composite feeds remain below.

| # | Finding ID | Owner pkg | Issue | State | Original dependencies | Fed by | Earlier nightly |
|---|---|---|---|---|---|---|---|
| 1 | `f-042c16e1-fe62-4a70-8bd9-cc283c652004` | A01 | #5653 | CLOSED | - | - | 5609,5612 |
| 2 | `f-1473858b-602e-481f-bf8e-10a166560fd5` | A01 | #5653 | CLOSED | - | - | 5609,5612 |
| 3 | `f-0054d7ee-bbe7-4b3d-8f88-91695dbc247f` | A03 | #5655 | OPEN | A02,A06,A12,A14 | A02,A12,A14 | 5609,5612,5614,5617 |
| 4 | `f-16d7f58c-3049-4afc-8715-99b824be7108` | A03 | #5655 | OPEN | A02,A06,A12,A14 | A02,A12,A14 | 5609,5612,5614,5617 |
| 5 | `f-93db06c3-561a-49cf-8f42-16c66137301c` | A03 | #5655 | OPEN | A02,A06,A12,A14 | A02,A12,A14 | 5609,5612,5614,5617 |
| 6 | `f-4fcee527-02cd-46d8-8036-7672b9375688` | A05 | #5656 | CLOSED | A01,A04 | A04 | 5609,5610,5611 |
| 7 | `f-9289cf29-ffbf-4f94-820e-ad446874fa1a` | A05 | #5656 | CLOSED | A01,A04 | A04 | 5609,5610,5611 |
| 8 | `f-1416bf9f-8e69-4b54-a47f-a64e3ff8af95` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 9 | `f-2c92584b-b7f1-47d3-97ce-68008910aa21` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 10 | `f-3633a91e-c67a-4a45-b255-5701159813f1` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 11 | `f-5065d163-b429-4a57-9d3d-a8b9cec7e30c` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 12 | `f-83ad0918-2ceb-44b3-a153-096829422ed5` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 13 | `f-a9f8dda7-7bce-48fc-a001-a95faffa373a` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 14 | `f-afbff07b-0f5a-406c-8185-3a692aa29858` | A06 | #5658 | CLOSED | A01 | - | 5614 |
| 15 | `f-10be6bbc-1e9c-4e97-ac46-b5d56f5d3938` | A07 | #5660 | CLOSED | - | - | 5615 |
| 16 | `f-1c42a409-3bee-41bb-97c8-868d035090e2` | A07 | #5660 | CLOSED | - | - | 5615 |
| 17 | `f-ee68a4da-688e-42aa-b973-854ee12bea34` | A07 | #5660 | CLOSED | - | - | 5615 |
| 18 | `f-a7de2523-ae6a-4230-8ed7-cf4380d30ade` | A08 | #5662 | CLOSED | - | - | 5616 |
| 19 | `f-37867072-eabe-4eaf-9b67-946581e17141` | A09 | #5663 | CLOSED | A01,A10 | - | 5610,5611 |
| 20 | `f-5716ea91-dddb-4cb1-9555-7dedb75e9f44` | A09 | #5663 | CLOSED | A01,A10 | - | 5610,5611 |
| 21 | `f-5efb8f93-04db-442b-8879-d01dd7bf0bc8` | A09 | #5663 | CLOSED | A01,A10 | - | 5610,5611 |
| 22 | `f-7c46ead6-06bf-4726-94a7-b09ea88efbb5` | A09 | #5663 | CLOSED | A01,A10 | - | 5610,5611 |
| 23 | `f-32c4047a-643a-45cc-821a-e45ca5586239` | A10 | #5664 | CLOSED | A05 | - | 5610,5611 |
| 24 | `f-5726d42e-1699-417a-8cd1-7ef62b29e0f5` | A10 | #5664 | CLOSED | A05 | - | 5610,5611 |
| 25 | `f-a265c73e-f2e5-4a41-a7b8-5de3dcd63744` | A10 | #5664 | CLOSED | A05 | - | 5610,5611 |
| 26 | `f-f72357f3-911b-4046-94fa-b20a30b3286b` | A10 | #5664 | CLOSED | A05 | - | 5610,5611 |
| 27 | `f-169973a3-5eaa-4e21-aab8-2511d308cb38` | A11 | #5666 | OPEN | A10 | - | 5612,5617 |
| 28 | `f-47879254-cca8-4e12-baf8-2db1d18c78f7` | A11 | #5666 | OPEN | A10 | - | 5612,5617 |
| 29 | `f-7aa10bca-c6d1-44e2-9329-acd0e51411e4` | A11 | #5666 | OPEN | A10 | - | 5612,5617 |
| 30 | `f-f070371e-2653-48e9-bac3-32e671285e7f` | A11 | #5666 | OPEN | A10 | - | 5612,5617 |
| 31 | `f-4bb47eec-8e4f-4496-8bf7-59019021e065` | A12 | #5668 | OPEN | A11 | - | 5612,5617 |
| 32 | `f-a16723b6-5efa-465f-9ae9-7e0ce8029013` | A12 | #5668 | OPEN | A11 | - | 5612,5617 |
| 33 | `f-0c88fc4e-8abd-4b23-b913-27360a71a31c` | A13 | #5669 | OPEN | A11 | - | 5617 |
| 34 | `f-0ff37a38-1f7f-4892-8bdc-e1b182995d64` | A13 | #5669 | OPEN | A11 | - | 5617 |
| 35 | `f-37399a05-b05a-463c-8309-21fd16b284ec` | A14 | #5670 | OPEN | A13 | - | - |
| 36 | `f-f9e5efa9-7a0e-4568-b048-c2bf53eab3c3` | A14 | #5670 | OPEN | A13 | - | - |
| 37 | `f-96315bea-5e27-4d48-aacb-ca91301cf971` | A15 | #5671 | CLOSED | A02 | - | - |
| 38 | `f-f27c7448-7885-4cf7-abf5-4b89f96260a3` | A15 | #5671 | CLOSED | A02 | - | - |
| 39 | `f-5c6b7efb-9108-4bad-bc96-bef27072cb92` | A16 | #5672 | CLOSED | - | - | 5617,5618 |
| 40 | `f-80675759-b1d9-4f93-a7a0-4691685aeb12` | A16 | #5672 | CLOSED | - | - | 5617,5618 |
| 41 | `f-93177516-24d1-451e-bfd1-ddd2e3c60048` | A17 | #5673 | CLOSED | A02 | - | - |
| 42 | `f-51343273-7105-40c2-90e7-8011a508e996` | A18 | #5674 | CLOSED | A19,A24 | A19,A24 | 5613 |
| 43 | `f-c47fb5bc-a9e9-4927-972d-768be914c047` | A18 | #5674 | CLOSED | A19,A24 | A19,A24 | 5613 |
| 44 | `f-50fd079e-3650-4184-87ae-2bdd91b19bab` | A20 | #5675 | CLOSED | A21 | A21 | 5618 |
| 45 | `f-b6bc5a6d-433f-4be1-9a72-9b824172c155` | A20 | #5675 | CLOSED | A21 | A21 | 5618 |
| 46 | `f-ac1f6bcf-d79b-4411-a399-25d336eee878` | A22 | #5676 | CLOSED | A04,A23 | A23 | - |
| 47 | `f-fe6aaade-4cf3-4fb2-adbf-4c1942fef222` | A22 | #5676 | CLOSED | A04,A23 | A23 | - |

### B. All 33 source-rated occurrences → original owner

Owner issue state does not replace the scoped package dispositions or the final scanner result.

| # | Grp | Sev | Advisory / rule | Subject | Fix avail | Owner | Issue | State |
|---|---|---|---|---|---|---|---|---|
| 1 | container | critical | `CVE-2022-32511` | `py3-jmespath 1.0.1-r3` | 1.6.1 | S01 | #5600 | CLOSED |
| 2 | container | high | `CVE-2025-31498` | `c-ares 1.33.1-r0` | 1.34.5 | S01 | #5600 | CLOSED |
| 3 | container | critical | `CVE-2025-3277` | `sqlite-libs 3.45.3-r3` | 3.49.1 | S01 | #5600 | CLOSED |
| 4 | container | high | `CVE-2025-53547` | `helm 3.14.3-r4` | 3.18.4 | S01 | #5600 | CLOSED |
| 5 | container | high | `GHSA-hcg3-q754-cr77` | `golang.org/x/crypto v0.17.0` | 0.35.0 | S01 | #5600 | CLOSED |
| 6 | container | high | `GHSA-r6ph-v2qm-q3c2` | `cryptography 42.0.7` | 46.0.5 | S01 | #5600 | CLOSED |
| 7 | container | critical | `GHSA-v23v-6jw2-98fq` | `github.com/docker/docker v24.0.7+incompatible` | 25.0.6 | S01 | #5600 | CLOSED |
| 8 | container | critical | `GHSA-v778-237x-gjrc` | `golang.org/x/crypto v0.17.0` | 0.31.0 | S01 | #5600 | CLOSED |
| 9 | container | high | `GHSA-wj6h-64fc-37mp` | `ecdsa 0.19.2` | none | S02 | #5601 | CLOSED |
| 10 | container | high | `CVE-2023-36632` | `python 3.10.19` | none | S03 | #5602 | CLOSED |
| 11 | container | high | `GHSA-58pv-8j8x-9vj2` | `jaraco-context 5.3.0` | 6.1.0 | S03 | #5602 | CLOSED |
| 12 | container | high | `GHSA-72hv-8253-57qq` | `jackson-core 2.16.1` | 2.18.6 | S03 | #5602 | CLOSED |
| 13 | container | high | `GHSA-8rrh-rw8j-w5fx` | `wheel 0.45.1` | 0.46.2 | S03 | #5602 | CLOSED |
| 14 | container | high | `GHSA-r6ph-v2qm-q3c2` | `cryptography 43.0.3` | 46.0.5 | S03 | #5602 | CLOSED |
| 15 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/complex-task-chat/recall-at-task-start.ts:171` | - | S04 | #5603 | CLOSED |
| 16 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/invocability-probe/capture-proxy.ts:187` | - | S04 | #5603 | CLOSED |
| 17 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/invocability-probe/gateway-client.ts:86` | - | S04 | #5603 | CLOSED |
| 18 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/lib/artifactGateway.ts:27` | - | S04 | #5603 | CLOSED |
| 19 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/lib/knowledgeBridge.ts:60` | - | S04 | #5603 | CLOSED |
| 20 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/utils/installation.ts:61` | - | S05 | #5604 | CLOSED |
| 21 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/agent/src/utils/installation.ts:74` | - | S05 | #5604 | CLOSED |
| 22 | code | critical | `tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf` | `modules/agent-factory/codex-reviewer/src/github.ts:114` | - | S05 | #5604 | CLOSED |
| 23 | code | high | `B324` | `modules/gateway/src/orchestration/deployment_workflow_provider.py:162` | - | S08 | #5607 | CLOSED |
| 24 | code | high | `tmp.gitlab.bandit.B102` | `docs/analysis/cli-uplift-rework/probe_historical_failures.py:34` | - | S09 | #5608 | CLOSED |
| 25 | npm | high | `brace-expansion` | `modules/agent-factory/agent range <=1.1.17 \|\| 2.0.0 - 2.1.3` | yes | S06 | #5605 | CLOSED |
| 26 | npm | high | `fast-uri` | `modules/agent-factory/agent range 3.0.0 - 3.1.5` | yes | S06 | #5605 | CLOSED |
| 27 | npm | high | `ip-address` | `modules/agent-factory/agent range <=10.3.0` | yes | S06 | #5605 | CLOSED |
| 28 | npm | high | `js-yaml` | `modules/agent-factory/agent range 3.0.0 - 3.15.1` | yes | S06 | #5605 | CLOSED |
| 29 | npm | high | `brace-expansion` | `modules/gateway/frontend range <=1.1.17 \|\| 2.0.0 - 2.1.3` | yes | S07 | #5606 | CLOSED |
| 30 | npm | high | `browserslist` | `modules/gateway/frontend range <=4.28.6` | yes | S07 | #5606 | CLOSED |
| 31 | npm | high | `js-yaml` | `modules/gateway/frontend range 4.0.0 - 4.3.1` | yes | S07 | #5606 | CLOSED |
| 32 | npm | high | `nanoid` | `modules/gateway/frontend range <3.3.18` | yes | S07 | #5606 | CLOSED |
| 33 | npm | high | `undici` | `modules/gateway/frontend range 7.0.0 - 7.28.0` | yes | S07 | #5606 | CLOSED |

Selectors in the same row order (`tool | artifact | result_index`, or `npm-audit | project | package`):

| # | Stable selector |
|---|---|
| 1 | `grype\|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif\|ri=1` |
| 2 | `grype\|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif\|ri=22` |
| 3 | `grype\|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif\|ri=25` |
| 4 | `grype\|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif\|ri=26` |
| 5 | `grype\|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif\|ri=47` |
| 6 | `grype\|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif\|ri=53` |
| 7 | `grype\|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif\|ri=54` |
| 8 | `grype\|superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif\|ri=55` |
| 9 | `grype\|superplane/grype/modules-domain-apps-superplane-src-superplane-api.sarif\|ri=94` |
| 10 | `grype\|superplane/grype/superplane-skypilot-api.sarif\|ri=104` |
| 11 | `grype\|superplane/grype/superplane-skypilot-api.sarif\|ri=440` |
| 12 | `grype\|superplane/grype/superplane-skypilot-api.sarif\|ri=442` |
| 13 | `grype\|superplane/grype/superplane-skypilot-api.sarif\|ri=444` |
| 14 | `grype\|superplane/grype/superplane-skypilot-api.sarif\|ri=448` |
| 15 | `semgrep\|original/semgrep/semgrep-results.sarif\|ri=2066` |
| 16 | `semgrep\|original/semgrep/semgrep-results.sarif\|ri=2178` |
| 17 | `semgrep\|original/semgrep/semgrep-results.sarif\|ri=2180` |
| 18 | `semgrep\|original/semgrep/semgrep-results.sarif\|ri=2243` |
| 19 | `semgrep\|original/semgrep/semgrep-results.sarif\|ri=2245` |
| 20 | `semgrep\|original/semgrep/semgrep-results.sarif\|ri=2347` |
| 21 | `semgrep\|original/semgrep/semgrep-results.sarif\|ri=2348` |
| 22 | `semgrep\|original/semgrep/semgrep-results.sarif\|ri=2418` |
| 23 | `bandit\|original/bandit/bandit-results.sarif\|ri=8712` |
| 24 | `semgrep\|original/semgrep/semgrep-results.sarif\|ri=758` |
| 25 | `npm-audit\|modules/agent-factory/agent\|brace-expansion` |
| 26 | `npm-audit\|modules/agent-factory/agent\|fast-uri` |
| 27 | `npm-audit\|modules/agent-factory/agent\|ip-address` |
| 28 | `npm-audit\|modules/agent-factory/agent\|js-yaml` |
| 29 | `npm-audit\|modules/gateway/frontend\|brace-expansion` |
| 30 | `npm-audit\|modules/gateway/frontend\|browserslist` |
| 31 | `npm-audit\|modules/gateway/frontend\|js-yaml` |
| 32 | `npm-audit\|modules/gateway/frontend\|nanoid` |
| 33 | `npm-audit\|modules/gateway/frontend\|undici` |

### C. All 16 older primary tickets → original current owner

The original 16-ticket inventory is retained even after a ticket closes. **#4722 is CLOSED; the other 15 are OPEN** at this snapshot. Closed owners do not automatically close older tickets. #4725 remains composite: S14 primary and S13 audit contributor.

| # | Ticket | Ticket state | Severity | Owner issue state | Original title |
|---|---|---|---|---|---|
| 1 | [#4701](https://github.com/aws-e/adp/issues/4701) | OPEN | high | S10/#5609 (CLOSED) | [Security 2026-08-30] Internal and gateway planes trust unverified caller-id |
| 2 | [#4702](https://github.com/aws-e/adp/issues/4702) | OPEN | high | S12/#5611 (OPEN) | [Security 2026-08-30] Vault credential delivery routes rely on client-declar |
| 3 | [#4703](https://github.com/aws-e/adp/issues/4703) | OPEN | high | S11/#5610 (CLOSED) | [Security 2026-08-30] Internal identity and installation resolution endpoint |
| 4 | [#4704](https://github.com/aws-e/adp/issues/4704) | OPEN | high | S11/#5610 (CLOSED) | [Security 2026-08-30] External identity linking never proves control of the |
| 5 | [#4706](https://github.com/aws-e/adp/issues/4706) | OPEN | high | S13/#5612 (OPEN) | [Security 2026-08-30] Admin routes miss permission and role-ceiling checks; |
| 6 | [#4707](https://github.com/aws-e/adp/issues/4707) | OPEN | high | S13/#5612 (OPEN) | [Security 2026-08-30] Agent Cognito clients let a user rewrite their own rol |
| 7 | [#4709](https://github.com/aws-e/adp/issues/4709) | OPEN | high | S18/#5617 (OPEN) | [Security 2026-08-30] Client-supplied request identifier drives budget reser |
| 8 | [#4710](https://github.com/aws-e/adp/issues/4710) | OPEN | high | S18/#5617 (OPEN) | [Security 2026-08-30] Spend on the main model-invocation routes never reache |
| 9 | [#4718](https://github.com/aws-e/adp/issues/4718) | OPEN | high | S15/#5614 (CLOSED) | [Security 2026-08-30] Ingestion writes content with no real access labels an |
| 10 | [#4719](https://github.com/aws-e/adp/issues/4719) | OPEN | high | S15/#5614 (CLOSED) | [Security 2026-08-30] Ingestion fetches arbitrary caller-named web and objec |
| 11 | [#4720](https://github.com/aws-e/adp/issues/4720) | OPEN | critical | S15/#5614 (CLOSED) | [Security 2026-08-30] Knowledge ingestion runs build scripts from untrusted |
| 12 | [#4722](https://github.com/aws-e/adp/issues/4722) | CLOSED | high | S16/#5615 (CLOSED) | [Security 2026-08-30] Chat session and artifact identifiers accepted from th |
| 13 | [#4724](https://github.com/aws-e/adp/issues/4724) | OPEN | high | S12/#5611 (OPEN) | [Security 2026-08-30] Agent worker cloud roles can reach other tenants' secr |
| 14 | [#4725](https://github.com/aws-e/adp/issues/4725) | OPEN | high | S14/#5613 (CLOSED) primary; S13/#5612 (OPEN) audit contributor | [Security 2026-08-30] CI runner role can escalate to full account administra |
| 15 | [#4729](https://github.com/aws-e/adp/issues/4729) | OPEN | high | S17/#5616 (CLOSED) | [Security 2026-08-30] Cyber worker runs caller-named scripts with no real va |
| 16 | [#4730](https://github.com/aws-e/adp/issues/4730) | OPEN | high | S17/#5616 (CLOSED) | [Security 2026-08-30] Cyber workers read arbitrary and cross-org stored obje |

### D. Suppressed and CVSS-disagreement selectors

All 13 suppressed selectors and all eight CVSS-disagreement selectors remain explicitly dispositioned in [S21-suppressed-and-cvss-dispositions.md](S21-suppressed-and-cvss-dispositions.md), merged by #6090 (`209762ff0`). Suppressed indices: `1736,2022,2023,2024,2025,2078,2079,2123,2157,8465,8466,8469,8470`. CVSS indices: `0,38,44,70,71,72,445,446`, retaining their original per-artifact identities. They are not part of the 860 unrated records. See §3 for the three persistent OpenSSH residuals.

## 6. Verification and completion boundary

This revision verified the original joins against source artifacts: **47 unique AWS IDs across the 24-package plan, 33 unique source selectors, 16 unique older tickets, and 860 unique retained disposition selectors with zero missing or extra records**. GitHub states were read in one dated batch; selected PR states and full merge commits were resolved separately. No source-code change, new scan, live change, deployment or issue closure was performed for this ledger.

Final scan reconciliation is **pending**. Append its artifact references, source revision, scanner coverage and per-finding outcomes here before claiming S21 complete. Where a residual remains, preserve the owner and explicit disposition; do not convert a vendor rating, an open risk decision, a merge or a package closure into evidence of a fix.

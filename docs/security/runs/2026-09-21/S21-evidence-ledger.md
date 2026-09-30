# S21 — Consolidated evidence ledger

Work package **S21**, issue [#5620](https://github.com/aws-e/adp/issues/5620), daily epic [#5599](https://github.com/aws-e/adp/issues/5599), AWS epic [#5677](https://github.com/aws-e/adp/issues/5677).

## 1. Current reconciliation snapshot

**Acceptance evidence is complete for all 45 original packages. This evidence merge closes the remaining S21 issue.** The verified pre-merge GitHub snapshot at **2026-09-25 11:35:39 UTC** remains **24/24 AWS + 20/21 daily = 44/45 closed**, with only #5620 open. Merging this PR with `Fixes #5620` produces 45/45 issue closure; it is not recorded as already closed here. The nested AWS epic is counted once.

The final manual operator run is `s21-final-20260925-b1d0894c17`, scanning merged source `b1d0894c17c686f27c2747057dead0b5a0e6b17e`. All seven source legs and both complete 23-target image inventories succeeded. Exact artifact hashes, S3 VersionIds, CodeBuild IDs, original finding joins and the pre-merge issue snapshot are in [S21-reconciliation-status.json](evidence/S21-reconciliation-status.json).

Both dated epics remain open. **Package completion does not mean all vulnerabilities are fixed or all environments are deployed.** Twenty-five scoped follow-on issues (#6101–#6125), existing older owners and the open dated epic retain residual remediation, adjudication and rollout work. The original 45-package denominator is unchanged. Historical snapshots remain in Git history; original scan records are preserved.

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
| S12 | #5611 | CLOSED | [#6062](https://github.com/aws-e/adp/pull/6062) merged `700bacb9`; [#6084](https://github.com/aws-e/adp/pull/6084) merged `e2d35aa8` |
| S13 | #5612 | CLOSED | [#6053](https://github.com/aws-e/adp/pull/6053) merged `85d8e6e6`; [#5959](https://github.com/aws-e/adp/pull/5959) merged `a28b8c42` |
| S14 | #5613 | CLOSED | [#5850](https://github.com/aws-e/adp/pull/5850) merged `51a78e34`; [#6066](https://github.com/aws-e/adp/pull/6066) merged `e0ba2fad` |
| S15 | #5614 | CLOSED | [#5851](https://github.com/aws-e/adp/pull/5851) merged `c180d10e`; [#6043](https://github.com/aws-e/adp/pull/6043) merged `79aa4134`; [#6060](https://github.com/aws-e/adp/pull/6060) merged `35953b36`; [#6068](https://github.com/aws-e/adp/pull/6068) merged `7ffef8f6`; [#6069](https://github.com/aws-e/adp/pull/6069) merged `1ca9ee50`; [#6079](https://github.com/aws-e/adp/pull/6079) merged `b361419c` |
| S16 | #5615 | CLOSED | [#5857](https://github.com/aws-e/adp/pull/5857) merged `0ea010af`; [#5987](https://github.com/aws-e/adp/pull/5987) merged `228b0185`; [#5995](https://github.com/aws-e/adp/pull/5995) merged `c614fca7` |
| S17 | #5616 | CLOSED | [#5758](https://github.com/aws-e/adp/pull/5758) merged `eed0fbe7` |
| S18 | #5617 | CLOSED | [#6093](https://github.com/aws-e/adp/pull/6093) merged `5e452200` |
| S19 | #5618 | CLOSED | [#5756](https://github.com/aws-e/adp/pull/5756) merged `42fbe6cd` |
| S20 | #5619 | CLOSED | [#5702](https://github.com/aws-e/adp/pull/5702) merged `956a60da` |
| S21 | #5620 | OPEN | [#5957](https://github.com/aws-e/adp/pull/5957) merged `8f21f175`; [#6090](https://github.com/aws-e/adp/pull/6090) merged `209762ff`; [#6092](https://github.com/aws-e/adp/pull/6092) merged `b1d0894c` |
| A01 | #5653 | CLOSED | [#5737](https://github.com/aws-e/adp/pull/5737) merged `6455f8b5` |
| A02 | #5682 | CLOSED | [#5786](https://github.com/aws-e/adp/pull/5786) merged `3cb303b0` |
| A03 | #5655 | CLOSED | [#6088](https://github.com/aws-e/adp/pull/6088) merged `3d74b256` |
| A04 | #5683 | CLOSED | [#5739](https://github.com/aws-e/adp/pull/5739) merged `a5f3570c` |
| A05 | #5656 | CLOSED | [#5787](https://github.com/aws-e/adp/pull/5787) merged `058745bc` |
| A06 | #5658 | CLOSED | [#5790](https://github.com/aws-e/adp/pull/5790) merged `e6e8f322` |
| A07 | #5660 | CLOSED | [#5742](https://github.com/aws-e/adp/pull/5742) merged `a38cecbc` |
| A08 | #5662 | CLOSED | [#5721](https://github.com/aws-e/adp/pull/5721) merged `1786f856` |
| A09 | #5663 | CLOSED | [#5950](https://github.com/aws-e/adp/pull/5950) merged `207518e4`; [#6027](https://github.com/aws-e/adp/pull/6027) merged `2eee18c2` |
| A10 | #5664 | CLOSED | [#5848](https://github.com/aws-e/adp/pull/5848) merged `d23d60dc` |
| A11 | #5666 | CLOSED | [#5959](https://github.com/aws-e/adp/pull/5959) merged `a28b8c42` |
| A12 | #5668 | CLOSED | [#6087](https://github.com/aws-e/adp/pull/6087) merged `b76e9d48` |
| A13 | #5669 | CLOSED | [#6093](https://github.com/aws-e/adp/pull/6093) merged `5e452200` |
| A14 | #5670 | CLOSED | [#6085](https://github.com/aws-e/adp/pull/6085) merged `f1260332` |
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

S13 shares work with A11 #5959; A13/S18 share #6093; A14 uses #6085. These references do not discharge either package's separate criteria. A03, A11, A12, A13/S18 and A14 source integrations are merged; their package issue closures are recorded separately from rollout. The actual A12/A14 merge references here supersede the pre-merge wording in `docs/security/A03-defaults-reconciliation.md`. The original AWS dependency graph and the six zero-primary satellites remain unchanged: A02, A04, A19, A21, A23 and A24. All 24 package rows, including satellites, are preserved in the companion evidence file.

## 3. Release/scanner reconciliation and residual owners

| Evidence or residual | Current disposition | Accountable owner / remaining action |
|---|---|---|
| S01 controller, S02 API, executor exact artifacts | #6092 merged their reviewed digest promotion and preserved build provenance. S01/S02 scoped advisory results remain valid within their original scope; no whole-image clean claim. | Final scan/provenance attached. Open #5538 retains release/deployment acceptance; #6124 owns image residual review. |
| S03 derived SkyPilot | #6092 records publication of exact OCI digest `sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec` to `000000000101.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot`, with paired lock, runtime fixture and Terraform pin. The fixture differs from reviewed S03 facts only in final image reference. | Final same-image scan reconciliation complete; #6124 owns remaining image findings. Existing startup/auth/restart evidence remains scoped to S03; #5538 owns deployment. |
| Platform monitor | Its deployment release pin remains pending. Scanner artifact acceptance is tracked separately below. | The final **complete inventory scan and provenance** establish scanner artifact acceptance, including the monitor build. Open #5538 owns monitor release publication, pinning and deployment compatibility after scanner acceptance. |
| Three OpenSSH occurrences, indices 70/71/72 | `CVE-2020-15778` remains in openssh-client/server/sftp-server `1:10.0p1-7+deb13u4`; Historical Debian native **Negligible**, current Grype native **LOW**. **Not fixed or removed.** Startup without scp does not prove every provisioning path unreachable. | Open #6124 residual adjudication, retaining S03 #5602 historical image evidence. Retain the vendor rating rationale and exposure limits; do not call package closure a vulnerability fix. |
| Five other CVSS-disagreement occurrences | Controller indices 0/38/44 and SkyPilot 445/446 have scoped replacement-image no-match evidence. S01 prose/inventory inconsistency for Helm is explicitly disclosed. | Final per-occurrence review retained; #6124 owns continuing image review and #6121 owns ignore-scope review. |
| 13 suppressed source occurrences | #6090 merged: eight server fetch sinks reject redirects; four browser fetches and one direct CLI exec retain individual context-based dispositions. No suppressions were added. | All original selectors and final callsite evidence retained. Open #6119 owns continuing source review; S04/S05 and the per-location repair retain their implementation attribution. |
| Four S20 ECR `CKV_AWS_51` records | Source default repaired to IMMUTABLE in **#6092 (merged `b1d0894c`)**. This changes the shared default; it is not a live apply or proof that existing repositories are immutable. | Open #6120 owns shared publisher alias migration, bounded live tag-mutability convergence and import of the exact existing SkyPilot repository into its owning control-plane state before the next apply. Source repair is merged; no live apply is claimed. |
| Bandit YAML configuration | #6092 replaces `--ini` with `--configfile` and removes blanket B101/B311 skips. Existing pinned-tool fixture validation is recorded by the integration owner. | Configuration merged and final output reviewed. S19 #5618 retains historical reporting-contract evidence; #6108 owns remaining Bandit observations. |
| Final scanner evidence | **COMPLETE under the configured scan contract.** Seven source legs and 23/23 targets for each image tool succeeded. | Exact source, image config IDs, hashes, S3 versions and per-finding outcomes are attached. Open #6121 owns global ignore review; no unsuppressed-clean claim. |

The prior ledger also retained rollout qualifications for A08 (deployed-agent acceptance), A22 (CA provisioning before the TLS default), A23 (CLI artifact publication), A17 (event retention), A18 (effective Checkov policy), A24 (cfn-nag evidence), and S17 (sandbox/image/CI prerequisites). This snapshot does not independently revalidate or discharge those qualifications. The open dated epic #5599 owns reconciliation of remaining rollout qualifications with existing open underlying owners before a live-protection claim; issue closure is insufficient. Historical details remain in the previous ledger and package dispositions.

## 4. Retained 860 S20 dispositions

The [original disposition file](triage/s20-dispositions.json) is retained byte-for-byte: **234 Semgrep + 626 Checkov = 860 unique records, zero unaccounted**. Its SHA-256 is recorded in the companion evidence. These are unrated source records, not evidence of safety. The 33 source-rated occurrences, 860 dispositions, 47 AWS findings and 16 older mappings are overlapping inventories and must not be added as distinct vulnerabilities.

Original S20 owner counts remain 464 S20-reviewed, 27 S12, 27 S14, 4 S17, 4 S21 and 335 proposed follow-on records. The original verdicts include 335 needs-followon and 175 accepted-risk across three tiers. The 335 verdict records comprise 334 FOLLOWON-* records and one S14 shared-CodeBuild record; this is a different set from the 335 records grouped under FOLLOWON-* owners. Dedicated scanner-role maintenance moved only two project bindings and explicitly preserved the old shared role for other consumers. Current A18 source uses per-project roles; source repair and remaining shared-role rollout must stay distinct. Retention is not renewed risk acceptance, evidence that every proposed follow-on is still unfiled, or a claim that later remediation never occurred. Current package state is in §2; later per-record evidence must confirm or overturn the retained verdict rather than silently delete the record. S21 #5620 retains current reconciliation accountability. Existing open #2386 owns the four EKS endpoint occurrences; #4726 owns rotation follow-up; #2385 owns remaining shared CodeBuild-role consumers. Partial overlaps #2312 (XML), #4728 (pod hardening) and #5408 (release management) do not automatically own every retained occurrence. All retained FOLLOWON-A/B/F/I source scopes now have open owners #6115/#6116/#6117/#6118. Image follow-ons are published when their complete family evidence is available. The dated epic #5599 remains open; closed S21 must not become the sole future owner of residual work. Open #5599 explicitly owns continued adjudication of the 175 retained accepted-risk records and unresolved S12/S14/S17 routed records. Their original package attribution remains, but closed package issues are not the sole future owners. Retention does not renew risk acceptance.

## 5. Required per-finding joins

These are the existing reviewed joins, refreshed against live issue states. Original finding IDs, source selectors, legacy owners and composite contributions are unchanged. Source plan: `7b0004eda47a82c0d33a5f78f699ca83fe5053c2:doc/ai-dlc-engine/security-agent-35547077368/work-packages.json`; source inventory: `3193c78b167f583eed5026c582ab58794c88c34e:docs/security/runs/2026-09-21/findings.json`.

### A. All 47 AWS finding IDs → original owner

The state column is **owner issue state**, not finding resolution. Dependencies are the original plan, not a claim that each is still open. Zero-primary satellites appear in §2 and the original composite feeds remain below.

| # | Finding ID | Owner pkg | Issue | State | Original dependencies | Fed by | Earlier nightly |
|---|---|---|---|---|---|---|---|
| 1 | `f-042c16e1-fe62-4a70-8bd9-cc283c652004` | A01 | #5653 | CLOSED | - | - | 5609,5612 |
| 2 | `f-1473858b-602e-481f-bf8e-10a166560fd5` | A01 | #5653 | CLOSED | - | - | 5609,5612 |
| 3 | `f-0054d7ee-bbe7-4b3d-8f88-91695dbc247f` | A03 | #5655 | CLOSED | A02,A06,A12,A14 | A02,A12,A14 | 5609,5612,5614,5617 |
| 4 | `f-16d7f58c-3049-4afc-8715-99b824be7108` | A03 | #5655 | CLOSED | A02,A06,A12,A14 | A02,A12,A14 | 5609,5612,5614,5617 |
| 5 | `f-93db06c3-561a-49cf-8f42-16c66137301c` | A03 | #5655 | CLOSED | A02,A06,A12,A14 | A02,A12,A14 | 5609,5612,5614,5617 |
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
| 27 | `f-169973a3-5eaa-4e21-aab8-2511d308cb38` | A11 | #5666 | CLOSED | A10 | - | 5612,5617 |
| 28 | `f-47879254-cca8-4e12-baf8-2db1d18c78f7` | A11 | #5666 | CLOSED | A10 | - | 5612,5617 |
| 29 | `f-7aa10bca-c6d1-44e2-9329-acd0e51411e4` | A11 | #5666 | CLOSED | A10 | - | 5612,5617 |
| 30 | `f-f070371e-2653-48e9-bac3-32e671285e7f` | A11 | #5666 | CLOSED | A10 | - | 5612,5617 |
| 31 | `f-4bb47eec-8e4f-4496-8bf7-59019021e065` | A12 | #5668 | CLOSED | A11 | - | 5612,5617 |
| 32 | `f-a16723b6-5efa-465f-9ae9-7e0ce8029013` | A12 | #5668 | CLOSED | A11 | - | 5612,5617 |
| 33 | `f-0c88fc4e-8abd-4b23-b913-27360a71a31c` | A13 | #5669 | CLOSED | A11 | - | 5617 |
| 34 | `f-0ff37a38-1f7f-4892-8bdc-e1b182995d64` | A13 | #5669 | CLOSED | A11 | - | 5617 |
| 35 | `f-37399a05-b05a-463c-8309-21fd16b284ec` | A14 | #5670 | CLOSED | A13 | - | - |
| 36 | `f-f9e5efa9-7a0e-4568-b048-c2bf53eab3c3` | A14 | #5670 | CLOSED | A13 | - | - |
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
| 2 | [#4702](https://github.com/aws-e/adp/issues/4702) | OPEN | high | S12/#5611 (CLOSED) | [Security 2026-08-30] Vault credential delivery routes rely on client-declar |
| 3 | [#4703](https://github.com/aws-e/adp/issues/4703) | OPEN | high | S11/#5610 (CLOSED) | [Security 2026-08-30] Internal identity and installation resolution endpoint |
| 4 | [#4704](https://github.com/aws-e/adp/issues/4704) | OPEN | high | S11/#5610 (CLOSED) | [Security 2026-08-30] External identity linking never proves control of the |
| 5 | [#4706](https://github.com/aws-e/adp/issues/4706) | OPEN | high | S13/#5612 (CLOSED) | [Security 2026-08-30] Admin routes miss permission and role-ceiling checks; |
| 6 | [#4707](https://github.com/aws-e/adp/issues/4707) | OPEN | high | S13/#5612 (CLOSED) | [Security 2026-08-30] Agent Cognito clients let a user rewrite their own rol |
| 7 | [#4709](https://github.com/aws-e/adp/issues/4709) | OPEN | high | S18/#5617 (CLOSED) | [Security 2026-08-30] Client-supplied request identifier drives budget reser |
| 8 | [#4710](https://github.com/aws-e/adp/issues/4710) | OPEN | high | S18/#5617 (CLOSED) | [Security 2026-08-30] Spend on the main model-invocation routes never reache |
| 9 | [#4718](https://github.com/aws-e/adp/issues/4718) | OPEN | high | S15/#5614 (CLOSED) | [Security 2026-08-30] Ingestion writes content with no real access labels an |
| 10 | [#4719](https://github.com/aws-e/adp/issues/4719) | OPEN | high | S15/#5614 (CLOSED) | [Security 2026-08-30] Ingestion fetches arbitrary caller-named web and objec |
| 11 | [#4720](https://github.com/aws-e/adp/issues/4720) | OPEN | critical | S15/#5614 (CLOSED) | [Security 2026-08-30] Knowledge ingestion runs build scripts from untrusted |
| 12 | [#4722](https://github.com/aws-e/adp/issues/4722) | CLOSED | high | S16/#5615 (CLOSED) | [Security 2026-08-30] Chat session and artifact identifiers accepted from th |
| 13 | [#4724](https://github.com/aws-e/adp/issues/4724) | OPEN | high | S12/#5611 (CLOSED) | [Security 2026-08-30] Agent worker cloud roles can reach other tenants' secr |
| 14 | [#4725](https://github.com/aws-e/adp/issues/4725) | OPEN | high | S14/#5613 (CLOSED) primary; S13/#5612 (CLOSED) audit contributor | [Security 2026-08-30] CI runner role can escalate to full account administra |
| 15 | [#4729](https://github.com/aws-e/adp/issues/4729) | OPEN | high | S17/#5616 (CLOSED) | [Security 2026-08-30] Cyber worker runs caller-named scripts with no real va |
| 16 | [#4730](https://github.com/aws-e/adp/issues/4730) | OPEN | high | S17/#5616 (CLOSED) | [Security 2026-08-30] Cyber workers read arbitrary and cross-org stored obje |

### D. Suppressed and CVSS-disagreement selectors

All 13 suppressed selectors and all eight CVSS-disagreement selectors remain explicitly dispositioned in [S21-suppressed-and-cvss-dispositions.md](S21-suppressed-and-cvss-dispositions.md), merged by #6090 (`209762ff0`). Suppressed indices: `1736,2022,2023,2024,2025,2078,2079,2123,2157,8465,8466,8469,8470`. CVSS indices: `0,38,44,70,71,72,445,446`, retaining their original per-artifact identities. They are not part of the 860 unrated records. See §3 for the three persistent OpenSSH residuals.

## 6. Verification and completion boundary

Data-integrity checks preserve **47 AWS IDs, 33 original rated selectors, 16 older issue mappings, 860 S20 selectors, 13 original suppressed occurrences, eight CVSS-disagreement occurrences and all 189 configured Grype-ignore entries**. The original S20 file remains byte-identical, SHA-256 `42838f1a59611094a2ec6f60c4c8c87de629da170059761b2783abc4771e62e9`. These inventories overlap and are not additive vulnerability counts. All 33 rated records have an explicit completed scoped review; candidate matching status is retained separately.

The 33 outcomes comprise nine npm lockfile remediations, one Bandit Git-protocol applicability decision, nine Semgrep control/applicability reviews, eight retained controller dependency dispositions, one retained API dependency removal and five SkyPilot outcomes. SkyPilot's four updated dependencies are verified in the same final image: vendored jaraco-context 6.1.0, vendored wheel 0.46.3, nested Jackson 2.18.6 and cryptography 46.0.5. Python 3.10.19 and its disputed original advisory remain; it is **not fixed**. All three OpenSSH CVSS occurrences remain installed and reported native LOW; historical Debian Negligible is a separate rating, not remediation.

All 13 original suppressed source records still appear suppressed. Eight delivered redirect-refusal controls and five narrow applicability decisions are individually revalidated against final source. Final-tree inclusion does not change the actual predecessor/package attribution. The final source scan also preserves 75 total suppressed Semgrep observations under its separate ongoing review scope.

Controller/API historical exact-image evidence and unchanged or reviewed active dependency paths support their scoped outcomes. Their final Grype/Syft builds have different Docker config IDs, so separate Syft output is not same-final-Grype-image package-removal or startup proof. The API Docker path uses PyJWT from pyproject.toml; stale unused uv.lock packaging follow-through remains with #6124. SkyPilot's final matching config ID is `sha256:d05a57076d2c5c277b280d37d938716cedc908c25ae4e71931789ca7f7e0ef87`, bound to the published OCI manifest `sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec`. Config IDs and OCI manifest digests are distinct identities.

## 7. Final operator evidence

PR #6092 merged as `b1d0894c17c686f27c2747057dead0b5a0e6b17e`; tree `e2f94d5592296a983a0599e31c2d54fcfe14f52c` equals the reviewed `3ea9fee8d34d68d019c690e35333b4326a393dc0` tree. The manual operator run binds source archive SHA-256 `066072c2d4002cf1aeb68b2c7faf04fba51c9c474666800b4e204a2b98d0345a` and extracted workflow SHA-256 `4e29abb9ef414517049a50c06e75254e4f88817e6823a716f2ff8159603be14a` to per-leg scripts/buildspecs and CodeBuild receipts. This is manual operator evidence, with no GitHub workflow correlation or automated producer-acceptance claim.

| Final evidence | Result | Continuing owner |
|---|---|---|
| Checkov | 636 observations | Scoped follow-ons; remaining infrastructure review #6109 |
| Bandit | 1,472: 1,296 LOW, 174 MEDIUM, 2 HIGH | #6108 owns 1,470; two HIGH Git-blob protocol decisions retain exact source evidence |
| Semgrep | 13,139 observations | #6119; original installer/XML/workflow/secret-template scopes #6115–#6118 |
| detect-secrets | 1,859 unverified scan records; 1,849 overlapping audit groups | #6110; no confirmed-leak or additive-count claim |
| npm audit | Agent 2 moderate; frontend 4 moderate; zero HIGH/CRITICAL | #6106/#6107; lockfile audits, not fresh install/deployment proof |
| cfn-nag | 3 templates, 4 WARN observations | #6109 |
| Grype | 23/23 targets; 11,909 retained post-filter occurrences: 544 CRITICAL, 2,869 HIGH, 4,043 MEDIUM, 4,453 LOW | Eight component-family owners below; ignore review #6121 |
| Syft | 23/23 targets with hashed SBOMs and config IDs | Same family owners; same-image checks required before package-removal claims |

All 12 image partitions succeeded, no builds remain active, and 46 image artifact hashes/provenance bindings were verified. The independent global coverage receipt, not a single partition's count, establishes 23/23 per tool. All 14 temporary image-input objects were cleaned after consumers became terminal; source-leg cleanup has its separate receipt. Frozen final source reports one gateway Alembic head `074_budget_settlement_receipts` and one agent-context head `014_verified_public_acl`; no database migration was executed by reconciliation.

The eight image owners cover the exact 23-target union without omissions: #6122 agent-context (6), #6111 agent-factory (4), #6123 cyber (2), #6124 Superplane (6), #6112 gateway (1), #6113 harness (1), #6114 gbrain (1), #6125 platform (2). #6120 owns shared ECR immutable-tag rollout, publisher-alias compatibility and SkyPilot Terraform import; #5538 owns monitor release/deployment. #6101–#6105 and existing #2386/#4726/#2385 retain their scoped infrastructure work. Only the one original S14 shared-CodeBuild needs-followon record is assigned to #2385; other routed S14 records are not silently collapsed into that issue.

**Grype counts are post-filter observations, not an unsuppressed vulnerability inventory.** The frozen contract applies `.grype.yaml` and then an advisory-prefix postprocessor that does not honor package/image constraints. Its 189 configured entries collapse to 146 identifiers; no pre-filter report was retained. #6121 owns scope repair, expiry/applicability review and future raw-report retention. Existing exceptions are not renewed. In particular, cryptography's CVE-2026-26007 alias is configured for ignoring, so fixed-version/source evidence supplies the remediation basis independently of filtered non-observation. Earlier published family manifests stay byte-immutable; all family issue bodies and this final ledger carry the shared qualification.

The 29 final evidence artifacts are published under `s3://adp-dev-security-scans-000000000101/repository-scans/s21-final-20260925-b1d0894c17/reconciliation/final/`. The companion JSON records each exact key, SHA-256, S3 VersionId and upload receipt, including seven source-leg provenance records, safe terminal-build summary, source artifact/hash indexes, full original/current joins, scoped outcomes and global image coverage. All 17 published selector-manifest hashes were independently checked. No raw secret candidate values were copied into reconciliation inventories.

The earlier `s21-manual-20260925-f0b0a59112` and `s21-manual-20260925-92b36e7165` runs are diagnostic only and do not supply final acceptance. This evidence PR changes documentation only. Its merge closes S21 and completes original-package tracking at 45/45; the open dated epics and scoped successor issues retain unresolved vulnerabilities, review and rollout work.

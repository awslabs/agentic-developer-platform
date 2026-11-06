package collection

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

func TestExportAuthorizedPagePreservesSealedBindingAfterRotation(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	batch, sources, state, reader := exportFixture(t)
	for index := range sources {
		sources[index].GrantID, sources[index].Consumer = "grant", "insights"
	}
	storeExportFixture(batch, state, len(batch.Items))
	original, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", 1, 8192, &policyFixture{})
	if err != nil {
		t.Fatal(err)
	}
	sources[0].BindingID = "replacement-binding"
	sources[0].BindingGeneration++
	sources[0].BindingRevision++
	sources[0].GrantID = "replacement-grant"
	replayed, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", 1, 8192, &policyFixture{})
	if err != nil {
		t.Fatal(err)
	}
	originalBytes, _ := json.Marshal(original.Page.Items[0].Observation)
	replayedBytes, _ := json.Marshal(replayed.Page.Items[0].Observation)
	if !bytes.Equal(originalBytes, replayedBytes) || original.Policies[0].ObservationSHA256 != replayed.Policies[0].ObservationSHA256 ||
		replayed.Page.Items[0].Observation.Binding != (ObservationBinding{"github-binding", 3, 2}) {
		t.Fatal("binding rotation rewrote sealed observation provenance")
	}
	if _, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", 1, 8192, &policyFixture{revokedAt: 3}); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("replay bypassed current grant checks: %v", err)
	}
}

func TestExportAuthorizedPageFitsCompleteResponseBudget(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	batch, sources, state, reader := exportFixture(t)
	for index := range sources {
		sources[index].GrantID, sources[index].Consumer = "grant", "insights"
	}
	storeExportFixture(batch, state, len(batch.Items))
	first, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", 1, 8192, &policyFixture{})
	if err != nil {
		t.Fatal(err)
	}
	firstBytes, _ := json.Marshal(first)
	budget := len(firstBytes)
	page, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", MaxExportPageItems, budget, &policyFixture{})
	if err != nil {
		t.Fatalf("valid authorized prefix of %d bytes rejected: %v", budget, err)
	}
	pageBytes, _ := json.Marshal(page)
	if !bytes.Equal(firstBytes, pageBytes) {
		t.Fatal("byte-limited page differs from the fitting one-item page")
	}
	for count := 2; count <= len(batch.Items); count++ {
		expected, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", count, 8192, &policyFixture{})
		if err != nil {
			t.Fatal(err)
		}
		expectedBytes, _ := json.Marshal(expected)
		bounded, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", MaxExportPageItems, len(expectedBytes), &policyFixture{})
		if err != nil {
			t.Fatal(err)
		}
		boundedBytes, _ := json.Marshal(bounded)
		if !bytes.Equal(expectedBytes, boundedBytes) {
			t.Fatalf("byte limit changed the fitting %d-item prefix", count)
		}
	}
	if _, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", MaxExportPageItems, budget-1, &policyFixture{}); !errors.Is(err, ErrBatch) {
		t.Fatalf("oversized authorized item accepted: %v", err)
	}
	if _, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", MaxExportPageItems, budget, &policyFixture{revokedAt: 3}); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("shortened page bypassed final authorization: %v", err)
	}
	seen := 0
	for {
		encoded, _ := json.Marshal(page)
		if len(encoded) > budget || len(page.Page.Items) == 0 {
			t.Fatal("page exceeded budget or made no progress")
		}
		policyIndex := 0
		for _, item := range page.Page.Items {
			if seen >= len(batch.Items) || item.ItemSHA256 != batch.ItemDigests[seen].SHA256 {
				t.Fatal("byte-limited pagination skipped or duplicated an item")
			}
			if item.Observation != nil {
				if policyIndex >= len(page.Policies) || page.Policies[policyIndex].ItemSHA256 != item.ItemSHA256 {
					t.Fatal("policy descriptors do not match page items")
				}
				policyIndex++
			} else if item.RemovalReason != batch.Items[seen].RemovalReason {
				t.Fatal("removal marker changed")
			}
			seen++
		}
		if policyIndex != len(page.Policies) {
			t.Fatal("page contains descriptors for unexported items")
		}
		if page.Page.Complete {
			if seen != len(batch.Items) || page.Page.NextCursor != "" {
				t.Fatal("page completed before exporting the whole manifest")
			}
			break
		}
		if page.Page.NextCursor != batch.ItemDigests[seen-1].SHA256 {
			t.Fatal("cursor does not match the last returned item")
		}
		page, err = ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, page.Page.NextCursor, MaxExportPageItems, budget, &policyFixture{})
		if err != nil {
			t.Fatal(err)
		}
	}
}

func TestExportAuthorizedPageCompletesEmptySealedBatch(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	batch, sources, state, reader := exportFixture(t)
	for index := range sources {
		sources[index].GrantID, sources[index].Consumer = "grant", "insights"
	}
	empty, err := PrepareSourceBatch(batch.BatchID, batch.CellID, batch.Generation, nil, sources)
	if err != nil {
		t.Fatal(err)
	}
	storeExportFixture(empty, state, 0)
	if _, err := LoadSealedBatch(context.Background(), reader, empty.CellID, empty.BatchID); err != nil {
		t.Fatal(err)
	}
	authority := &policyFixture{}
	page, err := ExportAuthorizedPage(context.Background(), reader, empty.CellID, empty.BatchID, sources, "", MaxExportPageItems, 512, authority)
	if err != nil {
		t.Fatalf("valid empty sealed batch rejected: %v", err)
	}
	encoded, _ := json.Marshal(page)
	if !page.Page.Complete || page.Page.NextCursor != "" || len(page.Page.Items) != 0 || len(page.Policies) != 0 ||
		page.Page.BatchID != empty.BatchID || page.Page.ManifestSHA256 != empty.ContentSHA256 ||
		page.Page.SchemaVersion != ObservationSchemaVersion || page.Page.MappingVersion != ObservationSchemaVersion ||
		authority.calls != 4 || len(encoded) > 512 {
		t.Fatalf("empty completion lost identity, authorization or bounds: %+v", page)
	}
	for _, scenario := range []struct {
		name      string
		after     string
		authority *policyFixture
		want      error
	}{
		{"invalid cursor", "invalid", &policyFixture{}, ErrBatch},
		{"revoked before read", "", &policyFixture{revokedAt: 1}, ErrUnauthorized},
		{"revoked after read", "", &policyFixture{revokedAt: 3}, ErrUnauthorized},
		{"collection grant", "", &policyFixture{purpose: "collection"}, ErrUnauthorized},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			if _, err := ExportAuthorizedPage(context.Background(), reader, empty.CellID, empty.BatchID, sources, scenario.after, 1, 512, scenario.authority); !errors.Is(err, scenario.want) {
				t.Fatalf("empty batch bypassed validation: %v", err)
			}
		})
	}
}

func TestExportAuthorizedPageRejectsUnboundLegacyBatch(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	batch, sources, state, reader := exportFixture(t)
	for index := range sources {
		sources[index].GrantID, sources[index].Consumer = "grant", "insights"
	}
	for _, items := range [][]BatchItem{batch.Items, nil} {
		legacy, err := prepareSourceBatch(batch.BatchID, batch.CellID, batch.Generation, items, batch.SourceKeys, nil)
		if err != nil {
			t.Fatal(err)
		}
		storeExportFixture(legacy, state, len(legacy.Items))
		if _, err := LoadSealedBatch(context.Background(), reader, legacy.CellID, legacy.BatchID); err != nil {
			t.Fatalf("legacy readback failed: %v", err)
		}
		if _, err := ExportAuthorizedPage(context.Background(), reader, legacy.CellID, legacy.BatchID, sources, "", 3, 8192, &policyFixture{}); !errors.Is(err, ErrBatch) {
			t.Fatalf("current binding substituted for missing historical snapshot: %v", err)
		}
	}
}

type policyFixture struct {
	calls       int
	revokedAt   int
	purpose     string
	expired     bool
	denied      bool
	private     bool
	missingLink bool
	falseShared bool
	foreign     bool
}

func (fixture *policyFixture) CurrentSource(_ context.Context, source Source) (CurrentSource, error) {
	fixture.calls++
	purpose := fixture.purpose
	if purpose == "" {
		purpose = "export"
	}
	expiration := time.Now().Add(time.Hour)
	if fixture.expired {
		expiration = time.Now().Add(-time.Minute)
	}
	return CurrentSource{Source: source, BindingActive: true, CellActive: true,
		GrantActive:  fixture.revokedAt == 0 || fixture.calls < fixture.revokedAt,
		GrantPurpose: purpose, GrantExpires: expiration}, nil
}

func (fixture *policyFixture) DeriveEvidence(_ context.Context, source Source, root QualifiedRef) (ExportPolicyEnvelope, error) {
	if fixture.denied {
		return ExportPolicyEnvelope{}, ErrPolicy
	}
	dependencies := []QualifiedRef{root}
	if root.SourceConnectionID == "github-connection" && !fixture.missingLink {
		dependencies = append(dependencies, QualifiedRef{root.TenantID, root.ProjectID, "jira-connection", "issue", "1", "native-r2"})
	}
	if fixture.foreign {
		dependencies[0].ProjectID = "foreign"
	}
	versions := make([]ExportDependencyVersion, 0, len(dependencies))
	for _, dependency := range dependencies {
		versions = append(versions, ExportDependencyVersion{Identity: dependency, Revision: 1, MetadataDigest: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"})
	}
	audience := "shared"
	requirements := []ExportPolicyRequirement{{"source-readers", 1, "shared", "team"}, {"execution-evidence", 1, "shared", "team"}}
	if fixture.private {
		requirements = append(requirements, ExportPolicyRequirement{"ci-report", 1, "private", "maintainers"})
	} else if fixture.falseShared {
		requirements = append(requirements, ExportPolicyRequirement{"ci-report", 1, "private", "maintainers"})
	}
	if fixture.private {
		audience = "private"
	}
	return ExportPolicyEnvelope{EnvelopeVersion: 1, Identity: root, RealmID: "realm", RealmRevision: 1,
		DescriptorDigest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		RequiredPolicies: requirements, DependencyIdentities: dependencies, DependencyVersions: versions, Audience: audience}, nil
}

func TestExportAuthorizedPageChecksCurrentGrantAndCanonicalIdentity(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	batch, sources, state, reader := exportFixture(t)
	for index := range sources {
		sources[index].GrantID, sources[index].Consumer = "grant", "insights"
	}
	storeExportFixture(batch, state, len(batch.Items))
	authority := &policyFixture{}
	page, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", 3, 8192, authority)
	if err != nil || !page.Page.Complete || len(page.Page.Items) != 3 || len(page.Policies) != 2 || authority.calls != 4 ||
		page.Page.Items[0].Observation.SourceRef.SourceConnectionID == page.Page.Items[2].Observation.SourceRef.SourceConnectionID ||
		page.Page.Items[0].Observation.SourceRef.RecordID != page.Page.Items[2].Observation.SourceRef.RecordID ||
		page.Page.Items[2].Observation.ObservationStatus != "observation_only" ||
		page.Policies[0].SchemaVersion != EvidencePolicyVersion || !validDigest(page.Policies[0].DescriptorSHA256) || !validDigest(page.Policies[0].ObservationSHA256) {
		t.Fatalf("native identities collapsed or current grant unchecked: %+v, %v", page, err)
	}
	descriptor := page.Policies[0]
	if descriptor.ItemSHA256 != page.Page.Items[0].ItemSHA256 {
		t.Fatal("policy descriptor lost its sealed item identity")
	}
	originalDigest := descriptor.DescriptorSHA256
	descriptor.DescriptorSHA256 = ""
	bytes, _ := json.Marshal(descriptor)
	digest := sha256.Sum256(bytes)
	if originalDigest != hex.EncodeToString(digest[:]) {
		t.Fatal("policy descriptor digest is not stable")
	}
	for _, scenario := range []struct {
		name      string
		authority *policyFixture
		sources   []Source
		want      error
	}{
		{"revoked before read", &policyFixture{revokedAt: 1}, sources, ErrUnauthorized},
		{"collection grant is not export authority", &policyFixture{purpose: "collection"}, sources, ErrUnauthorized},
		{"expired export grant", &policyFixture{expired: true}, sources, ErrUnauthorized},
		{"revoked after read", &policyFixture{revokedAt: 3}, sources, ErrUnauthorized},
		{"policy unavailable", &policyFixture{denied: true}, sources, ErrPolicy},
		{"foreign dependency", &policyFixture{foreign: true}, sources, ErrPolicy},
		{"unregistered native link", &policyFixture{missingLink: true}, sources, ErrPolicy},
		{"false shared label", &policyFixture{falseShared: true}, sources, ErrPolicy},
		{"foreign source", &policyFixture{}, []Source{sources[0], {Tool: "jira", CellID: "cell", TenantID: "tenant", ProjectID: "foreign"}}, ErrUnauthorized},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			_, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, scenario.sources, "", 3, 8192, scenario.authority)
			if !errors.Is(err, scenario.want) {
				t.Fatalf("unsafe export: %v", err)
			}
		})
	}
	private := &policyFixture{private: true}
	if authorized, err := ExportAuthorizedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", 3, 8192, private); err != nil || authorized.Policies[0].Envelope.Audience != "private" {
		t.Fatalf("private policy lost: %v", err)
	}
}

func TestExportPolicyRejectsForgedNestedIdentityAndMissingProof(t *testing.T) {
	_, _, items := batchFixture()
	observation := Observation{SubjectRef: qualify(items[0].Key, items[0].Revision), SourceNativeKey: items[0].Key, Relationships: items[0].Links, ResolvedRelationships: []QualifiedRelationship{{qualify(items[0].Key, items[0].Revision), qualify(items[1].Key, items[1].Revision), "source-reference", "relation-1"}}}
	authority := &policyFixture{}
	envelope, _ := authority.DeriveEvidence(context.Background(), Source{}, observation.SubjectRef)
	if err := checkExportEnvelope(observation, envelope); err != nil {
		t.Fatal(err)
	}
	observation.Relationships[0].To.ProjectID = "foreign"
	if err := checkExportEnvelope(observation, envelope); !errors.Is(err, ErrPolicy) {
		t.Fatalf("foreign nested reference admitted: %v", err)
	}
	observation.Relationships[0].To.ProjectID = items[1].Key.ProjectID
	observation.ResolvedRelationships[0].To.RecordRevision = "latest"
	if err := checkExportEnvelope(observation, envelope); !errors.Is(err, ErrPolicy) {
		t.Fatalf("unpinned relationship revision accepted: %v", err)
	}
	observation.Relationships = nil
	observation.ResolvedRelationships = nil
	envelope.DependencyVersions = nil
	if err := checkExportEnvelope(observation, envelope); !errors.Is(err, ErrPolicy) {
		t.Fatalf("missing dependency version admitted: %v", err)
	}
}

package collection

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"testing"
	"time"
)

func TestRemovalAndEmptyResultsPreserveNativeHistory(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	start := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	batches, sources := domainBatches(t, start)
	prior := bytes.Clone(batches[0].ManifestPayload)
	fixture := &domainFixture{batches: batches}
	reader := sql.OpenDB(domainConnector{fixture})
	defer reader.Close()
	current, err := QueryCollectedDomain(context.Background(), reader, sources, &fixtureAuthority{}, start, start.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	plan := DeltaPlan{Generation: 3}
	for _, item := range current.Items {
		plan.Changes = append(plan.Changes, Change{Key: item.Key, Action: "delete", DeleteReason: "absent_in_complete_source"})
	}
	removals, err := PrepareIncrementalItems(plan, nil, "p1", "fit-v1", "schema-v2")
	if err != nil {
		t.Fatal(err)
	}
	removed, err := PrepareSourceBatch("aaaaaaaa-bbbb-cccc-dddd-000000000003", "cell", 3, removals, sources)
	if err != nil || VerifyPreparedBatch(removed) != nil {
		t.Fatalf("removal cannot seal: %v", err)
	}
	fixture.batches = append(fixture.batches, removed)
	for range 2 {
		result, err := QueryCollectedDomain(context.Background(), reader, sources, &fixtureAuthority{}, start, start.Add(time.Hour))
		if err != nil || len(result.Items) != 0 || len(result.Issues) != 0 || len(result.PRs) != 0 {
			t.Fatalf("removed native records still visible: %+v, %v", result, err)
		}
	}
	if !bytes.Equal(batches[0].ManifestPayload, prior) || VerifyPreparedBatch(batches[0]) != nil {
		t.Fatal("B1 history changed")
	}
	empty, err := PrepareSourceBatch("aaaaaaaa-bbbb-cccc-dddd-000000000004", "cell", 4, nil, sources)
	if err != nil || VerifyPreparedBatch(empty) != nil || len(empty.ItemPayloads) != 0 {
		t.Fatalf("empty result fabricated payload: %+v, %v", empty, err)
	}
	fixture.batches = []PreparedBatch{empty}
	result, err := QueryCollectedDomain(context.Background(), reader, sources, &fixtureAuthority{}, start, start.Add(time.Hour))
	if err != nil || len(result.Items) != 0 {
		t.Fatalf("explicit empty sources unavailable: %+v, %v", result, err)
	}
	foreign := append([]Source(nil), sources...)
	foreign[1].Scope = "unselected"
	if _, err := QueryCollectedDomain(context.Background(), reader, foreign, &fixtureAuthority{}, start, start.Add(time.Hour)); err == nil {
		t.Fatal("empty source result claimed coverage of an unselected scope")
	}
}

func TestRemovalCannotCarryInventedNativeData(t *testing.T) {
	batchID, cellID, items := batchFixture()
	removal := items[0]
	removal.RemovalReason = "policy_excluded"
	if _, err := PrepareBatch(batchID, cellID, 1, []BatchItem{removal}); err == nil {
		t.Fatal("removal retained native data")
	}
	removal.Revision, removal.NativeBytes, removal.NormalizedBytes, removal.Links = "", nil, nil, nil
	if _, err := PrepareBatch(batchID, cellID, 1, []BatchItem{removal}); err != nil {
		t.Fatal(err)
	}
	removal.RemovalReason = "inferred"
	if _, err := PrepareBatch(batchID, cellID, 1, []BatchItem{removal}); err == nil {
		t.Fatal("unrecognized removal reason admitted")
	}
}

func TestEmptySealReadbackAndAuthorityRollback(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	sources := fixtureJob(time.Now()).Sources
	batch, err := PrepareSourceBatch("aaaaaaaa-bbbb-cccc-dddd-000000000001", sources[0].CellID, 1, nil, sources)
	if err != nil {
		t.Fatal(err)
	}
	state := &sealState{}
	database := sql.OpenDB(sealConnector{state})
	defer database.Close()
	updates := []CheckpointUpdate{{SourceKey: SourceCheckpointKey(sources[0]), NextCursor: "first"},
		{SourceKey: SourceCheckpointKey(sources[1]), NextCursor: "first"}}
	if _, err := SealPreparedBatch(context.Background(), database, sources, &expiringAuthority{revokeAt: 4}, batch, updates); err == nil || state.commits != 0 {
		t.Fatal("empty result committed after authority was revoked")
	}
	for range 2 {
		if _, err := SealPreparedBatch(context.Background(), database, sources, &fixtureAuthority{}, batch, updates); err != nil {
			t.Fatal(err)
		}
		if err := ReadSealedBatch(context.Background(), database, batch); err != nil {
			t.Fatal(err)
		}
	}
	if len(state.items) != 0 || len(state.stored) != 1 {
		t.Fatal("empty replay invented items or duplicate history")
	}
}

func TestIncrementalPreparationPreservesPlannedBytes(t *testing.T) {
	_, _, fixtures := batchFixture()
	native := fixtures[0]
	digest := sha256.Sum256(native.NativeBytes)
	plan := DeltaPlan{Changes: []Change{{Key: native.Key, Action: "upsert", Revision: native.Revision,
		NativeBytes: native.NativeBytes, SHA256: hex.EncodeToString(digest[:])},
		{Key: fixtures[1].Key, Action: "delete", DeleteReason: "policy_excluded"}}}
	items, err := PrepareIncrementalItems(plan, []BatchItem{native}, "p1", "f1", "s2")
	if err != nil || len(items) != 2 || !bytes.Equal(items[0].NativeBytes, native.NativeBytes) ||
		!bytes.Equal(items[0].NormalizedBytes, native.NormalizedBytes) || len(items[1].NativeBytes) != 0 {
		t.Fatalf("incremental preparation changed source data: %+v, %v", items, err)
	}
	tampered := native
	tampered.NativeBytes = []byte(`{"invented":true}`)
	if _, err := PrepareIncrementalItems(plan, []BatchItem{tampered}, "p1", "f1", "s2"); err == nil {
		t.Fatal("unplanned native bytes admitted")
	}
	if _, err := PrepareIncrementalItems(plan, []BatchItem{native, fixtures[1]}, "p1", "f1", "s2"); err == nil {
		t.Fatal("removed native bytes persisted as an upsert")
	}
}

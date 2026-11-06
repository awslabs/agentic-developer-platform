package collection

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"
)

func TestPersistedIncrementalPlansFromSealedHistoryWithoutAdvancingCursor(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	start := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	batches, sources := domainBatches(t, start)
	sealedReader := sql.OpenDB(domainConnector{&domainFixture{batches: batches}})
	defer sealedReader.Close()
	checkpoint := &receiptConnector{sourceKey: SourceCheckpointKey(sources[0]), cursor: "cursor-1"}
	writer := sql.OpenDB(checkpoint)
	defer writer.Close()
	generation := &fixtureGeneration{generation: 3, state: "collecting"}
	var changed NativeRecord
	for _, item := range batches[1].Items {
		if item.Key.Kind == "issue" {
			changed = NativeRecord{Key: item.Key, Revision: "r3", Payload: []byte(`{"title":"third"}`)}
		}
	}
	current := Reconciled{Allowed: []NativeRecord{changed}}
	plan := func(authority Authority, full bool) (DeltaPlan, CheckpointUpdate, error) {
		return PlanPersistedIncremental(context.Background(), writer, sealedReader, sources, sources[0], 3,
			authority, generation, current, "cursor-2", full, start, start.Add(24*time.Hour))
	}
	for range 2 {
		planned, update, err := plan(&fixtureAuthority{}, false)
		if err != nil || len(planned.Changes) != 1 || planned.Changes[0].Key != changed.Key ||
			planned.Changes[0].Action != "upsert" || !bytes.Equal(planned.Changes[0].NativeBytes, changed.Payload) ||
			planned.PreviousCursor != "cursor-1" || update.ExpectedCursor == nil ||
			*update.ExpectedCursor != "cursor-1" || update.NextCursor != "cursor-2" ||
			update.SourceKey != SourceCheckpointKey(sources[0]) || checkpoint.cursor != "cursor-1" {
			t.Fatalf("replayed plan advanced or invented cursor: %+v, %+v, %v", planned, update, err)
		}
	}
	if planned, _, err := plan(&fixtureAuthority{}, true); err != nil || len(planned.Changes) < 2 || checkpoint.cursor != "cursor-1" {
		t.Fatalf("complete deletion not planned without advancing cursor: %+v, %v", planned, err)
	}
	current = Reconciled{Allowed: []NativeRecord{changed}}
	for _, item := range batches[1].Items {
		if item.Key.Kind == "pull_request" {
			current.Exclusions = []Exclusion{{Key: item.Key, Reason: "private"}}
			break
		}
	}
	if planned, _, err := plan(&fixtureAuthority{}, false); err != nil || len(planned.Changes) != 2 || checkpoint.cursor != "cursor-1" {
		t.Fatalf("policy exclusion not planned without advancing cursor: %+v, %v", planned, err)
	}
	current.Exclusions = nil
	generation.generation = 4
	if _, _, err := plan(&fixtureAuthority{}, false); !errors.Is(err, ErrGenerationFenced) {
		t.Fatalf("stale generation planned: %v", err)
	}
	generation.generation = 3
	if _, _, err := plan(&expiringAuthority{revokeAt: 6}, false); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("revoked source planned: %v", err)
	}
}

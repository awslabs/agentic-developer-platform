package collection

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"testing"
	"time"
)

func incrementalFixture() (Source, map[NativeKey]RecordState, Reconciled) {
	source := fixtureJob(time.Now()).Sources[0]
	key := func(id string) NativeKey {
		return NativeKey{CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
			Tool: source.Tool, ConnectionID: source.ConnectionID, Scope: source.Scope, Kind: "issue", NativeID: id}
	}
	previous := map[NativeKey]RecordState{}
	for _, id := range []string{"1", "2", "3", "4"} {
		digest := sha256.Sum256([]byte("old-" + id))
		previous[key(id)] = RecordState{Revision: "old", SHA256: hex.EncodeToString(digest[:])}
	}
	current := Reconciled{Allowed: []NativeRecord{
		{Key: key("1"), Revision: "old", Payload: []byte("old-1")},
		{Key: key("2"), Revision: "new", Payload: []byte("updated")},
		{Key: key("5"), Revision: "new", Payload: []byte("created")},
	}, Exclusions: []Exclusion{{Key: key("3"), Reason: "private"}}}
	return source, previous, current
}

func TestPlanIncrementalOnlyDeletesAbsentRecordsAfterFullEnumeration(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	source, previous, current := incrementalFixture()
	partial, err := PlanIncremental(source, 2, "old-cursor", "new-cursor", previous, current, false)
	if err != nil || len(partial.Changes) != 3 {
		t.Fatalf("partial delta: %+v, %v", partial, err)
	}
	if partial.Changes[0].Action != "upsert" || partial.Changes[0].Key.NativeID != "2" ||
		partial.Changes[1].Action != "delete" || partial.Changes[1].DeleteReason != "policy_excluded" ||
		partial.Changes[2].Action != "upsert" || partial.Changes[2].Key.NativeID != "5" ||
		partial.ProposedCursor != "new-cursor" || previous[partial.Changes[0].Key].Revision != "old" {
		t.Fatalf("unexpected delta: %+v", partial)
	}
	complete, err := PlanIncremental(source, 2, "old-cursor", "new-cursor", previous, current, true)
	if err != nil || len(complete.Changes) != 4 || complete.Changes[2].Key.NativeID != "4" ||
		complete.Changes[2].DeleteReason != "absent_in_complete_source" {
		t.Fatalf("full enumeration failed to identify deletion: %+v, %v", complete, err)
	}
	original := bytes.Clone(complete.Changes[0].NativeBytes)
	current.Allowed[1].Payload[0] = 'X'
	if !bytes.Equal(complete.Changes[0].NativeBytes, original) {
		t.Fatal("new workspace page mutated a prepared change")
	}
}

func TestPlanIncrementalRejectsCollisionsStaleContextAndUnapprovedExclusions(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	for _, scenario := range []struct {
		name string
		edit func(*Source, map[NativeKey]RecordState, *Reconciled)
	}{
		{"duplicate", func(_ *Source, _ map[NativeKey]RecordState, current *Reconciled) {
			current.Allowed = append(current.Allowed, current.Allowed[0])
		}},
		{"foreign cell", func(_ *Source, _ map[NativeKey]RecordState, current *Reconciled) {
			current.Allowed[0].Key.CellID = "foreign"
		}},
		{"unapproved exclusion", func(_ *Source, _ map[NativeKey]RecordState, current *Reconciled) {
			current.Exclusions[0].Reason = "heuristic"
		}},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			source, previous, current := incrementalFixture()
			scenario.edit(&source, previous, &current)
			plan, err := PlanIncremental(source, 2, "old", "new", previous, current, true)
			if !errors.Is(err, ErrIncomplete) || len(plan.Changes) != 0 {
				t.Fatalf("invalid delta admitted: %+v, %v", plan, err)
			}
		})
	}
	source, previous, current := incrementalFixture()
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "false")
	if _, err := PlanIncremental(source, 2, "old", "new", previous, current, true); !errors.Is(err, ErrDisabled) {
		t.Fatalf("disabled delta admitted: %v", err)
	}
}

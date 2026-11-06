package collection

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
)

type RecordState struct {
	Revision string
	SHA256   string
}

type Change struct {
	Key          NativeKey
	Action       string
	Revision     string
	NativeBytes  []byte
	SHA256       string
	DeleteReason string
}

type DeltaPlan struct {
	Changes        []Change
	PreviousCursor string
	ProposedCursor string
	Generation     int64
}

func PlanIncremental(source Source, generation int64, previousCursor, proposedCursor string,
	previous map[NativeKey]RecordState, current Reconciled, fullEnumeration bool) (DeltaPlan, error) {
	if err := CheckFlags(); err != nil {
		return DeltaPlan{}, err
	}
	if generation < 1 || source.CellID == "" || source.ConnectionID == "" || previous == nil ||
		proposedCursor == "" {
		return DeltaPlan{}, ErrIncomplete
	}
	plan := DeltaPlan{PreviousCursor: previousCursor, ProposedCursor: proposedCursor, Generation: generation}
	seen := make(map[NativeKey]bool, len(current.Allowed)+len(current.Exclusions))
	for _, record := range current.Allowed {
		if !validKey(record.Key, source, source.ConnectionID) || seen[record.Key] ||
			record.Revision == "" || len(record.Payload) == 0 {
			return DeltaPlan{}, ErrIncomplete
		}
		seen[record.Key] = true
		digest := sha256.Sum256(record.Payload)
		encoded := hex.EncodeToString(digest[:])
		if prior, exists := previous[record.Key]; !exists || prior.Revision != record.Revision || prior.SHA256 != encoded {
			plan.Changes = append(plan.Changes, Change{Key: record.Key, Action: "upsert",
				Revision: record.Revision, NativeBytes: bytes.Clone(record.Payload), SHA256: encoded})
		}
	}
	for _, exclusion := range current.Exclusions {
		if !validKey(exclusion.Key, source, source.ConnectionID) || seen[exclusion.Key] ||
			(exclusion.Reason != "private" && exclusion.Reason != "out_of_scope") {
			return DeltaPlan{}, ErrIncomplete
		}
		seen[exclusion.Key] = true
		if _, exists := previous[exclusion.Key]; exists {
			plan.Changes = append(plan.Changes, Change{Key: exclusion.Key, Action: "delete", DeleteReason: "policy_excluded"})
		}
	}
	for key, prior := range previous {
		if !validKey(key, source, source.ConnectionID) || prior.Revision == "" || len(prior.SHA256) != 64 {
			return DeltaPlan{}, ErrIncomplete
		}
		if _, err := hex.DecodeString(prior.SHA256); err != nil {
			return DeltaPlan{}, ErrIncomplete
		}
		if !seen[key] && fullEnumeration {
			plan.Changes = append(plan.Changes, Change{Key: key, Action: "delete", DeleteReason: "absent_in_complete_source"})
		}
	}
	sort.Slice(plan.Changes, func(left, right int) bool {
		leftKey, _ := json.Marshal(plan.Changes[left].Key)
		rightKey, _ := json.Marshal(plan.Changes[right].Key)
		return bytes.Compare(leftKey, rightKey) < 0
	})
	return plan, nil
}

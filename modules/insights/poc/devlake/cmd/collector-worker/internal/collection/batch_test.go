package collection

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"testing"
)

func batchFixture() (string, string, []BatchItem) {
	batchID := "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
	cellID := "cell"
	first := NativeKey{TenantID: "tenant", ProjectID: "project", CellID: "cell", Tool: "github", ConnectionID: "github-connection", Scope: "repo", Kind: "issue", NativeID: "1"}
	second := NativeKey{TenantID: "tenant", ProjectID: "project", CellID: "cell", Tool: "jira", ConnectionID: "jira-connection", Scope: "board", Kind: "issue", NativeID: "1"}
	items := []BatchItem{
		{Key: first, Revision: "native-r1", NativeBytes: []byte(`{"body":"approved"}`), NormalizedBytes: []byte(`{"status":"open"}`),
			Links:         []EvidenceLink{{From: first, To: second, Relation: "source-reference", NativeEvidenceID: "relation-1"}},
			PolicyVersion: "policy-v1", FieldMapVersion: "field-map-v1", SchemaVersion: "schema-v2"},
		{Key: second, Revision: "native-r2", NativeBytes: []byte(`{"status":"Todo"}`), NormalizedBytes: []byte(`{"status":"new"}`),
			PolicyVersion: "policy-v1", FieldMapVersion: "field-map-v1", SchemaVersion: "schema-v2"},
	}
	return batchID, cellID, items
}

func TestPreparedBatchPinsBytesLinksAndPolicyAgainstLaterUpdates(t *testing.T) {
	batchID, cellID, items := batchFixture()
	first, err := PrepareBatch(batchID, cellID, 1, items)
	if err != nil || VerifyPreparedBatch(first) != nil || len(first.ItemDigests) != 2 {
		t.Fatalf("failed to prepare B1: %+v, %v", first, err)
	}
	items[0].NativeBytes[0] = 'X'
	items[0].Links[0].NativeEvidenceID = "changed"
	items[0].PolicyVersion = "policy-v2"
	if !bytes.Equal(first.Items[0].NativeBytes, []byte(`{"body":"approved"}`)) ||
		first.Items[0].Links[0].NativeEvidenceID != "relation-1" || VerifyPreparedBatch(first) != nil {
		t.Fatal("B1 changed when later workspace changed")
	}
	second, err := PrepareBatch("aaaaaaaa-bbbb-cccc-dddd-ffffffffffff", cellID, 2, items)
	if err != nil || second.ContentSHA256 == first.ContentSHA256 {
		t.Fatalf("B2 incorrectly reused B1: %+v, %v", second, err)
	}
	first.Items[0].PolicyVersion = "policy-v2"
	if !errors.Is(VerifyPreparedBatch(first), ErrBatch) {
		t.Fatal("changed policy was accepted against B1 digest")
	}
}

func TestPreparedBatchRejectsDuplicateOrHeuristicLinksAndConflictingReplay(t *testing.T) {
	batchID, cellID, items := batchFixture()
	first, _ := PrepareBatch(batchID, cellID, 1, items)
	for _, scenario := range []struct {
		name string
		edit func([]BatchItem) []BatchItem
	}{
		{"duplicate item", func(items []BatchItem) []BatchItem { return append(items, items[0]) }},
		{"invented production link", func(items []BatchItem) []BatchItem {
			items[0].Links[0].Relation = "production-deployment"
			return items
		}},
		{"unproven link", func(items []BatchItem) []BatchItem { items[0].Links[0].NativeEvidenceID = ""; return items }},
		{"duplicate link", func(items []BatchItem) []BatchItem {
			items[0].Links = append(items[0].Links, items[0].Links[0])
			return items
		}},
		{"foreign cell link", func(items []BatchItem) []BatchItem { items[0].Links[0].To.CellID = "foreign"; return items }},
		{"foreign project link", func(items []BatchItem) []BatchItem { items[0].Links[0].To.ProjectID = "foreign"; return items }},
		{"missing policy", func(items []BatchItem) []BatchItem { items[0].PolicyVersion = ""; return items }},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			_, _, fresh := batchFixture()
			if _, err := PrepareBatch(batchID, cellID, 1, scenario.edit(fresh)); !errors.Is(err, ErrBatch) {
				t.Fatalf("unsafe batch accepted: %v", err)
			}
		})
	}
	first.ItemDigests[0].SHA256 = "wrong"
	if !errors.Is(VerifyPreparedBatch(first), ErrBatch) {
		t.Fatal("conflicting readback accepted")
	}
}

func TestPreparedBatchRetainsTheExactHashedPayloads(t *testing.T) {
	batchID, cellID, items := batchFixture()
	prepared, err := PrepareBatch(batchID, cellID, 1, items)
	if err != nil || len(prepared.ItemPayloads) != len(prepared.Items) || len(prepared.ManifestPayload) == 0 {
		t.Fatalf("missing prepared bytes: %v", err)
	}
	for index, payload := range prepared.ItemPayloads {
		digest := sha256.Sum256(payload)
		if prepared.ItemDigests[index].SHA256 != hex.EncodeToString(digest[:]) {
			t.Fatal("item bytes differ from the prepared digest")
		}
	}
	digest := sha256.Sum256(prepared.ManifestPayload)
	if prepared.ContentSHA256 != hex.EncodeToString(digest[:]) {
		t.Fatal("manifest bytes differ from the prepared digest")
	}
	prepared.ItemPayloads[0][0] = 'X'
	if !errors.Is(VerifyPreparedBatch(prepared), ErrBatch) {
		t.Fatal("modified prepared bytes were accepted")
	}
}

package collection

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"testing"
)

func exportFixture(t *testing.T) (PreparedBatch, []Source, *sealState, *sql.DB) {
	t.Helper()
	batchID, cellID, items := batchFixture()
	sources := observationSources(items)
	removed := items[0].Key
	removed.Kind, removed.NativeID = "pull_request", "9"
	items = append(items, BatchItem{Key: removed, RemovalReason: "absent_in_complete_source", PolicyVersion: "policy-v1", FieldMapVersion: "field-map-v1", SchemaVersion: "schema-v2"})
	batch, err := PrepareSourceBatch(batchID, cellID, 1, items, sources)
	if err != nil {
		t.Fatal(err)
	}
	state := &sealState{stored: map[string]storedSeal{}}
	reader := sql.OpenDB(sealConnector{state})
	t.Cleanup(func() { reader.Close() })
	return batch, sources, state, reader
}

func storeExportFixture(batch PreparedBatch, state *sealState, itemCount int) {
	digest, _ := hex.DecodeString(batch.ContentSHA256)
	state.stored[batch.BatchID] = storedSeal{batchID: batch.BatchID, manifest: bytes.Clone(batch.ManifestPayload), digest: digest, items: append([][]byte(nil), batch.ItemPayloads[:itemCount]...)}
}

func TestExportSealedPageBoundedManifestOrderAndRemoval(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	batch, sources, state, reader := exportFixture(t)
	ctx := context.Background()
	if _, err := loadSealedPage(ctx, reader, batch.CellID, batch.BatchID, sources, "", 1, 4096); !errors.Is(err, ErrBatch) {
		t.Fatalf("unsealed batch exported: %v", err)
	}
	storeExportFixture(batch, state, 1)
	if _, err := loadSealedPage(ctx, reader, batch.CellID, batch.BatchID, sources, "", 1, 4096); !errors.Is(err, ErrBatch) {
		t.Fatalf("partial batch exported: %v", err)
	}
	storeExportFixture(batch, state, len(batch.Items))
	first, err := loadSealedPage(ctx, reader, batch.CellID, batch.BatchID, sources, "", 1, 4096)
	if err != nil {
		t.Fatal(err)
	}
	firstBytes, _ := json.Marshal(first)
	byteLimited, err := loadSealedPage(ctx, reader, batch.CellID, batch.BatchID, sources, "", 100, len(firstBytes)+1)
	if err != nil || len(byteLimited.Items) != 1 || byteLimited.Complete || byteLimited.NextCursor != first.Items[0].ItemSHA256 {
		t.Fatalf("byte budget skipped or overran a sealed page: %+v, %v", byteLimited, err)
	}
	after := ""
	for index, original := range batch.Items {
		page, err := loadSealedPage(ctx, reader, batch.CellID, batch.BatchID, sources, after, 1, 4096)
		if err != nil || len(page.Items) != 1 || page.ManifestSHA256 != batch.ContentSHA256 || page.SchemaVersion != ObservationSchemaVersion || page.MappingVersion != ObservationSchemaVersion {
			t.Fatalf("sealed page differs: %+v, %v", page, err)
		}
		encoded, _ := json.Marshal(page)
		if len(encoded) > 4096 || page.Items[0].ItemSHA256 != batch.ItemDigests[index].SHA256 {
			t.Fatalf("page exceeded limit or skipped item: %s", encoded)
		}
		if original.RemovalReason != "" {
			if page.Items[0].Observation != nil || page.Items[0].RemovalReason != original.RemovalReason {
				t.Fatalf("removal emitted as fact: %+v", page.Items[0])
			}
		} else if page.Items[0].Observation == nil || page.Items[0].Observation.SourceRevision != original.Revision {
			t.Fatalf("source revision missing: %+v", page.Items[0])
		}
		if index == len(batch.Items)-1 {
			if !page.Complete || page.NextCursor != "" {
				t.Fatalf("last page incomplete: %+v", page)
			}
		} else {
			if page.Complete || page.NextCursor != page.Items[0].ItemSHA256 {
				t.Fatalf("cursor missing: %+v", page)
			}
			after = page.NextCursor
		}
	}
	if _, err := loadSealedPage(ctx, reader, batch.CellID, batch.BatchID, sources, "invalid", 1, 4096); !errors.Is(err, ErrBatch) {
		t.Fatalf("invalid cursor exported: %v", err)
	}
}

func TestExportSealedPageRejectsOversizedAndTamperedInput(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	batch, sources, state, reader := exportFixture(t)
	storeExportFixture(batch, state, len(batch.Items))
	for _, scenario := range []struct {
		cell        string
		count, size int
		sources     []Source
	}{
		{batch.CellID, 0, 4096, sources}, {batch.CellID, 101, 4096, sources},
		{batch.CellID, 1, 511, sources}, {batch.CellID, 1, MaxExportPageBytes + 1, sources},
		{batch.CellID, 1, 512, sources}, {"foreign", 1, 4096, sources},
		{batch.CellID, 1, 4096, sources[:1]},
	} {
		page, err := loadSealedPage(context.Background(), reader, scenario.cell, batch.BatchID, scenario.sources, "", scenario.count, scenario.size)
		if !errors.Is(err, ErrBatch) {
			t.Fatalf("invalid export returned %+v, %v", page, err)
		}
	}
	stored := state.stored[batch.BatchID]
	stored.items = append([][]byte(nil), stored.items...)
	stored.items[0] = []byte(`{"workspace":"later-edit"}`)
	state.stored[batch.BatchID] = stored
	if _, err := loadSealedPage(context.Background(), reader, batch.CellID, batch.BatchID, sources, "", 1, 4096); !errors.Is(err, ErrBatch) {
		t.Fatalf("modified sealed item exported: %v", err)
	}
}

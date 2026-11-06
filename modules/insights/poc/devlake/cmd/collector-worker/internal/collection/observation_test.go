package collection

import (
	"bytes"
	"encoding/json"
	"errors"
	"testing"
)

func observationSources(items []BatchItem) []Source {
	return []Source{
		{CellID: items[0].Key.CellID, TenantID: items[0].Key.TenantID, ProjectID: items[0].Key.ProjectID,
			Tool: "github", ConnectionID: "github-connection", Scope: "repo", BindingID: "github-binding", BindingGeneration: 3, BindingRevision: 2},
		{CellID: items[1].Key.CellID, TenantID: items[1].Key.TenantID, ProjectID: items[1].Key.ProjectID,
			Tool: "jira", ConnectionID: "jira-connection", Scope: "board", BindingID: "jira-binding", BindingGeneration: 4, BindingRevision: 5},
	}
}

func TestMapPreparedObservationsPinsNativeBytesAndVersions(t *testing.T) {
	batchID, cellID, items := batchFixture()
	sources := observationSources(items)
	batch, err := PrepareSourceBatch(batchID, cellID, 1, items, sources)
	if err != nil {
		t.Fatal(err)
	}
	observations, err := MapPreparedObservations(batch, sources)
	if err != nil || len(observations) != 2 {
		t.Fatalf("could not map both native sources: %v, %+v", err, observations)
	}
	github := observations[0]
	if github.SchemaVersion != "devlake-observation/1.0" || github.MappingVersion != github.SchemaVersion ||
		github.SourceRef != qualify(items[0].Key, "native-r1") || github.SubjectRef != github.SourceRef || github.SourceNativeKey != items[0].Key || github.SourceRevision != "native-r1" ||
		github.Binding != (ObservationBinding{"github-binding", 3, 2}) ||
		github.SourceSnapshot.BatchID != batchID || github.SourceSnapshot.ManifestSHA256 != batch.ContentSHA256 ||
		github.SourceSnapshot.ItemSHA256 != batch.ItemDigests[0].SHA256 ||
		github.NativeSHA256 != "2a7f39d04a552d83edc15c18b627292104b1eacc7d01b378826f7829a7f66866" ||
		github.NormalizedSHA256 != "bc99066a8b813e763201c114d47dd9300710ba0acf1c4e522ae78e2c5c473b87" ||
		!bytes.Equal(github.NativeBytes, []byte(`{"body":"approved"}`)) ||
		github.FieldMapVersion != "field-map-v1" || github.SourceSchema != "schema-v2" || github.SourcePolicy != "policy-v1" ||
		len(github.Relationships) != 1 || github.Relationships[0].NativeEvidenceID != "relation-1" ||
		github.ResolvedRelationships[0].To.RecordRevision != "native-r2" || github.ResolvedRelationships[0].To.SourceConnectionID != "jira-connection" {
		t.Fatalf("native source oracle differs: %+v", github)
	}
	if observations[1].SourceNativeKey.Tool != "jira" || observations[1].SourceRef.RecordID != github.SourceRef.RecordID || observations[1].SourceRef.SourceConnectionID == github.SourceRef.SourceConnectionID ||
		observations[1].Binding.ID != "jira-binding" || observations[1].ObservationStatus != "observation_only" ||
		len(observations[1].Relationships) != 0 {
		t.Fatalf("colliding local ID or absent Story link misrepresented: %+v", observations[1])
	}
	items[0].NativeBytes[0] = 'X'
	batch.Items[0].NativeBytes[0] = 'Y'
	batch.Items[0].Links[0].NativeEvidenceID = "later-edit"
	if !bytes.Equal(github.NativeBytes, []byte(`{"body":"approved"}`)) || github.Relationships[0].NativeEvidenceID != "relation-1" ||
		github.ResolvedRelationships[0].To.RecordRevision != "native-r2" || github.ResolvedRelationships[0].To.SourceConnectionID != "jira-connection" {
		t.Fatal("mapped bytes and links changed with the workspace")
	}
	serialized, err := json.Marshal(github)
	if err != nil || !bytes.Contains(serialized, []byte(`"observation_status":"observation_only"`)) ||
		bytes.Contains(serialized, []byte(`"story"`)) || bytes.Contains(serialized, []byte(`"ingested_at"`)) {
		t.Fatalf("mapping invented lineage or timestamps: %s, %v", serialized, err)
	}
}

func TestMapPreparedObservationsRejectsWrongSourceAndModifiedBatch(t *testing.T) {
	batchID, cellID, items := batchFixture()
	sources := observationSources(items)
	batch, err := PrepareSourceBatch(batchID, cellID, 1, items, sources)
	if err != nil {
		t.Fatal(err)
	}
	wrong := append([]Source(nil), sources...)
	wrong[0].ProjectID = "foreign"
	if _, err := MapPreparedObservations(batch, wrong); !errors.Is(err, ErrBatch) {
		t.Fatalf("foreign project mapped: %v", err)
	}
	wrong = append([]Source(nil), sources...)
	wrong[0].BindingID = ""
	if _, err := MapPreparedObservations(batch, wrong); !errors.Is(err, ErrBatch) {
		t.Fatalf("missing binding mapped: %v", err)
	}
	batch.ItemPayloads[0][0] = 'X'
	if _, err := MapPreparedObservations(batch, sources); !errors.Is(err, ErrBatch) {
		t.Fatalf("tampered item mapped: %v", err)
	}
}

func TestPreparedBatchPinsAndVerifiesBindingSnapshots(t *testing.T) {
	batchID, cellID, items := batchFixture()
	sources := observationSources(items)
	batch, err := PrepareSourceBatch(batchID, cellID, 1, items, sources)
	if err != nil {
		t.Fatal(err)
	}
	reordered, err := PrepareSourceBatch(batchID, cellID, 1, items, []Source{sources[1], sources[0]})
	if err != nil || !bytes.Equal(batch.ManifestPayload, reordered.ManifestPayload) {
		t.Fatal("source order changed the sealed binding snapshot")
	}
	for _, edit := range []func(*Source){
		func(source *Source) { source.BindingID = "" },
		func(source *Source) { source.BindingGeneration = 0 },
		func(source *Source) { source.BindingRevision = 0 },
	} {
		invalid := append([]Source(nil), sources...)
		edit(&invalid[0])
		if _, err := PrepareSourceBatch(batchID, cellID, 1, items, invalid); !errors.Is(err, ErrBatch) {
			t.Fatalf("invalid binding snapshot prepared: %v", err)
		}
	}
	key := SourceCheckpointKey(sources[0])
	batch.SourceBindings[key] = ObservationBinding{"forged-binding", 9, 9}
	if err := VerifyPreparedBatch(batch); !errors.Is(err, ErrBatch) {
		t.Fatalf("binding mutation was not authenticated by the manifest: %v", err)
	}
	if _, err := MapPreparedObservations(batch, sources); !errors.Is(err, ErrBatch) {
		t.Fatalf("mutated binding mapped: %v", err)
	}
}

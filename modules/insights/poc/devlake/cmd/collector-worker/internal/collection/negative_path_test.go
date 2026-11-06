package collection

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

func TestTwoSourcePagesAndSealRejectCollisionsAndStaleWork(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	job := fixtureJob(time.Now())
	key := func(source Source) NativeKey {
		return NativeKey{CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
			Tool: source.Tool, ConnectionID: source.ConnectionID, Scope: source.Scope, Kind: "issue", NativeID: "1"}
	}
	github, jira := key(job.Sources[0]), key(job.Sources[1])
	record := func(nativeKey NativeKey) NativeRecord {
		return NativeRecord{Key: nativeKey, Revision: "r1", Payload: json.RawMessage(`{"number":1}`)}
	}
	classifier := &fixtureClassifier{decisions: map[NativeKey]Decision{
		github: {Disposition: "allow", Fields: map[string]bool{"number": true}},
		jira:   {Disposition: "allow", Fields: map[string]bool{"number": true}},
	}}
	expected := map[Source][]NativeKey{job.Sources[0]: {github}, job.Sources[1]: {jira}}
	provider := func() *fixtureNativeProvider {
		return &fixtureNativeProvider{pages: map[string][]NativePage{
			"github": {{Records: []NativeRecord{record(github)}, Complete: true}},
			"jira":   {{Records: []NativeRecord{record(jira)}, Complete: true}},
		}, requests: map[string]int{}, generation: &fixtureGeneration{generation: 1, state: "collecting"}}
	}
	collect := func(native *fixtureNativeProvider, authority Authority) ([]Reconciled, error) {
		return CollectVerifiedPages(context.Background(), job, 1, authority, native.generation,
			native, classifier, expected)
	}
	native := provider()
	results, err := collect(native, &fixtureAuthority{})
	if err != nil || len(results) != 2 || results[0].Allowed[0].Key == results[1].Allowed[0].Key ||
		results[0].Allowed[0].Key.NativeID != results[1].Allowed[0].Key.NativeID {
		t.Fatalf("tool identity collision: %+v, %v", results, err)
	}
	items := make([]BatchItem, 0, 2)
	for _, result := range results {
		for _, allowed := range result.Allowed {
			items = append(items, BatchItem{Key: allowed.Key, Revision: allowed.Revision,
				NativeBytes: allowed.Payload, NormalizedBytes: []byte(`{"status":"open"}`),
				PolicyVersion: "policy-v1", FieldMapVersion: "map-v1", SchemaVersion: "schema-v2"})
		}
	}
	batch, err := PrepareBatch("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "cell", 1, items)
	if err != nil {
		t.Fatal(err)
	}
	checkpoint := []CheckpointUpdate{{SourceKey: SourceCheckpointKey(job.Sources[0]), NextCursor: "github-complete"},
		{SourceKey: SourceCheckpointKey(job.Sources[1]), NextCursor: "jira-complete"}}
	state := &sealState{}
	database := sql.OpenDB(sealConnector{state})
	defer database.Close()
	if _, err := SealPreparedBatch(context.Background(), database, job.Sources, &fixtureAuthority{}, batch, checkpoint); err != nil ||
		len(state.stored) != 1 || ReadSealedBatch(context.Background(), database, batch) != nil {
		t.Fatalf("distinct native identities were not sealed exactly: %v", err)
	}
	negative := provider()
	negative.pages["github"][0].Records = append(negative.pages["github"][0].Records, record(github))
	if result, err := collect(negative, &fixtureAuthority{}); !errors.Is(err, ErrIncomplete) || result != nil || negative.requests["jira"] != 0 {
		t.Fatalf("duplicate provider identity escaped: %+v, %v", result, err)
	}
	negative = provider()
	negative.pages["github"][0].Records[0].Key.Tool = "jira"
	if result, err := collect(negative, &fixtureAuthority{}); !errors.Is(err, ErrIncomplete) || result != nil || negative.requests["jira"] != 0 {
		t.Fatalf("spoofed tool identity escaped: %+v, %v", result, err)
	}
	negative = provider()
	if result, err := collect(negative, &expiringAuthority{revokeAt: 4}); !errors.Is(err, ErrUnauthorized) || result != nil ||
		negative.requests["jira"] != 1 {
		t.Fatalf("revoked second page escaped: %+v, %v", result, err)
	}
	negative = provider()
	negative.pages["github"] = nil
	if result, err := collect(negative, &fixtureAuthority{}); !errors.Is(err, ErrSourceUnavailable) || result != nil ||
		negative.requests["jira"] != 0 {
		t.Fatalf("failed provider page escaped: %+v, %v", result, err)
	}
	badItems := append([]BatchItem(nil), items...)
	badItems[0].Links = []EvidenceLink{{From: github, To: jira,
		Relation: "production-deployment", NativeEvidenceID: "unsupported"}}
	if _, err := PrepareBatch("aaaaaaaa-bbbb-cccc-dddd-ffffffffffff", "cell", 2, badItems); !errors.Is(err, ErrBatch) || len(state.stored) != 1 {
		t.Fatalf("heuristic production link became a fact: %v", err)
	}
	stale := &workspaceState{generation: 2, state: "collecting"}
	writer := sql.OpenDB(&workspaceConnector{stale})
	defer writer.Close()
	write := func(transaction *sql.Tx) error {
		_, err := transaction.ExecContext(context.Background(), "INSERT INTO public.fixture VALUES (1)")
		return err
	}
	if err := WriteFencedWorkspaceSources(context.Background(), writer, job.Sources, 1, &fixtureAuthority{}, write); !errors.Is(err, ErrGenerationFenced) || stale.writes != 0 || stale.commits != 0 {
		t.Fatalf("stale workspace write escaped: %+v, %v", stale, err)
	}
}

package worker

import (
	"bytes"
	"context"
	"database/sql"
	"os"
	"testing"
	"time"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models/common"
	_ "github.com/jackc/pgx/v5/stdlib"
)

type postgresNativeAuthority struct{ revoked bool }

func (authority *postgresNativeAuthority) CurrentSource(_ context.Context, source collection.Source) (collection.CurrentSource, error) {
	if authority.revoked {
		return collection.CurrentSource{}, collection.ErrUnauthorized
	}
	return collection.CurrentSource{Source: source, BindingActive: true, CellActive: true, GrantActive: true,
		GrantPurpose: "collection", GrantExpires: time.Now().Add(time.Hour)}, nil
}

func TestPostgresNativeWorkerConversion(t *testing.T) {
	if os.Getenv("DEVLAKE_S03_WRITER_DSN") == "" {
		t.Skip("requires disposable migrated PostgreSQL")
	}
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	writer, err := sql.Open("pgx", os.Getenv("DEVLAKE_S03_WRITER_DSN"))
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	reader, err := sql.Open("pgx", os.Getenv("DEVLAKE_S03_READER_DSN"))
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	source := collection.Source{Tool: "sonarqube", TenantID: "tenant-one", ProjectID: "project-one", CellID: "fixture-cell",
		ConnectionID: "credential-opaque", BindingID: "binding-one", BindingGeneration: 1, BindingRevision: 1,
		GrantID: "grant-one", Consumer: "registered-worker", Scope: "api"}
	raw := []byte(`{"key":"ISSUE-1","project":"api","message":"Native issue","status":"OPEN","creationDate":"2026-10-01T00:00:00Z","updateDate":"2026-10-02T00:00:00Z"}`)
	projectKey := collection.NativeKey{CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
		Tool: source.Tool, ConnectionID: source.ConnectionID, Scope: source.Scope, Kind: "project", NativeID: "api"}
	issueKey := projectKey
	issueKey.Kind, issueKey.NativeID = "issue", "ISSUE-1"
	records := []collection.NativeRecord{
		{Key: projectKey, Revision: "project-v1", Payload: []byte(`{"key":"api","name":"Example"}`)},
		{Key: issueKey, Revision: "issue-v1", Payload: raw},
	}
	authority := &postgresNativeAuthority{}
	var items []collection.BatchItem
	_, _, err = stageAndProcessNativeRows(t.Context(), writer, source, 1, authority, records,
		func(ctx context.Context, tx *sql.Tx, connectionID uint64, origins map[collection.NativeKey]common.RawDataOrigin) error {
			if err := seedNativeScope(ctx, tx, source, connectionID, records, origins); err != nil {
				return err
			}
			if err := invokeNativeTasks(ctx, tx, source, connectionID); err != nil {
				return err
			}
			items, err = readNativeDomainRows(ctx, tx, source, connectionID, records, origins, "policy-one")
			return err
		})
	if err != nil {
		t.Fatalf("pinned conversion failed: %v", err)
	}
	if len(items) != 1 || items[0].Revision != "issue-v1" || string(items[0].NativeBytes) != string(raw) || len(items[0].Links) != 0 {
		t.Fatalf("independent native issue origin was lost: %+v", items)
	}
	linear := source
	linear.Tool, linear.Scope, linear.ConnectionID, linear.BindingID, linear.GrantID = "linear", "team", "linear-credential", "binding-two", "grant-two"
	linearKey := collection.NativeKey{CellID: linear.CellID, TenantID: linear.TenantID, ProjectID: linear.ProjectID,
		Tool: linear.Tool, ConnectionID: linear.ConnectionID, Scope: linear.Scope, Kind: "team", NativeID: "team"}
	linearIssueKey := linearKey
	linearIssueKey.Kind, linearIssueKey.NativeID = "issue", "issue-1"
	linearRaw := []byte(`{"id":"issue-1","identifier":"PL-1","title":"Plan","createdAt":"2026-10-01T00:00:00Z","updatedAt":"2026-10-02T00:00:00Z","state":{"type":"started"}}`)
	linearRecords := []collection.NativeRecord{
		{Key: linearKey, Revision: "team-v1", Payload: []byte(`{"id":"team","name":"Planning","key":"PL","description":"Native planning"}`)},
		{Key: linearIssueKey, Revision: "linear-v1", Payload: linearRaw},
	}
	var linearItems []collection.BatchItem
	_, _, err = stageAndProcessNativeRows(t.Context(), writer, linear, 1, authority, linearRecords,
		func(ctx context.Context, tx *sql.Tx, connectionID uint64, origins map[collection.NativeKey]common.RawDataOrigin) error {
			if err := seedNativeScope(ctx, tx, linear, connectionID, linearRecords, origins); err != nil {
				return err
			}
			if err := invokeNativeTasks(ctx, tx, linear, connectionID); err != nil {
				return err
			}
			linearItems, err = readNativeDomainRows(ctx, tx, linear, connectionID, linearRecords, origins, "policy-one")
			return err
		})
	if err != nil || len(linearItems) != 2 {
		t.Fatalf("independent Linear task failed: %d %v", len(linearItems), err)
	}
	for _, item := range linearItems {
		if item.Key.Kind == "boards" && !bytes.Contains(item.NormalizedBytes, []byte("Native planning")) {
			t.Fatal("selected Linear team description was lost")
		}
	}
	items = append(items, linearItems...)
	sources := []collection.Source{source, linear}
	batch, err := collection.PrepareSourceBatch("aaaaaaaa-bbbb-cccc-dddd-000000000019", source.CellID, 1, items, sources)
	if err != nil {
		t.Fatal(err)
	}
	updates := []collection.CheckpointUpdate{{SourceKey: collection.SourceCheckpointKey(source), NextCursor: "complete"},
		{SourceKey: collection.SourceCheckpointKey(linear), NextCursor: "complete"}}
	if _, err := collection.SealPreparedBatch(t.Context(), writer, sources, authority, batch, updates); err != nil {
		t.Fatalf("persisted native seal failed: %v", err)
	}
	if err := collection.ReadSealedBatch(t.Context(), reader, batch); err != nil {
		t.Fatalf("readback failed: %v", err)
	}
	var count int
	if err := reader.QueryRowContext(t.Context(), "SELECT count(*) FROM devlake_meta.collection_batch_items WHERE batch_id = $1", batch.BatchID).Scan(&count); err != nil || count != 3 {
		t.Fatalf("sealed native row unavailable: %d %v", count, err)
	}
	domain, err := collection.QueryCollectedDomain(t.Context(), reader, sources, authority,
		time.Now().UTC().Add(-24*time.Hour), time.Now().UTC())
	if err != nil || len(domain.Items) != 3 {
		t.Fatalf("independent cross-tool readback failed: %d %v", len(domain.Items), err)
	}
	for _, item := range domain.Items {
		if len(item.Links) != 0 {
			t.Fatal("unverified native evidence acquired a join")
		}
		if item.Key.Tool == "linear" && item.Key.Kind == "issues" && string(item.NativeBytes) != string(linearRaw) {
			t.Fatal("Linear evidence no longer matches its native origin")
		}
	}
	foreign := linear
	foreign.CellID = "foreign-cell"
	if _, err := collection.QueryCollectedDomain(t.Context(), reader, []collection.Source{source, foreign}, authority,
		time.Now().UTC().Add(-24*time.Hour), time.Now().UTC()); err == nil {
		t.Fatal("foreign cell received a cross-tool export")
	}
	authority.revoked = true
	if _, err := collection.QueryCollectedDomain(t.Context(), reader, sources, authority,
		time.Now().UTC().Add(-24*time.Hour), time.Now().UTC()); err == nil {
		t.Fatal("revoked grant received a cross-tool export")
	}
}

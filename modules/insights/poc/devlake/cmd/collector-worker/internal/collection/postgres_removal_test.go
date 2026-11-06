package collection

import (
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"testing"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestPostgresIncrementalRemoval(t *testing.T) {
	writerDSN, readerDSN := os.Getenv("DEVLAKE_S03_WRITER_DSN"), os.Getenv("DEVLAKE_S03_READER_DSN")
	if writerDSN == "" && readerDSN == "" {
		t.Skip("requires disposable migrated PostgreSQL writer and reader roles")
	}
	if writerDSN == "" || readerDSN == "" {
		t.Fatal("both PostgreSQL roles are required")
	}
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	writer, err := sql.Open("pgx", writerDSN)
	if err != nil {
		t.Fatal("writer configuration failed")
	}
	defer writer.Close()
	reader, err := sql.Open("pgx", readerDSN)
	if err != nil {
		t.Fatal("reader configuration failed")
	}
	defer reader.Close()
	ctx := context.Background()
	now := time.Now().UTC()
	job := fixtureJob(now)
	var items []BatchItem
	for index := range job.Sources {
		source := &job.Sources[index]
		source.CellID, source.TenantID, source.ProjectID = "fixture-cell", "tenant-one", "project-one"
		key := NativeKey{CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
			Tool: source.Tool, ConnectionID: source.ConnectionID, Scope: source.Scope, Kind: "issue", NativeID: "42"}
		native := []byte("{ \"id\":42, \"title\":\"kept <exact>\" }")
		normalized, err := json.Marshal(CommonIssue{Key: key, Revision: "r1", FieldMap: "f1", Native: native, Title: "kept <exact>"})
		if err != nil {
			t.Fatal(err)
		}
		items = append(items, BatchItem{Key: key, Revision: "r1", NativeBytes: native,
			NormalizedBytes: normalized, PolicyVersion: "p1", FieldMapVersion: "f1", SchemaVersion: "s2"})
	}
	authority := &fixtureAuthority{}
	migration := SQLMigration{DB: writer, ResourceID: "cluster-FIXTURE123"}
	begin := func(expected int64, attempt string) int64 {
		t.Helper()
		generation, err := StartGeneration(ctx, writer, job, authority, migration,
			os.Getenv("DEVLAKE_S03_MANIFEST_DIGEST"), now, expected, "operation-one", attempt)
		if err != nil {
			t.Fatalf("generation admission failed: %v", err)
		}
		return generation
	}
	first, err := PrepareSourceBatch("aaaaaaaa-bbbb-cccc-dddd-000000000001", "fixture-cell", begin(0, "first"), items, job.Sources)
	if err != nil {
		t.Fatal(err)
	}
	updates := []CheckpointUpdate{{SourceKey: SourceCheckpointKey(job.Sources[0]), NextCursor: "first"},
		{SourceKey: SourceCheckpointKey(job.Sources[1]), NextCursor: "first"}}
	_, err = PersistAndSealPreparedBatch(ctx, writer, job.Sources, authority, first, updates, func(transaction *sql.Tx) error {
		_, err := transaction.ExecContext(ctx, "INSERT INTO public._raw_github_api_issues (params, data, input) VALUES ('rollback-fixture', $1, '{}'::json)", items[0].NativeBytes)
		authority.wrong = true
		return err
	})
	authority.wrong = false
	if err == nil {
		t.Fatal("post-write revoked authority committed")
	}
	for _, table := range []string{"public._raw_github_api_issues", "devlake_meta.collection_batch_manifests", "devlake_meta.collection_checkpoints"} {
		queryDB := reader
		if table == "devlake_meta.collection_checkpoints" {
			queryDB = writer
		}
		var count int
		if err := queryDB.QueryRowContext(ctx, "SELECT count(*) FROM "+table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("workspace/seal/checkpoint rollback failed for %s: %d, %v", table, count, err)
		}
	}
	if _, err := SealPreparedBatch(ctx, writer, job.Sources, authority, first, updates); err != nil {
		t.Fatalf("initial seal failed: %v", err)
	}
	query := func() CollectedDomain {
		t.Helper()
		result, err := QueryCollectedDomain(ctx, reader, job.Sources, authority, now.Add(-time.Hour), now)
		if err != nil {
			t.Fatalf("domain query failed: %v", err)
		}
		return result
	}
	if len(query().Items) != 2 {
		t.Fatal("initial sources missing")
	}
	generation := begin(1, "second")
	var removals []BatchItem
	for index, source := range job.Sources {
		current := Reconciled{}
		if index == 1 {
			current.Exclusions = []Exclusion{{Key: items[index].Key, Reason: "private"}}
		}
		plan, update, err := PlanPersistedIncremental(ctx, writer, reader, job.Sources, source, generation,
			authority, SQLGenerationReader{DB: writer}, current, "second", index == 0, now.Add(-time.Hour), now)
		if err != nil || len(plan.Changes) != 1 || plan.Changes[0].Action != "delete" {
			t.Fatalf("removal planning failed: %+v, %v", plan, err)
		}
		prepared, err := PrepareIncrementalItems(plan, nil, "p1", "f1", "s2")
		if err != nil {
			t.Fatal(err)
		}
		removals = append(removals, prepared...)
		updates[index] = update
	}
	second, err := PrepareSourceBatch("aaaaaaaa-bbbb-cccc-dddd-000000000002", "fixture-cell", generation, removals, job.Sources)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := SealPreparedBatch(ctx, writer, job.Sources, &expiringAuthority{revokeAt: 4}, second, updates); err == nil {
		t.Fatal("revoked removal sealed")
	}
	if len(query().Items) != 2 {
		t.Fatal("rolled-back removal changed current records")
	}
	for range 2 {
		if _, err := SealPreparedBatch(ctx, writer, job.Sources, authority, second, updates); err != nil {
			t.Fatalf("removal seal/replay failed: %v", err)
		}
		if len(query().Items) != 0 || ReadSealedBatch(ctx, reader, first) != nil || ReadSealedBatch(ctx, reader, second) != nil {
			t.Fatal("removed records resurrected or sealed bytes changed")
		}
	}
	empty, err := PrepareSourceBatch("aaaaaaaa-bbbb-cccc-dddd-000000000003", "fixture-cell", begin(2, "third"), nil, job.Sources)
	if err != nil {
		t.Fatal(err)
	}
	previous := "second"
	for index := range updates {
		updates[index].ExpectedCursor, updates[index].NextCursor = &previous, "third"
	}
	if _, err := SealPreparedBatch(ctx, writer, job.Sources, authority, empty, updates); err != nil {
		t.Fatalf("empty seal failed: %v", err)
	}
	if len(query().Items) != 0 || ReadSealedBatch(ctx, reader, empty) != nil {
		t.Fatal("empty manifest not readable")
	}
	for _, source := range job.Sources {
		cursor, err := ReadSourceCheckpoint(ctx, writer, source)
		if err != nil || cursor == nil || *cursor != "third" {
			t.Fatal("checkpoint did not advance atomically")
		}
	}
}

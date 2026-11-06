package worker

import (
	"bytes"
	"context"
	"database/sql"
	"os"
	"testing"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models/common"
	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestPostgresMalformedNativeRollback(t *testing.T) {
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
	source := collection.Source{Tool: "pagerduty", TenantID: "tenant-one", ProjectID: "project-one", CellID: "fixture-cell",
		ConnectionID: "credential-opaque", Consumer: "registered-worker", Scope: "P123"}
	key := collection.NativeKey{CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
		Tool: source.Tool, ConnectionID: source.ConnectionID, Scope: source.Scope, Kind: "service", NativeID: "P123"}
	incidentKey := key
	incidentKey.Kind, incidentKey.NativeID = "incident", "INC1"
	records := []collection.NativeRecord{
		{Key: key, Revision: "service-v1", Payload: []byte(`{"id":"P123","name":"Operations"}`)},
		{Key: incidentKey, Revision: "incident-v1", Payload: []byte(`{"id":"INC1","title":"Partial","service":{"id":"P123"},"status":"triggered","created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-02T00:00:00Z"}`)},
	}
	_, _, err = stageAndProcessNativeRows(t.Context(), writer, source, 1, &postgresNativeAuthority{}, records,
		func(ctx context.Context, tx *sql.Tx, connectionID uint64, origins map[collection.NativeKey]common.RawDataOrigin) error {
			if err := seedNativeScope(ctx, tx, source, connectionID, records, origins); err != nil {
				return err
			}
			return invokeNativeTasks(ctx, tx, source, connectionID)
		})
	if err == nil {
		t.Fatal("incomplete native incident was published")
	}
	for _, table := range []string{"_raw_pagerduty_services", "_raw_pagerduty_incidents", "_tool_pagerduty_services"} {
		var count int
		if err := reader.QueryRowContext(t.Context(), "SELECT count(*) FROM public."+table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("malformed native input left staged rows in %s: %d %v", table, count, err)
		}
	}
}

func TestPostgresPagerDutyConversion(t *testing.T) {
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
	source := collection.Source{Tool: "pagerduty", TenantID: "tenant-one", ProjectID: "project-one", CellID: "fixture-cell",
		ConnectionID: "credential-opaque", Consumer: "registered-worker", Scope: "P123"}
	key := collection.NativeKey{CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
		Tool: source.Tool, ConnectionID: source.ConnectionID, Scope: source.Scope, Kind: "service", NativeID: "P123"}
	incidentKey := key
	incidentKey.Kind, incidentKey.NativeID = "incident", "INC1"
	payload := []byte(`{"id":"INC1","title":"Alert","service":{"id":"P123","summary":"Operations"},"incident_number":7,"html_url":"https://example.invalid/INC1","summary":"Alert","status":"triggered","urgency":"high","created_at":"2026-10-01T00:00:00Z","last_status_change_at":"2026-10-02T00:00:00Z"}`)
	records := []collection.NativeRecord{
		{Key: key, Revision: "service-v1", Payload: []byte(`{"id":"P123","name":"Operations","html_url":"https://example.invalid/services/P123"}`)},
		{Key: incidentKey, Revision: "incident-v1", Payload: payload},
	}
	var items []collection.BatchItem
	_, _, err = stageAndProcessNativeRows(t.Context(), writer, source, 1, &postgresNativeAuthority{}, records,
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
	if err != nil || len(items) != 2 {
		t.Fatalf("PagerDuty native task failed: %d %v", len(items), err)
	}
	for _, item := range items {
		if item.Key.Kind == "boards" && !bytes.Contains(item.NormalizedBytes, []byte("https://example.invalid/services/P123")) {
			t.Fatal("selected PagerDuty service URL was lost")
		}
		if item.Key.Kind == "issues" && (string(item.NativeBytes) != string(payload) || len(item.Links) != 0) {
			t.Fatal("PagerDuty incident lost its independent native origin")
		}
	}
}

package collection

import (
	"context"
	"database/sql"
	"errors"
	"testing"
)

func TestNativeConnectionMappingRequiresScopedActiveGeneration(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	source := Source{Tool: "jenkins", CellID: "cell", TenantID: "tenant", ProjectID: "project", ConnectionID: "credential-opaque"}
	connector := &receiptConnector{nativeID: 7}
	db := sql.OpenDB(connector)
	defer db.Close()
	selected, err := ResolveNativeConnection(context.Background(), db, source, 3)
	if err != nil || selected != 7 || connector.reads != 1 {
		t.Fatalf("native numeric identity did not bind to opaque selection: %d, %v", selected, err)
	}
	for _, change := range []func(*Source){
		func(value *Source) { value.Tool = "github" },
		func(value *Source) { value.CellID = "foreign" },
		func(value *Source) { value.TenantID = "foreign" },
		func(value *Source) { value.ProjectID = "foreign" },
		func(value *Source) { value.ConnectionID = "" },
	} {
		wrong := source
		change(&wrong)
		if _, err := ResolveNativeConnection(context.Background(), db, wrong, 3); !errors.Is(err, ErrUnauthorized) {
			t.Fatalf("unbound selection was assigned a native ID: %v", err)
		}
	}
	if _, err := ResolveNativeConnection(context.Background(), db, source, 0); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("missing generation assigned a native ID: %v", err)
	}
	connector.nativeErr = errors.New("generation retired")
	if _, err := ResolveNativeConnection(context.Background(), db, source, 3); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("retired generation returned native ID: %v", err)
	}
	connector.nativeErr, connector.nativeID = nil, 0
	if _, err := ResolveNativeConnection(context.Background(), db, source, 3); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("zero native ID accepted: %v", err)
	}
}

package collection

import (
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"
)

type expiringAuthority struct {
	reads    int
	revokeAt int
}

func (authority *expiringAuthority) CurrentSource(_ context.Context, source Source) (CurrentSource, error) {
	authority.reads++
	return CurrentSource{Source: source, BindingActive: true, CellActive: true,
		GrantActive:  authority.revokeAt == 0 || authority.reads < authority.revokeAt,
		GrantPurpose: "collection", GrantExpires: time.Now().Add(time.Hour)}, nil
}

func TestStartGenerationAdmitsTwoSourcesBeforeCellMutation(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	for _, scenario := range []struct {
		name          string
		revokeAt      int
		migrationFail bool
		want          error
		starts        int
		cancels       int
	}{
		{"current", 0, false, nil, 1, 0},
		{"absent migration", 0, true, ErrMigrationRequired, 0, 0},
		{"revoked after start", 4, false, ErrUnauthorized, 1, 1},
		{"revoked before start", 2, false, ErrUnauthorized, 0, 0},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			connector := &receiptConnector{resourceID: "db-fixture", generation: 1, state: "collecting", digest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
			pool := sql.OpenDB(connector)
			defer pool.Close()
			authority := &expiringAuthority{revokeAt: scenario.revokeAt}
			migration := Migration(SQLMigration{DB: pool, ResourceID: "db-fixture"})
			if scenario.migrationFail {
				connector.digest = ""
			}
			now := time.Now()
			generation, err := StartGeneration(context.Background(), pool, fixtureJob(now), authority, migration,
				"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", now, 0, "operation", "attempt")
			if !errors.Is(err, scenario.want) && (scenario.want != nil || err != nil) {
				t.Fatalf("start error %v, expected %v", err, scenario.want)
			}
			if connector.starts != scenario.starts || connector.cancels != scenario.cancels {
				t.Fatalf("starts %d cancels %d", connector.starts, connector.cancels)
			}
			if scenario.want == nil && generation != 1 {
				t.Fatalf("generation %d, expected 1", generation)
			}
		})
	}
}

func TestStartGenerationDisabledOrUnownedNeverTouchesDatabase(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "false")
	connector := &receiptConnector{generation: 1, state: "collecting"}
	pool := sql.OpenDB(connector)
	defer pool.Close()
	now := time.Now()
	generation, err := StartGeneration(context.Background(), pool, fixtureJob(now), &expiringAuthority{}, &fixtureMigration{},
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", now, 0, "operation", "attempt")
	if generation != 0 || !errors.Is(err, ErrDisabled) || connector.reads != 0 {
		t.Fatalf("disabled generation %d, error %v, reads %d", generation, err, connector.reads)
	}
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	_, err = StartGeneration(context.Background(), pool, fixtureJob(now), &expiringAuthority{}, &fixtureMigration{},
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", now, 0, "", "attempt")
	if !errors.Is(err, ErrGenerationFenced) || connector.reads != 0 {
		t.Fatalf("missing operation: %v, reads %d", err, connector.reads)
	}
}

func TestSQLGenerationReaderRetainsCellScope(t *testing.T) {
	connector := &receiptConnector{generation: 7, state: "sealed"}
	pool := sql.OpenDB(connector)
	defer pool.Close()
	generation, state, err := (SQLGenerationReader{DB: pool}).CurrentGeneration(context.Background(), "cell")
	if err != nil || generation != 7 || state != "sealed" || connector.reads != 1 {
		t.Fatalf("generation %d state %q error %v reads %d", generation, state, err, connector.reads)
	}
	generation, _, err = (SQLGenerationReader{DB: pool}).CurrentGeneration(context.Background(), "foreign")
	if !errors.Is(err, ErrGenerationFenced) || generation != 0 {
		t.Fatalf("foreign generation %d error %v", generation, err)
	}
}

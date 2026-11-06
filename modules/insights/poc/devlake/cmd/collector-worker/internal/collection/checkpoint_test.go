package collection

import (
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"
)

func TestReadSourceCheckpointUsesCompleteSourceIdentity(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	source := fixtureJob(time.Now()).Sources[0]
	for _, scenario := range []struct {
		name   string
		cursor string
		change func(*Source)
		want   error
		reads  int
	}{
		{"first scope", "", func(*Source) {}, nil, 1},
		{"incremental scope", "cursor-2", func(*Source) {}, nil, 1},
		{"different connection", "cursor-2", func(source *Source) { source.ConnectionID = "other" }, ErrIncomplete, 1},
		{"missing scope", "cursor-2", func(source *Source) { source.Scope = "" }, ErrIncomplete, 0},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			connector := &receiptConnector{cursor: scenario.cursor, sourceKey: SourceCheckpointKey(source)}
			pool := sql.OpenDB(connector)
			defer pool.Close()
			selected := source
			scenario.change(&selected)
			cursor, err := ReadSourceCheckpoint(context.Background(), pool, selected)
			if !errors.Is(err, scenario.want) && (scenario.want != nil || err != nil) {
				t.Fatalf("checkpoint error %v, expected %v", err, scenario.want)
			}
			if connector.reads != scenario.reads {
				t.Fatalf("reads %d", connector.reads)
			}
			if scenario.want == nil && ((scenario.cursor == "" && cursor != nil) ||
				(scenario.cursor != "" && (cursor == nil || *cursor != scenario.cursor))) {
				t.Fatalf("cursor %+v, expected %q", cursor, scenario.cursor)
			}
		})
	}
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "false")
	connector := &receiptConnector{sourceKey: SourceCheckpointKey(source)}
	pool := sql.OpenDB(connector)
	defer pool.Close()
	if _, err := ReadSourceCheckpoint(context.Background(), pool, source); !errors.Is(err, ErrDisabled) || connector.reads != 0 {
		t.Fatalf("disabled checkpoint read: %v, queries %d", err, connector.reads)
	}
}

func TestExpandedSourceCheckpointRequiresExactIdentity(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	source := fixtureJob(time.Now()).Sources[0]
	source.Tool, source.Scope = "gitlab", "project:7"
	connector := &receiptConnector{cursor: "cursor-r2", sourceKey: SourceCheckpointKey(source)}
	pool := sql.OpenDB(connector)
	defer pool.Close()
	cursor, err := ReadSourceCheckpoint(context.Background(), pool, source)
	if err != nil || cursor == nil || *cursor != "cursor-r2" || connector.reads != 1 {
		t.Fatalf("expanded checkpoint: %v, %v, reads %d", cursor, err, connector.reads)
	}
	source.Scope = "foreign-project"
	cursor, err = ReadSourceCheckpoint(context.Background(), pool, source)
	if !errors.Is(err, ErrIncomplete) || cursor != nil {
		t.Fatalf("foreign checkpoint: %v, %v", cursor, err)
	}
	source.Tool = "unsupported"
	reads := connector.reads
	cursor, err = ReadSourceCheckpoint(context.Background(), pool, source)
	if !errors.Is(err, ErrIncomplete) || cursor != nil || connector.reads != reads {
		t.Fatalf("unsupported checkpoint: %v, %v, reads %d", cursor, err, connector.reads)
	}
}

package collection

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"io"
	"strings"
	"testing"
)

type receiptConnector struct {
	resourceID string
	digest     string
	reads      int
	starts     int
	cancels    int
	generation int64
	state      string
	cursor     string
	sourceKey  string
	nativeID   int64
	nativeErr  error
}

type receiptConnection struct{ connector *receiptConnector }

type receiptRows struct {
	digest string
	read   bool
}

func (connector *receiptConnector) Connect(context.Context) (driver.Conn, error) {
	return receiptConnection{connector}, nil
}
func (connector *receiptConnector) Driver() driver.Driver { return connector }
func (connector *receiptConnector) Open(string) (driver.Conn, error) {
	return receiptConnection{connector}, nil
}
func (connection receiptConnection) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("prepared statements not supported")
}
func (connection receiptConnection) Close() error              { return nil }
func (connection receiptConnection) Begin() (driver.Tx, error) { return nil, errors.New("read only") }
func (connection receiptConnection) QueryContext(_ context.Context, query string, arguments []driver.NamedValue) (driver.Rows, error) {
	connection.connector.reads++
	if query == "SELECT devlake_meta.native_connection($1, $2, $3, $4, $5, $6)" {
		if len(arguments) != 6 || arguments[0].Value != "cell" || arguments[1].Value != "tenant" ||
			arguments[2].Value != "project" || arguments[3].Value != "jenkins" ||
			arguments[4].Value != "credential-opaque" || arguments[5].Value != int64(3) {
			return nil, errors.New("wrong native connection scope")
		}
		if connection.connector.nativeErr != nil {
			return nil, connection.connector.nativeErr
		}
		return &receiptNumberRows{number: connection.connector.nativeID}, nil
	}
	if query == "SELECT cursor_value FROM devlake_meta.collection_checkpoints WHERE cell_id = $1 AND source_key = $2" {
		if len(arguments) != 2 || arguments[0].Value != "cell" || arguments[1].Value != connection.connector.sourceKey {
			return nil, errors.New("wrong checkpoint scope")
		}
		return &checkpointRows{cursor: connection.connector.cursor}, nil
	}
	if query == "SELECT devlake_meta.begin_collection($1, $2, $3, $4, $5, $6)" {
		if len(arguments) != 6 || arguments[0].Value != "cell" || arguments[1].Value != int64(0) ||
			arguments[2].Value != "tenant" || arguments[3].Value != "project" ||
			arguments[4].Value != "operation" || arguments[5].Value != "attempt" {
			return nil, errors.New("invalid generation owner")
		}
		connection.connector.starts++
		return &receiptNumberRows{number: connection.connector.generation}, nil
	}
	if query == "SELECT generation, state FROM devlake_meta.collection_generations WHERE cell_id = $1" {
		if len(arguments) != 1 || arguments[0].Value != "cell" {
			return nil, errors.New("invalid cell")
		}
		return &receiptNumberRows{number: connection.connector.generation, state: connection.connector.state}, nil
	}
	if !strings.Contains(query, "FROM devlake_meta.schema_receipts") ||
		!strings.Contains(query, "schema_version = 2") || !strings.Contains(query, "status = 'applied'") ||
		len(arguments) != 2 || arguments[0].Value != "cell" ||
		arguments[1].Value != connection.connector.resourceID {
		return nil, errors.New("receipt query must use registered cell and resource")
	}
	if connection.connector.digest == "" {
		return &receiptRows{}, nil
	}
	return &receiptRows{digest: connection.connector.digest}, nil
}
func (connection receiptConnection) ExecContext(_ context.Context, query string, arguments []driver.NamedValue) (driver.Result, error) {
	if query != "SELECT devlake_meta.cancel_collection($1, $2)" || len(arguments) != 2 ||
		arguments[0].Value != "cell" || arguments[1].Value != int64(1) {
		return nil, errors.New("invalid cancellation")
	}
	connection.connector.cancels++
	return driver.RowsAffected(1), nil
}

type checkpointRows struct {
	cursor string
	read   bool
}

func (rows *checkpointRows) Columns() []string { return []string{"cursor_value"} }
func (rows *checkpointRows) Close() error      { return nil }
func (rows *checkpointRows) Next(values []driver.Value) error {
	if rows.read || rows.cursor == "" {
		return io.EOF
	}
	rows.read = true
	values[0] = rows.cursor
	return nil
}

type receiptNumberRows struct {
	number int64
	state  string
	read   bool
}

func (rows *receiptNumberRows) Columns() []string {
	if rows.state != "" {
		return []string{"generation", "state"}
	}
	return []string{"generation"}
}
func (rows *receiptNumberRows) Close() error { return nil }
func (rows *receiptNumberRows) Next(values []driver.Value) error {
	if rows.read || rows.number == 0 {
		return io.EOF
	}
	rows.read = true
	values[0] = rows.number
	if rows.state != "" {
		values[1] = rows.state
	}
	return nil
}

func (rows *receiptRows) Columns() []string { return []string{"manifest_sha256"} }
func (rows *receiptRows) Close() error      { return nil }
func (rows *receiptRows) Next(values []driver.Value) error {
	if rows.read || rows.digest == "" {
		return io.EOF
	}
	rows.read = true
	values[0] = rows.digest
	return nil
}

func TestSQLMigrationRequiresMatchingCellResourceAndReceipt(t *testing.T) {
	const digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	for _, scenario := range []struct {
		name       string
		stored     string
		resourceID string
		cellID     string
		want       error
		reads      int
	}{
		{"matching", digest, "db-fixture", "cell", nil, 1},
		{"absent", "", "db-fixture", "cell", ErrMigrationRequired, 1},
		{"stale", strings.Repeat("b", 64), "db-fixture", "cell", ErrMigrationRequired, 1},
		{"wrong resource", digest, "db-other", "cell", ErrMigrationRequired, 1},
		{"wrong cell", digest, "db-fixture", "foreign", ErrMigrationRequired, 1},
		{"missing resource", digest, "", "cell", ErrMigrationRequired, 0},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			connector := &receiptConnector{resourceID: "db-fixture", digest: scenario.stored}
			pool := sql.OpenDB(connector)
			defer pool.Close()
			err := (SQLMigration{DB: pool, ResourceID: scenario.resourceID}).CheckReceipt(
				context.Background(), scenario.cellID, digest)
			if !errors.Is(err, scenario.want) && (scenario.want != nil || err != nil) {
				t.Fatalf("receipt: %v, expected %v", err, scenario.want)
			}
			if connector.reads != scenario.reads {
				t.Fatalf("read count: %d, expected %d", connector.reads, scenario.reads)
			}
		})
	}
}

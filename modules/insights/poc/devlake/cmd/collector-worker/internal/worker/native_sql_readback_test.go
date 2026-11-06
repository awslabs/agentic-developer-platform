package worker

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models/common"
)

type domainReadConnector struct {
	foreign bool
	queries int
}
type domainReadConn struct{ state *domainReadConnector }
type domainReadRows struct {
	columns []string
	values  []driver.Value
	read    bool
}
type domainReadTx struct{}

func (state *domainReadConnector) Connect(context.Context) (driver.Conn, error) {
	return &domainReadConn{state}, nil
}
func (state *domainReadConnector) Driver() driver.Driver { return domainReadDriver{} }

type domainReadDriver struct{}

func (domainReadDriver) Open(string) (driver.Conn, error)   { return nil, errors.New("use connector") }
func (*domainReadConn) Prepare(string) (driver.Stmt, error) { return nil, errors.New("no statements") }
func (*domainReadConn) Close() error                        { return nil }
func (*domainReadConn) Begin() (driver.Tx, error)           { return domainReadTx{}, nil }
func (domainReadTx) Commit() error                          { return nil }
func (domainReadTx) Rollback() error                        { return nil }
func (connection *domainReadConn) QueryContext(_ context.Context, query string, arguments []driver.NamedValue) (driver.Rows, error) {
	if !strings.Contains(query, "_raw_data_table") || !strings.Contains(query, "_raw_data_params") || !strings.Contains(query, "_raw_data_id") || len(arguments) != 4 ||
		arguments[0].Value != "_raw_jenkins_api_builds" || arguments[1].Value != `{"ConnectionId":17,"FullName":"team/api"}` || fmt.Sprint(arguments[2].Value) != "11" || arguments[3].Value != int64(2001) {
		return nil, fmt.Errorf("unscoped read: %s (%v)", query, arguments)
	}
	connection.state.queries++
	if strings.Contains(query, "cicd_pipelines") {
		rawID := int64(11)
		if connection.state.foreign {
			rawID = 99
		}
		return &domainReadRows{columns: []string{"id", "original_result", "_raw_data_table", "_raw_data_params", "_raw_data_id"},
			values: []driver.Value{"jenkins:JenkinsBuild:17:team/api#7", "UNSTABLE", "_raw_jenkins_api_builds", `{"ConnectionId":17,"FullName":"team/api"}`, rawID}}, nil
	}
	if strings.Contains(query, "cicd_tasks") {
		return &domainReadRows{columns: []string{"id"}}, nil
	}
	return nil, errors.New("unknown domain table")
}
func (rows *domainReadRows) Columns() []string { return rows.columns }
func (rows *domainReadRows) Close() error      { return nil }
func (rows *domainReadRows) Next(values []driver.Value) error {
	if rows.read || rows.values == nil {
		return io.EOF
	}
	rows.read = true
	copy(values, rows.values)
	return nil
}

func TestNativeSQLReadbackIsScopedToFencedStagedOrigin(t *testing.T) {
	source := collection.Source{Tool: "jenkins", TenantID: "tenant", ProjectID: "project", CellID: "cell", ConnectionID: "opaque", Scope: "team/api"}
	key := collection.NativeKey{Tool: source.Tool, TenantID: source.TenantID, ProjectID: source.ProjectID, CellID: source.CellID, ConnectionID: source.ConnectionID,
		Scope: source.Scope, Kind: "build", NativeID: "7"}
	record := collection.NativeRecord{Key: key, Revision: "rev-7", Payload: []byte(`{"number":7,"result":"UNSTABLE"}`)}
	staged, err := stageSpec(source, 17, record)
	if err != nil {
		t.Fatal(err)
	}
	origins := map[collection.NativeKey]common.RawDataOrigin{key: {RawDataTable: staged.table, RawDataParams: staged.params, RawDataId: 11}}
	for _, foreign := range []bool{false, true} {
		state := &domainReadConnector{foreign: foreign}
		db := sql.OpenDB(state)
		tx, err := db.BeginTx(t.Context(), nil)
		if err != nil {
			t.Fatal(err)
		}
		items, err := readNativeDomainRows(t.Context(), tx, source, 17, []collection.NativeRecord{record}, origins, "policy-3")
		_ = tx.Rollback()
		_ = db.Close()
		if foreign {
			if !errors.Is(err, collection.ErrIncomplete) {
				t.Fatalf("foreign SQL row accepted: %+v, %v", items, err)
			}
		} else if err != nil || len(items) != 1 || items[0].Revision != "rev-7" || items[0].RawDataOrigin.RawDataId != 11 || state.queries != 2 {
			t.Fatalf("scoped domain readback failed: %+v, queries=%d, %v", items, state.queries, err)
		}
	}
}

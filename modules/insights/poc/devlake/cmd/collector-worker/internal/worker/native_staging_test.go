package worker

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models/common"
)

func TestStageSpecPinsNativeTablesAndScopes(t *testing.T) {
	for _, scenario := range []struct {
		tool, kind, scope, table, params string
	}{
		{"sonarqube", "project", "api", "_raw_sonarqube_api_projects", `{"connectionId":17,"ProjectKey":"api"}`},
		{"sonarqube", "issue", "api", "_raw_sonarqube_api_issues", `{"connectionId":17,"ProjectKey":"api"}`},
		{"jenkins", "job", "team/api", "_raw_jenkins_api_jobs", `{"ConnectionId":17,"FullName":"team/api"}`},
		{"jenkins", "build", "team/api", "_raw_jenkins_api_builds", `{"ConnectionId":17,"FullName":"team/api"}`},
		{"argocd", "application", "api", "_raw_argocd_api_applications", `{"ConnectionId":17,"Name":"api"}`},
		{"argocd", "sync_operation", "api", "_raw_argocd_api_sync_operations", `{"ConnectionId":17,"Name":"api"}`},
		{"gitlab", "pipeline", "901", "_raw_gitlab_api_pipeline_details", `{"ConnectionId":17,"ProjectId":901}`},
		{"pagerduty", "service", "api", "_raw_pagerduty_services", `{"ConnectionId":17,"ScopeId":"api"}`},
		{"pagerduty", "incident", "api", "_raw_pagerduty_incidents", `{"ConnectionId":17,"ScopeId":"api"}`},
		{"linear", "team", "api", "_raw_linear_teams", `{"ConnectionId":17,"TeamId":"api"}`},
		{"linear", "issue", "api", "_raw_linear_issues", `{"ConnectionId":17,"TeamId":"api"}`},
		{"slack", "channel", "C123", "_raw_slack_channel", `{"connectionId":17}`},
		{"slack", "channel_message", "C123", "_raw_slack_channel_message", `{"connectionId":17,"scopeId":"C123"}`},
	} {
		t.Run(scenario.tool+"_"+scenario.kind, func(t *testing.T) {
			source := collection.Source{Tool: scenario.tool, TenantID: "tenant", ProjectID: "project", CellID: "cell", ConnectionID: "opaque", Scope: scenario.scope}
			key := collection.NativeKey{Tool: source.Tool, TenantID: source.TenantID, ProjectID: source.ProjectID,
				CellID: source.CellID, ConnectionID: source.ConnectionID, Scope: source.Scope, Kind: scenario.kind, NativeID: "native"}
			record := collection.NativeRecord{Key: key, Revision: "r1", Payload: []byte(`{"native":true}`)}
			result, err := stageSpec(source, 17, record)
			if err != nil || result.table != scenario.table || result.params != scenario.params {
				t.Fatalf("wrong native raw table or params: %+v, %v", result, err)
			}
			if scenario.tool == "slack" && scenario.kind == "channel_message" && string(result.input) != `{"channel_id":"C123"}` {
				t.Fatalf("Slack channel message lost selected input: %s", result.input)
			}
			for _, mutate := range []func(*collection.NativeRecord){
				func(value *collection.NativeRecord) { value.Key.CellID = "foreign" },
				func(value *collection.NativeRecord) { value.Key.Kind = "unknown" },
				func(value *collection.NativeRecord) { value.Payload = []byte(`[]`) },
			} {
				invalid := record
				mutate(&invalid)
				if _, err := stageSpec(source, 17, invalid); err == nil {
					t.Fatal("foreign or invalid raw record entered staging")
				}
			}
		})
	}
}

type rawStageState struct {
	locked, wrote, revoked, revokeOnWrite bool
	commits, rollbacks                    int
}
type rawStageConnector struct{ state *rawStageState }
type rawStageDriver struct{ state *rawStageState }
type rawStageConn struct{ state *rawStageState }
type rawStageTx struct{ state *rawStageState }
type rawStageRows struct {
	value int64
	done  bool
}

func (connector rawStageConnector) Connect(context.Context) (driver.Conn, error) {
	return rawStageConn{connector.state}, nil
}
func (connector rawStageConnector) Driver() driver.Driver { return rawStageDriver{connector.state} }
func (instance rawStageDriver) Open(string) (driver.Conn, error) {
	return rawStageConn{instance.state}, nil
}
func (connection rawStageConn) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("no statements")
}
func (connection rawStageConn) Close() error { return nil }
func (connection rawStageConn) Begin() (driver.Tx, error) {
	return rawStageTx{connection.state}, nil
}
func (connection rawStageConn) BeginTx(context.Context, driver.TxOptions) (driver.Tx, error) {
	return rawStageTx{connection.state}, nil
}
func (transaction rawStageTx) Commit() error {
	transaction.state.commits++
	return nil
}
func (transaction rawStageTx) Rollback() error {
	transaction.state.rollbacks++
	return nil
}
func (connection rawStageConn) ExecContext(_ context.Context, query string, arguments []driver.NamedValue) (driver.Result, error) {
	state := connection.state
	if query == "SELECT devlake_meta.lock_generation($1, $2)" && len(arguments) == 2 &&
		arguments[0].Value == "cell" && arguments[1].Value == int64(7) {
		state.locked = true
		return driver.RowsAffected(1), nil
	}
	if query == "DELETE FROM public._raw_sonarqube_api_projects WHERE params = $1" &&
		state.locked && len(arguments) == 1 && arguments[0].Value == `{"connectionId":17,"ProjectKey":"api"}` {
		return driver.RowsAffected(0), nil
	}
	return nil, errors.New("write without generation lock or selected scope")
}
func (connection rawStageConn) QueryContext(_ context.Context, query string, arguments []driver.NamedValue) (driver.Rows, error) {
	state := connection.state
	if !state.locked {
		return nil, errors.New("query without generation lock")
	}
	if query == "SELECT devlake_meta.native_connection($1, $2, $3, $4, $5, $6)" &&
		len(arguments) == 6 && arguments[0].Value == "cell" && arguments[1].Value == "tenant" &&
		arguments[2].Value == "project" && arguments[3].Value == "sonarqube" &&
		arguments[4].Value == "opaque" && arguments[5].Value == int64(7) {
		return &rawStageRows{value: 17}, nil
	}
	if query == "INSERT INTO public._raw_sonarqube_api_projects (params, data, input, created_at) VALUES ($1, $2, $3::json, now()) RETURNING id" &&
		len(arguments) == 3 && arguments[0].Value == `{"connectionId":17,"ProjectKey":"api"}` &&
		string(arguments[1].Value.([]byte)) == `{"key":"api"}` && string(arguments[2].Value.([]byte)) == `{}` {
		state.wrote = true
		if state.revokeOnWrite {
			state.revoked = true
		}
		return &rawStageRows{value: 21}, nil
	}
	return nil, errors.New("unscoped native lookup or insert")
}
func (*rawStageRows) Columns() []string { return []string{"id"} }
func (*rawStageRows) Close() error      { return nil }
func (rows *rawStageRows) Next(values []driver.Value) error {
	if rows.done {
		return io.EOF
	}
	rows.done = true
	values[0] = rows.value
	return nil
}

type rawStageAuthority struct{ state *rawStageState }

func (authority rawStageAuthority) CurrentSource(_ context.Context, source collection.Source) (collection.CurrentSource, error) {
	if authority.state.revoked {
		return collection.CurrentSource{}, errors.New("grant revoked")
	}
	return collection.CurrentSource{Source: source, BindingActive: true, CellActive: true, GrantActive: true,
		GrantPurpose: "collection", GrantExpires: time.Now().Add(time.Minute)}, nil
}

func TestStageNativeRowsLocksGenerationAndRollsBackRevocation(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	source := collection.Source{Tool: "sonarqube", CellID: "cell", TenantID: "tenant", ProjectID: "project", ConnectionID: "opaque", Scope: "api", Consumer: "insights"}
	key := collection.NativeKey{Tool: source.Tool, CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
		ConnectionID: source.ConnectionID, Scope: source.Scope, Kind: "project", NativeID: "api"}
	record := collection.NativeRecord{Key: key, Revision: "r1", Payload: []byte(`{"key":"api"}`)}
	for _, scenario := range []struct {
		name   string
		revoke bool
	}{{"current", false}, {"revoked_after_insert", true}} {
		t.Run(scenario.name, func(t *testing.T) {
			state := &rawStageState{revokeOnWrite: scenario.revoke}
			db := sql.OpenDB(rawStageConnector{state})
			defer db.Close()
			origins, nativeID, err := stageNativeRows(t.Context(), db, source, 7, rawStageAuthority{state}, []collection.NativeRecord{record})
			if scenario.revoke {
				if !errors.Is(err, collection.ErrUnauthorized) || state.commits != 0 || state.rollbacks != 1 || !state.wrote || len(origins) != 0 {
					t.Fatalf("revocation committed native rows: %+v %v", state, err)
				}
			} else if err != nil || nativeID != 17 || state.commits != 1 || !state.wrote || origins[key].RawDataId != 21 ||
				origins[key].RawDataTable != "_raw_sonarqube_api_projects" || !strings.Contains(origins[key].RawDataParams, `"connectionId":17`) {
				t.Fatalf("selected staged origin missing: %+v, %d, %+v, %v", origins, nativeID, state, err)
			}
		})
	}
}

func TestStageNativeRowsRollsBackNativeTaskFailure(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	source := collection.Source{Tool: "sonarqube", CellID: "cell", TenantID: "tenant", ProjectID: "project", ConnectionID: "opaque", Scope: "api", Consumer: "insights"}
	key := collection.NativeKey{Tool: source.Tool, CellID: source.CellID, TenantID: source.TenantID, ProjectID: source.ProjectID,
		ConnectionID: source.ConnectionID, Scope: source.Scope, Kind: "project", NativeID: "api"}
	state := &rawStageState{}
	db := sql.OpenDB(rawStageConnector{state})
	defer db.Close()
	called := false
	result, nativeID, err := stageAndProcessNativeRows(t.Context(), db, source, 7, rawStageAuthority{state},
		[]collection.NativeRecord{{Key: key, Revision: "r1", Payload: []byte(`{"key":"api"}`)}},
		func(ctx context.Context, tx *sql.Tx, connectionID uint64, origins map[collection.NativeKey]common.RawDataOrigin) error {
			called = true
			if ctx.Err() != nil || tx == nil || !state.locked || !state.wrote || connectionID != 17 || origins[key].RawDataId != 21 {
				t.Fatal("native tasks were not given locked, staged rows")
			}
			return errors.New("native conversion failed")
		})
	if !called || !errors.Is(err, collection.ErrGenerationFenced) || result != nil || nativeID != 0 || state.commits != 0 || state.rollbacks != 1 {
		t.Fatalf("task failure published partial native work: called=%t result=%v id=%d state=%+v err=%v", called, result, nativeID, state, err)
	}
}

package collection

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"testing"
	"time"
)

type fixtureGeneration struct {
	generation int64
	state      string
	reads      int
}

func (fixture *fixtureGeneration) CurrentGeneration(_ context.Context, _ string) (int64, string, error) {
	fixture.reads++
	return fixture.generation, fixture.state, nil
}

func TestFetchFencedPageRechecksAfterProviderRead(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	source := fixtureJob(time.Now()).Sources[0]
	for _, scenario := range []struct {
		name     string
		change   func(*fixtureAuthority, *fixtureGeneration)
		want     error
		requests int
	}{
		{"valid", func(*fixtureAuthority, *fixtureGeneration) {}, nil, 1},
		{"fenced during provider", func(_ *fixtureAuthority, generation *fixtureGeneration) { generation.generation = 2 }, ErrGenerationFenced, 1},
		{"revoked during provider", func(authority *fixtureAuthority, _ *fixtureGeneration) { authority.wrong = true }, ErrUnauthorized, 1},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			authority := &fixtureAuthority{}
			generation := &fixtureGeneration{generation: 1, state: "collecting"}
			requests := 0
			_, err := FetchFencedPage(context.Background(), source, 1, authority, generation,
				func(context.Context) (NativePage, error) {
					requests++
					scenario.change(authority, generation)
					return NativePage{Complete: true}, nil
				})
			if !errors.Is(err, scenario.want) || requests != scenario.requests {
				t.Fatalf("provider fence: %v, requests %d", err, requests)
			}
		})
	}
	t.Run("disabled during provider", func(t *testing.T) {
		authority := &fixtureAuthority{}
		generation := &fixtureGeneration{generation: 1, state: "collecting"}
		page, err := FetchFencedPage(context.Background(), source, 1, authority, generation,
			func(context.Context) (NativePage, error) {
				t.Setenv("DEVLAKE_COLLECTION_ENABLED", "false")
				return NativePage{Complete: true}, nil
			})
		if !errors.Is(err, ErrDisabled) || page.Complete {
			t.Fatalf("disabled page was exposed: %+v, %v", page, err)
		}
	})
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "false")
	requests := 0
	_, err := FetchFencedPage(context.Background(), source, 1, &fixtureAuthority{}, &fixtureGeneration{generation: 1, state: "collecting"},
		func(context.Context) (NativePage, error) { requests++; return NativePage{}, nil })
	if !errors.Is(err, ErrDisabled) || requests != 0 {
		t.Fatalf("disabled worker made a request: %v, requests %d", err, requests)
	}
}

type workspaceState struct {
	generation int64
	state      string
	writes     int
	commits    int
	rollbacks  int
	settings   []string
}

type workspaceDriver struct{ state *workspaceState }

type workspaceConn struct{ state *workspaceState }

type workspaceTx struct{ state *workspaceState }

func (driverInstance workspaceDriver) Open(string) (driver.Conn, error) {
	return workspaceConn{driverInstance.state}, nil
}
func (connection workspaceConn) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("unsupported")
}
func (connection workspaceConn) Close() error              { return nil }
func (connection workspaceConn) Begin() (driver.Tx, error) { return workspaceTx{connection.state}, nil }
func (connection workspaceConn) BeginTx(context.Context, driver.TxOptions) (driver.Tx, error) {
	return workspaceTx{connection.state}, nil
}
func (connection workspaceConn) QueryContext(context.Context, string, []driver.NamedValue) (driver.Rows, error) {
	return nil, errors.New("direct generation reads are not authorized")
}
func (connection workspaceConn) ExecContext(_ context.Context, query string, arguments []driver.NamedValue) (driver.Result, error) {
	if query == "INSERT INTO public.fixture VALUES (1)" {
		if len(connection.state.settings) != 2 {
			return nil, errors.New("write without generation lock")
		}
		connection.state.writes++
		return driver.RowsAffected(1), nil
	}
	if query != "SELECT devlake_meta.lock_generation($1, $2)" || len(arguments) != 2 ||
		arguments[0].Value != "cell" || arguments[1].Value != int64(1) {
		return nil, errors.New("missing cell generation lock")
	}
	if connection.state.generation != 1 || connection.state.state != "collecting" {
		return nil, errors.New("collection generation is inactive")
	}
	connection.state.settings = []string{"cell", "1"}
	return driver.RowsAffected(1), nil
}
func (transaction workspaceTx) Commit() error {
	transaction.state.commits++
	return nil
}
func (transaction workspaceTx) Rollback() error {
	transaction.state.rollbacks++
	return nil
}
func TestWriteFencedWorkspaceRequiresGenerationLockAndRollsBackRevocation(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	source := fixtureJob(time.Now()).Sources[0]
	for _, scenario := range []struct {
		name       string
		generation int64
		state      string
		revoke     bool
		want       error
		commits    int
	}{
		{"active", 1, "collecting", false, nil, 1},
		{"stale", 2, "collecting", false, ErrGenerationFenced, 0},
		{"cancelled", 1, "cancelled", false, ErrGenerationFenced, 0},
		{"revoked after write", 1, "collecting", true, ErrUnauthorized, 0},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			state := &workspaceState{generation: scenario.generation, state: scenario.state}
			database := sql.OpenDB(&workspaceConnector{state})
			defer database.Close()
			authority := &fixtureAuthority{}
			err := WriteFencedWorkspace(context.Background(), database, source, 1, authority, func(transaction *sql.Tx) error {
				_, err := transaction.ExecContext(context.Background(), "INSERT INTO public.fixture VALUES (1)")
				if scenario.revoke {
					authority.wrong = true
				}
				return err
			})
			if !errors.Is(err, scenario.want) || state.commits != scenario.commits {
				t.Fatalf("write fence: %v, commits %d", err, state.commits)
			}
			if scenario.want != nil && state.rollbacks != 1 {
				t.Fatalf("missing rollback: %+v", state)
			}
			if scenario.want == nil && (len(state.settings) != 2 || state.settings[0] != "cell" || state.settings[1] != "1") {
				t.Fatalf("guard settings: %+v", state)
			}
		})
	}
}

type selectiveAuthority struct {
	revoked  bool
	reads    int
	denyTool string
}

func (authority *selectiveAuthority) CurrentSource(_ context.Context, source Source) (CurrentSource, error) {
	authority.reads++
	denyTool := authority.denyTool
	if denyTool == "" {
		denyTool = "jira"
	}
	return CurrentSource{Source: source, BindingActive: true, CellActive: true,
		GrantActive: !authority.revoked || source.Tool != denyTool, GrantPurpose: "collection",
		GrantExpires: time.Now().Add(time.Hour)}, nil
}

func TestWriteFencedWorkspaceSourcesRechecksBothBeforeCommit(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	for _, scenario := range []struct {
		name          string
		revokeOnWrite bool
		wrongCell     bool
		want          error
		commits       int
	}{
		{"both current", false, false, nil, 1},
		{"second source revoked", true, false, ErrUnauthorized, 0},
		{"different cell", false, true, ErrGenerationFenced, 0},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			state := &workspaceState{generation: 1, state: "collecting"}
			database := sql.OpenDB(&workspaceConnector{state})
			defer database.Close()
			authority := &selectiveAuthority{}
			sources := fixtureJob(time.Now()).Sources
			if scenario.wrongCell {
				sources[1].CellID = "foreign"
			}
			err := WriteFencedWorkspaceSources(context.Background(), database, sources, 1, authority, func(transaction *sql.Tx) error {
				_, err := transaction.ExecContext(context.Background(), "INSERT INTO public.fixture VALUES (1)")
				authority.revoked = scenario.revokeOnWrite
				return err
			})
			if !errors.Is(err, scenario.want) && (scenario.want != nil || err != nil) {
				t.Fatalf("write error %v, expected %v", err, scenario.want)
			}
			if state.commits != scenario.commits {
				t.Fatalf("commits %d, expected %d", state.commits, scenario.commits)
			}
			if scenario.revokeOnWrite && state.rollbacks != 1 {
				t.Fatalf("revocation did not roll back: %+v", state)
			}
		})
	}
}

type workspaceConnector struct{ state *workspaceState }

func (connector *workspaceConnector) Connect(context.Context) (driver.Conn, error) {
	return workspaceConn{connector.state}, nil
}
func (connector *workspaceConnector) Driver() driver.Driver { return workspaceDriver{connector.state} }

func TestWriteFencedWorkspaceSourcesRechecksExpandedGrants(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	for _, scenario := range []struct {
		name   string
		mutate func([]Source)
		revoke bool
		want   error
	}{
		{"all current", func([]Source) {}, false, nil},
		{"last grant revoked after write", func([]Source) {}, true, ErrUnauthorized},
		{"last source foreign", func(sources []Source) { sources[8].CellID = "foreign" }, false, ErrGenerationFenced},
		{"duplicate provider", func(sources []Source) { sources[8].Tool = "linear" }, false, ErrGenerationFenced},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			sources := fixtureJob(time.Now()).Sources
			for _, provider := range []string{"sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear", "slack"} {
				source := sources[0]
				source.Tool, source.Scope, source.GrantID = provider, provider+"-scope", provider+"-grant"
				sources = append(sources, source)
			}
			scenario.mutate(sources)
			state := &workspaceState{generation: 1, state: "collecting"}
			database := sql.OpenDB(&workspaceConnector{state})
			defer database.Close()
			authority := &selectiveAuthority{denyTool: "slack"}
			err := WriteFencedWorkspaceSources(context.Background(), database, sources, 1, authority, func(transaction *sql.Tx) error {
				_, err := transaction.ExecContext(context.Background(), "INSERT INTO public.fixture VALUES (1)")
				authority.revoked = scenario.revoke
				return err
			})
			if !errors.Is(err, scenario.want) && (err != nil || scenario.want != nil) {
				t.Fatalf("workspace write: %v, expected %v", err, scenario.want)
			}
			if (scenario.want == nil && state.commits != 1) || (scenario.want != nil && state.commits != 0) {
				t.Fatalf("invalid commit: %+v", state)
			}
		})
	}
}

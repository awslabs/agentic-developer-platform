package worker

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
)

type nativeReadConnector struct{}
type nativeReadConn struct{}
type nativeReadRows struct{ used bool }

func (nativeReadConnector) Connect(context.Context) (driver.Conn, error) {
	return nativeReadConn{}, nil
}
func (nativeReadConnector) Driver() driver.Driver { return nativeReadDriver{} }

type nativeReadDriver struct{}

func (nativeReadDriver) Open(string) (driver.Conn, error)  { return nativeReadConn{}, nil }
func (nativeReadConn) Prepare(string) (driver.Stmt, error) { return nil, errors.New("no statements") }
func (nativeReadConn) Close() error                        { return nil }
func (nativeReadConn) Begin() (driver.Tx, error)           { return nil, errors.New("no transactions") }
func (nativeReadConn) QueryContext(_ context.Context, query string, arguments []driver.NamedValue) (driver.Rows, error) {
	if query != "SELECT generation, state FROM devlake_meta.collection_generations WHERE cell_id = $1" ||
		len(arguments) != 1 || arguments[0].Value != "cell" {
		return nil, errors.New("unscoped generation query")
	}
	return &nativeReadRows{}, nil
}
func (*nativeReadRows) Columns() []string { return []string{"generation", "state"} }
func (*nativeReadRows) Close() error      { return nil }
func (rows *nativeReadRows) Next(values []driver.Value) error {
	if rows.used {
		return io.EOF
	}
	rows.used = true
	values[0], values[1] = int64(7), "collecting"
	return nil
}

func TestCollectReadsOnlyCurrentSelectedNativePages(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	for _, revoke := range []bool{false, true} {
		name := "current"
		if revoke {
			name = "revoked_after_first_page"
		}
		t.Run(name, func(t *testing.T) {
			source := collection.Source{Tool: "sonarqube", CellID: "cell", TenantID: "tenant", ProjectID: "project", ConnectionID: "connection", Scope: "api"}
			current := SourceContext{Handle: "selected", Purpose: "collection", Expires: time.Now().Add(5 * time.Minute), Provider: "sonarqube", TenantID: "tenant", PolicyEnvelope: json.RawMessage(`{}`)}
			pages := 0
			server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
				response.Header().Set("Content-Type", "application/json")
				switch request.URL.Path {
				case "/internal/v1/collection-jobs/tenant/job/renew":
					_, _ = response.Write([]byte(`{"expires_at":"2099-01-01T00:00:00Z"}`))
				case "/internal/v1/connector-contexts/selected":
					value := current
					if revoke && pages > 0 {
						value.Provider = "foreign"
					}
					_ = json.NewEncoder(response).Encode(value)
				case "/internal/v1/connector-contexts/selected/page":
					if request.URL.Query().Get("limit") != "100" || request.URL.Query().Get("page") != "1" || pages >= 2 {
						t.Errorf("unexpected native page request: %s", request.URL)
					}
					kind := request.URL.Query().Get("kind")
					var body []byte
					switch kind {
					case "project":
						body = []byte(`{"component":{"key":"api","name":"Project"}}`)
					case "issue":
						body = []byte(`{"issues":[{"key":"ISSUE-1","project":"api","message":"Native issue","status":"OPEN","creationDate":"2026-10-01T00:00:00Z","updateDate":"2026-10-02T00:00:00Z"}]}`)
					default:
						t.Errorf("foreign native kind: %s", kind)
					}
					pages++
					_ = json.NewEncoder(response).Encode(map[string]any{"payload": body, "complete": true, "private": false})
				default:
					t.Errorf("unexpected gateway path: %s", request.URL.Path)
				}
			}))
			defer server.Close()
			writer := sql.OpenDB(nativeReadConnector{})
			defer writer.Close()
			gateway := &Gateway{URL: server.URL, Client: server.Client(), Sign: func(*http.Request, []byte) error { return nil },
				JobPath: "/internal/v1/collection-jobs/tenant/job", Claim: Claim{AttemptID: "attempt", Fence: 1},
				Contexts: map[collection.Source]SourceContext{source: current}}
			job := collection.Job{Sources: []collection.Source{source}, PageSize: 100, PageLimit: 5,
				Cutoff: time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC)}
			seen, items, err := collect(t.Context(), gateway, writer, source, job, 7)
			if revoke {
				if !errors.Is(err, collection.ErrUnauthorized) || pages != 1 || len(seen.Allowed) != 0 {
					t.Fatalf("revoked source issued a second page: pages=%d err=%v", pages, err)
				}
			} else if err != nil || pages != 2 || len(items) != 2 || len(seen.Allowed) != 2 ||
				seen.Allowed[1].Key.Kind != "issue" || seen.Allowed[1].Key.Scope != "api" {
				t.Fatalf("selected native pages lost: pages=%d allowed=%v items=%v err=%v", pages, seen.Allowed, len(items), err)
			}
		})
	}
}

package worker

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/celldb"
	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestPostgresNativeRun(t *testing.T) {
	if os.Getenv("DEVLAKE_S03_WRITER_DSN") == "" {
		t.Skip("requires disposable migrated PostgreSQL")
	}
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	now := time.Now().UTC().Add(-time.Second).Truncate(time.Second)
	created, updated := now.Add(-time.Hour).Format(time.RFC3339), now.Format(time.RFC3339)
	scope := map[string]string{"sonarqube": "api", "linear": "team"}
	kind := map[string]string{"sonarqube": "quality_project", "linear": "tracker_team"}
	pages, finishes := 0, 0
	contexts := func(provider string) map[string]any {
		return map[string]any{"context_version": 1, "tenant_id": "tenant-one", "project_id": "project-one",
			"cell_id": "fixture-cell", "grant_id": "grant-" + provider, "operation_id": "child-" + provider,
			"attempt_id": "attempt-one", "purpose": "collection", "audience": "private", "binding_id": "binding-" + provider,
			"binding_revision": 1, "source_generation": 1, "provider": provider, "consumer": "worker",
			"lease_fence": 1, "module_generation": 1, "routing_revision": 1, "policy_revision": 1,
			"policy_envelope": map[string]string{"visibility": "private"}, "expires_at": now.Add(20 * time.Minute),
			"destination": map[string]string{"database_ref": "database-ref", "role_ref": "role-ref", "object_prefix": "restricted/"},
			"source_selections": []any{map[string]string{"tenant_id": "tenant-one", "project_id": "project-one",
				"source_connection_id": "connection-" + provider, "record_id": scope[provider],
				"record_type": kind[provider], "record_revision": "revision-one"}}}
	}
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		var output any
		switch {
		case strings.HasSuffix(request.URL.Path, "/receipt"):
			output = map[string]any{"job_id": "job-native", "tenant_id": "tenant-one", "state": "pending"}
		case strings.HasSuffix(request.URL.Path, "/claim"):
			output = map[string]any{"job_id": "job-native", "tenant_id": "tenant-one", "attempt_id": "attempt-one", "lease_fence": 1,
				"cutoff": now.Add(-24 * time.Hour), "window_end": now, "page_size": 100, "page_limit": 5,
				"contexts": []any{map[string]string{"provider": "sonarqube"}, map[string]string{"provider": "linear"}}}
		case strings.HasSuffix(request.URL.Path, "/resolve"):
			var selection map[string]string
			if json.NewDecoder(request.Body).Decode(&selection) != nil {
				t.Error("invalid context request")
			}
			output = map[string]string{"handle": selection["provider"]}
		case strings.HasSuffix(request.URL.Path, "/renew"):
			output = map[string]any{"expires_at": now.Add(20 * time.Minute)}
		case strings.HasSuffix(request.URL.Path, "/finish"):
			var receipt map[string]any
			if json.NewDecoder(request.Body).Decode(&receipt) != nil {
				t.Error("invalid finish receipt")
			}
			if receipt["state"] == "succeeded" {
				finishes++
			}
			output = map[string]any{"state": receipt["state"]}
		case strings.HasSuffix(request.URL.Path, "/page"):
			provider := strings.Split(strings.TrimPrefix(request.URL.Path, "/internal/v1/connector-contexts/"), "/")[0]
			if request.URL.Query().Get("page") != "1" || request.URL.Query().Get("limit") != "100" {
				t.Error("unbounded native page")
			}
			var payload string
			switch provider + ":" + request.URL.Query().Get("kind") {
			case "sonarqube:project":
				payload = `{"component":{"key":"api","name":"Example"}}`
			case "sonarqube:issue":
				payload = `{"issues":[{"key":"ISSUE-1","project":"api","message":"Native issue","status":"OPEN","creationDate":"` + created + `","updateDate":"` + updated + `"}]}`
			case "linear:team":
				payload = `{"data":{"team":{"id":"team","name":"Planning","key":"PL"}}}`
			case "linear:issue":
				payload = `{"data":{"team":{"id":"team","issues":{"nodes":[{"id":"issue-1","identifier":"PL-1","title":"Plan","createdAt":"` + created + `","updatedAt":"` + updated + `","state":{"type":"started"}}],"pageInfo":{"hasNextPage":false}}}}}`
			default:
				t.Errorf("unsupported native page %s %s", provider, request.URL.Query().Get("kind"))
				response.WriteHeader(404)
				return
			}
			pages++
			output = map[string]any{"payload": []byte(payload), "complete": true, "private": false}
		case strings.HasPrefix(request.URL.Path, "/internal/v1/connector-contexts/"):
			output = contexts(strings.TrimPrefix(request.URL.Path, "/internal/v1/connector-contexts/"))
		default:
			t.Errorf("unexpected path %s", request.URL.Path)
			response.WriteHeader(404)
			return
		}
		if err := json.NewEncoder(response).Encode(output); err != nil {
			t.Error(err)
		}
	}))
	defer server.Close()
	schema, err := os.ReadFile(os.Getenv("DEVLAKE_S03_SCHEMA_PATH"))
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(schema)
	directory := t.TempDir()
	schemaPath, buildPath := filepath.Join(directory, "schema.json"), filepath.Join(directory, "build.json")
	if err := os.WriteFile(schemaPath, schema, 0600); err != nil {
		t.Fatal(err)
	}
	build, _ := json.Marshal(map[string]string{"component": "worker", "schema_sha256": hex.EncodeToString(digest[:])})
	if err := os.WriteFile(buildPath, build, 0600); err != nil {
		t.Fatal(err)
	}
	config := Config{TenantID: "tenant-one", JobID: "job-native", ClaimKey: "worker-claim-fixture", SchemaPath: schemaPath, BuildManifestPath: buildPath,
		Cells: []Cell{{DatabaseRef: "database-ref", RoleRef: "role-ref", ResourceID: "cluster-FIXTURE123", Writer: "writer", Reader: "reader"}}}
	gateway := &Gateway{URL: server.URL, Client: server.Client(), Sign: func(*http.Request, []byte) error { return nil }}
	open := func(config celldb.Config) (*sql.DB, error) {
		if config.Username == "writer" {
			return sql.Open("pgx", os.Getenv("DEVLAKE_S03_WRITER_DSN"))
		}
		return sql.Open("pgx", os.Getenv("DEVLAKE_S03_READER_DSN"))
	}
	result, err := Run(context.Background(), config, gateway, open)
	if err != nil || result.Items != 3 || pages != 4 || finishes != 1 {
		t.Fatalf("admitted native run did not publish both sources: %+v pages=%d finishes=%d err=%v", result, pages, finishes, err)
	}
}

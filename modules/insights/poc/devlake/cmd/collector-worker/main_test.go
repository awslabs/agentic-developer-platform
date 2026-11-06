package main

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/celldb"
	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/cmd/adp-collector-worker/internal/worker"
)

func TestEntryPointDisabledBeforeDependencies(test *testing.T) {
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "false")
	var output, errors bytes.Buffer
	if run(nil, &output, &errors, nil, nil) != 2 || !strings.Contains(errors.String(), "feature_disabled") {
		test.Fatal("disabled executable reached runtime", errors.String())
	}
}

func TestPostgresWorkerEntryPoint(test *testing.T) {
	if os.Getenv("DEVLAKE_S03_WRITER_DSN") == "" {
		test.Skip("requires isolated PostgreSQL writer and reader roles")
	}
	test.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	test.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	now := time.Now().UTC().Add(-time.Second).Truncate(time.Second)
	jobID, attempt, scope := "job-one", "attempt-one", "42"
	mode, pageReads, failedFinishes := "uncommitted-finish", 0, 0
	finished := false
	receipts := map[string]map[string]any{}
	issue := []byte(fmt.Sprintf(`{ "id":7, "title":"Issue", "state":"closed", "created_at":%q, "updated_at":%q, "pull_request":{"url":"https://api.github.com/repos/fixture/repo/pulls/1"}}`, now.Add(-time.Hour).Format(time.RFC3339), now.Format(time.RFC3339)))
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		if receipts[jobID] != nil && !strings.HasSuffix(request.URL.Path, "/receipt") {
			test.Error("terminal job attempted collection instead of receipt recovery")
			response.WriteHeader(409)
			return
		}
		var result any
		switch {
		case strings.HasSuffix(request.URL.Path, "/receipt"):
			result = map[string]any{"job_id": jobID, "tenant_id": "tenant-one", "state": "pending"}
			if receipt := receipts[jobID]; receipt != nil {
				result = receipt
			}
		case strings.HasSuffix(request.URL.Path, "/claim"):
			result = map[string]any{"job_id": jobID, "tenant_id": "tenant-one", "attempt_id": attempt, "lease_fence": 1,
				"cutoff": now.Add(-24 * time.Hour), "window_end": now, "page_size": 100, "page_limit": 5,
				"contexts": []any{map[string]string{"provider": "github"}, map[string]string{"provider": "jira"}}}
		case strings.HasSuffix(request.URL.Path, "/resolve"):
			var selected map[string]string
			if json.NewDecoder(request.Body).Decode(&selected) != nil {
				test.Error("invalid context request")
			}
			result = map[string]string{"handle": selected["provider"]}
		case strings.HasSuffix(request.URL.Path, "/renew"):
			if mode == "revoked" && pageReads > 0 {
				response.WriteHeader(409)
				return
			}
			result = map[string]any{"expires_at": now.Add(20 * time.Minute)}
		case strings.HasSuffix(request.URL.Path, "/finish"):
			var receipt map[string]any
			if json.NewDecoder(request.Body).Decode(&receipt) != nil {
				test.Error("invalid finish")
			}
			if receipt["state"] == "failed" {
				failedFinishes++
			}
			if mode == "uncommitted-finish" {
				response.WriteHeader(503)
				return
			}
			finished = receipt["state"] == "succeeded"
			if finished {
				receipts[jobID] = map[string]any{"job_id": jobID, "tenant_id": "tenant-one", "state": "succeeded",
					"attempt_id": attempt, "lease_fence": 1, "receipt": map[string]any{
						"batch_id": receipt["batch_id"], "content_digest": receipt["content_digest"], "items": receipt["items"]}}
			}
			if mode == "lost-finish" {
				response.WriteHeader(503)
				return
			}
			result = map[string]any{"state": receipt["state"]}
		case strings.HasSuffix(request.URL.Path, "/page"):
			pageReads++
			if mode == "throttled" {
				response.WriteHeader(429)
				return
			}
			kind := request.URL.Query().Get("kind")
			payload := []byte("[]")
			if strings.Contains(request.URL.Path, "/github/") {
				switch kind {
				case "repository":
					payload = []byte(fmt.Sprintf(`{"id":%s,"name":"repo","updated_at":%q}`, scope, now.Format(time.RFC3339)))
				case "issue":
					if mode != "removed" {
						payload = append(append([]byte("["), issue...), ']')
					}
				case "pull_request":
					payload = []byte(fmt.Sprintf(`[{"id":8,"title":"PR","state":"closed","url":"https://api.github.com/repos/fixture/repo/pulls/1","created_at":%q,"updated_at":%q,"merged_at":%q}]`, now.Add(-time.Hour).Format(time.RFC3339), now.Format(time.RFC3339), now.Format(time.RFC3339)))
				}
			} else if kind == "board" {
				payload = []byte(`{"id":91,"name":"board"}`)
			} else {
				payload = []byte(fmt.Sprintf(`{"issues":[{"id":"7","fields":{"summary":"Jira issue","status":{"name":"Done"},"security":null,"created":%q,"updated":%q}},{"id":"9","fields":{"summary":"PRIVATE-NEVER-PERSIST","status":{"name":"Done"},"security":{"id":"restricted"},"created":%q,"updated":%q}}]}`, now.Add(-time.Hour).Format(time.RFC3339), now.Format(time.RFC3339), now.Add(-time.Hour).Format(time.RFC3339), now.Format(time.RFC3339)))
			}
			result = map[string]any{"payload": payload, "complete": mode != "incomplete", "private": false}
			if mode == "missing-privacy" {
				delete(result.(map[string]any), "private")
			}
		default:
			provider := filepath.Base(request.URL.Path)
			nativeID, recordType := scope, "repository"
			if provider == "jira" {
				nativeID, recordType = "91", "tracker"
			}
			result = map[string]any{"context_version": 1, "tenant_id": "tenant-one", "project_id": "project-one", "cell_id": "fixture-cell",
				"grant_id": "grant-" + provider, "operation_id": "child-" + provider, "attempt_id": attempt, "purpose": "collection", "audience": "private",
				"binding_id": "binding-" + provider, "binding_revision": 1, "source_generation": 1, "provider": provider, "consumer": "worker",
				"lease_fence": 1, "module_generation": 1, "routing_revision": 1, "policy_revision": 1, "policy_envelope": map[string]string{"visibility": "private"},
				"expires_at": now.Add(20 * time.Minute), "destination": map[string]string{"database_ref": "database-ref", "role_ref": "role-ref", "object_prefix": "restricted/"},
				"source_selections": []any{map[string]string{"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "connection-" + provider, "record_id": nativeID, "record_type": recordType, "record_revision": "r1"}}}
		}
		if err := json.NewEncoder(response).Encode(result); err != nil {
			test.Error(err)
		}
	}))
	defer server.Close()
	directory := test.TempDir()
	schema, err := os.ReadFile(os.Getenv("DEVLAKE_S03_SCHEMA_PATH"))
	if err != nil {
		test.Fatal(err)
	}
	digest := sha256.Sum256(schema)
	schemaPath, buildPath, configPath := filepath.Join(directory, "schema.json"), filepath.Join(directory, "build.json"), filepath.Join(directory, "job.json")
	write := func(path string, data []byte) {
		test.Helper()
		if err := os.WriteFile(path, data, 0600); err != nil {
			test.Fatal(err)
		}
	}
	write(schemaPath, schema)
	write(buildPath, []byte(fmt.Sprintf(`{"component":"worker","schema_sha256":%q}`, hex.EncodeToString(digest[:]))))
	config := worker.Config{GatewayURL: server.URL, TenantID: "tenant-one", JobID: jobID, ClaimKey: "worker-claim-fixture", SchemaPath: schemaPath, BuildManifestPath: buildPath,
		Cells: []worker.Cell{{DatabaseRef: "database-ref", RoleRef: "role-ref", ResourceID: "cluster-FIXTURE123", Writer: "writer", Reader: "reader"}}}
	factory := func(address, region string) (*worker.Gateway, error) {
		return &worker.Gateway{URL: address, Client: server.Client(), Sign: func(*http.Request, []byte) error { return nil }}, nil
	}
	open := func(config celldb.Config) (*sql.DB, error) {
		if config.Username == "writer" {
			return sql.Open("pgx", os.Getenv("DEVLAKE_S03_WRITER_DSN"))
		}
		return sql.Open("pgx", os.Getenv("DEVLAKE_S03_READER_DSN"))
	}
	invoke := func(expected int) worker.Result {
		test.Helper()
		config.JobID = jobID
		encoded, err := json.Marshal(config)
		if err != nil {
			test.Fatal(err)
		}
		write(configPath, encoded)
		var output, errors bytes.Buffer
		code := run([]string{"--config", configPath}, &output, &errors, factory, open)
		if code != expected {
			test.Fatalf("mode %s: exit %d expected %d: %s", mode, code, expected, errors.String())
		}
		var result worker.Result
		if expected == 0 && json.Unmarshal(output.Bytes(), &result) != nil {
			test.Fatal("invalid success receipt", output.String())
		}
		return result
	}
	reader, err := open(celldb.Config{Username: "reader"})
	if err != nil {
		test.Fatal(err)
	}
	defer reader.Close()
	invoke(1)
	mode = "lost-finish"
	invoke(1)
	mode = ""
	first := invoke(0)
	if !finished || first.Items != 5 || pageReads != 5 {
		test.Fatalf("incomplete run: %+v pages %d finished %t", first, pageReads, finished)
	}
	batch, err := collection.LoadSealedBatch(test.Context(), reader, "fixture-cell", first.BatchID)
	if err != nil {
		test.Fatal(err)
	}
	foundIssue := false
	for _, item := range batch.Items {
		if bytes.Contains(item.NativeBytes, []byte("PRIVATE-NEVER-PERSIST")) {
			test.Fatal("private content persisted")
		}
		if item.Key.Tool == "github" && item.Key.Kind == "issue" {
			foundIssue = true
			if !bytes.Equal(item.NativeBytes, issue) || len(item.Links) != 1 || item.Links[0].To.NativeID != "8" {
				test.Fatal("native bytes or reference lost")
			}
		}
	}
	if !foundIssue {
		test.Fatal("GitHub issue missing")
	}
	var raw []byte
	if err := reader.QueryRow("SELECT data FROM public._raw_github_api_issues").Scan(&raw); err != nil || !bytes.Equal(raw, issue) {
		test.Fatal("raw bytes differ", err)
	}
	readsBefore := pageReads
	if replay := invoke(0); replay != first {
		test.Fatal("replay changed sealed receipt")
	}
	if pageReads != readsBefore || failedFinishes != 0 {
		test.Fatal("sealed replay fetched again or finalized failure")
	}
	jobID, attempt, mode = "job-two", "attempt-two", "removed"
	second := invoke(0)
	if second.BatchID == first.BatchID {
		test.Fatal("successor reused B1")
	}
	var count int
	if err := reader.QueryRow("SELECT count(*) FROM public._raw_github_api_issues").Scan(&count); err != nil || count != 0 {
		test.Fatal("workspace removal failed", err, count)
	}
	if err := collection.ReadSealedBatch(test.Context(), reader, batch); err != nil {
		test.Fatal("B1 changed", err)
	}
	jobID, attempt, scope, mode = "job-new-scope", "attempt-new-scope", "43", ""
	invoke(0)
	for _, failure := range []string{"incomplete", "missing-privacy", "revoked", "throttled"} {
		jobID, attempt, mode, pageReads = "job-"+failure, "attempt-"+failure, failure, 0
		invoke(1)
		if err := reader.QueryRow("SELECT count(*) FROM devlake_meta.collection_batch_manifests").Scan(&count); err != nil || count != 3 {
			test.Fatalf("%s published partial result: %d %v", failure, count, err)
		}
	}
	config.Cells[0].ResourceID = "wrong-resource"
	mode, pageReads = "", 0
	invoke(1)
	if pageReads != 0 {
		test.Fatal("unmigrated target fetched provider data")
	}
}

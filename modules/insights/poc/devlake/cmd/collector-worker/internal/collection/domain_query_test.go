package collection

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"io"
	"sort"
	"strings"
	"testing"
	"time"
)

type domainFixture struct {
	batches []PreparedBatch
	drop    NativeKey
	tamper  bool
}
type domainConnector struct{ fixture *domainFixture }
type domainConnection struct{ fixture *domainFixture }
type domainTransaction struct{}
type domainRows struct {
	values [][]driver.Value
	index  int
}

func (connector domainConnector) Connect(context.Context) (driver.Conn, error) {
	return domainConnection{connector.fixture}, nil
}
func (connector domainConnector) Driver() driver.Driver { return connector }
func (connector domainConnector) Open(string) (driver.Conn, error) {
	return domainConnection{connector.fixture}, nil
}
func (connection domainConnection) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("unavailable")
}
func (connection domainConnection) Close() error              { return nil }
func (connection domainConnection) Begin() (driver.Tx, error) { return domainTransaction{}, nil }
func (connection domainConnection) BeginTx(context.Context, driver.TxOptions) (driver.Tx, error) {
	return domainTransaction{}, nil
}
func (connection domainConnection) CheckNamedValue(*driver.NamedValue) error { return nil }
func (domainTransaction) Commit() error                                      { return nil }
func (domainTransaction) Rollback() error                                    { return nil }
func (rows *domainRows) Columns() []string {
	return []string{"batch_id", "generation", "item_count", "manifest_payload", "content_digest", "item_key", "item_payload", "native_payload", "normalized_payload", "payload_digest"}
}
func (rows *domainRows) Close() error { return nil }
func (rows *domainRows) Next(values []driver.Value) error {
	if rows.index == len(rows.values) {
		return io.EOF
	}
	copy(values, rows.values[rows.index])
	rows.index++
	return nil
}
func (connection domainConnection) QueryContext(_ context.Context, query string, arguments []driver.NamedValue) (driver.Rows, error) {
	if !strings.Contains(query, "FROM devlake_meta.collection_batch_manifests AS manifest") ||
		!strings.Contains(query, "item.item_key::jsonb->>'ConnectionID'") || !strings.Contains(query, "LIMIT 100001") ||
		len(arguments) < 9 || len(arguments) > 30 || (len(arguments)-3)%3 != 0 || arguments[0].Value != "cell" {
		return nil, errors.New("unscoped read")
	}
	result := &domainRows{}
	for _, batch := range connection.fixture.batches {
		if len(batch.Items) == 0 {
			digest := sha256.Sum256(batch.ManifestPayload)
			result.values = append(result.values, []driver.Value{batch.BatchID, batch.Generation, int64(0),
				batch.ManifestPayload, digest[:], "", nil, nil, nil, nil})
		}
		for index, item := range batch.Items {
			key := item.Key
			matched := false
			for offset := 3; offset < len(arguments); offset += 3 {
				matched = matched || key.Tool == arguments[offset].Value && key.ConnectionID == arguments[offset+1].Value && key.Scope == arguments[offset+2].Value
			}
			if key == connection.fixture.drop || key.CellID != arguments[0].Value || key.TenantID != arguments[1].Value ||
				key.ProjectID != arguments[2].Value || !matched {
				continue
			}
			if strings.Contains(query, key.Scope) {
				return nil, errors.New("source scope interpolated into query")
			}
			itemKey, _ := json.Marshal(key)
			itemBytes := batch.ItemPayloads[index]
			itemHash := sha256.Sum256(itemBytes)
			manifestHash := sha256.Sum256(batch.ManifestPayload)
			native := bytes.Clone(item.NativeBytes)
			if connection.fixture.tamper && batch.Generation == 2 && key.Kind == "issue" {
				native[0] ^= 0xff
			}
			result.values = append(result.values, []driver.Value{batch.BatchID, batch.Generation, int64(len(batch.Items)),
				batch.ManifestPayload, manifestHash[:], string(itemKey), itemBytes, native, item.NormalizedBytes, itemHash[:]})
		}
	}
	sort.Slice(result.values, func(left, right int) bool { return result.values[left][1].(int64) > result.values[right][1].(int64) })
	return result, nil
}

func domainBatches(t *testing.T, start time.Time) ([]PreparedBatch, []Source) {
	t.Helper()
	batchID, cellID, items := batchFixture()
	sources := fixtureJob(time.Now()).Sources
	sources[0].ConnectionID, sources[0].Scope = "github-connection", "repo"
	sources[1].ConnectionID, sources[1].Scope = "jira-connection", "board"
	makeIssue := func(item BatchItem, revision, status, common, title string) BatchItem {
		item.Revision, item.FieldMapVersion = revision, "fit-v1"
		item.NativeBytes = []byte(`{"title":"` + title + `","status":"` + status + `"}`)
		created := start.Add(-time.Hour)
		issue := CommonIssue{Key: item.Key, Revision: revision, FieldMap: item.FieldMapVersion,
			Native: item.NativeBytes, Title: title, OriginalStatus: status, CommonStatus: common, CreatedAt: &created}
		item.NormalizedBytes, _ = json.Marshal(issue)
		return item
	}
	items[0] = makeIssue(items[0], "r1", "closed", "done", "same issue")
	items[1] = makeIssue(items[1], "r1", "Done", "unavailable", "same issue")
	other := items[0]
	other.Key.Scope, other.Key.NativeID = "unselected-scope", "other"
	other.Links = nil
	other = makeIssue(other, "r1", "closed", "done", "unselected")
	first, err := PrepareBatch(batchID, cellID, 1, []BatchItem{items[0], items[1], other})
	if err != nil {
		t.Fatal(err)
	}
	edited := makeIssue(items[0], "r2", "open", "in_progress", "edited issue")
	makePR := func(id string, created *time.Time) BatchItem {
		key := items[0].Key
		key.Kind, key.NativeID = "pull_request", id
		merged := start.Add(3 * time.Hour)
		request := PullRequest{Key: key, CreatedAt: created, MergedAt: &merged, Merged: true}
		encoded, _ := json.Marshal(request)
		return BatchItem{Key: key, Revision: "r2", NativeBytes: []byte(`{"id":` + id + `}`),
			NormalizedBytes: encoded, PolicyVersion: "policy-v1", FieldMapVersion: "fit-v1", SchemaVersion: "schema-v2"}
	}
	created := start.Add(time.Hour)
	second, err := PrepareBatch("aaaaaaaa-bbbb-cccc-dddd-ffffffffffff", cellID, 2,
		[]BatchItem{edited, makePR("9", &created), makePR("10", nil)})
	if err != nil {
		t.Fatal(err)
	}
	return []PreparedBatch{first, second}, sources
}

func TestCollectedDomainSelectedNativeSourceAndLateRevocation(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	start := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	legacy, sources := domainBatches(t, start)
	native := expandedSource("gitlab", "opaque-gitlab")
	native.GrantID, native.Consumer = "gitlab-grant", "worker"
	sources = append(sources, native)
	pipeline := pipelineItem(t, native, "gitlab:GitlabPipeline:1:7", "failed", "")
	selected := []BatchItem{pipeline}
	for _, item := range legacy[0].Items {
		if item.Key.Scope != "unselected-scope" {
			selected = append(selected, item)
		}
	}
	batch, err := PrepareSourceBatch("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "cell", 3, selected, sources)
	if err != nil {
		t.Fatal(err)
	}
	reader := sql.OpenDB(domainConnector{&domainFixture{batches: []PreparedBatch{batch}}})
	defer reader.Close()
	read := func(selected []Source, authority Authority) (CollectedDomain, error) {
		return QueryCollectedDomain(context.Background(), reader, selected, authority, start, start.Add(24*time.Hour))
	}
	result, err := read(sources, &fixtureAuthority{})
	if err != nil || len(result.Items) != 3 || len(result.Issues) != 2 || len(result.PRs) != 0 || result.Metrics != nil {
		t.Fatalf("selected native readback failed: %+v, %v", result, err)
	}
	found := false
	for _, item := range result.Items {
		if item.Key.Tool == "gitlab" {
			found = bytes.Equal(item.NativeBytes, pipeline.NativeBytes) && bytes.Equal(item.NormalizedBytes, pipeline.NormalizedBytes)
		}
	}
	if !found {
		t.Fatalf("GitLab native record lost or changed: %+v", result.Items)
	}
	checkpoint := &receiptConnector{sourceKey: SourceCheckpointKey(native), cursor: "cursor-1"}
	writer := sql.OpenDB(checkpoint)
	defer writer.Close()
	plan, update, err := PlanPersistedIncremental(context.Background(), writer, reader, sources, native, 4,
		&fixtureAuthority{}, &fixtureGeneration{generation: 4, state: "collecting"},
		Reconciled{Allowed: []NativeRecord{{Key: pipeline.Key, Revision: pipeline.Revision, Payload: pipeline.NativeBytes}}},
		"cursor-2", true, start, start.Add(24*time.Hour))
	if err != nil || len(plan.Changes) != 0 || update.SourceKey != SourceCheckpointKey(native) ||
		update.ExpectedCursor == nil || *update.ExpectedCursor != "cursor-1" || checkpoint.cursor != "cursor-1" {
		t.Fatalf("native incremental readback advanced or invented cursor: %+v, %+v, %v", plan, update, err)
	}
	foreign := append([]Source(nil), sources...)
	foreign[2].Scope = "foreign') OR TRUE --"
	if _, err := read(foreign, &fixtureAuthority{}); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("foreign scope accessed sealed native rows: %v", err)
	}
	if result, err := read(sources, &expiringAuthority{revokeAt: 4}); !errors.Is(err, ErrUnauthorized) || len(result.Items) != 0 {
		t.Fatalf("late revocation returned native rows: %+v, %v", result, err)
	}
}

func TestCollectedDomainTwoToolFitAndNativeMetricOracle(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	start := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	batches, sources := domainBatches(t, start)
	reader := sql.OpenDB(domainConnector{&domainFixture{batches: batches}})
	defer reader.Close()
	result, err := QueryCollectedDomain(context.Background(), reader, sources, &fixtureAuthority{}, start, start.Add(24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Items) != 4 || len(result.Issues) != 2 || len(result.PRs) != 2 || len(result.Fit) != 10 || result.Metrics == nil ||
		result.Metrics.MergedCount != 2 || len(result.Metrics.CycleDurations) != 1 || result.Metrics.CycleDurations[0] != 2*time.Hour ||
		len(result.Metrics.Gaps) != 1 || result.Metrics.Gaps[0].Reason != "invalid_creation_time" {
		t.Fatalf("source metric oracle or population differs: %+v", result)
	}
	if result.Issues[0].Key.Tool != "github" || result.Issues[1].Key.Tool != "jira" ||
		result.Issues[0].Title != "edited issue" || result.Issues[1].Title != "same issue" ||
		result.Issues[0].OriginalStatus != "open" || result.Issues[1].OriginalStatus != "Done" ||
		!bytes.Equal(result.Issues[0].Native, []byte(`{"title":"edited issue","status":"open"}`)) ||
		result.Issues[0].Key.NativeID != result.Issues[1].Key.NativeID {
		t.Fatalf("native provenance lost: %+v", result.Issues)
	}
	for _, field := range result.Fit {
		if field.FieldMapVersion != "fit-v1" || field.Present+field.Missing != 1 ||
			(field.Field == "updated_at" && field.Missing != 1) ||
			(field.Field == "common_status" && field.Tool == "jira" && field.Missing != 1) {
			t.Fatalf("field fit incorrect: %+v", field)
		}
	}
	firstOnly := sql.OpenDB(domainConnector{&domainFixture{batches: batches[:1]}})
	defer firstOnly.Close()
	withoutPRs, err := QueryCollectedDomain(context.Background(), firstOnly, sources, &fixtureAuthority{}, start, start.Add(24*time.Hour))
	if err != nil || withoutPRs.Metrics != nil {
		t.Fatalf("absent PR population treated as a measured zero: %+v, %v", withoutPRs, err)
	}
}

func TestCollectedDomainRejectsGapsTamperingAndRevocation(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	start := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	batches, sources := domainBatches(t, start)
	fixture := &domainFixture{batches: batches}
	reader := sql.OpenDB(domainConnector{fixture})
	defer reader.Close()
	query := func(authority Authority) error {
		_, err := QueryCollectedDomain(context.Background(), reader, sources, authority, start, start.Add(24*time.Hour))
		return err
	}
	for _, item := range batches[0].Items {
		if item.Key.Tool == "jira" {
			fixture.drop = item.Key
		}
	}
	if err := query(&fixtureAuthority{}); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("omitted sealed item: %v", err)
	}
	fixture.drop = NativeKey{}
	for _, item := range batches[0].Items {
		if item.Key.Tool == "github" && item.Key.Scope == "repo" {
			fixture.drop = item.Key
		}
	}
	if err := query(&fixtureAuthority{}); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("partial B1 concealed by B2: %v", err)
	}
	fixture.drop = NativeKey{}
	fixture.tamper = true
	if err := query(&fixtureAuthority{}); !errors.Is(err, ErrBatch) {
		t.Fatalf("tampered native row: %v", err)
	}
	fixture.tamper = false
	if err := query(&expiringAuthority{revokeAt: 4}); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("revoked before return: %v", err)
	}
	sources[1].Scope = "unapproved"
	if err := query(&fixtureAuthority{}); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("missing source: %v", err)
	}
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "false")
	if err := query(&fixtureAuthority{}); !errors.Is(err, ErrDisabled) {
		t.Fatalf("disabled query: %v", err)
	}
}

func TestCollectedDomainNormalizedNativeRoundTrip(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	start := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	for _, test := range []struct {
		name     string
		payload  string
		mismatch bool
	}{
		{name: "compact", payload: `{"id":1,"title":"native title","status":"open"}`},
		{name: "whitespace", payload: " \n{ \"id\": 1, \"title\": \"native title\", \"status\": \"open\" }\n"},
		{name: "html", payload: `{"id":1,"title":"<tag> & value","status":"open"}`},
		{name: "mismatched native", payload: `{"id":1,"title":"<tag> & value","status":"open"}`, mismatch: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			batchID, cellID, items := batchFixture()
			sources := fixtureJob(time.Now()).Sources
			mappings := make(map[string]IssueMapping)
			var records []NativeRecord
			for index := range items {
				item := &items[index]
				item.NativeBytes = []byte(test.payload)
				sources[index].ConnectionID, sources[index].Scope = item.Key.ConnectionID, item.Key.Scope
				mappings[item.Key.Tool] = IssueMapping{IDField: "id", TitleField: "title", StatusField: "status",
					CreatedField: "created_at", UpdatedField: "updated_at", Statuses: map[string]string{"open": "in_progress"}}
				records = append(records, NativeRecord{Key: item.Key, Revision: item.Revision, Payload: item.NativeBytes})
			}
			issues, err := NormalizeIssues(records, mappings, items[0].FieldMapVersion)
			if err != nil {
				t.Fatal(err)
			}
			if test.mismatch {
				issues[0].Native = json.RawMessage(`{"id":1,"title":"forged"}`)
			}
			for index := range items {
				items[index].NormalizedBytes, err = json.Marshal(issues[index])
				if err != nil {
					t.Fatal(err)
				}
			}
			batch, err := PrepareBatch(batchID, cellID, 1, items)
			if err != nil {
				t.Fatal(err)
			}
			reader := sql.OpenDB(domainConnector{&domainFixture{batches: []PreparedBatch{batch}}})
			defer reader.Close()
			result, err := QueryCollectedDomain(context.Background(), reader, sources, &fixtureAuthority{}, start, start.Add(24*time.Hour))
			if test.mismatch {
				if !errors.Is(err, ErrMapping) || len(result.Items) != 0 || len(result.Issues) != 0 {
					t.Fatalf("mismatched native JSON returned data: %+v, %v", result, err)
				}
				return
			}
			if err != nil || len(result.Issues) != 2 || len(result.Items) != 2 {
				t.Fatalf("normalized JSON round trip failed: %+v, %v", result, err)
			}
			for index, issue := range result.Issues {
				if issue.Key != issues[index].Key || issue.Title != issues[index].Title || issue.CommonStatus != "in_progress" ||
					!bytes.Equal(issue.Native, []byte(test.payload)) || !bytes.Equal(result.Items[index].NativeBytes, []byte(test.payload)) ||
					!bytes.Equal(result.Items[index].NormalizedBytes, items[index].NormalizedBytes) {
					t.Fatalf("native bytes or normalized values changed: %+v", issue)
				}
			}
			checkpoint := &receiptConnector{sourceKey: SourceCheckpointKey(sources[0]), cursor: "cursor-1"}
			writer := sql.OpenDB(checkpoint)
			defer writer.Close()
			plan, update, err := PlanPersistedIncremental(context.Background(), writer, reader, sources, sources[0], 2,
				&fixtureAuthority{}, &fixtureGeneration{generation: 2, state: "collecting"},
				Reconciled{Allowed: records[:1]}, "cursor-2", true, start, start.Add(24*time.Hour))
			if err != nil || len(plan.Changes) != 0 || plan.PreviousCursor != "cursor-1" || update.ExpectedCursor == nil ||
				*update.ExpectedCursor != "cursor-1" || update.NextCursor != "cursor-2" || checkpoint.cursor != "cursor-1" {
				t.Fatalf("unchanged native record failed incremental planning: %+v, %+v, %v", plan, update, err)
			}
		})
	}
}

package collection

import (
	"bytes"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

func TestNormalizeIssuesPreservesDistinctNativeValues(t *testing.T) {
	github := NativeRecord{Key: NativeKey{TenantID: "tenant", ProjectID: "project", CellID: "cell", Tool: "github", ConnectionID: "connection-a", Scope: "repo", Kind: "issue", NativeID: "42"},
		Revision: "revision-a", Payload: json.RawMessage(`{"GithubId":42,"Title":"same","State":"closed","Created":"2026-10-02T00:00:00+01:00"}`)}
	jira := NativeRecord{Key: NativeKey{TenantID: "tenant", ProjectID: "project", CellID: "cell", Tool: "jira", ConnectionID: "connection-b", Scope: "board", Kind: "issue", NativeID: "42"},
		Revision: "revision-b", Payload: json.RawMessage(`{"IssueId":42,"Summary":"same","StatusName":"Done"}`)}
	mappings := map[string]IssueMapping{
		"github": {IDField: "GithubId", TitleField: "Title", StatusField: "State", CreatedField: "Created", UpdatedField: "Updated", Statuses: map[string]string{"closed": "done"}},
		"jira":   {IDField: "IssueId", TitleField: "Summary", StatusField: "StatusName", CreatedField: "Created", UpdatedField: "Updated", Statuses: map[string]string{}},
	}
	issues, err := NormalizeIssues([]NativeRecord{github, jira}, mappings, "fixture-map-v1")
	if err != nil || len(issues) != 2 || issues[0].CommonStatus != "done" || issues[1].CommonStatus != "unavailable" ||
		issues[0].OriginalStatus != "closed" || issues[1].OriginalStatus != "Done" ||
		issues[0].CreatedAt.Format(time.RFC3339) != "2026-10-01T23:00:00Z" || issues[1].CreatedAt != nil ||
		!bytes.Equal(issues[1].Native, jira.Payload) || issues[0].Key == issues[1].Key {
		t.Fatalf("common comparison lost native information: %+v, %v", issues, err)
	}
	jira.Payload[0] = 'X'
	if issues[1].Native[0] == 'X' {
		t.Fatal("native value aliases mutable input")
	}
	github.Key.NativeID = "other"
	if _, err := NormalizeIssues([]NativeRecord{github}, mappings, "fixture-map-v1"); !errors.Is(err, ErrMapping) {
		t.Fatalf("mismatched native ID admitted: %v", err)
	}
}

func TestMeasurePRsUsesDistinctNativeIDsAndHalfOpenUTCWindow(t *testing.T) {
	start := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	end := start.Add(24 * time.Hour)
	key := func(id string) NativeKey {
		return NativeKey{TenantID: "tenant", ProjectID: "project", CellID: "cell", Tool: "github", ConnectionID: "connection", Scope: "repo", Kind: "pull_request", NativeID: id}
	}
	at := func(instant time.Time) *time.Time { return &instant }
	input := []PullRequest{
		{Key: key("1"), Merged: true, CreatedAt: at(start.Add(-time.Hour)), MergedAt: at(start)},
		{Key: key("2"), Merged: true, CreatedAt: at(start.Add(time.Hour)), MergedAt: at(end.Add(-time.Nanosecond))},
		{Key: key("3"), Merged: true, CreatedAt: at(start), MergedAt: at(end)},
		{Key: key("4"), Merged: true, MergedAt: at(start.Add(time.Hour))},
		{Key: key("5"), Merged: true},
	}
	metrics, err := MeasurePRs(input, start, end)
	if err != nil || metrics.MergedCount != 3 || len(metrics.CycleDurations) != 2 ||
		metrics.CycleDurations[0] != time.Hour || metrics.CycleDurations[1] != 23*time.Hour-time.Nanosecond ||
		len(metrics.Gaps) != 2 || metrics.Gaps[0].Reason != "invalid_creation_time" || metrics.Gaps[1].Reason != "missing_merge_time" {
		t.Fatalf("source oracle differs: %+v, %v", metrics, err)
	}
	input = append(input, input[0])
	if _, err := MeasurePRs(input, start, end); !errors.Is(err, ErrMapping) {
		t.Fatalf("duplicate PR counted: %v", err)
	}
}

package sourceapi

import (
	"bytes"
	"strings"
	"testing"
)

func TestLinearPagePreservesExactTeamAndIssue(t *testing.T) {
	team := []byte(`{"id":"team-1","name":"Team"}`)
	issue := []byte(`{"id":"issue-1","title":"Original","createdAt":"2026-10-01T00:00:00Z","updatedAt":"2026-10-02T00:00:00Z","state":{"type":"started"}}`)
	for _, scenario := range []struct {
		kind, id     string
		payload, raw []byte
	}{
		{"team", "team-1", []byte(`{"data":{"team":` + string(team) + `}}`), team},
		{"issue", "issue-1", []byte(`{"data":{"team":{"id":"team-1","issues":{"nodes":[` + string(issue) + `],"pageInfo":{"hasNextPage":false}}}}}`), issue},
	} {
		records, err := DecodePage("linear", scenario.kind, "team-1", scenario.payload, 100, false)
		if err != nil || len(records) != 1 || records[0].Kind != scenario.kind || records[0].ID != scenario.id ||
			!bytes.Equal(records[0].Raw, scenario.raw) || records[0].Revision == "" {
			t.Fatalf("native Linear %s lost: %v, %v", scenario.kind, records, err)
		}
	}
}

func TestLinearPageRejectsForeignAndAmbiguousIssue(t *testing.T) {
	base := `{"data":{"team":{"id":"team-1","issues":{"nodes":[{"id":"issue-1","createdAt":"2026-10-01T00:00:00Z","updatedAt":"2026-10-02T00:00:00Z"}]}}}}`
	for _, payload := range []string{
		strings.Replace(base, `"team-1"`, `"other"`, 1),
		strings.Replace(base, `"issue-1"`, `""`, 1),
		strings.Replace(base, `"updatedAt":"2026-10-02T00:00:00Z"`, `"updatedAt":null`, 1),
		strings.Replace(base, `"nodes":[`, `"nodes":[{"id":"issue-1","createdAt":"2026-10-01T00:00:00Z","updatedAt":"2026-10-02T00:00:00Z"},`, 1),
		strings.Replace(base, `"nodes":[{"id":"issue-1","createdAt":"2026-10-01T00:00:00Z","updatedAt":"2026-10-02T00:00:00Z"}]`, `"nodes":null`, 1),
		strings.Replace(base, `"data":`, `"errors":[{"message":"denied"}],"data":`, 1),
	} {
		if _, err := DecodePage("linear", "issue", "team-1", []byte(payload), 100, false); err == nil {
			t.Fatal("unsafe Linear issue accepted", payload)
		}
	}
}

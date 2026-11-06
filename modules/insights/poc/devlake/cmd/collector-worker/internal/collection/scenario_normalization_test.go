package collection

import (
	"bytes"
	"context"
	"database/sql"
	"testing"
	"time"
)

func TestSCN02Normalization(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	windowStart := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	batches, sources := domainBatches(t, windowStart)
	reader := sql.OpenDB(domainConnector{&domainFixture{batches: batches}})
	defer reader.Close()
	domain, err := QueryCollectedDomain(context.Background(), reader, sources, &fixtureAuthority{},
		windowStart, windowStart.Add(24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if len(domain.Items) != 4 || len(domain.Issues) != 2 || len(domain.PRs) != 2 || domain.Metrics == nil ||
		domain.Metrics.MergedCount != 2 || len(domain.Metrics.CycleDurations) != 1 ||
		domain.Metrics.CycleDurations[0] != 2*time.Hour || len(domain.Metrics.Gaps) != 1 ||
		domain.Metrics.Gaps[0].Key.NativeID != "10" || domain.Metrics.Gaps[0].Reason != "invalid_creation_time" {
		t.Fatalf("native PR oracle differs from normalized population: %+v", domain)
	}
	if domain.Issues[0].Key.Tool != "github" || domain.Issues[1].Key.Tool != "jira" ||
		domain.Issues[0].Key.NativeID != domain.Issues[1].Key.NativeID ||
		domain.Issues[0].OriginalStatus != "open" || domain.Issues[1].OriginalStatus != "Done" ||
		!bytes.Equal(domain.Issues[0].Native, []byte(`{"title":"edited issue","status":"open"}`)) ||
		!bytes.Equal(domain.Issues[1].Native, []byte(`{"title":"same issue","status":"Done"}`)) {
		t.Fatalf("native source comparison collapsed distinct tools: %+v", domain.Issues)
	}
	for _, request := range domain.PRs {
		if request.Key.Tool != "github" {
			t.Fatalf("a Jira PR was invented: %+v", request)
		}
	}
	foundGap := false
	for _, field := range domain.Fit {
		if field.Tool == "jira" && field.Field == "common_status" && field.Missing == 1 &&
			field.FieldMapVersion == "fit-v1" {
			foundGap = true
		}
	}
	if !foundGap {
		t.Fatalf("unmapped Jira status was hidden: %+v", domain.Fit)
	}
}

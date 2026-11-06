package collection

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

type fixtureNativeProvider struct {
	pages      map[string][]NativePage
	requests   map[string]int
	generation *fixtureGeneration
	fenceAfter bool
}

func (provider *fixtureNativeProvider) FetchPage(_ context.Context, source Source, cursor string, limit int) (NativePage, error) {
	if limit != 100 {
		return NativePage{}, errors.New("unexpected page size")
	}
	position := provider.requests[source.Tool]
	if position > 0 && cursor != "page-2" {
		return NativePage{}, errors.New("unexpected cursor")
	}
	provider.requests[source.Tool]++
	if provider.fenceAfter {
		provider.generation.generation = 2
	}
	if position >= len(provider.pages[source.Tool]) {
		return NativePage{}, errors.New("page unavailable")
	}
	return provider.pages[source.Tool][position], nil
}

func TestCollectVerifiedPagesKeepsBothNativePopulationsAtomic(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	for _, scenario := range []struct {
		name       string
		dropGithub bool
		private    bool
		fence      bool
		want       error
		jiraReads  int
	}{
		{"both reconciled", false, false, false, nil, 1},
		{"github page omitted", true, false, false, ErrIncomplete, 0},
		{"private field", false, true, false, ErrPolicy, 0},
		{"provider fenced", false, false, true, ErrGenerationFenced, 0},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			job := fixtureJob(time.Now())
			_, githubPages, githubExpected, classifier := nativeFixture()
			jiraKey := NativeKey{CellID: "cell", TenantID: "tenant", ProjectID: "project", Tool: "jira",
				ConnectionID: "connection", Scope: "project-b", Kind: "issue", NativeID: "jira-1"}
			classifier.decisions[jiraKey] = Decision{Disposition: "allow", Fields: map[string]bool{"title": true}}
			jiraPages := []NativePage{{Records: []NativeRecord{{Key: jiraKey, Revision: "2", Payload: json.RawMessage(`{"title":"issue"}`)}}, Complete: true}}
			if scenario.dropGithub {
				githubPages[1].Records = nil
			}
			if scenario.private {
				githubPages[0].Records[0].Payload = json.RawMessage(`{"body":"private"}`)
			}
			generation := &fixtureGeneration{generation: 1, state: "collecting"}
			provider := &fixtureNativeProvider{pages: map[string][]NativePage{"github": githubPages, "jira": jiraPages},
				requests: map[string]int{}, generation: generation, fenceAfter: scenario.fence}
			expected := map[Source][]NativeKey{job.Sources[0]: githubExpected, job.Sources[1]: {jiraKey}}
			results, err := CollectVerifiedPages(context.Background(), job, 1, &fixtureAuthority{}, generation, provider, classifier, expected)
			if !errors.Is(err, scenario.want) && (scenario.want != nil || err != nil) {
				t.Fatalf("reconcile: %v, expected %v", err, scenario.want)
			}
			if provider.requests["jira"] != scenario.jiraReads {
				t.Fatalf("jira requests: %d", provider.requests["jira"])
			}
			if scenario.want == nil {
				if len(results) != 2 || len(results[0].Allowed) != 1 || len(results[0].Exclusions) != 1 || len(results[1].Allowed) != 1 {
					t.Fatalf("incomplete two-source result: %+v", results)
				}
			} else if results != nil {
				t.Fatalf("partial result escaped: %+v", results)
			}
		})
	}
}

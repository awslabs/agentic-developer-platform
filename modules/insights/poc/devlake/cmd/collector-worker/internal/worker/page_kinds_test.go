package worker

import (
	"reflect"
	"testing"
)

func TestPageKindsLimitScopedNativeReads(t *testing.T) {
	for _, scenario := range []struct {
		tool  string
		kinds []collectionPageKind
	}{
		{"github", []collectionPageKind{{"repository", 100}, {"issue", 100}, {"pull_request", 100}}},
		{"jira", []collectionPageKind{{"board", 100}, {"issue", 100}}},
		{"sonarqube", []collectionPageKind{{"project", 100}, {"issue", 100}}},
		{"argocd", []collectionPageKind{{"application", 100}, {"sync_operation", 100}}},
		{"jenkins", []collectionPageKind{{"job", 100}, {"build", 100}}},
		{"slack", []collectionPageKind{{"channel", 100}, {"channel_message", 100}}},
		{"linear", []collectionPageKind{{"team", 100}, {"issue", 100}}},
		{"pagerduty", []collectionPageKind{{"service", 100}, {"incident", 100}}},
		{"gitlab", []collectionPageKind{{"pipeline_detail", 10}}},
	} {
		t.Run(scenario.tool, func(t *testing.T) {
			actual, err := pageKinds(scenario.tool, 100)
			if err != nil || !reflect.DeepEqual(actual, scenario.kinds) {
				t.Fatalf("unscoped native read: %v, %v", actual, err)
			}
		})
	}
	for _, tool := range []string{"", "unsupported"} {
		if _, err := pageKinds(tool, 100); err == nil {
			t.Fatalf("unsupported provider %q reached a page read", tool)
		}
	}
	for _, size := range []int{0, 101} {
		if _, err := pageKinds("github", size); err == nil {
			t.Fatalf("unbounded page size %d accepted", size)
		}
	}
	if actual, err := pageKinds("gitlab", 3); err != nil || actual[0].limit != 3 {
		t.Fatalf("small GitLab page changed: %v, %v", actual, err)
	}
}

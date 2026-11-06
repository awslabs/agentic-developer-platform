package collection

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"testing"
)

type fixtureClassifier struct {
	decisions map[NativeKey]Decision
	reads     int
}

func (classifier *fixtureClassifier) Classify(_ context.Context, _ Source, record NativeRecord) (Decision, error) {
	classifier.reads++
	return classifier.decisions[record.Key], nil
}

func nativeFixture() (Source, []NativePage, []NativeKey, *fixtureClassifier) {
	source := Source{Tool: "github", TenantID: "tenant", ProjectID: "project", CellID: "cell", ConnectionID: "connection", Scope: "repo-a"}
	first := NativeKey{TenantID: "tenant", ProjectID: "project", CellID: "cell", Tool: "github", ConnectionID: "connection", Scope: "repo-a", Kind: "issue", NativeID: "1"}
	second := NativeKey{TenantID: "tenant", ProjectID: "project", CellID: "cell", Tool: "github", ConnectionID: "connection", Scope: "repo-a", Kind: "issue", NativeID: "2"}
	pages := []NativePage{
		{Records: []NativeRecord{{Key: first, Revision: "1", Payload: json.RawMessage(`{"number":12,"state":"open"}`)}}, NextCursor: "page-2"},
		{Records: []NativeRecord{{Key: second, Revision: "2", Payload: json.RawMessage(`{"body":"private data"}`)}}, Complete: true},
	}
	classifier := &fixtureClassifier{decisions: map[NativeKey]Decision{
		first:  {Disposition: "allow", Fields: map[string]bool{"number": true, "state": true}},
		second: {Disposition: "exclude", Reason: "private"},
	}}
	return source, pages, []NativeKey{first, second}, classifier
}

func TestReconcilePreservesOnlyApprovedNativeBytesAndExplicitExclusions(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	source, pages, expected, classifier := nativeFixture()
	original := bytes.Clone(pages[0].Records[0].Payload)
	result, err := Reconcile(context.Background(), source, "connection", pages, expected, classifier)
	if err != nil || len(result.Allowed) != 1 || len(result.Exclusions) != 1 || classifier.reads != 2 {
		t.Fatalf("reconciliation: %+v, %v; classification reads %d", result, err, classifier.reads)
	}
	if !bytes.Equal(result.Allowed[0].Payload, original) || result.Exclusions[0].Reason != "private" {
		t.Fatalf("native bytes or exclusion changed: %+v", result)
	}
	pages[0].Records[0].Payload[0] = 'X'
	if !bytes.Equal(result.Allowed[0].Payload, original) {
		t.Fatal("returned native bytes alias the provider page")
	}
}

func TestReconcileRefusesGapsDuplicatesAndUnknownPayloads(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	for _, scenario := range []struct {
		name string
		edit func([]NativePage, []NativeKey, *fixtureClassifier) ([]NativePage, []NativeKey)
		want error
	}{
		{"missing object", func(pages []NativePage, expected []NativeKey, _ *fixtureClassifier) ([]NativePage, []NativeKey) {
			return pages[:1], expected
		}, ErrIncomplete},
		{"omitted record on final page", func(pages []NativePage, expected []NativeKey, _ *fixtureClassifier) ([]NativePage, []NativeKey) {
			pages[1].Records = nil
			return pages, expected
		}, ErrIncomplete},
		{"no independent enumeration", func(pages []NativePage, _ []NativeKey, _ *fixtureClassifier) ([]NativePage, []NativeKey) {
			return pages, nil
		}, ErrIncomplete},
		{"duplicate record", func(pages []NativePage, expected []NativeKey, _ *fixtureClassifier) ([]NativePage, []NativeKey) {
			pages[1].Records = append(pages[1].Records, pages[0].Records[0])
			return pages, expected
		}, ErrIncomplete},
		{"unapproved field", func(pages []NativePage, expected []NativeKey, _ *fixtureClassifier) ([]NativePage, []NativeKey) {
			pages[0].Records[0].Payload = json.RawMessage(`{"number":12,"body":"secret"}`)
			return pages, expected
		}, ErrPolicy},
		{"foreign project", func(pages []NativePage, expected []NativeKey, _ *fixtureClassifier) ([]NativePage, []NativeKey) {
			pages[1].Records[0].Key.ProjectID = "foreign"
			return pages, expected
		}, ErrIncomplete},
		{"wrong provider", func(pages []NativePage, expected []NativeKey, _ *fixtureClassifier) ([]NativePage, []NativeKey) {
			pages[1].Records[0].Key.Tool = "jira"
			return pages, expected
		}, ErrIncomplete},
		{"duplicate key", func(pages []NativePage, expected []NativeKey, _ *fixtureClassifier) ([]NativePage, []NativeKey) {
			pages[0].Records[0].Payload = json.RawMessage(`{"number":12,"number":13}`)
			return pages, expected
		}, ErrPolicy},
		{"nested private key", func(pages []NativePage, expected []NativeKey, _ *fixtureClassifier) ([]NativePage, []NativeKey) {
			pages[0].Records[0].Payload = json.RawMessage(`{"state":{"private":"secret"}}`)
			return pages, expected
		}, ErrPolicy},
		{"trailing bytes", func(pages []NativePage, expected []NativeKey, _ *fixtureClassifier) ([]NativePage, []NativeKey) {
			pages[0].Records[0].Payload = json.RawMessage(`{"number":12} {"body":"secret"}`)
			return pages, expected
		}, ErrPolicy},
		{"unknown decision", func(pages []NativePage, expected []NativeKey, classifier *fixtureClassifier) ([]NativePage, []NativeKey) {
			delete(classifier.decisions, expected[0])
			return pages, expected
		}, ErrPolicy},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			source, pages, expected, classifier := nativeFixture()
			pages, expected = scenario.edit(pages, expected, classifier)
			result, err := Reconcile(context.Background(), source, "connection", pages, expected, classifier)
			if !errors.Is(err, scenario.want) || len(result.Allowed) != 0 || len(result.Exclusions) != 0 {
				t.Fatalf("incomplete population was returned: %+v, %v", result, err)
			}
		})
	}
}

func TestReconcileDisabledBeforePolicyRead(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "false")
	source, pages, expected, classifier := nativeFixture()
	_, err := Reconcile(context.Background(), source, "connection", pages, expected, classifier)
	if !errors.Is(err, ErrDisabled) || classifier.reads != 0 {
		t.Fatalf("disabled collection classified records: %v, reads %d", err, classifier.reads)
	}
}

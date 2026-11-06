package worker

import (
	"testing"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
)

func TestSelectCellChecksEveryClaimedSourceBeforeOpeningPool(t *testing.T) {
	providers := []string{"github", "jira", "sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear", "slack"}
	job := collection.Job{}
	contexts := make(map[collection.Source]SourceContext)
	for _, provider := range providers {
		source := collection.Source{Tool: provider, TenantID: "tenant", ProjectID: "project", CellID: "cell",
			ConnectionID: provider + "-connection", Scope: provider + "-scope", BindingID: provider + "-binding",
			BindingRevision: 2, BindingGeneration: 3, GrantID: provider + "-grant", Consumer: "worker"}
		current := SourceContext{Handle: provider + "-handle", Provider: provider, TenantID: "tenant", ProjectID: "project", CellID: "cell",
			Consumer: "worker", BindingID: source.BindingID, BindingRevision: 2, SourceGeneration: 3, GrantID: source.GrantID}
		current.Destination.DatabaseRef = "database"
		current.Destination.RoleRef = "role"
		contexts[source] = current
		job.Sources = append(job.Sources, source)
	}
	cells := []Cell{{DatabaseRef: "database", RoleRef: "role", ResourceID: "resource", Writer: "writer", Reader: "reader"}}
	for _, scenario := range []struct {
		name   string
		mutate func(*collection.Job, map[collection.Source]SourceContext, *[]Cell)
		valid  bool
	}{
		{"native pair", func(job *collection.Job, contexts map[collection.Source]SourceContext, _ *[]Cell) {
			job.Sources = job.Sources[:2]
			for source := range contexts {
				if source.Tool != "github" && source.Tool != "jira" {
					delete(contexts, source)
				}
			}
		}, true},
		{"nine sources", func(*collection.Job, map[collection.Source]SourceContext, *[]Cell) {}, true},
		{"foreign cell", func(job *collection.Job, contexts map[collection.Source]SourceContext, _ *[]Cell) {
			current := contexts[job.Sources[8]]
			current.CellID = "foreign"
			contexts[job.Sources[8]] = current
		}, false},
		{"foreign tenant", func(job *collection.Job, contexts map[collection.Source]SourceContext, _ *[]Cell) {
			current := contexts[job.Sources[8]]
			current.TenantID = "foreign"
			contexts[job.Sources[8]] = current
		}, false},
		{"duplicate provider", func(job *collection.Job, contexts map[collection.Source]SourceContext, _ *[]Cell) {
			current := contexts[job.Sources[8]]
			current.Provider = "linear"
			contexts[job.Sources[8]] = current
		}, false},
		{"foreign project", func(job *collection.Job, contexts map[collection.Source]SourceContext, _ *[]Cell) {
			current := contexts[job.Sources[8]]
			current.ProjectID = "foreign"
			contexts[job.Sources[8]] = current
		}, false},
		{"different destination", func(job *collection.Job, contexts map[collection.Source]SourceContext, _ *[]Cell) {
			current := contexts[job.Sources[8]]
			current.Destination.RoleRef = "other"
			contexts[job.Sources[8]] = current
		}, false},
		{"stale binding", func(job *collection.Job, contexts map[collection.Source]SourceContext, _ *[]Cell) {
			current := contexts[job.Sources[8]]
			current.SourceGeneration++
			contexts[job.Sources[8]] = current
		}, false},
		{"missing handle", func(job *collection.Job, contexts map[collection.Source]SourceContext, _ *[]Cell) {
			current := contexts[job.Sources[8]]
			current.Handle = ""
			contexts[job.Sources[8]] = current
		}, false},
		{"missing source context", func(job *collection.Job, contexts map[collection.Source]SourceContext, _ *[]Cell) {
			delete(contexts, job.Sources[8])
		}, false},
		{"duplicate cell mapping", func(_ *collection.Job, _ map[collection.Source]SourceContext, cells *[]Cell) {
			*cells = append(*cells, (*cells)[0])
		}, false},
		{"writer is reader", func(_ *collection.Job, _ map[collection.Source]SourceContext, cells *[]Cell) {
			(*cells)[0].Reader = "writer"
		}, false},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			selected := job
			selected.Sources = append([]collection.Source(nil), job.Sources...)
			contextCopy := make(map[collection.Source]SourceContext, len(contexts))
			for key, value := range contexts {
				contextCopy[key] = value
			}
			cellCopy := append([]Cell(nil), cells...)
			scenario.mutate(&selected, contextCopy, &cellCopy)
			cell, err := selectCell(selected, contextCopy, cellCopy)
			if scenario.valid && (err != nil || cell != &cellCopy[0]) {
				t.Fatalf("valid cell selection: %v, %+v", err, cell)
			}
			if !scenario.valid && (err == nil || cell != nil) {
				t.Fatalf("invalid cell selection: %v, %+v", err, cell)
			}
		})
	}
}

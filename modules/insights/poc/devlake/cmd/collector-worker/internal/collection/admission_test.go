package collection

import (
	"context"
	"errors"
	"testing"
	"time"
)

type fixtureAuthority struct {
	reads int
	wrong bool
}

func (authority *fixtureAuthority) CurrentSource(_ context.Context, source Source) (CurrentSource, error) {
	authority.reads++
	if authority.wrong {
		source.BindingGeneration++
	}
	return CurrentSource{Source: source, BindingActive: true, CellActive: true,
		GrantActive: true, GrantPurpose: "collection", GrantExpires: time.Now().Add(time.Hour)}, nil
}

type fixtureMigration struct {
	reads int
	err   error
}

func (migration *fixtureMigration) CheckReceipt(_ context.Context, cell, digest string) error {
	migration.reads++
	if cell != "cell" || digest != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" {
		return errors.New("wrong receipt")
	}
	return migration.err
}

func fixtureJob(now time.Time) Job {
	return Job{Sources: []Source{
		{Tool: "github", TenantID: "tenant", ProjectID: "project", CellID: "cell", ConnectionID: "connection", BindingID: "binding-a", BindingGeneration: 1, BindingRevision: 2, GrantID: "grant-a", Consumer: "worker", Scope: "repo-a"},
		{Tool: "jira", TenantID: "tenant", ProjectID: "project", CellID: "cell", ConnectionID: "connection", BindingID: "binding-b", BindingGeneration: 3, BindingRevision: 4, GrantID: "grant-b", Consumer: "worker", Scope: "project-b"},
	}, Cutoff: now.Add(-30 * 24 * time.Hour), PageSize: 100, PageLimit: 5}
}

func TestAdmitRequiresCurrentAuthoritiesAndMigration(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	now := time.Now()
	digest := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	for _, item := range []struct {
		name string
		edit func(*Job, *fixtureAuthority, *fixtureMigration)
		want error
	}{
		{"valid", func(*Job, *fixtureAuthority, *fixtureMigration) {}, nil},
		{"foreign cell", func(job *Job, _ *fixtureAuthority, _ *fixtureMigration) { job.Sources[1].CellID = "foreign" }, ErrUnauthorized},
		{"reused provider", func(job *Job, _ *fixtureAuthority, _ *fixtureMigration) { job.Sources[1].Tool = "github" }, ErrUnauthorized},
		{"stale binding", func(_ *Job, authority *fixtureAuthority, _ *fixtureMigration) { authority.wrong = true }, ErrUnauthorized},
		{"missing receipt", func(_ *Job, _ *fixtureAuthority, migration *fixtureMigration) { migration.err = errors.New("absent") }, ErrMigrationRequired},
		{"over limit", func(job *Job, _ *fixtureAuthority, _ *fixtureMigration) { job.PageLimit = 6 }, ErrUnauthorized},
		{"old cutoff", func(job *Job, _ *fixtureAuthority, _ *fixtureMigration) { job.Cutoff = now.Add(-31 * 24 * time.Hour) }, ErrUnauthorized},
	} {
		t.Run(item.name, func(t *testing.T) {
			job := fixtureJob(now)
			authority := &fixtureAuthority{}
			migration := &fixtureMigration{}
			item.edit(&job, authority, migration)
			err := Admit(context.Background(), job, authority, migration, digest, now)
			if !errors.Is(err, item.want) && (item.want != nil || err != nil) {
				t.Fatalf("admission result: %v, want %v", err, item.want)
			}
			if item.want == nil && (authority.reads != 2 || migration.reads != 1) {
				t.Fatalf("authority reads %d, migration reads %d", authority.reads, migration.reads)
			}
		})
	}
}

func TestDisabledNeverReadsAuthority(t *testing.T) {
	for _, flags := range [][2]string{{"", ""}, {"true", "false"}, {"false", "true"}, {"invalid", "true"}} {
		t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", flags[0])
		t.Setenv("DEVLAKE_COLLECTION_ENABLED", flags[1])
		authority := &fixtureAuthority{}
		migration := &fixtureMigration{}
		err := Admit(context.Background(), fixtureJob(time.Now()), authority, migration,
			"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", time.Now())
		if !errors.Is(err, ErrDisabled) || authority.reads != 0 || migration.reads != 0 {
			t.Fatalf("flags %v: %v, authority reads %d, migration reads %d", flags, err, authority.reads, migration.reads)
		}
	}
}
func TestAdmitExpandedSourcesRequiresCurrentScopedGrants(t *testing.T) {
	t.Setenv("FEATURE_DEVLAKE_CONNECTORS_ENABLED", "true")
	t.Setenv("DEVLAKE_COLLECTION_ENABLED", "true")
	now := time.Now()
	digest := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	providers := []string{"sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear", "slack"}
	job := fixtureJob(now)
	for _, provider := range providers {
		source := job.Sources[0]
		source.Tool = provider
		source.Scope = provider + "-native-scope"
		source.BindingID = provider + "-binding"
		source.GrantID = provider + "-grant"
		job.Sources = append(job.Sources, source)
	}
	for _, scenario := range []struct {
		name string
		edit func(*Job, *fixtureAuthority)
		want error
	}{
		{"all current", func(*Job, *fixtureAuthority) {}, nil},
		{"single expanded scope", func(job *Job, _ *fixtureAuthority) { job.Sources = job.Sources[2:3] }, nil},
		{"foreign tenant", func(job *Job, _ *fixtureAuthority) { job.Sources[2].TenantID = "foreign" }, ErrUnauthorized},
		{"foreign project", func(job *Job, _ *fixtureAuthority) { job.Sources[2].ProjectID = "foreign" }, ErrUnauthorized},
		{"foreign cell", func(job *Job, _ *fixtureAuthority) { job.Sources[2].CellID = "foreign" }, ErrUnauthorized},
		{"wrong consumer", func(job *Job, _ *fixtureAuthority) { job.Sources[2].Consumer = "foreign" }, ErrUnauthorized},
		{"duplicate provider", func(job *Job, _ *fixtureAuthority) { job.Sources[3].Tool = job.Sources[2].Tool }, ErrUnauthorized},
		{"extra provider", func(job *Job, _ *fixtureAuthority) { job.Sources = append(job.Sources, job.Sources[2]) }, ErrUnauthorized},
		{"unsupported provider", func(job *Job, _ *fixtureAuthority) { job.Sources[2].Tool = "unknown" }, ErrUnauthorized},
		{"missing grant", func(job *Job, _ *fixtureAuthority) { job.Sources[2].GrantID = "" }, ErrUnauthorized},
		{"stale generation", func(_ *Job, authority *fixtureAuthority) { authority.wrong = true }, ErrUnauthorized},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			selected := job
			selected.Sources = append([]Source(nil), job.Sources...)
			authority := &fixtureAuthority{}
			migration := &fixtureMigration{}
			scenario.edit(&selected, authority)
			err := Admit(context.Background(), selected, authority, migration, digest, now)
			if !errors.Is(err, scenario.want) && (scenario.want != nil || err != nil) {
				t.Fatalf("admission: %v, want %v", err, scenario.want)
			}
			if scenario.want == nil && (authority.reads != len(selected.Sources) || migration.reads != 1) {
				t.Fatalf("authority reads %d, migration reads %d", authority.reads, migration.reads)
			}
		})
	}
}

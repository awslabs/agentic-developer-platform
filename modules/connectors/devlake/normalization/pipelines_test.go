package normalization

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	corecontext "github.com/apache/devlake/core/context"
	"github.com/apache/devlake/core/models"
	"github.com/apache/devlake/core/models/domainlayer/devops"
	"github.com/apache/devlake/core/plugin"
	"github.com/apache/devlake/helpers/pluginhelper/api"
	implcontext "github.com/apache/devlake/impls/context"
	"github.com/apache/devlake/impls/dalgorm"
	"github.com/apache/devlake/impls/logruslog"
	gitlabimpl "github.com/apache/devlake/plugins/gitlab/impl"
	gitlabmodels "github.com/apache/devlake/plugins/gitlab/models"
	gitlab "github.com/apache/devlake/plugins/gitlab/tasks"
	jenkinsimpl "github.com/apache/devlake/plugins/jenkins/impl"
	jenkinsmodels "github.com/apache/devlake/plugins/jenkins/models"
	jenkins "github.com/apache/devlake/plugins/jenkins/tasks"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/sirupsen/logrus"
	"github.com/spf13/viper"
	"github.com/stretchr/testify/require"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

const (
	jenkinsRaw    = "_raw_jenkins_api_builds"
	gitlabRaw     = "_raw_gitlab_api_pipeline_details"
	jenkinsParams = `{"ConnectionId":1,"FullName":"team/api"}`
	gitlabParams  = `{"ConnectionId":1,"ProjectId":42}`
)

func TestNativePipelineNormalization(t *testing.T) {
	if os.Getenv("DEVLAKE_TEST_PG_SOCKET") == "" {
		t.Skip("use scripts/test-normalization.sh to create the disposable PostgreSQL cluster")
	}
	// Real upstream plugin metadata is needed by didgen. Do not call InitPlugins:
	// the DB-only task path must not initialize global API clients or credentials.
	require.NoError(t, plugin.RegisterPlugin("jenkins", jenkinsimpl.Jenkins{}))
	require.NoError(t, plugin.RegisterPlugin("gitlab", gitlabimpl.Gitlab{}))
	admin := connect(t, "postgres", "devlake_test_admin")
	aAdmin, a := cell(t, admin, "cell_a")
	_, b := cell(t, admin, "cell_b")
	seed(t, a, false)
	seed(t, b, true)
	run(t, a)
	run(t, b)

	// One domain-table query reconciles both sources. Expected mappings are
	// independent literal values, never computed by calling upstream map helpers.
	rows := pipelines(t, a)
	require.Len(t, rows, 6)
	want := map[string][4]string{
		"jenkins:JenkinsBuild:1:team/api#7": {"DONE", "SUCCESS", "false", "SUCCESS"},
		"jenkins:JenkinsBuild:1:team/api#8": {"DONE", "", "false", "UNSTABLE"},
		"jenkins:JenkinsBuild:1:team/api#9": {"IN_PROGRESS", "", "true", "SUCCESS"},
		"gitlab:GitlabPipeline:1:7":         {"DONE", "FAILURE", "failed", ""},
		"gitlab:GitlabPipeline:1:8":         {"IN_PROGRESS", "", "running", ""},
		"gitlab:GitlabPipeline:1:9":         {"OTHER", "", "manual", ""},
	}
	for _, row := range rows {
		expected, ok := want[row.Id]
		require.True(t, ok, "unexpected pipeline %s", row.Id)
		require.Equal(t, expected, [4]string{row.Status, row.Result, row.OriginalStatus, row.OriginalResult}, row.Id)
		// Neither source's build success nor a nonmatching enrichment expression
		// establishes a deployment or production environment.
		require.Empty(t, row.Environment, row.Id)
		require.Empty(t, row.Type, row.Id)
		assertOrigin(t, a, row)
	}
	started := time.Date(2025, 1, 1, 0, 0, 0, 0, time.UTC)
	for _, id := range []string{"jenkins:JenkinsBuild:1:team/api#7", "gitlab:GitlabPipeline:1:7"} {
		row := pipeline(t, a, id)
		require.Equal(t, 42.0, row.DurationSec, id)
		require.True(t, row.CreatedDate.Equal(started), id)
		require.NotNil(t, row.StartedDate, id)
		require.True(t, row.StartedDate.Equal(started), id)
		require.NotNil(t, row.FinishedDate, id)
		require.True(t, row.FinishedDate.Equal(started.Add(42*time.Second)), id)
	}
	require.Nil(t, pipeline(t, a, "jenkins:JenkinsBuild:1:team/api#9").FinishedDate)
	require.True(t, pipeline(t, a, "gitlab:GitlabPipeline:1:8").IsChild)
	require.Equal(t, "jenkins:JenkinsJob:1:team/api", pipeline(t, a, "jenkins:JenkinsBuild:1:team/api#7").CicdScopeId)
	require.Equal(t, "gitlab:GitlabProject:1:42", pipeline(t, a, "gitlab:GitlabPipeline:1:7").CicdScopeId)

	t.Run("native tasks and ambiguous revisions", func(t *testing.T) {
		var tasks []devops.CICDTask
		require.NoError(t, a.Order("id").Find(&tasks).Error)
		require.Len(t, tasks, 3)
		for _, task := range tasks {
			parent := pipeline(t, a, task.PipelineId)
			require.Equal(t, parent.Id, task.Id)
			require.Equal(t, parent.RawDataOrigin, task.RawDataOrigin)
		}
		var commits []jenkinsmodels.JenkinsBuildCommit
		require.NoError(t, a.Order("commit_sha").Find(&commits).Error)
		require.Len(t, commits, 2)
		require.Equal(t, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", commits[0].CommitSha)
		require.Equal(t, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", commits[1].CommitSha)
		require.Equal(t, "https://example.org/team/api.git", commits[0].RepoUrl)
		require.Equal(t, "https://example.org/team/library.git", commits[1].RepoUrl)
		for _, commit := range commits {
			require.Equal(t, "team/api#7", commit.BuildName)
			require.Equal(t, pipeline(t, a, "jenkins:JenkinsBuild:1:team/api#7").RawDataOrigin, commit.RawDataOrigin)
		}
		// No first-match commit join or invented revision for empty native SHA.
		var missing []gitlabmodels.GitlabPipeline
		require.NoError(t, a.Where("gitlab_id IN ?", []int{8, 9}).Find(&missing).Error)
		require.Len(t, missing, 2)
		for _, tool := range missing {
			require.Empty(t, tool.Sha)
		}
		var invented int64
		require.NoError(t, a.Model(&devops.CiCDPipelineCommit{}).Count(&invented).Error)
		require.Zero(t, invented)
	})

	t.Run("repeat preserves identities and raw lineage", func(t *testing.T) {
		run(t, a)
		again := pipelines(t, a)
		require.Len(t, again, len(rows))
		for i := range again {
			// Storage timestamps legitimately change during a full native replay.
			again[i].CreatedAt = rows[i].CreatedAt
			again[i].UpdatedAt = rows[i].UpdatedAt
		}
		require.Equal(t, rows, again)
		var states []models.SubtaskState
		require.NoError(t, a.Find(&states).Error)
		require.Len(t, states, 2)
		for _, state := range states {
			require.Equal(t, "gitlab", state.Plugin)
			require.Equal(t, gitlabParams, state.Params)
			require.NotNil(t, state.PrevStartedAt)
		}
	})

	t.Run("same upstream IDs remain isolated by cell role and database", func(t *testing.T) {
		id := "jenkins:JenkinsBuild:1:team/api#7"
		first, second := pipeline(t, a, id), pipeline(t, b, id)
		require.Equal(t, first.Id, second.Id)
		require.Equal(t, first.RawDataOrigin, second.RawDataOrigin)
		require.Equal(t, "SUCCESS", first.Result)
		require.Equal(t, "FAILURE", second.Result)
		for _, pair := range [][2]string{{"cell_b", "cell_a_runtime"}, {"cell_a", "cell_b_runtime"}} {
			conn, err := pgx.Connect(context.Background(), dsn(pair[0], pair[1]))
			if conn != nil {
				_ = conn.Close(context.Background())
			}
			require.Error(t, err, "cross-cell connection unexpectedly succeeded")
			var denied *pgconn.PgError
			require.True(t, errors.As(err, &denied))
			require.Equal(t, "42501", denied.Code)
		}
		for _, query := range []string{"SET ROLE cell_b_runtime", "CREATE TABLE unauthorized(id integer)"} {
			var denied *pgconn.PgError
			require.True(t, errors.As(a.Exec(query).Error, &denied))
			require.Equal(t, "42501", denied.Code)
		}
	})

	t.Run("cancellation and unsafe selection do not write", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		err := JenkinsBuilds(ctx, resources(t, a), job(), enrichment(t))
		require.ErrorIs(t, err, context.Canceled)
		err = GitLabPipelines(ctx, resources(t, a), project(), enrichment(t), fullSync())
		require.ErrorIs(t, err, context.Canceled)
		options := job()
		options.Class = jenkins.WORKFLOW_MULTI_BRANCH_PROJECT
		require.ErrorContains(t, JenkinsBuilds(context.Background(), resources(t, a), options, enrichment(t)), "multibranch")
		require.Len(t, pipelines(t, a), 6)
	})

	t.Run("missing raw table fails rather than reporting success", func(t *testing.T) {
		require.NoError(t, aAdmin.Exec("DROP TABLE "+jenkinsRaw).Error)
		require.ErrorContains(t, JenkinsBuilds(context.Background(), resources(t, a), job(), enrichment(t)), "not provisioned")
		require.NoError(t, aAdmin.Exec("DROP TABLE "+gitlabRaw).Error)
		require.ErrorContains(t, GitLabPipelines(context.Background(), resources(t, a), project(), enrichment(t), fullSync()), "not provisioned")
		require.Len(t, pipelines(t, a), 6)
	})
}

func dsn(database, role string) string {
	u := url.URL{Scheme: "postgres", User: url.User(role), Path: "/" + database}
	query := url.Values{"host": {os.Getenv("DEVLAKE_TEST_PG_SOCKET")}, "sslmode": {"disable"}, "connect_timeout": {"5"}}
	u.RawQuery = query.Encode()
	return u.String()
}

func connect(t *testing.T, database, role string) *gorm.DB {
	t.Helper()
	db, err := gorm.Open(postgres.Open(dsn(database, role)), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	require.NoError(t, err)
	pool, err := db.DB()
	require.NoError(t, err)
	pool.SetMaxOpenConns(3)
	t.Cleanup(func() { require.NoError(t, pool.Close()) })
	return db
}

func cell(t *testing.T, admin *gorm.DB, name string) (*gorm.DB, *gorm.DB) {
	t.Helper()
	// Names are fixed test literals in a disposable cluster. Runtime roles own
	// neither schemas nor databases and get only DML on provisioned native tables.
	role := name + "_runtime"
	require.NoError(t, admin.Exec("CREATE ROLE "+role+" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS").Error)
	require.NoError(t, admin.Exec("CREATE DATABASE "+name).Error)
	require.NoError(t, admin.Exec("REVOKE ALL ON DATABASE "+name+" FROM PUBLIC").Error)
	require.NoError(t, admin.Exec("GRANT CONNECT ON DATABASE "+name+" TO "+role).Error)
	db := connect(t, name, "devlake_test_admin")
	require.NoError(t, db.Exec("REVOKE ALL ON SCHEMA public FROM PUBLIC").Error)
	require.NoError(t, db.AutoMigrate(&jenkinsmodels.JenkinsBuild{}, &jenkinsmodels.JenkinsBuildCommit{}, &gitlabmodels.GitlabPipeline{}, &devops.CICDPipeline{}, &devops.CICDTask{}, &devops.CiCDPipelineCommit{}, &models.SubtaskState{}))
	for _, table := range []string{jenkinsRaw, gitlabRaw} {
		require.NoError(t, db.Table(table).AutoMigrate(&api.RawData{}))
	}
	require.NoError(t, db.Exec("GRANT USAGE ON SCHEMA public TO "+role).Error)
	require.NoError(t, db.Exec("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "+role).Error)
	require.NoError(t, db.Exec("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "+role).Error)
	return db, connect(t, name, role)
}

func resources(t *testing.T, db *gorm.DB) corecontext.BasicRes {
	t.Helper()
	log := logrus.New()
	log.SetOutput(io.Discard)
	native, err := logruslog.NewDefaultLogger(log)
	require.NoError(t, err)
	return implcontext.NewDefaultBasicRes(viper.New(), native, dalgorm.NewDalgorm(db))
}

func enrichment(t *testing.T) *api.RegexEnricher {
	t.Helper()
	enricher := api.NewRegexEnricher()
	// Explicit config prevents Jenkins' omitted-regex PRODUCTION default from
	// being mistaken for observed native environment data.
	require.NoError(t, enricher.TryAdd(devops.PRODUCTION, "^production/"))
	require.NoError(t, enricher.TryAdd(devops.DEPLOYMENT, "^deploy/"))
	return enricher
}

func job() jenkins.JenkinsOptions {
	return jenkins.JenkinsOptions{ConnectionId: 1, JobFullName: "team/api"}
}

func project() gitlab.GitlabOptions {
	return gitlab.GitlabOptions{ConnectionId: 1, ProjectId: 42}
}

func fullSync() models.SyncPolicy {
	return models.SyncPolicy{TriggerSyncPolicy: models.TriggerSyncPolicy{FullSync: true}}
}

func run(t *testing.T, db *gorm.DB) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	res := resources(t, db.WithContext(ctx))
	require.NoError(t, JenkinsBuilds(ctx, res, job(), enrichment(t)))
	require.NoError(t, GitLabPipelines(ctx, res, project(), enrichment(t), fullSync()))
}

func seed(t *testing.T, db *gorm.DB, failed bool) {
	t.Helper()
	for _, source := range [][3]string{{jenkinsRaw, jenkinsParams, "jenkins-builds.json"}, {gitlabRaw, gitlabParams, "gitlab-pipeline-details.json"}} {
		payload, err := os.ReadFile("testdata/" + source[2])
		require.NoError(t, err)
		if failed && source[0] == jenkinsRaw {
			payload = []byte(strings.Replace(string(payload), `"SUCCESS"`, `"FAILURE"`, 1))
		}
		var bodies []json.RawMessage
		require.NoError(t, json.Unmarshal(payload, &bodies))
		for i, body := range bodies {
			record := api.RawData{ID: uint64(i + 1), Params: source[1], Data: body, Input: json.RawMessage(`{}`), Url: fmt.Sprintf("https://example.org/native/%s/%d", source[2], i+7), CreatedAt: time.Date(2025, 1, 2, 0, 0, 0, 0, time.UTC)}
			require.NoError(t, db.Table(source[0]).Create(&record).Error)
		}
		// The actual raw task scope filter must exclude malformed other-scope
		// data, rather than parsing the entire cell or connection's raw history.
		other := api.RawData{ID: 99, Params: `{"ConnectionId":999}`, Data: []byte(`not json`), Input: json.RawMessage(`{}`), CreatedAt: time.Date(2025, 1, 2, 0, 0, 0, 0, time.UTC)}
		require.NoError(t, db.Table(source[0]).Create(&other).Error)
	}
	// Conversion must also filter native tool rows by job/project, not merely
	// the raw extractor's input filter or a connection-wide scan.
	require.NoError(t, db.Create(&jenkinsmodels.JenkinsBuild{ConnectionId: 1, JobPath: "job/team", JobName: "api-shadow", FullName: "team/api-shadow#7"}).Error)
	require.NoError(t, db.Create(&gitlabmodels.GitlabPipeline{ConnectionId: 1, ProjectId: 99, GitlabId: 900}).Error)
}

func pipelines(t *testing.T, db *gorm.DB) []devops.CICDPipeline {
	t.Helper()
	var rows []devops.CICDPipeline
	require.NoError(t, db.Order("id").Find(&rows).Error)
	return rows
}

func pipeline(t *testing.T, db *gorm.DB, id string) devops.CICDPipeline {
	t.Helper()
	var row devops.CICDPipeline
	require.NoError(t, db.Where("id = ?", id).First(&row).Error)
	return row
}

func assertOrigin(t *testing.T, db *gorm.DB, row devops.CICDPipeline) {
	t.Helper()
	var raw api.RawData
	require.Contains(t, []string{jenkinsRaw, gitlabRaw}, row.RawDataTable)
	require.NoError(t, db.Table(row.RawDataTable).Where("id = ? AND params = ?", row.RawDataId, row.RawDataParams).First(&raw).Error)
	if row.RawDataTable == jenkinsRaw {
		require.Equal(t, jenkinsParams, row.RawDataParams)
		var native struct {
			Number   int64
			Result   string
			Building bool
		}
		require.NoError(t, json.Unmarshal(raw.Data, &native))
		require.Equal(t, fmt.Sprintf("jenkins:JenkinsBuild:1:team/api#%d", native.Number), row.Id)
		require.Equal(t, native.Result, row.OriginalResult)
		require.Equal(t, fmt.Sprint(native.Building), row.OriginalStatus)
		var tool jenkinsmodels.JenkinsBuild
		require.NoError(t, db.Where("connection_id = ? AND full_name = ?", 1, fmt.Sprintf("team/api#%d", native.Number)).First(&tool).Error)
		require.Equal(t, tool.RawDataOrigin, row.RawDataOrigin)
	} else {
		require.Equal(t, gitlabParams, row.RawDataParams)
		var native struct {
			ID     int
			Status string
		}
		require.NoError(t, json.Unmarshal(raw.Data, &native))
		require.Equal(t, fmt.Sprintf("gitlab:GitlabPipeline:1:%d", native.ID), row.Id)
		require.Equal(t, native.Status, row.OriginalStatus)
		var tool gitlabmodels.GitlabPipeline
		require.NoError(t, db.Where("connection_id = ? AND gitlab_id = ?", 1, native.ID).First(&tool).Error)
		require.Equal(t, tool.RawDataOrigin, row.RawDataOrigin)
	}
}

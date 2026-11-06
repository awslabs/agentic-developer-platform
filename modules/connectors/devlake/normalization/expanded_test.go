package normalization

import (
	"context"
	"io"
	"strings"
	"testing"

	"github.com/apache/devlake/core/dal"
	lakeerrors "github.com/apache/devlake/core/errors"
	"github.com/apache/devlake/core/plugin"
	implcontext "github.com/apache/devlake/impls/context"
	"github.com/apache/devlake/impls/logruslog"
	argocdimpl "github.com/apache/devlake/plugins/argocd/impl"
	argocd "github.com/apache/devlake/plugins/argocd/tasks"
	linearimpl "github.com/apache/devlake/plugins/linear/impl"
	linear "github.com/apache/devlake/plugins/linear/tasks"
	pagerdutyimpl "github.com/apache/devlake/plugins/pagerduty/impl"
	pagerduty "github.com/apache/devlake/plugins/pagerduty/tasks"
	slackimpl "github.com/apache/devlake/plugins/slack/impl"
	slack "github.com/apache/devlake/plugins/slack/tasks"
	sonarqubeimpl "github.com/apache/devlake/plugins/sonarqube/impl"
	sonarqube "github.com/apache/devlake/plugins/sonarqube/tasks"
	"github.com/sirupsen/logrus"
	"github.com/spf13/viper"
	"github.com/stretchr/testify/require"
)

type selectedDAL struct {
	dal.Dal
	tables   map[string]bool
	count    int64
	countErr lakeerrors.Error
	reads    int
	clauses  []dal.Clause
}

func (fixture *selectedDAL) HasTable(table interface{}) bool {
	name, ok := table.(string)
	return ok && fixture.tables[name]
}

func (fixture *selectedDAL) Count(clauses ...dal.Clause) (int64, lakeerrors.Error) {
	fixture.clauses = append(fixture.clauses, clauses...)
	return fixture.count, fixture.countErr
}

func (fixture *selectedDAL) Cursor(...dal.Clause) (dal.Rows, lakeerrors.Error) {
	fixture.reads++
	return nil, lakeerrors.Default.New("reached upstream native converter")
}

func TestNativeRawPreflightRejectsMissingCollection(t *testing.T) {
	logging := logrus.New()
	logging.SetOutput(io.Discard)
	logger, logErr := logruslog.NewDefaultLogger(logging)
	require.NoError(t, logErr)
	fixture := &selectedDAL{}
	resources := implcontext.NewDefaultBasicRes(viper.New(), logger, fixture)
	for _, table := range []string{
		"sonarqube_api_issues", "argocd_api_sync_operations", "pagerduty_incidents", "linear_issues", "slack_channel_message",
	} {
		_, err := rawCount(resources, table, map[string]any{"selected": "scope"})
		require.ErrorContains(t, err, "unavailable", table)
	}
	fixture.count = 1
	count, err := rawCount(resources, "sonarqube_api_issues", map[string]any{"selected": "scope"})
	require.NoError(t, err)
	require.EqualValues(t, 1, count)
}

func TestExpandedNativeTasksRejectMissingScopesWithoutProviderCalls(t *testing.T) {
	for _, registration := range []struct {
		name string
		impl plugin.PluginMeta
	}{
		{"sonarqube", sonarqubeimpl.Sonarqube{}},
		{"argocd", argocdimpl.ArgoCD{}},
		{"pagerduty", pagerdutyimpl.PagerDuty{}},
		{"linear", linearimpl.Linear{}},
		{"slack", slackimpl.Slack{}},
	} {
		require.NoError(t, plugin.RegisterPlugin(registration.name, registration.impl))
	}
	logging := logrus.New()
	logging.SetOutput(io.Discard)
	logger, err := logruslog.NewDefaultLogger(logging)
	require.NoError(t, err)
	cases := []struct {
		name   string
		tables []string
		run    func(context.Context, *implcontext.DefaultBasicRes) error
	}{
		{"sonarqube", []string{"_tool_sonarqube_projects", "_raw_sonarqube_api_projects", "_raw_sonarqube_api_issues", "_tool_sonarqube_issues"}, func(ctx context.Context, res *implcontext.DefaultBasicRes) error {
			return SonarProject(ctx, res, sonarqube.SonarqubeOptions{ConnectionId: 1, ProjectKey: "api"})
		}},
		{"argocd", []string{"_tool_argocd_applications", "_raw_argocd_api_applications", "_raw_argocd_api_sync_operations", "_tool_argocd_sync_operations", "_tool_argocd_revision_images", "cicd_scopes", "cicd_deployments", "cicd_deployment_commits"}, func(ctx context.Context, res *implcontext.DefaultBasicRes) error {
			return ArgoApplication(ctx, res, argocd.ArgocdOptions{ConnectionId: 1, ApplicationName: "api"})
		}},
		{"pagerduty", []string{"_tool_pagerduty_services", "_raw_pagerduty_incidents", "_tool_pagerduty_incidents", "_tool_pagerduty_assignments", "_tool_pagerduty_users"}, func(ctx context.Context, res *implcontext.DefaultBasicRes) error {
			return PagerDutyService(ctx, res, pagerduty.PagerDutyOptions{ConnectionId: 1, ServiceId: "P123"})
		}},
		{"linear", []string{"_tool_linear_teams", "_raw_linear_issues", "_tool_linear_issues", "_tool_linear_issue_labels", "_tool_linear_accounts"}, func(ctx context.Context, res *implcontext.DefaultBasicRes) error {
			return LinearTeam(ctx, res, linear.LinearOptions{ConnectionId: 1, TeamId: "team"})
		}},
		{"slack", []string{"_tool_slack_channels", "_raw_slack_channel", "_raw_slack_channel_message", "_tool_slack_channel_messages"}, func(ctx context.Context, res *implcontext.DefaultBasicRes) error {
			return SlackChannel(ctx, res, slack.SlackOptions{ConnectionId: 1, ChannelId: "C123"})
		}},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			fixture := &selectedDAL{tables: map[string]bool{}}
			res := implcontext.NewDefaultBasicRes(viper.New(), logger, fixture)
			require.ErrorContains(t, testCase.run(context.Background(), res), "unavailable")
			require.Zero(t, fixture.reads)
			for _, table := range testCase.tables {
				fixture.tables[table] = true
			}
			canceled, cancel := context.WithCancel(context.Background())
			cancel()
			require.ErrorIs(t, testCase.run(canceled, res), context.Canceled)
			require.Zero(t, fixture.reads)
			if testCase.name != "argocd" && testCase.name != "slack" {
				require.ErrorContains(t, testCase.run(context.Background(), res), "selected native scope")
				require.Zero(t, fixture.reads)
			}
			fixture.count = 1
			require.Error(t, testCase.run(context.Background(), res))
			require.Positive(t, fixture.reads, "native DevLake task must use the supplied cell DAL")
			scoped := false
			for _, clause := range fixture.clauses {
				if clause.Type != dal.WhereClause {
					continue
				}
				query := clause.Data.(dal.DalClause).Expr
				if strings.Contains(query, "connection_id = ?") || strings.Contains(query, "params = ?") {
					scoped = true
				}
			}
			require.True(t, scoped, "selected native scope must be checked before any task reads")
		})
	}
}

func TestArgoRevisionRequiresNativeRepositoryBeforeConversion(t *testing.T) {
	fixture := &selectedDAL{count: 1}
	require.ErrorContains(t, argoCommitOrigin(fixture, 23, "selected-app"), "lacks a native repository")
	require.Zero(t, fixture.reads)
	require.Len(t, fixture.clauses, 2)
	where := fixture.clauses[1].Data.(dal.DalClause)
	require.Equal(t, dal.WhereClause, fixture.clauses[1].Type)
	require.Contains(t, where.Expr, "coalesce(revision, '') <> ''")
	require.Contains(t, where.Expr, "coalesce(repo_url, '') = ''")
	require.Equal(t, []interface{}{uint64(23), "selected-app"}, where.Params)

	fixture.count = 0
	require.NoError(t, argoCommitOrigin(fixture, 23, "selected-app"))
	fixture.countErr = lakeerrors.Default.New("cell query unavailable")
	require.ErrorContains(t, argoCommitOrigin(fixture, 23, "selected-app"), "lacks a native repository")
}

package normalization

import (
	"context"
	"fmt"

	corecontext "github.com/apache/devlake/core/context"
	"github.com/apache/devlake/core/dal"
	"github.com/apache/devlake/core/plugin"
	"github.com/apache/devlake/helpers/pluginhelper/api"
	implcontext "github.com/apache/devlake/impls/context"
	argocdmodels "github.com/apache/devlake/plugins/argocd/models"
	argocd "github.com/apache/devlake/plugins/argocd/tasks"
	linearmodels "github.com/apache/devlake/plugins/linear/models"
	linear "github.com/apache/devlake/plugins/linear/tasks"
	pagerduty "github.com/apache/devlake/plugins/pagerduty/tasks"
	slack "github.com/apache/devlake/plugins/slack/tasks"
	sonarqubemodels "github.com/apache/devlake/plugins/sonarqube/models"
	sonarqube "github.com/apache/devlake/plugins/sonarqube/tasks"
)

func rawCount(res corecontext.BasicRes, table string, params any) (int64, error) {
	count, err := res.GetDal().Count(dal.From("_raw_"+table), dal.Where("params = ?", plugin.MarshalScopeParams(params)))
	if err != nil || count == 0 {
		return 0, fmt.Errorf("selected native raw response is unavailable: %v", err)
	}
	return count, nil
}

func selectedScope(res corecontext.BasicRes, table, field, id string, connection uint64) error {
	count, err := res.GetDal().Count(dal.From(table), dal.Where("connection_id = ? AND "+field+" = ?", connection, id))
	if err != nil || count != 1 {
		return fmt.Errorf("selected native scope is unavailable: %v", err)
	}
	return nil
}

func selectedRaw(res corecontext.BasicRes, table string, params any, unique bool) error {
	count, err := res.GetDal().Count(dal.From("_raw_"+table), dal.Where("params = ?", plugin.MarshalScopeParams(params)))
	if err != nil || count < 1 || unique && count != 1 {
		return fmt.Errorf("selected native raw response is unavailable: %v", err)
	}
	return nil
}

func SonarProject(ctx context.Context, res corecontext.BasicRes, options sonarqube.SonarqubeOptions) error {
	if err := ready(ctx, res, "sonarqube", api.NewRegexEnricher()); err != nil {
		return err
	}
	if options.ConnectionId == 0 || options.ProjectKey == "" ||
		!res.GetDal().HasTable("_tool_sonarqube_projects") || !res.GetDal().HasTable("_raw_"+sonarqube.RAW_PROJECTS_TABLE) ||
		!res.GetDal().HasTable("_raw_"+sonarqube.RAW_ISSUES_TABLE) || !res.GetDal().HasTable("_tool_sonarqube_issues") {
		return fmt.Errorf("selected SonarQube project or its native tables are unavailable")
	}
	if err := selectedScope(res, "_tool_sonarqube_projects", "project_key", options.ProjectKey, options.ConnectionId); err != nil {
		return err
	}
	if err := selectedRaw(res, sonarqube.RAW_PROJECTS_TABLE, sonarqubemodels.SonarqubeApiParams{ConnectionId: options.ConnectionId, ProjectKey: options.ProjectKey}, false); err != nil {
		return err
	}
	data := &sonarqube.SonarqubeTaskData{Options: &options}
	if err := sonarqube.ConvertProjects(implcontext.NewStandaloneSubTaskContext(ctx, res, sonarqube.ConvertProjectsMeta.Name, data, "sonarqube", nil)); err != nil {
		return err
	}
	count, err := rawCount(res, sonarqube.RAW_ISSUES_TABLE, sonarqubemodels.SonarqubeApiParams{ConnectionId: options.ConnectionId, ProjectKey: options.ProjectKey})
	if err != nil || count == 0 {
		return err
	}
	if err := sonarqube.ExtractIssues(implcontext.NewStandaloneSubTaskContext(ctx, res, sonarqube.ExtractIssuesMeta.Name, data, "sonarqube", nil)); err != nil {
		return err
	}
	return sonarqube.ConvertIssues(implcontext.NewStandaloneSubTaskContext(ctx, res, sonarqube.ConvertIssuesMeta.Name, data, "sonarqube", nil))
}

func ArgoApplication(ctx context.Context, res corecontext.BasicRes, options argocd.ArgocdOptions) error {
	if err := ready(ctx, res, "argocd", api.NewRegexEnricher()); err != nil {
		return err
	}
	if options.ConnectionId == 0 || options.ApplicationName == "" ||
		!res.GetDal().HasTable("_raw_"+argocd.RAW_APPLICATION_TABLE) || !res.GetDal().HasTable("_tool_argocd_applications") ||
		!res.GetDal().HasTable("_raw_"+argocd.RAW_SYNC_OPERATION_TABLE) || !res.GetDal().HasTable("_tool_argocd_sync_operations") ||
		!res.GetDal().HasTable("_tool_argocd_revision_images") || !res.GetDal().HasTable("cicd_scopes") ||
		!res.GetDal().HasTable("cicd_deployments") || !res.GetDal().HasTable("cicd_deployment_commits") {
		return fmt.Errorf("selected Argo CD application or its native tables are unavailable")
	}
	data := &argocd.ArgocdTaskData{Options: &options}
	if err := selectedRaw(res, argocd.RAW_APPLICATION_TABLE, argocdmodels.ArgocdApiParams{ConnectionId: options.ConnectionId, Name: options.ApplicationName}, false); err != nil {
		return err
	}
	if err := argocd.ExtractApplications(implcontext.NewStandaloneSubTaskContext(ctx, res, argocd.ExtractApplicationsMeta.Name, data, "argocd", nil)); err != nil {
		return err
	}
	if err := selectedScope(res, "_tool_argocd_applications", "name", options.ApplicationName, options.ConnectionId); err != nil {
		return err
	}
	count, err := res.GetDal().Count(dal.From("_tool_argocd_applications"), dal.Where("connection_id = ?", options.ConnectionId))
	if err != nil || count != 1 {
		return fmt.Errorf("Argo CD conversion requires one selected application: %v", err)
	}
	if err := argocd.ConvertApplications(implcontext.NewStandaloneSubTaskContext(ctx, res, argocd.ConvertApplicationsMeta.Name, data, "argocd", nil)); err != nil {
		return err
	}
	count, rawErr := rawCount(res, argocd.RAW_SYNC_OPERATION_TABLE, argocdmodels.ArgocdApiParams{ConnectionId: options.ConnectionId, Name: options.ApplicationName})
	if rawErr != nil || count == 0 {
		return rawErr
	}
	if err := argocd.ExtractSyncOperations(implcontext.NewStandaloneSubTaskContext(ctx, res, argocd.ExtractSyncOperationsMeta.Name, data, "argocd", nil)); err != nil {
		return err
	}
	if err := argoCommitOrigin(res.GetDal(), options.ConnectionId, options.ApplicationName); err != nil {
		return err
	}
	return argocd.ConvertSyncOperations(implcontext.NewStandaloneSubTaskContext(ctx, res, argocd.ConvertSyncOperationsMeta.Name, data, "argocd", nil))
}

func argoCommitOrigin(database dal.Dal, connection uint64, application string) error {
	missing, err := database.Count(dal.From("_tool_argocd_sync_operations"),
		dal.Where("connection_id = ? AND application_name = ? AND coalesce(revision, '') <> '' AND coalesce(repo_url, '') = ''", connection, application))
	if err != nil || missing != 0 {
		return fmt.Errorf("selected Argo CD revision lacks a native repository")
	}
	return nil
}

func PagerDutyService(ctx context.Context, res corecontext.BasicRes, options pagerduty.PagerDutyOptions) error {
	if err := ready(ctx, res, "pagerduty", api.NewRegexEnricher()); err != nil {
		return err
	}
	if err := pagerduty.ValidateTaskOptions(&options); err != nil {
		return err
	}
	if !res.GetDal().HasTable("_tool_pagerduty_services") || !res.GetDal().HasTable("_raw_"+pagerduty.RAW_INCIDENTS_TABLE) ||
		!res.GetDal().HasTable("_tool_pagerduty_incidents") || !res.GetDal().HasTable("_tool_pagerduty_assignments") || !res.GetDal().HasTable("_tool_pagerduty_users") {
		return fmt.Errorf("PagerDuty service tool table is unavailable")
	}
	if err := selectedScope(res, "_tool_pagerduty_services", "id", options.ServiceId, options.ConnectionId); err != nil {
		return err
	}
	data := &pagerduty.PagerDutyTaskData{Options: &options}
	if err := pagerduty.ConvertServices(implcontext.NewStandaloneSubTaskContext(ctx, res, pagerduty.ConvertServicesMeta.Name, data, "pagerduty", nil)); err != nil {
		return err
	}
	count, err := rawCount(res, pagerduty.RAW_INCIDENTS_TABLE, options.GetParams())
	if err != nil || count == 0 {
		return err
	}
	if err := pagerduty.ExtractIncidents(implcontext.NewStandaloneSubTaskContext(ctx, res, pagerduty.ExtractIncidentsMeta.Name, data, "pagerduty", nil)); err != nil {
		return err
	}
	return pagerduty.ConvertIncidents(implcontext.NewStandaloneSubTaskContext(ctx, res, pagerduty.ConvertIncidentsMeta.Name, data, "pagerduty", nil))
}

func LinearTeam(ctx context.Context, res corecontext.BasicRes, options linear.LinearOptions) error {
	if err := ready(ctx, res, "linear", api.NewRegexEnricher()); err != nil {
		return err
	}
	if options.ConnectionId == 0 || options.TeamId == "" || !res.GetDal().HasTable("_tool_linear_teams") || !res.GetDal().HasTable("_raw_"+linear.RAW_ISSUES_TABLE) ||
		!res.GetDal().HasTable("_tool_linear_issues") || !res.GetDal().HasTable("_tool_linear_issue_labels") || !res.GetDal().HasTable("_tool_linear_accounts") {
		return fmt.Errorf("selected Linear team or its native table is unavailable")
	}
	if err := selectedScope(res, "_tool_linear_teams", "team_id", options.TeamId, options.ConnectionId); err != nil {
		return err
	}
	data := &linear.LinearTaskData{Options: &options, ScopeConfig: &linearmodels.LinearScopeConfig{}}
	if err := linear.ConvertTeams(implcontext.NewStandaloneSubTaskContext(ctx, res, linear.ConvertTeamsMeta.Name, data, "linear", nil)); err != nil {
		return err
	}
	count, err := rawCount(res, linear.RAW_ISSUES_TABLE, linear.LinearApiParams{ConnectionId: options.ConnectionId, TeamId: options.TeamId})
	if err != nil || count == 0 {
		return err
	}
	if err := linear.ExtractIssues(implcontext.NewStandaloneSubTaskContext(ctx, res, linear.ExtractIssuesMeta.Name, data, "linear", nil)); err != nil {
		return err
	}
	return linear.ConvertIssues(implcontext.NewStandaloneSubTaskContext(ctx, res, linear.ConvertIssuesMeta.Name, data, "linear", nil))
}

func SlackChannel(ctx context.Context, res corecontext.BasicRes, options slack.SlackOptions) error {
	if err := ready(ctx, res, "slack", api.NewRegexEnricher()); err != nil {
		return err
	}
	if options.ConnectionId == 0 || options.ChannelId == "" ||
		!res.GetDal().HasTable("_raw_"+slack.RAW_CHANNEL_TABLE) || !res.GetDal().HasTable("_tool_slack_channels") ||
		!res.GetDal().HasTable("_raw_"+slack.RAW_CHANNEL_MESSAGE_TABLE) || !res.GetDal().HasTable("_tool_slack_channel_messages") {
		return fmt.Errorf("selected Slack channel or its native tables are unavailable")
	}
	data := &slack.SlackTaskData{Options: &options}
	if err := selectedRaw(res, slack.RAW_CHANNEL_TABLE, slack.SlackApiParams{ConnectionId: options.ConnectionId}, true); err != nil {
		return err
	}
	count, err := res.GetDal().Count(dal.From("_tool_slack_channels"), dal.Where("connection_id = ?", options.ConnectionId))
	if err != nil || count > 1 {
		return fmt.Errorf("Slack extraction requires only the selected channel: %v", err)
	}
	if err := slack.ExtractChannel(implcontext.NewStandaloneSubTaskContext(ctx, res, slack.ExtractChannelMeta.Name, data, "slack", nil)); err != nil {
		return err
	}
	if err := selectedScope(res, "_tool_slack_channels", "id", options.ChannelId, options.ConnectionId); err != nil {
		return err
	}
	count, err = res.GetDal().Count(dal.From("_tool_slack_channels"), dal.Where("connection_id = ?", options.ConnectionId))
	if err != nil || count != 1 {
		return fmt.Errorf("Slack extraction requires only the selected channel: %v", err)
	}
	messageParams := plugin.MarshalScopeParams(options.GetParams())
	count, rawErr := rawCount(res, slack.RAW_CHANNEL_MESSAGE_TABLE, options.GetParams())
	if rawErr != nil || count == 0 {
		return rawErr
	}
	foreign, err := res.GetDal().Count(dal.From("_raw_"+slack.RAW_CHANNEL_MESSAGE_TABLE), dal.Where("params = ? AND (input->>'channel_id') IS DISTINCT FROM ?", messageParams, options.ChannelId))
	if err != nil || foreign != 0 {
		return fmt.Errorf("Slack raw messages include an unselected channel: %v", err)
	}
	return slack.ExtractChannelMessage(implcontext.NewStandaloneSubTaskContext(ctx, res, slack.ExtractChannelMessageMeta.Name, data, "slack", nil))
}

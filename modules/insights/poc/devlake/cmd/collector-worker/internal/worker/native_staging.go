package worker

import (
	"context"
	"database/sql"
	"encoding/json"
	"strconv"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models/common"
	"github.com/apache/devlake/core/plugin"
	argocd "github.com/apache/devlake/plugins/argocd/models"
	gitlab "github.com/apache/devlake/plugins/gitlab/models"
	jenkins "github.com/apache/devlake/plugins/jenkins/models"
	linear "github.com/apache/devlake/plugins/linear/models"
	pagerduty "github.com/apache/devlake/plugins/pagerduty/models"
	slack "github.com/apache/devlake/plugins/slack/tasks"
	sonarqube "github.com/apache/devlake/plugins/sonarqube/models"
)

type stagedNativeRow struct {
	table  string
	params string
	input  []byte
	record collection.NativeRecord
}

func stageSpec(source collection.Source, connectionID uint64, record collection.NativeRecord) (stagedNativeRow, error) {
	key := record.Key
	if connectionID == 0 || key.CellID != source.CellID || key.TenantID != source.TenantID ||
		key.ProjectID != source.ProjectID || key.Tool != source.Tool || key.ConnectionID != source.ConnectionID ||
		key.Scope != source.Scope || key.Kind == "" || key.NativeID == "" || record.Revision == "" ||
		len(record.Payload) == 0 || len(record.Payload) > 8*1024*1024 || !json.Valid(record.Payload) {
		return stagedNativeRow{}, collection.ErrIncomplete
	}
	var payload map[string]json.RawMessage
	if json.Unmarshal(record.Payload, &payload) != nil || payload == nil {
		return stagedNativeRow{}, collection.ErrIncomplete
	}
	result := stagedNativeRow{record: record, input: []byte(`{}`)}
	var params any
	switch source.Tool + ":" + key.Kind {
	case "sonarqube:project":
		result.table = "_raw_sonarqube_api_projects"
	case "sonarqube:issue":
		result.table = "_raw_sonarqube_api_issues"
	case "jenkins:job":
		result.table = "_raw_jenkins_api_jobs"
	case "jenkins:build":
		result.table = "_raw_jenkins_api_builds"
	case "argocd:application":
		result.table = "_raw_argocd_api_applications"
	case "argocd:sync_operation":
		result.table = "_raw_argocd_api_sync_operations"
	case "gitlab:pipeline":
		result.table = "_raw_gitlab_api_pipeline_details"
	case "pagerduty:service":
		result.table = "_raw_pagerduty_services"
	case "pagerduty:incident":
		result.table = "_raw_pagerduty_incidents"
	case "linear:team":
		result.table = "_raw_linear_teams"
	case "linear:issue":
		result.table = "_raw_linear_issues"
	case "slack:channel":
		result.table = "_raw_slack_channel"
	case "slack:channel_message":
		result.table = "_raw_slack_channel_message"
		result.input, _ = json.Marshal(struct {
			ChannelID string `json:"channel_id"`
		}{source.Scope})
	default:
		return stagedNativeRow{}, collection.ErrIncomplete
	}
	switch source.Tool {
	case "sonarqube":
		params = sonarqube.SonarqubeApiParams{ConnectionId: connectionID, ProjectKey: source.Scope}
	case "jenkins":
		params = jenkins.JenkinsApiParams{ConnectionId: connectionID, FullName: source.Scope}
	case "argocd":
		params = argocd.ArgocdApiParams{ConnectionId: connectionID, Name: source.Scope}
	case "gitlab":
		projectID, err := strconv.Atoi(source.Scope)
		if err != nil || projectID < 1 || strconv.Itoa(projectID) != source.Scope {
			return stagedNativeRow{}, collection.ErrIncomplete
		}
		params = gitlab.GitlabApiParams{ConnectionId: connectionID, ProjectId: projectID}
	case "pagerduty":
		params = pagerduty.PagerDutyParams{ConnectionId: connectionID, ScopeId: source.Scope}
	case "linear":
		params = linear.LinearApiParams{ConnectionId: connectionID, TeamId: source.Scope}
	case "slack":
		if key.Kind == "channel" {
			params = slack.SlackApiParams{ConnectionId: connectionID}
		} else {
			params = slack.SlackParams{ConnectionId: connectionID, ScopeId: source.Scope}
		}
	}
	result.params = plugin.MarshalScopeParams(params)
	if len(result.params) > 255 {
		return stagedNativeRow{}, collection.ErrIncomplete
	}
	return result, nil
}

func stageNativeRows(ctx context.Context, writer *sql.DB, source collection.Source, generation int64,
	authority collection.Authority, records []collection.NativeRecord) (map[collection.NativeKey]common.RawDataOrigin, uint64, error) {
	return stageAndProcessNativeRows(ctx, writer, source, generation, authority, records, nil)
}

func stageAndProcessNativeRows(ctx context.Context, writer *sql.DB, source collection.Source, generation int64,
	authority collection.Authority, records []collection.NativeRecord,
	process func(context.Context, *sql.Tx, uint64, map[collection.NativeKey]common.RawDataOrigin) error,
) (map[collection.NativeKey]common.RawDataOrigin, uint64, error) {
	if writer == nil || generation < 1 || len(records) == 0 || len(records) > 1000 {
		return nil, 0, collection.ErrIncomplete
	}
	var origins map[collection.NativeKey]common.RawDataOrigin
	var nativeID uint64
	err := collection.WriteFencedWorkspace(ctx, writer, source, generation, authority, func(tx *sql.Tx) error {
		var err error
		nativeID, err = collection.ResolveNativeConnection(ctx, tx, source, generation)
		if err != nil {
			return err
		}
		staged := make([]stagedNativeRow, 0, len(records))
		seen := make(map[collection.NativeKey]bool, len(records))
		tables := make(map[string]bool)
		for _, record := range records {
			row, err := stageSpec(source, nativeID, record)
			if err != nil || seen[record.Key] {
				return collection.ErrIncomplete
			}
			seen[record.Key] = true
			staged = append(staged, row)
			if !tables[row.table] {
				if _, err := tx.ExecContext(ctx, "DELETE FROM public."+row.table+" WHERE params = $1", row.params); err != nil {
					return err
				}
				tables[row.table] = true
			}
		}
		origins = make(map[collection.NativeKey]common.RawDataOrigin, len(staged))
		for _, row := range staged {
			var rawID uint64
			if err := tx.QueryRowContext(ctx, "INSERT INTO public."+row.table+" (params, data, input, created_at) VALUES ($1, $2, $3::json, now()) RETURNING id",
				row.params, []byte(row.record.Payload), row.input).Scan(&rawID); err != nil || rawID == 0 {
				return collection.ErrIncomplete
			}
			origins[row.record.Key] = common.RawDataOrigin{RawDataTable: row.table, RawDataParams: row.params, RawDataId: rawID}
		}
		if process != nil {
			return process(ctx, tx, nativeID, origins)
		}
		return nil
	})
	if err != nil {
		return nil, 0, err
	}
	return origins, nativeID, nil
}

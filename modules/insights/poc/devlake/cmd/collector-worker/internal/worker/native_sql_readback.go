package worker

import (
	"context"
	"database/sql"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models/common"
	"github.com/apache/devlake/core/models/domainlayer/codequality"
	"github.com/apache/devlake/core/models/domainlayer/devops"
	"github.com/apache/devlake/core/models/domainlayer/ticket"
	slackmodels "github.com/apache/devlake/plugins/slack/models"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

func appendNativeDomainRows[Row nativeDomainRow](ctx context.Context, database *gorm.DB, origin common.RawDataOrigin,
	table string, gathered *[]nativeDomainRow) error {
	var rows []Row
	result := database.WithContext(ctx).Table(table).Where("_raw_data_table = ? AND _raw_data_params = ? AND _raw_data_id = ?",
		origin.RawDataTable, origin.RawDataParams, origin.RawDataId).Limit(2001).Find(&rows)
	if result.Error != nil || len(rows) > 2000 || len(*gathered)+len(rows) > 2000 {
		return collection.ErrIncomplete
	}
	for _, row := range rows {
		*gathered = append(*gathered, row)
	}
	return nil
}

func readNativeDomainRows(ctx context.Context, tx *sql.Tx, source collection.Source, nativeID uint64,
	records []collection.NativeRecord, origins map[collection.NativeKey]common.RawDataOrigin,
	policyVersion string) ([]collection.BatchItem, error) {
	if tx == nil || len(records) == 0 || len(records) > 1000 || len(origins) != len(records) {
		return nil, collection.ErrIncomplete
	}
	database, err := gorm.Open(postgres.New(postgres.Config{Conn: tx}), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		return nil, collection.ErrIncomplete
	}
	var gathered []nativeDomainRow
	var messages []*slackmodels.SlackChannelMessage
	for _, record := range records {
		origin, exists := origins[record.Key]
		if !exists {
			return nil, collection.ErrIncomplete
		}
		switch source.Tool + ":" + record.Key.Kind {
		case "sonarqube:issue":
			err = appendNativeDomainRows[*codequality.CqIssue](ctx, database, origin, "cq_issues", &gathered)
		case "jenkins:build":
			err = appendNativeDomainRows[*devops.CICDPipeline](ctx, database, origin, "cicd_pipelines", &gathered)
			if err == nil {
				err = appendNativeDomainRows[*devops.CICDTask](ctx, database, origin, "cicd_tasks", &gathered)
			}
		case "gitlab:pipeline":
			err = appendNativeDomainRows[*devops.CICDPipeline](ctx, database, origin, "cicd_pipelines", &gathered)
		case "argocd:application":
			err = appendNativeDomainRows[*devops.CicdScope](ctx, database, origin, "cicd_scopes", &gathered)
		case "argocd:sync_operation":
			err = appendNativeDomainRows[*devops.CICDDeployment](ctx, database, origin, "cicd_deployments", &gathered)
		case "pagerduty:service", "linear:team":
			err = appendNativeDomainRows[*ticket.Board](ctx, database, origin, "boards", &gathered)
		case "pagerduty:incident", "linear:issue":
			err = appendNativeDomainRows[*ticket.Issue](ctx, database, origin, "issues", &gathered)
		case "slack:channel_message":
			var current []*slackmodels.SlackChannelMessage
			result := database.WithContext(ctx).Table("_tool_slack_channel_messages").Where("_raw_data_table = ? AND _raw_data_params = ? AND _raw_data_id = ?",
				origin.RawDataTable, origin.RawDataParams, origin.RawDataId).Limit(2001).Find(&current)
			if result.Error != nil || len(current) > 2000 || len(messages)+len(current) > 2000 {
				return nil, collection.ErrIncomplete
			}
			messages = append(messages, current...)
		case "sonarqube:project", "jenkins:job", "slack:channel":
		default:
			return nil, collection.ErrIncomplete
		}
		if err != nil {
			return nil, err
		}
	}
	if source.Tool != "slack" {
		return nativeItemsForOrigins(source, nativeID, records, origins, gathered, policyVersion)
	}
	if len(messages) == 0 {
		return nil, collection.ErrIncomplete
	}
	items := make([]collection.BatchItem, 0, len(messages))
	seen := make(map[collection.NativeKey]bool)
	for _, row := range messages {
		if row == nil || row.GetRawDataOrigin() == nil {
			return nil, collection.ErrIncomplete
		}
		origin := row.GetRawDataOrigin()
		var matched *collection.NativeRecord
		for index := range records {
			candidate := origins[records[index].Key]
			if candidate.RawDataId == origin.RawDataId && candidate.RawDataParams == origin.RawDataParams && candidate.RawDataTable == origin.RawDataTable {
				if matched != nil {
					return nil, collection.ErrIncomplete
				}
				matched = &records[index]
			}
		}
		if matched == nil {
			return nil, collection.ErrIncomplete
		}
		item, err := collection.NativeSlackMessageBatchItem(source, nativeID, matched.Revision, row, matched.Payload,
			policyVersion, "devlake-pinned", "schema-v2")
		if err != nil || seen[item.Key] {
			return nil, collection.ErrIncomplete
		}
		seen[item.Key] = true
		items = append(items, item)
	}
	return items, nil
}

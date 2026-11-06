package worker

import (
	"context"
	"database/sql"
	"encoding/json"
	"strconv"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models/common"
)

func seedNativeScope(ctx context.Context, tx *sql.Tx, source collection.Source, connectionID uint64,
	records []collection.NativeRecord, origins map[collection.NativeKey]common.RawDataOrigin) error {
	var kind, table, field, nativeField string
	var nativeFields [][2]string
	switch source.Tool {
	case "sonarqube":
		kind, table, field, nativeField = "project", "_tool_sonarqube_projects", "project_key", "key"
		nativeFields = [][2]string{{"qualifier", "qualifier"}, {"visibility", "visibility"}}
	case "pagerduty":
		kind, table, field, nativeField = "service", "_tool_pagerduty_services", "id", "id"
		nativeFields = [][2]string{{"url", "html_url"}}
	case "linear":
		kind, table, field, nativeField = "team", "_tool_linear_teams", "team_id", "id"
		nativeFields = [][2]string{{"key", "key"}, {"description", "description"}}
	default:
		return nil
	}
	if tx == nil || connectionID == 0 {
		return collection.ErrIncomplete
	}
	var chosen *collection.NativeRecord
	for index := range records {
		if records[index].Key.Kind == kind {
			if chosen != nil {
				return collection.ErrIncomplete
			}
			chosen = &records[index]
		}
	}
	if chosen == nil || chosen.Key.NativeID != source.Scope {
		return collection.ErrIncomplete
	}
	origin, exists := origins[chosen.Key]
	if !exists || origin.RawDataId == 0 {
		return collection.ErrIncomplete
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(chosen.Payload, &fields) != nil {
		return collection.ErrIncomplete
	}
	var identity, name string
	if json.Unmarshal(fields[nativeField], &identity) != nil || identity != source.Scope ||
		json.Unmarshal(fields["name"], &name) != nil || name == "" {
		return collection.ErrIncomplete
	}
	columns, placeholders := "", ""
	updates := ""
	values := []any{connectionID, identity, name, origin.RawDataTable, origin.RawDataParams, origin.RawDataId}
	for _, extra := range nativeFields {
		var value *string
		if raw, exists := fields[extra[1]]; exists && json.Unmarshal(raw, &value) != nil {
			return collection.ErrIncomplete
		}
		columns += ", " + extra[0]
		placeholders += ", $" + strconv.Itoa(len(values)+1)
		updates += ", " + extra[0] + " = EXCLUDED." + extra[0]
		values = append(values, value)
	}
	query := "INSERT INTO public." + table + " (connection_id, " + field + ", name, _raw_data_table, _raw_data_params, _raw_data_id" + columns + ") " +
		"VALUES ($1, $2, $3, $4, $5, $6" + placeholders + ") ON CONFLICT (connection_id, " + field + ") DO UPDATE SET " +
		"name = EXCLUDED.name, _raw_data_table = EXCLUDED._raw_data_table, " +
		"_raw_data_params = EXCLUDED._raw_data_params, _raw_data_id = EXCLUDED._raw_data_id" + updates
	if _, err := tx.ExecContext(ctx, query, values...); err != nil {
		return collection.ErrIncomplete
	}
	return nil
}

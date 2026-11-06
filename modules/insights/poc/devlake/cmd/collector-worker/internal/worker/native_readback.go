package worker

import (
	"encoding/json"
	"reflect"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models/common"
)

type nativeDomainRow interface {
	GetRawDataOrigin() *common.RawDataOrigin
	TableName() string
}

type nativeOriginKey struct {
	table  string
	params string
	id     uint64
}

func nativeItemsForOrigins(source collection.Source, nativeID uint64, records []collection.NativeRecord,
	origins map[collection.NativeKey]common.RawDataOrigin, rows []nativeDomainRow,
	policyVersion string) ([]collection.BatchItem, error) {
	if nativeID == 0 || len(records) == 0 || len(records) > 1000 || len(origins) != len(records) || len(rows) == 0 || len(rows) > 2000 {
		return nil, collection.ErrIncomplete
	}
	staged := make(map[nativeOriginKey]collection.NativeRecord, len(records))
	for _, record := range records {
		origin, exists := origins[record.Key]
		identity := nativeOriginKey{origin.RawDataTable, origin.RawDataParams, origin.RawDataId}
		if !exists || origin.RawDataId == 0 || origin.RawDataTable == "" || origin.RawDataParams == "" ||
			record.Key.CellID != source.CellID || record.Key.TenantID != source.TenantID ||
			record.Key.ProjectID != source.ProjectID || record.Key.Tool != source.Tool ||
			record.Key.ConnectionID != source.ConnectionID || record.Key.Scope != source.Scope ||
			record.Revision == "" || len(record.Payload) == 0 || !json.Valid(record.Payload) {
			return nil, collection.ErrIncomplete
		}
		expected, err := stageSpec(source, nativeID, record)
		if err != nil || expected.table != origin.RawDataTable || expected.params != origin.RawDataParams {
			return nil, collection.ErrIncomplete
		}
		if _, duplicate := staged[identity]; duplicate {
			return nil, collection.ErrIncomplete
		}
		staged[identity] = record
	}
	items := make([]collection.BatchItem, 0, len(rows))
	seen := make(map[collection.NativeKey]bool, len(rows))
	for _, row := range rows {
		if row == nil || reflect.ValueOf(row).Kind() == reflect.Ptr && reflect.ValueOf(row).IsNil() || row.GetRawDataOrigin() == nil {
			return nil, collection.ErrIncomplete
		}
		origin := row.GetRawDataOrigin()
		record, exists := staged[nativeOriginKey{origin.RawDataTable, origin.RawDataParams, origin.RawDataId}]
		if !exists {
			return nil, collection.ErrIncomplete
		}
		item, err := collection.NativeDomainBatchItem(source, nativeID, record.Revision, row,
			record.Payload, policyVersion, "devlake-pinned", "schema-v2")
		if err != nil || seen[item.Key] {
			return nil, collection.ErrIncomplete
		}
		seen[item.Key] = true
		items = append(items, item)
	}
	return items, nil
}

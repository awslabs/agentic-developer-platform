package worker

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models/common"
)

func collectAndProcessNative(ctx context.Context, gateway *Gateway, writer *sql.DB, source collection.Source,
	job collection.Job, generation int64) (collection.Reconciled, map[collection.NativeKey]collection.BatchItem, error) {
	raw, _, err := collect(ctx, gateway, writer, source, job, generation)
	if err != nil {
		return collection.Reconciled{}, nil, err
	}
	var converted []collection.BatchItem
	_, _, err = stageAndProcessNativeRows(ctx, writer, source, generation, gateway, raw.Allowed,
		func(ctx context.Context, transaction *sql.Tx, nativeID uint64, origins map[collection.NativeKey]common.RawDataOrigin) error {
			if err := seedNativeScope(ctx, transaction, source, nativeID, raw.Allowed, origins); err != nil {
				return err
			}
			if err := invokeNativeTasks(ctx, transaction, source, nativeID); err != nil {
				return err
			}
			var err error
			converted, err = readNativeDomainRows(ctx, transaction, source, nativeID, raw.Allowed, origins,
				fmt.Sprint(gateway.Contexts[source].PolicyRevision))
			return err
		})
	if err != nil {
		return collection.Reconciled{}, nil, err
	}
	result := collection.Reconciled{Allowed: make([]collection.NativeRecord, 0, len(converted))}
	items := make(map[collection.NativeKey]collection.BatchItem, len(converted))
	for _, item := range converted {
		if _, exists := items[item.Key]; exists {
			return collection.Reconciled{}, nil, collection.ErrIncomplete
		}
		items[item.Key] = item
		result.Allowed = append(result.Allowed, collection.NativeRecord{Key: item.Key, Revision: item.Revision, Payload: item.NativeBytes})
	}
	return result, items, nil
}

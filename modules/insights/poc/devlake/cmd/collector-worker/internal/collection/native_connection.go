package collection

import (
	"context"
	"database/sql"
)

type NativeConnectionQuerier interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func ResolveNativeConnection(ctx context.Context, db NativeConnectionQuerier, source Source, generation int64) (uint64, error) {
	if err := CheckFlags(); err != nil {
		return 0, err
	}
	if db == nil || !expandedEvidenceTool(source.Tool) || source.CellID == "" ||
		source.TenantID == "" || source.ProjectID == "" || source.ConnectionID == "" || generation < 1 {
		return 0, ErrUnauthorized
	}
	var assigned int64
	err := db.QueryRowContext(ctx,
		"SELECT devlake_meta.native_connection($1, $2, $3, $4, $5, $6)",
		source.CellID, source.TenantID, source.ProjectID, source.Tool, source.ConnectionID, generation).Scan(&assigned)
	if err != nil || assigned < 1 {
		return 0, ErrUnauthorized
	}
	return uint64(assigned), nil
}

package collection

import (
	"context"
	"database/sql"
)

func ReadSourceCheckpoint(ctx context.Context, db *sql.DB, source Source) (*string, error) {
	if err := CheckFlags(); err != nil {
		return nil, err
	}
	if db == nil || source.CellID == "" || source.TenantID == "" || source.ProjectID == "" ||
		source.ConnectionID == "" || source.Scope == "" ||
		!supportedEvidenceTool(source.Tool) {
		return nil, ErrIncomplete
	}
	var cursor string
	err := db.QueryRowContext(ctx,
		"SELECT cursor_value FROM devlake_meta.collection_checkpoints WHERE cell_id = $1 AND source_key = $2",
		source.CellID, SourceCheckpointKey(source)).Scan(&cursor)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil || cursor == "" {
		return nil, ErrIncomplete
	}
	return &cursor, nil
}

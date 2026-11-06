package collection

import (
	"context"
	"database/sql"
	"time"
)

type SQLGenerationReader struct {
	DB *sql.DB
}

func (reader SQLGenerationReader) CurrentGeneration(ctx context.Context, cellID string) (int64, string, error) {
	if reader.DB == nil || cellID == "" {
		return 0, "", ErrGenerationFenced
	}
	var generation int64
	var state string
	if err := reader.DB.QueryRowContext(ctx,
		"SELECT generation, state FROM devlake_meta.collection_generations WHERE cell_id = $1",
		cellID).Scan(&generation, &state); err != nil {
		if err == sql.ErrNoRows {
			return 0, "", nil
		}
		return 0, "", ErrGenerationFenced
	}
	return generation, state, nil
}

func StartGeneration(ctx context.Context, db *sql.DB, job Job, authority Authority, migration Migration,
	manifestDigest string, now time.Time, expectedGeneration int64, operationID, attemptID string) (int64, error) {
	if db == nil || operationID == "" || attemptID == "" || expectedGeneration < 0 {
		return 0, ErrGenerationFenced
	}
	if err := Admit(ctx, job, authority, migration, manifestDigest, now); err != nil {
		return 0, err
	}
	first := job.Sources[0]
	var generation int64
	if err := db.QueryRowContext(ctx, "SELECT devlake_meta.begin_collection($1, $2, $3, $4, $5, $6)",
		first.CellID, expectedGeneration, first.TenantID, first.ProjectID, operationID, attemptID).Scan(&generation); err != nil || generation != expectedGeneration+1 {
		return 0, ErrGenerationFenced
	}
	for _, source := range job.Sources {
		if err := currentSource(ctx, source, authority); err != nil {
			_, _ = db.ExecContext(ctx, "SELECT devlake_meta.cancel_collection($1, $2)", first.CellID, generation)
			return 0, err
		}
	}
	return generation, nil
}

package collection

import (
	"context"
	"database/sql"
)

// SQLMigration checks the migration receipt in the registered cell's pool.
type SQLMigration struct {
	DB         *sql.DB
	ResourceID string
}

func (migration SQLMigration) CheckReceipt(ctx context.Context, cellID, manifestDigest string) error {
	if migration.DB == nil || migration.ResourceID == "" || cellID == "" || manifestDigest == "" {
		return ErrMigrationRequired
	}
	var digest string
	err := migration.DB.QueryRowContext(ctx,
		`SELECT manifest_sha256 FROM devlake_meta.schema_receipts
		 WHERE cell_id = $1 AND db_resource_id = $2 AND schema_version = 2 AND status = 'applied'`,
		cellID, migration.ResourceID).Scan(&digest)
	if err != nil || digest != manifestDigest {
		return ErrMigrationRequired
	}
	return nil
}

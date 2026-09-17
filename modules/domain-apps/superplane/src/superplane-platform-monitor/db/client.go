package db

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Client provides database operations for the platform monitor.
type Client struct {
	pool *pgxpool.Pool
}

// NewClient creates a new database client from a connection string.
func NewClient(ctx context.Context, connStr string) (*Client, error) {
	pool, err := pgxpool.New(ctx, connStr)
	if err != nil {
		return nil, fmt.Errorf("create connection pool: %w", err)
	}

	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping database: %w", err)
	}

	return &Client{pool: pool}, nil
}

// Close closes the database connection pool.
func (c *Client) Close() {
	c.pool.Close()
}

// ListActiveClusters returns all clusters that need health monitoring.
// Active clusters are those with status Active, Running, or Provisioning.
func (c *Client) ListActiveClusters(ctx context.Context) ([]Cluster, error) {
	rows, err := c.pool.Query(ctx, `
		SELECT id, org_id, workspace_id, name, status, health_status,
		       last_heartbeat, reconcile_at, last_reconciled_at,
		       eks_cluster_arn, endpoint, actual_state_json
		FROM clusters
		WHERE status IN ('Active', 'Running', 'Provisioning', 'Pending')
		ORDER BY last_heartbeat ASC NULLS FIRST
	`)
	if err != nil {
		return nil, fmt.Errorf("query clusters: %w", err)
	}
	defer rows.Close()

	var clusters []Cluster
	for rows.Next() {
		var cl Cluster
		if err := rows.Scan(
			&cl.ID, &cl.OrgID, &cl.WorkspaceID, &cl.Name, &cl.Status,
			&cl.HealthStatus, &cl.LastHeartbeat, &cl.ReconcileAt,
			&cl.LastReconciledAt, &cl.EKSClusterARN, &cl.Endpoint,
			&cl.ActualStateJSON,
		); err != nil {
			return nil, fmt.Errorf("scan cluster row: %w", err)
		}
		clusters = append(clusters, cl)
	}

	return clusters, rows.Err()
}

// UpdateClusterHealth updates the health_status and last_reconciled_at for a cluster.
func (c *Client) UpdateClusterHealth(ctx context.Context, clusterID string, healthStatus string, details json.RawMessage) error {
	_, err := c.pool.Exec(ctx, `
		UPDATE clusters
		SET health_status = $1,
		    last_reconciled_at = NOW(),
		    actual_state_json = COALESCE($3, actual_state_json),
		    updated_at = NOW()
		WHERE id = $2
	`, healthStatus, clusterID, details)
	if err != nil {
		return fmt.Errorf("update cluster health: %w", err)
	}
	return nil
}

// InsertEvent creates an event record for health transitions.
func (c *Client) InsertEvent(ctx context.Context, orgID, resourceID, eventType, message string, details json.RawMessage) error {
	_, err := c.pool.Exec(ctx, `
		INSERT INTO events (org_id, resource_type, resource_id, event_type, message, details_json, created_at)
		VALUES ($1, 'cluster', $2, $3, $4, $5, NOW())
	`, orgID, resourceID, eventType, message, details)
	if err != nil {
		return fmt.Errorf("insert event: %w", err)
	}
	return nil
}

// AcquireLock attempts to acquire a distributed monitor lock.
// Returns true if the lock was acquired, false if another monitor instance holds it.
func (c *Client) AcquireLock(ctx context.Context, resourceType, resourceID, lockedBy string, ttl time.Duration) (bool, error) {
	// Try to insert a new lock or take over an expired one.
	tag, err := c.pool.Exec(ctx, `
		INSERT INTO reconcile_locks (resource_type, resource_id, locked_by, locked_at, expires_at)
		VALUES ($1, $2, $3, NOW(), NOW() + $4::interval)
		ON CONFLICT (resource_type, resource_id)
		DO UPDATE SET locked_by = $3, locked_at = NOW(), expires_at = NOW() + $4::interval
		WHERE reconcile_locks.expires_at < NOW()
		   OR reconcile_locks.locked_by = $3
	`, resourceType, resourceID, lockedBy, fmt.Sprintf("%d seconds", int(ttl.Seconds())))
	if err != nil {
		return false, fmt.Errorf("acquire lock: %w", err)
	}
	return tag.RowsAffected() > 0, nil
}

// ReleaseLock releases a monitor lock held by this instance.
func (c *Client) ReleaseLock(ctx context.Context, resourceType, resourceID, lockedBy string) error {
	_, err := c.pool.Exec(ctx, `
		DELETE FROM reconcile_locks
		WHERE resource_type = $1 AND resource_id = $2 AND locked_by = $3
	`, resourceType, resourceID, lockedBy)
	if err != nil {
		return fmt.Errorf("release lock: %w", err)
	}
	return nil
}

// GetCostHistory returns recent hourly cost values for rolling average computation.
func (c *Client) GetCostHistory(ctx context.Context, clusterID string, window time.Duration) ([]float64, error) {
	rows, err := c.pool.Query(ctx, `
		SELECT COALESCE((details_json->>'cost_hourly')::float, 0)
		FROM events
		WHERE resource_type = 'cluster'
		  AND resource_id = $1
		  AND event_type = 'heartbeat_received'
		  AND created_at > NOW() - $2::interval
		ORDER BY created_at DESC
	`, clusterID, fmt.Sprintf("%d seconds", int(window.Seconds())))
	if err != nil {
		return nil, fmt.Errorf("query cost history: %w", err)
	}
	defer rows.Close()

	var costs []float64
	for rows.Next() {
		var cost float64
		if err := rows.Scan(&cost); err != nil {
			return nil, fmt.Errorf("scan cost: %w", err)
		}
		costs = append(costs, cost)
	}

	return costs, rows.Err()
}

// Ping checks database connectivity.
func (c *Client) Ping(ctx context.Context) error {
	return c.pool.Ping(ctx)
}

// Querier is an interface for database operations, enabling test mocking.
type Querier interface {
	ListActiveClusters(ctx context.Context) ([]Cluster, error)
	UpdateClusterHealth(ctx context.Context, clusterID string, healthStatus string, details json.RawMessage) error
	InsertEvent(ctx context.Context, orgID, resourceID, eventType, message string, details json.RawMessage) error
	AcquireLock(ctx context.Context, resourceType, resourceID, lockedBy string, ttl time.Duration) (bool, error)
	ReleaseLock(ctx context.Context, resourceType, resourceID, lockedBy string) error
	GetCostHistory(ctx context.Context, clusterID string, window time.Duration) ([]float64, error)
	Ping(ctx context.Context) error
}

// Verify Client implements Querier.
var _ Querier = (*Client)(nil)

// Tx wraps a pgx transaction for atomic operations.
type Tx struct {
	tx pgx.Tx
}

// NewTx begins a new transaction.
func (c *Client) NewTx(ctx context.Context) (*Tx, error) {
	tx, err := c.pool.Begin(ctx)
	if err != nil {
		return nil, fmt.Errorf("begin transaction: %w", err)
	}
	return &Tx{tx: tx}, nil
}

// Commit commits the transaction.
func (t *Tx) Commit(ctx context.Context) error {
	return t.tx.Commit(ctx)
}

// Rollback rolls back the transaction.
func (t *Tx) Rollback(ctx context.Context) error {
	return t.tx.Rollback(ctx)
}

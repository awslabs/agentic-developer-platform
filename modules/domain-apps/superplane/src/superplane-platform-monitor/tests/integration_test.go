//go:build integration

// Package tests contains integration tests for the platform monitor.
//
// These tests require a real PostgreSQL database. Set the INTEGRATION_DATABASE_URL
// environment variable to a valid connection string before running:
//
//	INTEGRATION_DATABASE_URL="postgres://user:pass@localhost:5432/superplane_test?sslmode=disable" \
//	  go test -tags=integration -v ./tests/...
package tests

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/config"
	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/db"
	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/monitors"

	"go.uber.org/zap"
)

// testDBURL returns the integration test database URL or skips the test.
func testDBURL(t *testing.T) string {
	t.Helper()
	url := os.Getenv("INTEGRATION_DATABASE_URL")
	if url == "" {
		t.Skip("INTEGRATION_DATABASE_URL not set — skipping integration test")
	}
	return url
}

// setupSchema creates the required tables for integration tests.
// This mirrors the Aurora PostgreSQL schema used in production.
func setupSchema(ctx context.Context, pool *pgxpool.Pool) error {
	schema := `
		-- Drop tables if they exist (clean slate for each test run).
		DROP TABLE IF EXISTS reconcile_locks CASCADE;
		DROP TABLE IF EXISTS events CASCADE;
		DROP TABLE IF EXISTS clusters CASCADE;
		DROP TABLE IF EXISTS workspaces CASCADE;
		DROP TABLE IF EXISTS organizations CASCADE;

		CREATE TABLE IF NOT EXISTS organizations (
			id TEXT PRIMARY KEY,
			name TEXT NOT NULL,
			created_at TIMESTAMPTZ DEFAULT NOW(),
			updated_at TIMESTAMPTZ DEFAULT NOW()
		);

		CREATE TABLE IF NOT EXISTS workspaces (
			id TEXT PRIMARY KEY,
			org_id TEXT NOT NULL REFERENCES organizations(id),
			name TEXT NOT NULL,
			status TEXT NOT NULL DEFAULT 'Active',
			created_at TIMESTAMPTZ DEFAULT NOW(),
			updated_at TIMESTAMPTZ DEFAULT NOW()
		);

		CREATE TABLE IF NOT EXISTS clusters (
			id TEXT PRIMARY KEY,
			org_id TEXT NOT NULL,
			workspace_id TEXT,
			name TEXT NOT NULL,
			status TEXT NOT NULL DEFAULT 'Pending',
			health_status TEXT,
			last_heartbeat TIMESTAMPTZ,
			reconcile_at TIMESTAMPTZ,
			last_reconciled_at TIMESTAMPTZ,
			eks_cluster_arn TEXT,
			endpoint TEXT,
			actual_state_json JSONB,
			created_at TIMESTAMPTZ DEFAULT NOW(),
			updated_at TIMESTAMPTZ DEFAULT NOW()
		);

		CREATE TABLE IF NOT EXISTS events (
			id BIGSERIAL PRIMARY KEY,
			org_id TEXT NOT NULL,
			resource_type TEXT NOT NULL,
			resource_id TEXT NOT NULL,
			event_type TEXT NOT NULL,
			message TEXT,
			details_json JSONB,
			created_at TIMESTAMPTZ DEFAULT NOW()
		);

		CREATE TABLE IF NOT EXISTS reconcile_locks (
			resource_type TEXT NOT NULL,
			resource_id TEXT NOT NULL,
			locked_by TEXT NOT NULL,
			locked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
			expires_at TIMESTAMPTZ NOT NULL,
			PRIMARY KEY (resource_type, resource_id)
		);
	`
	_, err := pool.Exec(ctx, schema)
	return err
}

// seedCluster inserts a test cluster with the given parameters.
func seedCluster(ctx context.Context, pool *pgxpool.Pool, id, orgID, name, status string, healthStatus *string, lastHeartbeat *time.Time, stateJSON json.RawMessage) error {
	_, err := pool.Exec(ctx, `
		INSERT INTO organizations (id, name) VALUES ($1, 'Test Org')
		ON CONFLICT (id) DO NOTHING
	`, orgID)
	if err != nil {
		return fmt.Errorf("seed org: %w", err)
	}

	_, err = pool.Exec(ctx, `
		INSERT INTO clusters (id, org_id, name, status, health_status, last_heartbeat, actual_state_json)
		VALUES ($1, $2, $3, $4, $5, $6, $7)
	`, id, orgID, name, status, healthStatus, lastHeartbeat, stateJSON)
	if err != nil {
		return fmt.Errorf("seed cluster: %w", err)
	}
	return nil
}

// getClusterHealth reads the health_status of a cluster from the database.
func getClusterHealth(ctx context.Context, pool *pgxpool.Pool, clusterID string) (string, error) {
	var health *string
	err := pool.QueryRow(ctx, `SELECT health_status FROM clusters WHERE id = $1`, clusterID).Scan(&health)
	if err != nil {
		return "", err
	}
	if health == nil {
		return "", nil
	}
	return *health, nil
}

// countEvents returns the number of events matching the given type for a resource.
func countEvents(ctx context.Context, pool *pgxpool.Pool, resourceID, eventType string) (int, error) {
	var count int
	err := pool.QueryRow(ctx, `
		SELECT COUNT(*) FROM events
		WHERE resource_id = $1 AND event_type = $2
	`, resourceID, eventType).Scan(&count)
	return count, err
}

// countLocks returns the number of active (non-expired) locks.
func countLocks(ctx context.Context, pool *pgxpool.Pool) (int, error) {
	var count int
	err := pool.QueryRow(ctx, `SELECT COUNT(*) FROM reconcile_locks WHERE expires_at > NOW()`).Scan(&count)
	return count, err
}

// TestIntegration_HealthyCluster_FullCycle verifies that the reconciler correctly
// evaluates a healthy cluster and updates the database.
func TestIntegration_HealthyCluster_FullCycle(t *testing.T) {
	dbURL := testDBURL(t)
	ctx := context.Background()

	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		t.Fatalf("connect to test DB: %v", err)
	}
	defer pool.Close()

	if err := setupSchema(ctx, pool); err != nil {
		t.Fatalf("setup schema: %v", err)
	}

	// Seed a healthy cluster with a recent heartbeat.
	now := time.Now().UTC()
	heartbeat := now.Add(-1 * time.Minute)
	previousHealth := "Unknown"
	stateJSON := json.RawMessage(`{
		"skypilot_healthy": true,
		"vault_sync_status": "ok",
		"node_summary": {"total": 5, "ready": 5, "not_ready": 0},
		"cost_hourly": 2.50,
		"cost_hourly_avg": 2.40
	}`)

	err = seedCluster(ctx, pool, "int-cluster-1", "int-org-1", "healthy-cluster", "Active", &previousHealth, &heartbeat, stateJSON)
	if err != nil {
		t.Fatalf("seed cluster: %v", err)
	}

	// Create db.Client and run the monitor.
	dbClient, err := db.NewClient(ctx, dbURL)
	if err != nil {
		t.Fatalf("create db client: %v", err)
	}
	defer dbClient.Close()

	cfg := &config.Config{
		PollInterval:                  30 * time.Second,
		HeartbeatDegradedThreshold:    5 * time.Minute,
		HeartbeatUnreachableThreshold: 30 * time.Minute,
		CostAnomalyMultiplier:         2.0,
		LockTTL:                       2 * time.Minute,
		MonitorID:                     "integration-test-monitor",
		MetricsAddr:                   ":0",
		LogLevel:                      "debug",
	}

	monitor := &monitors.ClusterHealthMonitor{
		DB:        dbClient,
		Config:    cfg,
		Logger:    zap.NewNop(),
		Clock:     monitors.RealClock{},
		EKSProber: &monitors.NoopEKSProber{},
	}

	// Run one check cycle.
	if err := monitor.Check(ctx); err != nil {
		t.Fatalf("monitor.Check failed: %v", err)
	}

	// Verify: cluster health should now be "Healthy".
	health, err := getClusterHealth(ctx, pool, "int-cluster-1")
	if err != nil {
		t.Fatalf("get cluster health: %v", err)
	}
	if health != "Healthy" {
		t.Errorf("expected cluster health Healthy, got %q", health)
	}

	// Verify: a health transition event should have been emitted (Unknown -> Healthy).
	eventCount, err := countEvents(ctx, pool, "int-cluster-1", "monitor_health_changed")
	if err != nil {
		t.Fatalf("count events: %v", err)
	}
	if eventCount != 1 {
		t.Errorf("expected 1 health transition event, got %d", eventCount)
	}

	// Verify: lock should be released after check completes.
	lockCount, err := countLocks(ctx, pool)
	if err != nil {
		t.Fatalf("count locks: %v", err)
	}
	if lockCount != 0 {
		t.Errorf("expected 0 active locks after check, got %d", lockCount)
	}
}

// TestIntegration_DegradedCluster_SkyPilotDown verifies that a cluster with
// unhealthy SkyPilot is marked as Degraded.
func TestIntegration_DegradedCluster_SkyPilotDown(t *testing.T) {
	dbURL := testDBURL(t)
	ctx := context.Background()

	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		t.Fatalf("connect to test DB: %v", err)
	}
	defer pool.Close()

	if err := setupSchema(ctx, pool); err != nil {
		t.Fatalf("setup schema: %v", err)
	}

	now := time.Now().UTC()
	heartbeat := now.Add(-30 * time.Second)
	previousHealth := "Healthy"
	stateJSON := json.RawMessage(`{
		"skypilot_healthy": false,
		"vault_sync_status": "ok",
		"node_summary": {"total": 3, "ready": 3, "not_ready": 0}
	}`)

	err = seedCluster(ctx, pool, "int-cluster-2", "int-org-1", "degraded-cluster", "Active", &previousHealth, &heartbeat, stateJSON)
	if err != nil {
		t.Fatalf("seed cluster: %v", err)
	}

	dbClient, err := db.NewClient(ctx, dbURL)
	if err != nil {
		t.Fatalf("create db client: %v", err)
	}
	defer dbClient.Close()

	cfg := &config.Config{
		HeartbeatDegradedThreshold:    5 * time.Minute,
		HeartbeatUnreachableThreshold: 30 * time.Minute,
		CostAnomalyMultiplier:         2.0,
		LockTTL:                       2 * time.Minute,
		MonitorID:                     "integration-test-monitor-2",
	}

	monitor := &monitors.ClusterHealthMonitor{
		DB:        dbClient,
		Config:    cfg,
		Logger:    zap.NewNop(),
		Clock:     monitors.RealClock{},
		EKSProber: &monitors.NoopEKSProber{},
	}

	if err := monitor.Check(ctx); err != nil {
		t.Fatalf("monitor.Check failed: %v", err)
	}

	health, err := getClusterHealth(ctx, pool, "int-cluster-2")
	if err != nil {
		t.Fatalf("get cluster health: %v", err)
	}
	if health != "Degraded" {
		t.Errorf("expected cluster health Degraded (SkyPilot down), got %q", health)
	}

	// Verify transition event Healthy -> Degraded.
	eventCount, err := countEvents(ctx, pool, "int-cluster-2", "monitor_health_changed")
	if err != nil {
		t.Fatalf("count events: %v", err)
	}
	if eventCount != 1 {
		t.Errorf("expected 1 health transition event, got %d", eventCount)
	}
}

// TestIntegration_UnreachableCluster_StaleHeartbeat verifies that a cluster
// with a stale heartbeat (>30m) is marked as Unreachable.
func TestIntegration_UnreachableCluster_StaleHeartbeat(t *testing.T) {
	dbURL := testDBURL(t)
	ctx := context.Background()

	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		t.Fatalf("connect to test DB: %v", err)
	}
	defer pool.Close()

	if err := setupSchema(ctx, pool); err != nil {
		t.Fatalf("setup schema: %v", err)
	}

	now := time.Now().UTC()
	heartbeat := now.Add(-45 * time.Minute) // >30min threshold
	previousHealth := "Healthy"
	stateJSON := json.RawMessage(`{
		"skypilot_healthy": true,
		"vault_sync_status": "ok"
	}`)

	err = seedCluster(ctx, pool, "int-cluster-3", "int-org-1", "unreachable-cluster", "Active", &previousHealth, &heartbeat, stateJSON)
	if err != nil {
		t.Fatalf("seed cluster: %v", err)
	}

	dbClient, err := db.NewClient(ctx, dbURL)
	if err != nil {
		t.Fatalf("create db client: %v", err)
	}
	defer dbClient.Close()

	cfg := &config.Config{
		HeartbeatDegradedThreshold:    5 * time.Minute,
		HeartbeatUnreachableThreshold: 30 * time.Minute,
		CostAnomalyMultiplier:         2.0,
		LockTTL:                       2 * time.Minute,
		MonitorID:                     "integration-test-monitor-3",
	}

	monitor := &monitors.ClusterHealthMonitor{
		DB:        dbClient,
		Config:    cfg,
		Logger:    zap.NewNop(),
		Clock:     monitors.RealClock{},
		EKSProber: &monitors.NoopEKSProber{},
	}

	if err := monitor.Check(ctx); err != nil {
		t.Fatalf("monitor.Check failed: %v", err)
	}

	health, err := getClusterHealth(ctx, pool, "int-cluster-3")
	if err != nil {
		t.Fatalf("get cluster health: %v", err)
	}
	if health != "Unreachable" {
		t.Errorf("expected cluster health Unreachable (stale heartbeat), got %q", health)
	}
}

// TestIntegration_NoTransitionEvent_StableHealth verifies that no event is
// emitted when the health status does not change.
func TestIntegration_NoTransitionEvent_StableHealth(t *testing.T) {
	dbURL := testDBURL(t)
	ctx := context.Background()

	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		t.Fatalf("connect to test DB: %v", err)
	}
	defer pool.Close()

	if err := setupSchema(ctx, pool); err != nil {
		t.Fatalf("setup schema: %v", err)
	}

	now := time.Now().UTC()
	heartbeat := now.Add(-30 * time.Second)
	alreadyHealthy := "Healthy"
	stateJSON := json.RawMessage(`{
		"skypilot_healthy": true,
		"vault_sync_status": "ok",
		"node_summary": {"total": 3, "ready": 3, "not_ready": 0}
	}`)

	err = seedCluster(ctx, pool, "int-cluster-4", "int-org-1", "stable-cluster", "Active", &alreadyHealthy, &heartbeat, stateJSON)
	if err != nil {
		t.Fatalf("seed cluster: %v", err)
	}

	dbClient, err := db.NewClient(ctx, dbURL)
	if err != nil {
		t.Fatalf("create db client: %v", err)
	}
	defer dbClient.Close()

	cfg := &config.Config{
		HeartbeatDegradedThreshold:    5 * time.Minute,
		HeartbeatUnreachableThreshold: 30 * time.Minute,
		CostAnomalyMultiplier:         2.0,
		LockTTL:                       2 * time.Minute,
		MonitorID:                     "integration-test-monitor-4",
	}

	monitor := &monitors.ClusterHealthMonitor{
		DB:        dbClient,
		Config:    cfg,
		Logger:    zap.NewNop(),
		Clock:     monitors.RealClock{},
		EKSProber: &monitors.NoopEKSProber{},
	}

	if err := monitor.Check(ctx); err != nil {
		t.Fatalf("monitor.Check failed: %v", err)
	}

	// No transition event should be emitted — health stayed Healthy.
	eventCount, err := countEvents(ctx, pool, "int-cluster-4", "monitor_health_changed")
	if err != nil {
		t.Fatalf("count events: %v", err)
	}
	if eventCount != 0 {
		t.Errorf("expected 0 transition events for stable health, got %d", eventCount)
	}
}

// TestIntegration_CostAnomaly verifies that a cost spike triggers Degraded status.
func TestIntegration_CostAnomaly(t *testing.T) {
	dbURL := testDBURL(t)
	ctx := context.Background()

	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		t.Fatalf("connect to test DB: %v", err)
	}
	defer pool.Close()

	if err := setupSchema(ctx, pool); err != nil {
		t.Fatalf("setup schema: %v", err)
	}

	now := time.Now().UTC()
	heartbeat := now.Add(-30 * time.Second)
	previousHealth := "Healthy"
	// cost_hourly is 30.0, avg is 10.0 -> ratio 3.0 > 2.0 threshold
	stateJSON := json.RawMessage(`{
		"skypilot_healthy": true,
		"vault_sync_status": "ok",
		"node_summary": {"total": 3, "ready": 3, "not_ready": 0},
		"cost_hourly": 30.0,
		"cost_hourly_avg": 10.0
	}`)

	err = seedCluster(ctx, pool, "int-cluster-5", "int-org-1", "cost-spike-cluster", "Active", &previousHealth, &heartbeat, stateJSON)
	if err != nil {
		t.Fatalf("seed cluster: %v", err)
	}

	dbClient, err := db.NewClient(ctx, dbURL)
	if err != nil {
		t.Fatalf("create db client: %v", err)
	}
	defer dbClient.Close()

	cfg := &config.Config{
		HeartbeatDegradedThreshold:    5 * time.Minute,
		HeartbeatUnreachableThreshold: 30 * time.Minute,
		CostAnomalyMultiplier:         2.0,
		LockTTL:                       2 * time.Minute,
		MonitorID:                     "integration-test-monitor-5",
	}

	monitor := &monitors.ClusterHealthMonitor{
		DB:        dbClient,
		Config:    cfg,
		Logger:    zap.NewNop(),
		Clock:     monitors.RealClock{},
		EKSProber: &monitors.NoopEKSProber{},
	}

	if err := monitor.Check(ctx); err != nil {
		t.Fatalf("monitor.Check failed: %v", err)
	}

	health, err := getClusterHealth(ctx, pool, "int-cluster-5")
	if err != nil {
		t.Fatalf("get cluster health: %v", err)
	}
	if health != "Degraded" {
		t.Errorf("expected cluster health Degraded (cost anomaly), got %q", health)
	}
}

// TestIntegration_DistributedLocking verifies that two monitor instances
// do not process the same cluster simultaneously.
func TestIntegration_DistributedLocking(t *testing.T) {
	dbURL := testDBURL(t)
	ctx := context.Background()

	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		t.Fatalf("connect to test DB: %v", err)
	}
	defer pool.Close()

	if err := setupSchema(ctx, pool); err != nil {
		t.Fatalf("setup schema: %v", err)
	}

	now := time.Now().UTC()
	heartbeat := now.Add(-1 * time.Minute)
	previousHealth := "Unknown"
	stateJSON := json.RawMessage(`{"skypilot_healthy": true, "vault_sync_status": "ok"}`)

	err = seedCluster(ctx, pool, "int-cluster-lock", "int-org-1", "lock-test-cluster", "Active", &previousHealth, &heartbeat, stateJSON)
	if err != nil {
		t.Fatalf("seed cluster: %v", err)
	}

	// Manually acquire a lock as "other-monitor".
	_, err = pool.Exec(ctx, `
		INSERT INTO reconcile_locks (resource_type, resource_id, locked_by, locked_at, expires_at)
		VALUES ('cluster_health', 'int-cluster-lock', 'other-monitor', NOW(), NOW() + INTERVAL '5 minutes')
	`)
	if err != nil {
		t.Fatalf("insert lock: %v", err)
	}

	dbClient, err := db.NewClient(ctx, dbURL)
	if err != nil {
		t.Fatalf("create db client: %v", err)
	}
	defer dbClient.Close()

	cfg := &config.Config{
		HeartbeatDegradedThreshold:    5 * time.Minute,
		HeartbeatUnreachableThreshold: 30 * time.Minute,
		CostAnomalyMultiplier:         2.0,
		LockTTL:                       2 * time.Minute,
		MonitorID:                     "integration-test-monitor-lock",
	}

	monitor := &monitors.ClusterHealthMonitor{
		DB:        dbClient,
		Config:    cfg,
		Logger:    zap.NewNop(),
		Clock:     monitors.RealClock{},
		EKSProber: &monitors.NoopEKSProber{},
	}

	// This should succeed (no error) but should NOT update the cluster
	// because the lock is held by "other-monitor".
	if err := monitor.Check(ctx); err != nil {
		t.Fatalf("monitor.Check failed: %v", err)
	}

	// Health should still be "Unknown" (not updated).
	health, err := getClusterHealth(ctx, pool, "int-cluster-lock")
	if err != nil {
		t.Fatalf("get cluster health: %v", err)
	}
	if health != "Unknown" {
		t.Errorf("expected cluster health unchanged (Unknown) due to lock, got %q", health)
	}

	// No events should have been emitted.
	eventCount, err := countEvents(ctx, pool, "int-cluster-lock", "monitor_health_changed")
	if err != nil {
		t.Fatalf("count events: %v", err)
	}
	if eventCount != 0 {
		t.Errorf("expected 0 events when lock held by another instance, got %d", eventCount)
	}
}

// TestIntegration_MultipleClustersBatchCheck verifies the reconciler handles
// multiple clusters in a single Check() invocation.
func TestIntegration_MultipleClustersBatchCheck(t *testing.T) {
	dbURL := testDBURL(t)
	ctx := context.Background()

	pool, err := pgxpool.New(ctx, dbURL)
	if err != nil {
		t.Fatalf("connect to test DB: %v", err)
	}
	defer pool.Close()

	if err := setupSchema(ctx, pool); err != nil {
		t.Fatalf("setup schema: %v", err)
	}

	now := time.Now().UTC()
	unknown := "Unknown"

	// Cluster A: healthy
	hbA := now.Add(-1 * time.Minute)
	err = seedCluster(ctx, pool, "batch-a", "int-org-1", "batch-cluster-a", "Active", &unknown,
		&hbA, json.RawMessage(`{"skypilot_healthy": true, "vault_sync_status": "ok"}`))
	if err != nil {
		t.Fatalf("seed cluster A: %v", err)
	}

	// Cluster B: degraded (SkyPilot down)
	hbB := now.Add(-30 * time.Second)
	err = seedCluster(ctx, pool, "batch-b", "int-org-1", "batch-cluster-b", "Active", &unknown,
		&hbB, json.RawMessage(`{"skypilot_healthy": false, "vault_sync_status": "ok"}`))
	if err != nil {
		t.Fatalf("seed cluster B: %v", err)
	}

	// Cluster C: unreachable (stale heartbeat)
	hbC := now.Add(-45 * time.Minute)
	err = seedCluster(ctx, pool, "batch-c", "int-org-1", "batch-cluster-c", "Active", &unknown,
		&hbC, json.RawMessage(`{"skypilot_healthy": true}`))
	if err != nil {
		t.Fatalf("seed cluster C: %v", err)
	}

	dbClient, err := db.NewClient(ctx, dbURL)
	if err != nil {
		t.Fatalf("create db client: %v", err)
	}
	defer dbClient.Close()

	cfg := &config.Config{
		HeartbeatDegradedThreshold:    5 * time.Minute,
		HeartbeatUnreachableThreshold: 30 * time.Minute,
		CostAnomalyMultiplier:         2.0,
		LockTTL:                       2 * time.Minute,
		MonitorID:                     "integration-test-batch",
	}

	monitor := &monitors.ClusterHealthMonitor{
		DB:        dbClient,
		Config:    cfg,
		Logger:    zap.NewNop(),
		Clock:     monitors.RealClock{},
		EKSProber: &monitors.NoopEKSProber{},
	}

	if err := monitor.Check(ctx); err != nil {
		t.Fatalf("monitor.Check failed: %v", err)
	}

	// Verify each cluster got the expected status.
	tests := []struct {
		clusterID string
		expected  string
	}{
		{"batch-a", "Healthy"},
		{"batch-b", "Degraded"},
		{"batch-c", "Unreachable"},
	}

	for _, tt := range tests {
		health, err := getClusterHealth(ctx, pool, tt.clusterID)
		if err != nil {
			t.Errorf("get health for %s: %v", tt.clusterID, err)
			continue
		}
		if health != tt.expected {
			t.Errorf("cluster %s: expected %s, got %s", tt.clusterID, tt.expected, health)
		}
	}

	// All three clusters should have emitted transition events (Unknown -> new status).
	for _, tt := range tests {
		eventCount, err := countEvents(ctx, pool, tt.clusterID, "monitor_health_changed")
		if err != nil {
			t.Errorf("count events for %s: %v", tt.clusterID, err)
			continue
		}
		if eventCount != 1 {
			t.Errorf("cluster %s: expected 1 transition event, got %d", tt.clusterID, eventCount)
		}
	}
}

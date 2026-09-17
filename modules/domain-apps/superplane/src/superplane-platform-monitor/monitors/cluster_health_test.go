package monitors

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/config"
	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/db"
)

// --- Test helpers ---

// fakeClock returns a fixed time for tests.
type fakeClock struct {
	now time.Time
}

func (c *fakeClock) Now() time.Time { return c.now }

// fakeDB implements db.Querier for unit tests.
type fakeDB struct {
	clusters    []db.Cluster
	lockResult  bool
	costHistory []float64
	events      []fakeEvent
	updates     []fakeUpdate
}

type fakeEvent struct {
	OrgID      string
	ResourceID string
	EventType  string
	Message    string
}

type fakeUpdate struct {
	ClusterID    string
	HealthStatus string
}

func (f *fakeDB) ListActiveClusters(_ context.Context) ([]db.Cluster, error) {
	return f.clusters, nil
}

func (f *fakeDB) UpdateClusterHealth(_ context.Context, clusterID, healthStatus string, _ json.RawMessage) error {
	f.updates = append(f.updates, fakeUpdate{ClusterID: clusterID, HealthStatus: healthStatus})
	return nil
}

func (f *fakeDB) InsertEvent(_ context.Context, orgID, resourceID, eventType, message string, _ json.RawMessage) error {
	f.events = append(f.events, fakeEvent{OrgID: orgID, ResourceID: resourceID, EventType: eventType, Message: message})
	return nil
}

func (f *fakeDB) AcquireLock(_ context.Context, _, _, _ string, _ time.Duration) (bool, error) {
	return f.lockResult, nil
}

func (f *fakeDB) ReleaseLock(_ context.Context, _, _, _ string) error {
	return nil
}

func (f *fakeDB) GetCostHistory(_ context.Context, _ string, _ time.Duration) ([]float64, error) {
	return f.costHistory, nil
}

func (f *fakeDB) Ping(_ context.Context) error {
	return nil
}

// fakeEKSProber for testing EKS reachability.
type fakeEKSProber struct {
	err error
}

func (p *fakeEKSProber) ProbeEKS(_ context.Context, _ *db.Cluster) error {
	return p.err
}

func defaultConfig() *config.Config {
	return &config.Config{
		HeartbeatDegradedThreshold:    5 * time.Minute,
		HeartbeatUnreachableThreshold: 30 * time.Minute,
		CostAnomalyMultiplier:         2.0,
		LockTTL:                       2 * time.Minute,
		MonitorID:                     "test-monitor",
	}
}

func newTestMonitor(database *fakeDB) *ClusterHealthMonitor {
	return &ClusterHealthMonitor{
		DB:        database,
		Config:    defaultConfig(),
		Logger:    zap.NewNop(),
		Clock:     &fakeClock{now: time.Date(2026, 4, 3, 12, 0, 0, 0, time.UTC)},
		EKSProber: &NoopEKSProber{},
	}
}

// --- Heartbeat Freshness Tests ---

func TestCheckHeartbeatFreshness_Healthy(t *testing.T) {
	r := newTestMonitor(nil)
	now := r.Clock.Now()
	lastHeartbeat := now.Add(-2 * time.Minute)
	cluster := &db.Cluster{LastHeartbeat: &lastHeartbeat}

	dim := r.checkHeartbeatFreshness(cluster, now)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy, got %s", dim.Status)
	}
	if dim.Name != "heartbeat_freshness" {
		t.Errorf("expected name heartbeat_freshness, got %s", dim.Name)
	}
}

func TestCheckHeartbeatFreshness_NoHeartbeat(t *testing.T) {
	r := newTestMonitor(nil)
	now := r.Clock.Now()
	cluster := &db.Cluster{LastHeartbeat: nil}

	dim := r.checkHeartbeatFreshness(cluster, now)

	if dim.Status != HealthStatusUnknown {
		t.Errorf("expected Unknown, got %s", dim.Status)
	}
}

func TestCheckHeartbeatFreshness_Degraded(t *testing.T) {
	r := newTestMonitor(nil)
	now := r.Clock.Now()
	lastHeartbeat := now.Add(-10 * time.Minute) // >5min, <30min
	cluster := &db.Cluster{LastHeartbeat: &lastHeartbeat}

	dim := r.checkHeartbeatFreshness(cluster, now)

	if dim.Status != HealthStatusDegraded {
		t.Errorf("expected Degraded, got %s", dim.Status)
	}
}

func TestCheckHeartbeatFreshness_Unreachable(t *testing.T) {
	r := newTestMonitor(nil)
	now := r.Clock.Now()
	lastHeartbeat := now.Add(-45 * time.Minute) // >30min
	cluster := &db.Cluster{LastHeartbeat: &lastHeartbeat}

	dim := r.checkHeartbeatFreshness(cluster, now)

	if dim.Status != HealthStatusUnreachable {
		t.Errorf("expected Unreachable, got %s", dim.Status)
	}
}

// --- SkyPilot Health Tests ---

func TestCheckSkyPilotHealth_Healthy(t *testing.T) {
	r := newTestMonitor(nil)
	healthy := true
	payload := &db.HeartbeatPayload{SkyPilotHealthy: &healthy}

	dim := r.checkSkyPilotHealth(payload)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy, got %s", dim.Status)
	}
}

func TestCheckSkyPilotHealth_Unhealthy(t *testing.T) {
	r := newTestMonitor(nil)
	healthy := false
	payload := &db.HeartbeatPayload{SkyPilotHealthy: &healthy}

	dim := r.checkSkyPilotHealth(payload)

	if dim.Status != HealthStatusDegraded {
		t.Errorf("expected Degraded, got %s", dim.Status)
	}
}

func TestCheckSkyPilotHealth_NotReported(t *testing.T) {
	r := newTestMonitor(nil)
	payload := &db.HeartbeatPayload{}

	dim := r.checkSkyPilotHealth(payload)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy when not reported, got %s", dim.Status)
	}
}

// --- Vault Sync Status Tests ---

func TestCheckVaultSyncStatus_OK(t *testing.T) {
	r := newTestMonitor(nil)
	payload := &db.HeartbeatPayload{VaultSyncStatus: "ok"}

	dim := r.checkVaultSyncStatus(payload)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy, got %s", dim.Status)
	}
}

func TestCheckVaultSyncStatus_Failed(t *testing.T) {
	r := newTestMonitor(nil)
	payload := &db.HeartbeatPayload{VaultSyncStatus: "failed"}

	dim := r.checkVaultSyncStatus(payload)

	if dim.Status != HealthStatusDegraded {
		t.Errorf("expected Degraded, got %s", dim.Status)
	}
}

func TestCheckVaultSyncStatus_Pending(t *testing.T) {
	r := newTestMonitor(nil)
	payload := &db.HeartbeatPayload{VaultSyncStatus: "pending"}

	dim := r.checkVaultSyncStatus(payload)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy for pending, got %s", dim.Status)
	}
}

func TestCheckVaultSyncStatus_Empty(t *testing.T) {
	r := newTestMonitor(nil)
	payload := &db.HeartbeatPayload{}

	dim := r.checkVaultSyncStatus(payload)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy when empty, got %s", dim.Status)
	}
}

// --- Node Health Tests ---

func TestCheckNodeHealth_AllReady(t *testing.T) {
	r := newTestMonitor(nil)
	payload := &db.HeartbeatPayload{
		NodeSummary: &db.NodeSummary{Total: 5, Ready: 5, NotReady: 0},
	}

	dim := r.checkNodeHealth(payload)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy, got %s", dim.Status)
	}
}

func TestCheckNodeHealth_SomeNotReady(t *testing.T) {
	r := newTestMonitor(nil)
	payload := &db.HeartbeatPayload{
		NodeSummary: &db.NodeSummary{Total: 10, Ready: 8, NotReady: 2},
	}

	dim := r.checkNodeHealth(payload)

	if dim.Status != HealthStatusDegraded {
		t.Errorf("expected Degraded, got %s", dim.Status)
	}
}

func TestCheckNodeHealth_MajorityNotReady(t *testing.T) {
	r := newTestMonitor(nil)
	payload := &db.HeartbeatPayload{
		NodeSummary: &db.NodeSummary{Total: 10, Ready: 4, NotReady: 6},
	}

	dim := r.checkNodeHealth(payload)

	if dim.Status != HealthStatusDegraded {
		t.Errorf("expected Degraded, got %s", dim.Status)
	}
	// Should contain CRITICAL
	if dim.Message == "" {
		t.Error("expected non-empty message")
	}
}

func TestCheckNodeHealth_NotReported(t *testing.T) {
	r := newTestMonitor(nil)
	payload := &db.HeartbeatPayload{}

	dim := r.checkNodeHealth(payload)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy when not reported, got %s", dim.Status)
	}
}

// --- EKS Reachability Tests ---

func TestCheckEKSReachability_Reachable(t *testing.T) {
	r := newTestMonitor(nil)
	r.EKSProber = &fakeEKSProber{err: nil}
	cluster := &db.Cluster{ID: "test-cluster"}

	dim := r.checkEKSReachability(context.Background(), cluster)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy, got %s", dim.Status)
	}
}

func TestCheckEKSReachability_Unreachable(t *testing.T) {
	r := newTestMonitor(nil)
	r.EKSProber = &fakeEKSProber{err: fmt.Errorf("connection refused")}
	cluster := &db.Cluster{ID: "test-cluster"}

	dim := r.checkEKSReachability(context.Background(), cluster)

	if dim.Status != HealthStatusUnreachable {
		t.Errorf("expected Unreachable, got %s", dim.Status)
	}
}

func TestCheckEKSReachability_NilProber(t *testing.T) {
	r := newTestMonitor(nil)
	r.EKSProber = nil
	cluster := &db.Cluster{ID: "test-cluster"}

	dim := r.checkEKSReachability(context.Background(), cluster)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy when prober nil, got %s", dim.Status)
	}
}

// --- Cost Anomaly Tests ---

func TestCheckCostAnomaly_NoCostData(t *testing.T) {
	r := newTestMonitor(&fakeDB{})
	payload := &db.HeartbeatPayload{}
	cluster := &db.Cluster{ID: "test-cluster"}

	dim := r.checkCostAnomaly(context.Background(), cluster, payload)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy when no cost data, got %s", dim.Status)
	}
}

func TestCheckCostAnomaly_Normal(t *testing.T) {
	r := newTestMonitor(&fakeDB{})
	cost := 10.0
	avg := 8.0
	payload := &db.HeartbeatPayload{CostHourly: &cost, CostHourlyAvg: &avg}
	cluster := &db.Cluster{ID: "test-cluster"}

	dim := r.checkCostAnomaly(context.Background(), cluster, payload)

	if dim.Status != HealthStatusHealthy {
		t.Errorf("expected Healthy, got %s (ratio=%.1f)", dim.Status, cost/avg)
	}
}

func TestCheckCostAnomaly_Spike(t *testing.T) {
	r := newTestMonitor(&fakeDB{})
	cost := 25.0
	avg := 10.0
	payload := &db.HeartbeatPayload{CostHourly: &cost, CostHourlyAvg: &avg}
	cluster := &db.Cluster{ID: "test-cluster"}

	dim := r.checkCostAnomaly(context.Background(), cluster, payload)

	if dim.Status != HealthStatusDegraded {
		t.Errorf("expected Degraded for cost spike, got %s", dim.Status)
	}
}

func TestCheckCostAnomaly_FallbackToHistory(t *testing.T) {
	r := newTestMonitor(&fakeDB{costHistory: []float64{10.0, 12.0, 8.0}})
	cost := 25.0 // avg is 10, ratio is 2.5 > 2.0 threshold
	payload := &db.HeartbeatPayload{CostHourly: &cost}
	cluster := &db.Cluster{ID: "test-cluster"}

	dim := r.checkCostAnomaly(context.Background(), cluster, payload)

	if dim.Status != HealthStatusDegraded {
		t.Errorf("expected Degraded for cost spike with DB fallback, got %s", dim.Status)
	}
}

// --- Aggregation Tests ---

func TestCheckAllDimensions_AllHealthy(t *testing.T) {
	database := &fakeDB{}
	r := newTestMonitor(database)

	now := r.Clock.Now()
	lastHeartbeat := now.Add(-1 * time.Minute)
	healthy := true
	cluster := &db.Cluster{
		ID:            "test-cluster",
		LastHeartbeat: &lastHeartbeat,
	}
	payload := &db.HeartbeatPayload{
		SkyPilotHealthy: &healthy,
		VaultSyncStatus: "ok",
		NodeSummary:     &db.NodeSummary{Total: 5, Ready: 5, NotReady: 0},
	}

	result := r.checkAllDimensions(context.Background(), cluster, payload, now)

	if result.OverallStatus != HealthStatusHealthy {
		t.Errorf("expected Healthy overall, got %s", result.OverallStatus)
	}
	if len(result.Dimensions) != 6 {
		t.Errorf("expected 6 dimensions, got %d", len(result.Dimensions))
	}
}

func TestCheckAllDimensions_WorstWins(t *testing.T) {
	database := &fakeDB{}
	r := newTestMonitor(database)
	r.EKSProber = &fakeEKSProber{err: fmt.Errorf("unreachable")}

	now := r.Clock.Now()
	lastHeartbeat := now.Add(-1 * time.Minute) // Heartbeat is OK
	cluster := &db.Cluster{
		ID:            "test-cluster",
		LastHeartbeat: &lastHeartbeat,
	}
	payload := &db.HeartbeatPayload{
		VaultSyncStatus: "ok",
	}

	result := r.checkAllDimensions(context.Background(), cluster, payload, now)

	// EKS unreachable should be the worst
	if result.OverallStatus != HealthStatusUnreachable {
		t.Errorf("expected Unreachable (worst wins), got %s", result.OverallStatus)
	}
}

func TestCheckAllDimensions_DegradedFromSkyPilot(t *testing.T) {
	database := &fakeDB{}
	r := newTestMonitor(database)

	now := r.Clock.Now()
	lastHeartbeat := now.Add(-1 * time.Minute)
	unhealthy := false
	cluster := &db.Cluster{
		ID:            "test-cluster",
		LastHeartbeat: &lastHeartbeat,
	}
	payload := &db.HeartbeatPayload{
		SkyPilotHealthy: &unhealthy,
		VaultSyncStatus: "ok",
	}

	result := r.checkAllDimensions(context.Background(), cluster, payload, now)

	if result.OverallStatus != HealthStatusDegraded {
		t.Errorf("expected Degraded from SkyPilot, got %s", result.OverallStatus)
	}
}

// --- Full Check Integration Test ---

func TestCheck_FullCycle(t *testing.T) {
	now := time.Date(2026, 4, 3, 12, 0, 0, 0, time.UTC)
	lastHeartbeat := now.Add(-1 * time.Minute)
	currentHealth := "Unknown"

	database := &fakeDB{
		clusters: []db.Cluster{
			{
				ID:            "cluster-1",
				OrgID:         "org-1",
				Name:          "test-cluster",
				Status:        "Active",
				HealthStatus:  &currentHealth,
				LastHeartbeat: &lastHeartbeat,
				ActualStateJSON: json.RawMessage(`{
					"skypilot_healthy": true,
					"vault_sync_status": "ok",
					"node_summary": {"total": 3, "ready": 3, "not_ready": 0}
				}`),
			},
		},
		lockResult: true,
	}

	r := &ClusterHealthMonitor{
		DB:        database,
		Config:    defaultConfig(),
		Logger:    zap.NewNop(),
		Clock:     &fakeClock{now: now},
		EKSProber: &NoopEKSProber{},
	}

	err := r.Check(context.Background())
	if err != nil {
		t.Fatalf("Check failed: %v", err)
	}

	// Should have updated the cluster health.
	if len(database.updates) != 1 {
		t.Fatalf("expected 1 update, got %d", len(database.updates))
	}
	if database.updates[0].HealthStatus != HealthStatusHealthy {
		t.Errorf("expected health status Healthy, got %s", database.updates[0].HealthStatus)
	}

	// Should have emitted an event (Unknown -> Healthy transition).
	if len(database.events) != 1 {
		t.Fatalf("expected 1 event for health transition, got %d", len(database.events))
	}
	if database.events[0].EventType != "monitor_health_changed" {
		t.Errorf("expected event type monitor_health_changed, got %s", database.events[0].EventType)
	}
}

func TestCheck_LockNotAcquired(t *testing.T) {
	now := time.Date(2026, 4, 3, 12, 0, 0, 0, time.UTC)
	lastHeartbeat := now.Add(-1 * time.Minute)

	database := &fakeDB{
		clusters: []db.Cluster{
			{
				ID:            "cluster-1",
				OrgID:         "org-1",
				Name:          "locked-cluster",
				LastHeartbeat: &lastHeartbeat,
			},
		},
		lockResult: false, // Lock not acquired
	}

	r := &ClusterHealthMonitor{
		DB:        database,
		Config:    defaultConfig(),
		Logger:    zap.NewNop(),
		Clock:     &fakeClock{now: now},
		EKSProber: &NoopEKSProber{},
	}

	err := r.Check(context.Background())
	if err != nil {
		t.Fatalf("Check failed: %v", err)
	}

	// Should NOT have updated (lock was held by another instance).
	if len(database.updates) != 0 {
		t.Errorf("expected 0 updates when lock not acquired, got %d", len(database.updates))
	}
}

func TestCheck_NoHealthTransitionEvent(t *testing.T) {
	now := time.Date(2026, 4, 3, 12, 0, 0, 0, time.UTC)
	lastHeartbeat := now.Add(-1 * time.Minute)
	currentHealth := HealthStatusHealthy // Already healthy

	database := &fakeDB{
		clusters: []db.Cluster{
			{
				ID:            "cluster-1",
				OrgID:         "org-1",
				Name:          "stable-cluster",
				HealthStatus:  &currentHealth,
				LastHeartbeat: &lastHeartbeat,
				ActualStateJSON: json.RawMessage(`{
					"skypilot_healthy": true,
					"vault_sync_status": "ok"
				}`),
			},
		},
		lockResult: true,
	}

	r := &ClusterHealthMonitor{
		DB:        database,
		Config:    defaultConfig(),
		Logger:    zap.NewNop(),
		Clock:     &fakeClock{now: now},
		EKSProber: &NoopEKSProber{},
	}

	err := r.Check(context.Background())
	if err != nil {
		t.Fatalf("Check failed: %v", err)
	}

	// No event should be emitted when status hasn't changed.
	if len(database.events) != 0 {
		t.Errorf("expected 0 events for stable health, got %d", len(database.events))
	}
}

// --- WorstStatus Tests ---

func TestWorstStatus(t *testing.T) {
	tests := []struct {
		statuses []string
		expected string
	}{
		{[]string{HealthStatusHealthy, HealthStatusHealthy}, HealthStatusHealthy},
		{[]string{HealthStatusHealthy, HealthStatusDegraded}, HealthStatusDegraded},
		{[]string{HealthStatusDegraded, HealthStatusUnreachable}, HealthStatusUnreachable},
		{[]string{HealthStatusHealthy, HealthStatusDegraded, HealthStatusUnreachable}, HealthStatusUnreachable},
		{[]string{HealthStatusUnknown}, HealthStatusUnknown},
	}

	for _, tt := range tests {
		result := WorstStatus(tt.statuses...)
		if result != tt.expected {
			t.Errorf("WorstStatus(%v) = %s, want %s", tt.statuses, result, tt.expected)
		}
	}
}

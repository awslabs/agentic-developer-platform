package monitors

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.uber.org/zap"

	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/config"
	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/db"
)

func newTestBudgetMonitor(fdb *fakeDB) *BudgetMonitor {
	logger, _ := zap.NewDevelopment()
	return &BudgetMonitor{
		DB: fdb,
		Config: &config.Config{
			MonitorID:             "test-monitor",
			LockTTL:              2 * time.Minute,
			CostAnomalyMultiplier: 2.0,
		},
		Logger: logger,
		Clock:  &fakeClock{now: time.Now().UTC()},
	}
}

func TestBudgetMonitor_Name(t *testing.T) {
	m := &BudgetMonitor{}
	if m.Name() != "budget" {
		t.Errorf("expected Name() = 'budget', got %q", m.Name())
	}
}

func TestBudgetMonitor_SkipsWhenLockNotAcquired(t *testing.T) {
	fdb := &fakeDB{lockResult: false}
	m := newTestBudgetMonitor(fdb)

	err := m.Check(context.Background())
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	// No events should be created
	if len(fdb.events) != 0 {
		t.Errorf("expected 0 events, got %d", len(fdb.events))
	}
}

func TestBudgetMonitor_NoCostData(t *testing.T) {
	// Cluster with no cost data in heartbeat should not trigger alerts
	payload := db.HeartbeatPayload{
		NodeCount: 2,
		GPUCount:  4,
	}
	payloadJSON, _ := json.Marshal(payload)

	fdb := &fakeDB{
		lockResult: true,
		clusters: []db.Cluster{
			{
				ID:              "cluster-1",
				OrgID:           "org-1",
				Name:            "test-cluster",
				Status:          "Active",
				ActualStateJSON: payloadJSON,
			},
		},
	}
	m := newTestBudgetMonitor(fdb)

	err := m.Check(context.Background())
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	if len(fdb.events) != 0 {
		t.Errorf("expected 0 events for no cost data, got %d", len(fdb.events))
	}
}

func TestBudgetMonitor_CostAnomalyDetected(t *testing.T) {
	// Current cost is 3x the average — should fire anomaly alert
	hourly := 30.0
	avg := 10.0
	payload := db.HeartbeatPayload{
		CostHourly:    &hourly,
		CostHourlyAvg: &avg,
		NodeCount:     2,
		GPUCount:      4,
	}
	payloadJSON, _ := json.Marshal(payload)

	fdb := &fakeDB{
		lockResult: true,
		clusters: []db.Cluster{
			{
				ID:              "cluster-anomaly",
				OrgID:           "org-1",
				Name:            "anomaly-cluster",
				Status:          "Active",
				ActualStateJSON: payloadJSON,
			},
		},
	}
	m := newTestBudgetMonitor(fdb)

	err := m.Check(context.Background())
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}

	// Should have 1 cost anomaly event
	if len(fdb.events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(fdb.events))
	}
	if fdb.events[0].EventType != "budget.cost_anomaly" {
		t.Errorf("expected event type 'budget.cost_anomaly', got %q", fdb.events[0].EventType)
	}
	if fdb.events[0].OrgID != "org-1" {
		t.Errorf("expected org_id 'org-1', got %q", fdb.events[0].OrgID)
	}
}

func TestBudgetMonitor_NormalCostNoAnomaly(t *testing.T) {
	// Current cost is within normal range — no alert
	hourly := 12.0
	avg := 10.0
	payload := db.HeartbeatPayload{
		CostHourly:    &hourly,
		CostHourlyAvg: &avg,
		NodeCount:     2,
		GPUCount:      4,
	}
	payloadJSON, _ := json.Marshal(payload)

	fdb := &fakeDB{
		lockResult: true,
		clusters: []db.Cluster{
			{
				ID:              "cluster-normal",
				OrgID:           "org-1",
				Name:            "normal-cluster",
				Status:          "Active",
				ActualStateJSON: payloadJSON,
			},
		},
	}
	m := newTestBudgetMonitor(fdb)

	err := m.Check(context.Background())
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	if len(fdb.events) != 0 {
		t.Errorf("expected 0 events for normal cost, got %d", len(fdb.events))
	}
}

func TestBudgetMonitor_EmptyActualState(t *testing.T) {
	fdb := &fakeDB{
		lockResult: true,
		clusters: []db.Cluster{
			{
				ID:              "cluster-empty",
				OrgID:           "org-1",
				Name:            "empty-cluster",
				Status:          "Active",
				ActualStateJSON: nil,
			},
		},
	}
	m := newTestBudgetMonitor(fdb)

	err := m.Check(context.Background())
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	if len(fdb.events) != 0 {
		t.Errorf("expected 0 events, got %d", len(fdb.events))
	}
}

func TestBudgetMonitor_MultipleClusters(t *testing.T) {
	// One cluster with anomaly, one normal
	anomalyHourly := 50.0
	anomalyAvg := 10.0
	normalHourly := 11.0
	normalAvg := 10.0

	anomalyPayload, _ := json.Marshal(db.HeartbeatPayload{
		CostHourly:    &anomalyHourly,
		CostHourlyAvg: &anomalyAvg,
	})
	normalPayload, _ := json.Marshal(db.HeartbeatPayload{
		CostHourly:    &normalHourly,
		CostHourlyAvg: &normalAvg,
	})

	fdb := &fakeDB{
		lockResult: true,
		clusters: []db.Cluster{
			{ID: "anomaly", OrgID: "org-1", Name: "anomaly", Status: "Active", ActualStateJSON: anomalyPayload},
			{ID: "normal", OrgID: "org-2", Name: "normal", Status: "Active", ActualStateJSON: normalPayload},
		},
	}
	m := newTestBudgetMonitor(fdb)

	err := m.Check(context.Background())
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}
	// Only the anomaly cluster should fire an event
	if len(fdb.events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(fdb.events))
	}
	if fdb.events[0].ResourceID != "anomaly" {
		t.Errorf("expected event for cluster 'anomaly', got %q", fdb.events[0].ResourceID)
	}
}

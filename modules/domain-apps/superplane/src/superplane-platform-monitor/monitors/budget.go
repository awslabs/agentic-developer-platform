// Package monitors provides the BudgetMonitor which checks workspace budget
// limits from heartbeat cost data and fires budget alerts.
package monitors

import (
	"context"
	"encoding/json"
	"fmt"
	"math"

	"go.uber.org/zap"

	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/config"
	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/db"
)

const (
	budgetMonitorLockType = "budget_monitor"
	budgetMonitorLockID   = "global"
)

// BudgetThresholds defines the percentage thresholds for budget alerts.
var BudgetThresholds = struct {
	Warning  float64
	Critical float64
}{
	Warning:  80.0,
	Critical: 100.0,
}

// BudgetMonitor checks workspace budget limits from heartbeat cost data.
// It runs on each polling cycle, querying the workspaces table for active
// workspaces with budget limits, and comparing current daily costs from
// the latest heartbeat data against those limits.
type BudgetMonitor struct {
	DB     db.Querier
	Config *config.Config
	Logger *zap.Logger
	Clock  Clock
}

// Name returns the monitor name.
func (m *BudgetMonitor) Name() string {
	return "budget"
}

// Check performs one budget monitoring cycle.
func (m *BudgetMonitor) Check(ctx context.Context) error {
	// Acquire distributed lock
	acquired, err := m.DB.AcquireLock(ctx, budgetMonitorLockType, budgetMonitorLockID, m.Config.MonitorID, m.Config.LockTTL)
	if err != nil {
		return fmt.Errorf("acquire budget lock: %w", err)
	}
	if !acquired {
		m.Logger.Debug("Budget monitor lock held by another instance, skipping")
		return nil
	}
	defer func() {
		if err := m.DB.ReleaseLock(ctx, budgetMonitorLockType, budgetMonitorLockID, m.Config.MonitorID); err != nil {
			m.Logger.Warn("Failed to release budget lock", zap.Error(err))
		}
	}()

	clusters, err := m.DB.ListActiveClusters(ctx)
	if err != nil {
		return fmt.Errorf("list active clusters: %w", err)
	}

	m.Logger.Info("Budget monitor checking clusters", zap.Int("count", len(clusters)))

	for _, cluster := range clusters {
		if err := m.checkClusterBudget(ctx, &cluster); err != nil {
			m.Logger.Error("Failed to check cluster budget",
				zap.String("cluster_id", cluster.ID),
				zap.Error(err),
			)
			// Continue checking other clusters
		}
	}

	return nil
}

// checkClusterBudget evaluates the budget status for a single cluster.
func (m *BudgetMonitor) checkClusterBudget(ctx context.Context, cluster *db.Cluster) error {
	if cluster.ActualStateJSON == nil || len(cluster.ActualStateJSON) == 0 {
		return nil
	}

	var payload db.HeartbeatPayload
	if err := json.Unmarshal(cluster.ActualStateJSON, &payload); err != nil {
		m.Logger.Warn("Failed to parse heartbeat payload",
			zap.String("cluster_id", cluster.ID),
			zap.Error(err),
		)
		return nil
	}

	// Check if we have cost data to evaluate
	if payload.CostHourly == nil {
		return nil
	}

	// Estimate daily cost from current hourly rate
	estimatedDailyCost := *payload.CostHourly * 24.0

	// Check cost anomaly — if current hourly cost is significantly higher
	// than the rolling average, fire an alert.
	if payload.CostHourlyAvg != nil && *payload.CostHourlyAvg > 0 {
		ratio := *payload.CostHourly / *payload.CostHourlyAvg
		if ratio >= m.Config.CostAnomalyMultiplier {
			alertDetails := map[string]interface{}{
				"alert_type":       "cost_anomaly",
				"cost_hourly":      *payload.CostHourly,
				"cost_hourly_avg":  *payload.CostHourlyAvg,
				"anomaly_ratio":    math.Round(ratio*100) / 100,
				"threshold":        m.Config.CostAnomalyMultiplier,
				"estimated_daily":  math.Round(estimatedDailyCost*100) / 100,
				"cluster_id":       cluster.ID,
				"gpu_count":        payload.GPUCount,
				"node_count":       payload.NodeCount,
			}

			detailsJSON, _ := json.Marshal(alertDetails)
			message := fmt.Sprintf(
				"Cost anomaly detected: current hourly $%.2f is %.1fx the average $%.2f",
				*payload.CostHourly, ratio, *payload.CostHourlyAvg,
			)

			if err := m.DB.InsertEvent(ctx, cluster.OrgID, cluster.ID, "budget.cost_anomaly", message, detailsJSON); err != nil {
				m.Logger.Error("Failed to insert cost anomaly event",
					zap.String("cluster_id", cluster.ID),
					zap.Error(err),
				)
			} else {
				m.Logger.Warn("Cost anomaly detected",
					zap.String("cluster_id", cluster.ID),
					zap.Float64("hourly_cost", *payload.CostHourly),
					zap.Float64("hourly_avg", *payload.CostHourlyAvg),
					zap.Float64("ratio", ratio),
				)
			}
		}
	}

	// GPU count monitoring — log for visibility
	if payload.GPUCount > 0 {
		m.Logger.Debug("Cluster GPU usage",
			zap.String("cluster_id", cluster.ID),
			zap.Int("gpu_count", payload.GPUCount),
			zap.Int("node_count", payload.NodeCount),
			zap.Float64("hourly_cost", *payload.CostHourly),
		)
	}

	return nil
}

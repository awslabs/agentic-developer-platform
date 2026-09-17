// Package monitors implements control-plane monitoring loops.
//
// Each monitor runs on a polling interval, queries Aurora PostgreSQL for
// resources that need attention, acquires a distributed lock per resource,
// performs monitoring, and updates the resource state.
package monitors

import (
	"context"
	"time"

	"go.uber.org/zap"

	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/db"
)

// HealthStatus constants for cluster health.
const (
	HealthStatusHealthy     = "Healthy"
	HealthStatusDegraded    = "Degraded"
	HealthStatusUnreachable = "Unreachable"
	HealthStatusUnknown     = "Unknown"
)

// HealthDimensionSeverity ranks health dimensions from least to most severe.
// Higher values indicate worse health.
var HealthDimensionSeverity = map[string]int{
	HealthStatusHealthy:     0,
	HealthStatusDegraded:    1,
	HealthStatusUnreachable: 2,
	HealthStatusUnknown:     3,
}

// Monitor is the interface all monitor implementations must satisfy.
type Monitor interface {
	// Name returns the monitor name for logging and locking.
	Name() string

	// Check performs one monitoring cycle.
	Check(ctx context.Context) error
}

// Runner manages the lifecycle of a set of monitors.
type Runner struct {
	monitors []Monitor
	interval    time.Duration
	logger      *zap.Logger
}

// NewRunner creates a new monitor runner.
func NewRunner(interval time.Duration, logger *zap.Logger, monitors ...Monitor) *Runner {
	return &Runner{
		monitors: monitors,
		interval:    interval,
		logger:      logger,
	}
}

// Run starts all monitors and blocks until the context is cancelled.
func (r *Runner) Run(ctx context.Context) error {
	r.logger.Info("Starting monitor runner",
		zap.Int("monitor_count", len(r.monitors)),
		zap.Duration("interval", r.interval),
	)

	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()

	// Run once immediately on startup.
	r.runAll(ctx)

	for {
		select {
		case <-ctx.Done():
			r.logger.Info("Monitor runner shutting down")
			return ctx.Err()
		case <-ticker.C:
			r.runAll(ctx)
		}
	}
}

// runAll executes all monitors sequentially.
func (r *Runner) runAll(ctx context.Context) {
	for _, mon := range r.monitors {
		logger := r.logger.With(zap.String("monitor", mon.Name()))
		logger.Debug("Running monitor")

		if err := mon.Check(ctx); err != nil {
			logger.Error("Monitor error", zap.Error(err))
		} else {
			logger.Debug("Monitor completed")
		}
	}
}

// Clock abstracts time for testability.
type Clock interface {
	Now() time.Time
}

// RealClock uses the real system time.
type RealClock struct{}

// Now returns the current UTC time.
func (RealClock) Now() time.Time { return time.Now().UTC() }

// EKSProber checks whether a cluster's K8s API is reachable.
type EKSProber interface {
	// ProbeEKS tests connectivity to the EKS cluster's K8s API.
	// Returns nil if reachable, error describing the issue otherwise.
	ProbeEKS(ctx context.Context, cluster *db.Cluster) error
}

// NoopEKSProber always reports the cluster as reachable.
// Used as default when cross-account K8s probing is not configured.
type NoopEKSProber struct{}

// ProbeEKS always returns nil (cluster reachable).
func (NoopEKSProber) ProbeEKS(_ context.Context, _ *db.Cluster) error { return nil }

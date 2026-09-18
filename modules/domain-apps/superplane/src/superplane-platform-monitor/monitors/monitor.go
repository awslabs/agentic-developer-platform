// Package monitors implements control-plane monitoring loops.
//
// Each monitor runs on a polling interval, queries Aurora PostgreSQL for
// resources that need attention, acquires a distributed lock per resource,
// performs monitoring, and updates the resource state.
package monitors

import (
	"context"
	"errors"
	"time"

	"go.uber.org/zap"

	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/db"
)

// HealthStatus constants for cluster health.
//
// HealthStatusNotChecked exists because "we did not look" is not a health state,
// and every other value in this list is a claim about the cluster. Before issue
// #5056 a check with no data to inspect reported Healthy, which meant an absent
// probe and a passing probe were indistinguishable downstream — the exact
// condition R11 acceptance 4 forbids ("a probe never reports health it did not
// check"). It maps to the contract's `not_checked`.
const (
	HealthStatusHealthy     = "Healthy"
	HealthStatusNotChecked  = "NotChecked"
	HealthStatusDegraded    = "Degraded"
	HealthStatusUnreachable = "Unreachable"
	HealthStatusUnknown     = "Unknown"
)

// HealthDimensionSeverity ranks health dimensions from least to most severe.
// Higher values indicate worse health.
//
// Unreachable outranks Unknown, corrected in #5056. The previous ordering put
// Unknown (3) above Unreachable (2), so a cluster that was provably unreachable
// on one dimension and merely unexplained on another aggregated to "Unknown" —
// downgrading a confirmed failure to an open question, and hiding the outage that
// the escalation path keys on. A definite negative result is worse news than a
// missing one.
//
// NotChecked sits just above Healthy: it must not mask a real Degraded or worse
// finding from another dimension, but it must also never be mistaken for Healthy.
// Matches `SEVERITY_RANK` in the contract's health module.
var HealthDimensionSeverity = map[string]int{
	HealthStatusHealthy:     0,
	HealthStatusNotChecked:  1,
	HealthStatusDegraded:    2,
	HealthStatusUnknown:     3,
	HealthStatusUnreachable: 4,
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

// ErrProbeNotPerformed reports that no probe was attempted, as distinct from a
// probe that ran and failed. Callers must translate it to NotChecked rather than
// to a health verdict; `errors.Is` distinguishes it from a real probe error.
var ErrProbeNotPerformed = errors.New("probe not performed")

// UnconfiguredEKSProber performs no probe and says so.
//
// It replaces `NoopEKSProber`, which returned nil — indistinguishable from a
// successful probe, so every cluster in an unconfigured deployment was reported
// as having a reachable K8s API that nothing had contacted. That is the defect
// R11 acceptance 4 names at main.go:76, and it was the default wiring, so the
// false "reachable" was what production actually reported.
//
// Named for what it is rather than for doing nothing: a "noop" prober sounds
// harmless, while an unconfigured one obviously cannot answer the question.
type UnconfiguredEKSProber struct{}

// ProbeEKS reports that no probe was performed.
func (UnconfiguredEKSProber) ProbeEKS(_ context.Context, _ *db.Cluster) error {
	return ErrProbeNotPerformed
}

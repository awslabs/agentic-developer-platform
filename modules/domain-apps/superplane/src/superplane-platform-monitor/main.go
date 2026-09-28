// Package main is the entry point for the Superplane Platform Monitor.
//
// The platform monitor runs on the control plane and monitors cluster health by
// evaluating heartbeat data. It implements the monitor pattern with distributed
// locking, and since issue #5056 (U15) it reaches all of that through the API's
// authenticated observation contract rather than through Aurora directly.
//
// Monitors:
//   - ClusterHealthMonitor: Monitors 6 health dimensions per cluster
//   - BudgetMonitor: Detects cost anomalies
//
// Architecture:
//   - Polls the observation API on a configurable interval (default 30s)
//   - Leases (not `reconcile_locks` rows) provide distributed locking
//   - Exposes /healthz and /metrics endpoints
//
// This binary holds no database credential. That is the point of #5056: with the
// monitor's direct table grant withdrawn, monitoring continues, which proves the
// direct-write path is gone rather than merely unused.
package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"go.uber.org/zap"

	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/config"
	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/db"
	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/monitors"
)

func main() {
	// Initialize logger.
	logger, err := zap.NewProduction()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to create logger: %v\n", err)
		os.Exit(1)
	}
	defer logger.Sync()

	// Load configuration.
	cfg, err := config.LoadFromEnv()
	if err != nil {
		logger.Fatal("Failed to load configuration", zap.Error(err))
	}

	// Adjust log level.
	if cfg.LogLevel == "debug" {
		logger, _ = zap.NewDevelopment()
	}

	logger.Info("Starting Superplane Platform Monitor",
		zap.String("monitor_id", cfg.MonitorID),
		zap.Duration("poll_interval", cfg.PollInterval),
		zap.String("metrics_addr", cfg.MetricsAddr),
	)

	// Create context with graceful shutdown.
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	// Build the observation client. Since #5056 this is an authenticated API
	// client, not a database pool: the monitor holds no database credential, so
	// withdrawing its table grant does not affect this process.
	dbClient, err := db.NewClient(db.ClientConfig{
		BaseURL:    cfg.ObservationAPIURL,
		Credential: cfg.ObservationCredential,
		SigningKey: cfg.ObservationSigningKey,
		Reporter:   "superplane-platform-monitor",
		InstanceID: cfg.MonitorID,
	})
	if err != nil {
		// The error names which setting is missing and never its value; the
		// credential and signing key must not reach the log.
		logger.Fatal("Failed to build observation client", zap.Error(err))
	}
	defer dbClient.Close()

	// Only the URL is logged. Confirming reachability is /healthz's job, which
	// calls the scoped list route and therefore also exercises authentication.
	logger.Info("Observation client ready", zap.String("api_url", cfg.ObservationAPIURL))

	// Initialize monitors.
	clusterHealth := &monitors.ClusterHealthMonitor{
		DB:     dbClient,
		Config: cfg,
		Logger: logger.Named("cluster-health"),
		Clock:  monitors.RealClock{},
		// No real prober exists yet (a cross-account assume-role probe is still
		// to be built), so the wiring says so instead of returning nil: with
		// UnconfiguredEKSProber the eks_reachability dimension reports
		// NotChecked rather than claiming a reachable K8s API nothing contacted.
		EKSProber: &monitors.UnconfiguredEKSProber{},
	}

	budgetMonitor := &monitors.BudgetMonitor{
		DB:     dbClient,
		Config: cfg,
		Logger: logger.Named("budget"),
		Clock:  monitors.RealClock{},
	}

	runner := monitors.NewRunner(cfg.PollInterval, logger, clusterHealth, budgetMonitor)

	// Start health/metrics server.
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if err := dbClient.Ping(r.Context()); err != nil {
			// "unhealthy" rather than a reason: this response is reachable by
			// anything that can hit the pod's metrics port, and distinguishing
			// "credential rejected" from "API down" here would leak the state of
			// our authorization to a caller that has not authenticated at all.
			logger.Warn("Health check failed", zap.Error(err))
			http.Error(w, "observation API unavailable", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, "ok")
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, "ok")
	})

	server := &http.Server{
		Addr:    cfg.MetricsAddr,
		Handler: mux,
	}

	go func() {
		logger.Info("Starting health server", zap.String("addr", cfg.MetricsAddr))
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("Health server error", zap.Error(err))
		}
	}()

	// Run monitors (blocks until context is cancelled).
	if err := runner.Run(ctx); err != nil && err != context.Canceled {
		logger.Error("Monitor runner exited with error", zap.Error(err))
	}

	// Graceful shutdown.
	logger.Info("Shutting down health server")
	if err := server.Close(); err != nil {
		logger.Error("Failed to close health server", zap.Error(err))
	}

	logger.Info("Superplane Platform Monitor stopped")
}

// Package main is the entry point for the Superplane Platform Monitor.
//
// The platform monitor runs on the control plane and monitors cluster health
// by evaluating heartbeat data stored in Aurora PostgreSQL. It implements
// the monitor pattern with distributed locking via the reconcile_locks table.
//
// Monitors:
//   - ClusterHealthMonitor: Monitors 6 health dimensions per cluster
//
// Architecture:
//   - Polls Aurora PostgreSQL on a configurable interval (default 30s)
//   - Uses reconcile_locks table for distributed locking
//   - Exposes /healthz and /metrics endpoints
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

	// Connect to database.
	dbClient, err := db.NewClient(ctx, cfg.DatabaseURL)
	if err != nil {
		logger.Fatal("Failed to connect to database", zap.Error(err))
	}
	defer dbClient.Close()

	logger.Info("Connected to database")

	// Initialize monitors.
	clusterHealth := &monitors.ClusterHealthMonitor{
		DB:        dbClient,
		Config:    cfg,
		Logger:    logger.Named("cluster-health"),
		Clock:     monitors.RealClock{},
		EKSProber: &monitors.NoopEKSProber{}, // TODO: implement real EKS prober with cross-account assume-role
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
			http.Error(w, "database unreachable", http.StatusServiceUnavailable)
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

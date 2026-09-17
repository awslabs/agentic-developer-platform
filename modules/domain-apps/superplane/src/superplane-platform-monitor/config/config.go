// Package config provides configuration loading from environment variables.
package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config holds all platform monitor configuration.
type Config struct {
	// DatabaseURL is the PostgreSQL connection string (e.g., postgres://user:pass@host:5432/db).
	DatabaseURL string

	// PollInterval is how often the monitor polls for clusters to check.
	PollInterval time.Duration

	// HeartbeatDegradedThreshold is the duration after which a missing heartbeat marks a cluster Degraded.
	HeartbeatDegradedThreshold time.Duration

	// HeartbeatUnreachableThreshold is the duration after which a missing heartbeat marks a cluster Unreachable.
	HeartbeatUnreachableThreshold time.Duration

	// CostAnomalyMultiplier is the threshold multiplier for cost anomaly detection (e.g., 2.0 = 2x).
	CostAnomalyMultiplier float64

	// LockTTL is how long a monitor lock is held before expiry.
	LockTTL time.Duration

	// MonitorID identifies this monitor instance for distributed locking.
	MonitorID string

	// MetricsAddr is the address for the metrics/health endpoint.
	MetricsAddr string

	// LogLevel is the logging level (debug, info, warn, error).
	LogLevel string
}

// LoadFromEnv loads configuration from environment variables with sensible defaults.
func LoadFromEnv() (*Config, error) {
	dbURL := os.Getenv("DATABASE_URL")
	if dbURL == "" {
		return nil, fmt.Errorf("DATABASE_URL environment variable is required")
	}

	cfg := &Config{
		DatabaseURL:                   dbURL,
		PollInterval:                  durationEnv("POLL_INTERVAL", 30*time.Second),
		HeartbeatDegradedThreshold:    durationEnv("HEARTBEAT_DEGRADED_THRESHOLD", 5*time.Minute),
		HeartbeatUnreachableThreshold: durationEnv("HEARTBEAT_UNREACHABLE_THRESHOLD", 30*time.Minute),
		CostAnomalyMultiplier:         floatEnv("COST_ANOMALY_MULTIPLIER", 2.0),
		LockTTL:                       durationEnv("LOCK_TTL", 2*time.Minute),
		MonitorID:                     stringEnv("MONITOR_ID", hostname()),
		MetricsAddr:                   stringEnv("METRICS_ADDR", ":9090"),
		LogLevel:                      stringEnv("LOG_LEVEL", "info"),
	}

	return cfg, nil
}

func stringEnv(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

func durationEnv(key string, defaultVal time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		d, err := time.ParseDuration(v)
		if err == nil {
			return d
		}
	}
	return defaultVal
}

func floatEnv(key string, defaultVal float64) float64 {
	if v := os.Getenv(key); v != "" {
		f, err := strconv.ParseFloat(v, 64)
		if err == nil {
			return f
		}
	}
	return defaultVal
}

func hostname() string {
	h, err := os.Hostname()
	if err != nil {
		return "monitor-unknown"
	}
	return h
}

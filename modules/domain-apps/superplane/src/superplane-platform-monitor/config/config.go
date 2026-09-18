// Package config provides configuration loading from environment variables.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config holds all platform monitor configuration.
type Config struct {
	// ObservationAPIURL is the base URL of the API's observation receiver, e.g.
	// https://superplane-api.superplane-system.svc.cluster.local:8000.
	//
	// This replaced DatabaseURL in issue #5056. The monitor no longer holds a
	// PostgreSQL connection string at all, which is what makes withdrawing its
	// table grant a no-op for this process: there is no credential here that a
	// grant could apply to.
	ObservationAPIURL string

	// ObservationCredential authenticates this monitor to the receiver.
	//
	// Read from the environment, which reads it from the deployment's secret
	// store. Never logged, never included in an error message: the receiver's
	// refusals are deliberately non-enumerating, and echoing the credential we
	// sent would undo that.
	ObservationCredential string

	// ObservationSigningKey is the HMAC key for the submission body signature.
	//
	// Separate from the credential because they answer different questions — the
	// credential says who is calling, the signature says this body is the one that
	// caller sent. Also never logged.
	ObservationSigningKey []byte

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
//
// The three observation settings are required and have no defaults. A monitor
// that starts without them would run its polling loop and fail every call, which
// looks like an outage of the API rather than a misconfiguration of the monitor;
// failing at startup names the actual problem. There is deliberately no fallback
// to DATABASE_URL — that fallback is exactly what would keep the direct-write
// path alive past the grant withdrawal.
func LoadFromEnv() (*Config, error) {
	apiURL := os.Getenv("OBSERVATION_API_URL")
	if apiURL == "" {
		return nil, fmt.Errorf("OBSERVATION_API_URL environment variable is required")
	}
	credential := os.Getenv("OBSERVATION_CREDENTIAL")
	if credential == "" {
		return nil, fmt.Errorf("OBSERVATION_CREDENTIAL environment variable is required")
	}
	signingKey := os.Getenv("OBSERVATION_SIGNING_KEY")
	if signingKey == "" {
		return nil, fmt.Errorf("OBSERVATION_SIGNING_KEY environment variable is required")
	}

	cfg := &Config{
		ObservationAPIURL:             strings.TrimRight(apiURL, "/"),
		ObservationCredential:         credential,
		ObservationSigningKey:         []byte(signingKey),
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

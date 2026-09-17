// Package db provides database access for the platform monitor.
package db

import (
	"encoding/json"
	"time"
)

// Cluster represents a row in the clusters table.
type Cluster struct {
	ID              string
	OrgID           string
	WorkspaceID     *string
	Name            string
	Status          string
	HealthStatus    *string
	LastHeartbeat   *time.Time
	ReconcileAt     *time.Time
	LastReconciledAt *time.Time
	EKSClusterARN   *string
	Endpoint        *string
	ActualStateJSON json.RawMessage
}

// HeartbeatPayload represents the parsed actual_state_json from heartbeat data.
// These fields are reported by the data plane controller in each heartbeat.
type HeartbeatPayload struct {
	// SkyPilotHealthy indicates whether the SkyPilot pod is running.
	SkyPilotHealthy *bool `json:"skypilot_healthy,omitempty"`

	// VaultSyncStatus is the status of credential sync from Vault (ok, failed, pending).
	VaultSyncStatus string `json:"vault_sync_status,omitempty"`

	// NodeSummary contains a summary of node health.
	NodeSummary *NodeSummary `json:"node_summary,omitempty"`

	// CostHourly is the current hourly cost in USD.
	CostHourly *float64 `json:"cost_hourly,omitempty"`

	// CostHourlyAvg is the rolling average hourly cost in USD (for anomaly detection).
	CostHourlyAvg *float64 `json:"cost_hourly_avg,omitempty"`

	// NodeCount is the total number of nodes.
	NodeCount int `json:"node_count,omitempty"`

	// GPUCount is the total number of GPUs.
	GPUCount int `json:"gpu_count,omitempty"`

	// ControllerVersion is the version of the data plane controller.
	ControllerVersion string `json:"controller_version,omitempty"`
}

// NodeSummary contains aggregated node health information.
type NodeSummary struct {
	Total    int `json:"total"`
	Ready    int `json:"ready"`
	NotReady int `json:"not_ready"`
}

// ReconcileLock represents a row in the reconcile_locks table.
type ReconcileLock struct {
	ResourceType string
	ResourceID   string
	LockedBy     string
	LockedAt     time.Time
	ExpiresAt    time.Time
}

package controllers

import "time"

// ClusterHeartbeat is the payload sent to the control plane API on each heartbeat tick.
type ClusterHeartbeat struct {
	ClusterID         string    `json:"cluster_id"`
	Status            string    `json:"status"`              // healthy, degraded, recovering
	ControllerVersion string    `json:"controller_version"`
	Timestamp         time.Time `json:"timestamp"`

	NodeSummary NodeSummary `json:"node_summary"`
	GPUSummary  GPUSummary  `json:"gpu_summary"`

	SkyPilotHealthy bool    `json:"skypilot_healthy"`
	VaultSyncStatus string  `json:"vault_sync_status"` // synced, pending, failed
	CostHourly      float64 `json:"cost_hourly"`
	PendingPods     int     `json:"pending_pods"`

	Alerts []HeartbeatAlert `json:"alerts"`
}

// NodeSummary aggregates SuperplaneNode counts by phase.
type NodeSummary struct {
	Total        int `json:"total"`
	Ready        int `json:"ready"`
	Provisioning int `json:"provisioning"`
	NotReady     int `json:"not_ready"`
	Draining     int `json:"draining"`
}

// GPUSummary aggregates GPU counts across nodes.
type GPUSummary struct {
	Total     int            `json:"total"`
	Allocated int            `json:"allocated"`
	Idle      int            `json:"idle"`
	Types     map[string]int `json:"types"`
}

// HeartbeatAlert is a single alert included in the heartbeat payload.
type HeartbeatAlert struct {
	Severity string `json:"severity"` // critical, warning, info
	Message  string `json:"message"`
	Resource string `json:"resource"`
}

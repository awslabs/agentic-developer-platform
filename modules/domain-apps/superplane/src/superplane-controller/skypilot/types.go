// Package skypilot provides a Go HTTP client for the SkyPilot REST API server.
package skypilot

import "time"

// DefaultBaseURL is the default SkyPilot API server address in-cluster.
const DefaultBaseURL = "http://skypilot-api.skypilot.svc.cluster.local:46580"

// HealthResponse is returned by GET /api/health.
type HealthResponse struct {
	Status  string `json:"status"`
	Version string `json:"version"`
}

// LaunchRequest is the body for POST /launch.
type LaunchRequest struct {
	// Task is an inline YAML task definition (mutually exclusive with TaskYAML file path).
	Task map[string]interface{} `json:"task,omitempty"`

	// ClusterName is the name to assign to the cluster.
	ClusterName string `json:"cluster_name,omitempty"`

	// Envs are environment variables passed to the task.
	Envs map[string]string `json:"envs,omitempty"`

	// IdleMinutesToAutostop sets auto-stop timeout in minutes.
	IdleMinutesToAutostop *int `json:"idle_minutes_to_autostop,omitempty"`

	// DryRun if true, only validates the request.
	DryRun bool `json:"dry_run,omitempty"`

	// DownAfterCancel if true, tears down cluster on cancel.
	DownAfterCancel bool `json:"down,omitempty"`
}

// RequestResponse is returned by async POST endpoints (launch, down, etc.)
// that return a request ID to track progress.
type RequestResponse struct {
	RequestID string `json:"request_id"`
}

// ClusterStatus represents the status of a SkyPilot cluster.
type ClusterStatus string

const (
	ClusterStatusInit    ClusterStatus = "INIT"
	ClusterStatusUp      ClusterStatus = "UP"
	ClusterStatusStopped ClusterStatus = "STOPPED"
)

// ClusterInfo holds information about a single cluster.
type ClusterInfo struct {
	Name         string        `json:"name"`
	Status       ClusterStatus `json:"status"`
	Handle       ClusterHandle `json:"handle"`
	LaunchedAt   int64         `json:"launched_at"`
	LastUse      string        `json:"last_use"`
	Autostop     int           `json:"autostop"`
	ToDown       bool          `json:"to_down"`
	ClusterHash  string        `json:"cluster_hash,omitempty"`
}

// ClusterHandle has connection details for a cluster.
type ClusterHandle struct {
	ClusterName       string `json:"cluster_name"`
	HeadIP            string `json:"head_ip,omitempty"`
	StableInternalIPs []string `json:"stable_internal_ips,omitempty"`
	StableExternalIPs []string `json:"stable_external_ips,omitempty"`
	NumNodes          int      `json:"num_node,omitempty"`
	LaunchedResources *LaunchedResources `json:"launched_resources,omitempty"`
}

// LaunchedResources describes the cloud resources allocated.
type LaunchedResources struct {
	Cloud        string `json:"cloud,omitempty"`
	InstanceType string `json:"instance_type,omitempty"`
	Region       string `json:"region,omitempty"`
	Zone         string `json:"zone,omitempty"`
	Accelerators string `json:"accelerators,omitempty"`
}

// StatusRequest is the body for POST /status.
type StatusRequest struct {
	ClusterNames []string `json:"cluster_names,omitempty"`
}

// StatusResponse wraps the cluster list from /status.
type StatusResponse []ClusterInfo

// DownRequest is the body for POST /down.
type DownRequest struct {
	ClusterNames []string `json:"cluster_names"`
	Purge        bool     `json:"purge,omitempty"`
}

// CloudInfo describes an enabled cloud provider.
type CloudInfo struct {
	Name    string `json:"name"`
	Enabled bool   `json:"enabled"`
}

// EnabledCloudsResponse wraps the response from GET /enabled_clouds.
type EnabledCloudsResponse struct {
	EnabledClouds []CloudInfo `json:"enabled_clouds"`
}

// StreamEvent represents a single SSE event from /api/stream.
type StreamEvent struct {
	// ID is the SSE event id.
	ID string

	// Event is the SSE event type (e.g. "message", "complete", "error").
	Event string

	// Data is the SSE data payload.
	Data string

	// IsTerminal indicates this is the last event in the stream.
	IsTerminal bool

	// ReceivedAt is when the event was received by the client.
	ReceivedAt time.Time
}

// StreamEventTypeComplete marks a successfully finished request.
const StreamEventTypeComplete = "complete"

// StreamEventTypeError marks an errored request.
const StreamEventTypeError = "error"

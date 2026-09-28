// Package provisioner implements the GPU node provisioning and onboarding logic.
package provisioner

import (
	"context"
	"fmt"
	"strings"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/log"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/adapters"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
)

const (
	// DefaultTimeout is the default timeout for the onboarding process.
	DefaultTimeout = 30 * time.Minute

	// DefaultIdleMinutesToAutostop is the default auto-stop timeout for SkyPilot clusters.
	DefaultIdleMinutesToAutostop = 120

	// DefaultDiskSizeGB is the default disk size if not specified.
	DefaultDiskSizeGB = 256
)

// OnboardResult holds the outcome of an onboarding attempt.
type OnboardResult struct {
	// Success indicates whether the onboarding completed successfully.
	Success bool

	// PublicIP is the public IP address of the provisioned node.
	PublicIP string

	// K8sNodeName is the Kubernetes node name if the node joined the cluster.
	K8sNodeName string

	// SSMInstanceID is the SSM managed instance ID if SSM was configured.
	SSMInstanceID string

	// ClusterName is the SkyPilot cluster name used for this node.
	ClusterName string

	// RequestID is the SkyPilot request ID for the launch operation.
	RequestID string

	// Error holds the error message if onboarding failed.
	Error string

	// Output holds the captured log output from the onboarding process.
	Output string
}

// SkyClient abstracts the SkyPilot API client for testing.
type SkyClient interface {
	// Launch launches a cluster and returns a request ID for tracking.
	Launch(ctx context.Context, req skypilot.LaunchRequest) (string, error)

	// StreamProgress streams SSE events for a given request ID.
	StreamProgress(ctx context.Context, requestID string) (<-chan skypilot.StreamEvent, <-chan error)

	// Status returns cluster statuses, optionally filtered by name.
	Status(ctx context.Context, clusterNames ...string) ([]skypilot.ClusterInfo, error)

	// Health checks whether the SkyPilot API server is healthy.
	Health(ctx context.Context) (*skypilot.HealthResponse, error)
}

// OnboarderConfig configures the Onboarder.
type OnboarderConfig struct {
	// Timeout is the maximum time for the onboarding process.
	// Defaults to DefaultTimeout.
	Timeout time.Duration

	// EKSClusterName is the EKS cluster that nodes will join.
	EKSClusterName string

	// AWSRegion is the AWS region of the EKS cluster.
	AWSRegion string

	// SSMActivationID is the SSM hybrid activation ID.
	SSMActivationID string

	// SSMActivationCode is the SSM hybrid activation code.
	SSMActivationCode string

	// IdleMinutesToAutostop sets the auto-stop timeout for SkyPilot clusters.
	// Defaults to DefaultIdleMinutesToAutostop.
	IdleMinutesToAutostop int
}

// Onboarder handles the process of provisioning a GPU node via the SkyPilot
// REST API and onboarding it to the EKS cluster.
type Onboarder struct {
	config    OnboarderConfig
	skyClient SkyClient
}

// NewOnboarder creates a new Onboarder with the given configuration.
func NewOnboarder(cfg OnboarderConfig, opts ...OnboarderOption) *Onboarder {
	if cfg.Timeout == 0 {
		cfg.Timeout = DefaultTimeout
	}
	if cfg.IdleMinutesToAutostop == 0 {
		cfg.IdleMinutesToAutostop = DefaultIdleMinutesToAutostop
	}
	o := &Onboarder{
		config: cfg,
	}
	for _, opt := range opts {
		opt(o)
	}
	return o
}

// OnboarderOption configures the Onboarder.
type OnboarderOption func(*Onboarder)

// WithSkyClient sets the SkyPilot API client.
func WithSkyClient(c SkyClient) OnboarderOption {
	return func(o *Onboarder) {
		o.skyClient = c
	}
}

// Onboard runs the full onboarding flow for a node:
// 1. Validates SkyPilot API health
// 2. Generates a SkyPilot task from the NodeSpec
// 3. Calls skyClient.Launch() to provision the VM via SkyPilot REST API
// 4. Streams launch progress via skyClient.StreamProgress()
// 5. Gets the node IP from skyClient.Status()
// 6. Returns the result with connection details
func (o *Onboarder) Onboard(ctx context.Context, spec adapters.NodeSpec, clusterName string, outputFn func(line string)) (*OnboardResult, error) {
	logger := log.FromContext(ctx).WithName("onboarder").WithValues("cluster", clusterName)

	if o.skyClient == nil {
		return nil, fmt.Errorf("sky client is not configured")
	}

	// Apply timeout.
	ctx, cancel := context.WithTimeout(ctx, o.config.Timeout)
	defer cancel()

	var outputBuf strings.Builder
	emit := func(msg string) {
		outputBuf.WriteString(msg)
		outputBuf.WriteString("\n")
		if outputFn != nil {
			outputFn(msg)
		}
	}

	// Step 1: Health check.
	emit("Checking SkyPilot API health...")
	_, err := o.skyClient.Health(ctx)
	if err != nil {
		return &OnboardResult{
			Success: false,
			Error:   fmt.Sprintf("SkyPilot API health check failed: %v", err),
			Output:  outputBuf.String(),
		}, nil
	}
	emit("SkyPilot API is healthy")

	// Step 2: Build the SkyPilot task from the node spec.
	task := o.buildTask(spec)
	idleMinutes := o.config.IdleMinutesToAutostop
	launchReq := skypilot.LaunchRequest{
		Task:                  task,
		ClusterName:           clusterName,
		IdleMinutesToAutostop: &idleMinutes,
		Envs:                  o.buildEnvs(spec, clusterName),
	}

	// Step 3: Launch via SkyPilot REST API.
	emit(fmt.Sprintf("Launching cluster %q via SkyPilot API...", clusterName))
	logger.Info("launching cluster via SkyPilot API", "cluster", clusterName, "gpu", spec.GPUType, "count", spec.GPUCount, "cloud", spec.Cloud)

	reqID, err := o.skyClient.Launch(ctx, launchReq)
	if err != nil {
		return &OnboardResult{
			Success:     false,
			ClusterName: clusterName,
			Error:       fmt.Sprintf("SkyPilot launch failed: %v", err),
			Output:      outputBuf.String(),
		}, nil
	}
	emit(fmt.Sprintf("Launch request submitted (request_id=%s)", reqID))
	logger.Info("launch request submitted", "requestID", reqID)

	// Step 4: Stream progress until complete or error.
	emit("Streaming launch progress...")
	if err := o.streamLaunchProgress(ctx, reqID, emit); err != nil {
		return &OnboardResult{
			Success:     false,
			ClusterName: clusterName,
			RequestID:   reqID,
			Error:       fmt.Sprintf("launch progress streaming failed: %v", err),
			Output:      outputBuf.String(),
		}, nil
	}

	// Step 5: Get node IP from SkyPilot status.
	emit("Retrieving cluster status...")
	nodeIP, err := o.getClusterIP(ctx, clusterName)
	if err != nil {
		return &OnboardResult{
			Success:     false,
			ClusterName: clusterName,
			RequestID:   reqID,
			Error:       fmt.Sprintf("failed to get cluster IP: %v", err),
			Output:      outputBuf.String(),
		}, nil
	}
	emit(fmt.Sprintf("Cluster %q is UP with IP: %s", clusterName, nodeIP))
	logger.Info("cluster is up", "cluster", clusterName, "ip", nodeIP)

	return &OnboardResult{
		Success:     true,
		PublicIP:    nodeIP,
		ClusterName: clusterName,
		RequestID:   reqID,
		Output:      outputBuf.String(),
	}, nil
}

// streamLaunchProgress consumes the SSE stream for a launch request until
// a terminal event is received or the context is cancelled.
func (o *Onboarder) streamLaunchProgress(ctx context.Context, requestID string, emit func(string)) error {
	eventCh, errCh := o.skyClient.StreamProgress(ctx, requestID)

	for {
		select {
		case event, ok := <-eventCh:
			if !ok {
				// Channel closed — check for errors.
				select {
				case err := <-errCh:
					if err != nil {
						return fmt.Errorf("stream error: %w", err)
					}
				default:
				}
				return nil
			}
			// Emit each line of the event data.
			for _, line := range strings.Split(event.Data, "\n") {
				if line != "" {
					emit(fmt.Sprintf("[sky] %s", line))
				}
			}
			if event.Event == skypilot.StreamEventTypeError {
				return fmt.Errorf("SkyPilot launch error: %s", event.Data)
			}
			if event.IsTerminal {
				return nil
			}

		case err := <-errCh:
			if err != nil {
				return fmt.Errorf("stream error: %w", err)
			}

		case <-ctx.Done():
			return fmt.Errorf("context cancelled while streaming: %w", ctx.Err())
		}
	}
}

// getClusterIP queries SkyPilot status for the cluster and returns the head IP.
func (o *Onboarder) getClusterIP(ctx context.Context, clusterName string) (string, error) {
	clusters, err := o.skyClient.Status(ctx, clusterName)
	if err != nil {
		return "", fmt.Errorf("status query failed: %w", err)
	}
	if len(clusters) == 0 {
		return "", fmt.Errorf("cluster %q not found in SkyPilot status", clusterName)
	}

	c := clusters[0]
	if c.Status != skypilot.ClusterStatusUp {
		return "", fmt.Errorf("cluster %q is not UP (status: %s)", clusterName, c.Status)
	}

	ip := c.Handle.HeadIP
	if ip == "" {
		// Try stable external IPs.
		if len(c.Handle.StableExternalIPs) > 0 {
			ip = c.Handle.StableExternalIPs[0]
		}
	}
	if ip == "" {
		return "", fmt.Errorf("cluster %q has no IP address", clusterName)
	}

	return ip, nil
}

// buildTask converts a NodeSpec into a SkyPilot task map for the Launch API.
func (o *Onboarder) buildTask(spec adapters.NodeSpec) map[string]interface{} {
	resources := map[string]interface{}{
		"cloud":        spec.Cloud,
		"accelerators": fmt.Sprintf("%s:%d", spec.GPUType, spec.GPUCount),
	}

	diskSize := spec.DiskSizeGB
	if diskSize == 0 {
		diskSize = DefaultDiskSizeGB
	}
	resources["disk_size"] = diskSize

	if spec.Region != "" {
		resources["region"] = spec.Region
	}
	if spec.UseSpot {
		resources["use_spot"] = true
	}

	return map[string]interface{}{
		"resources": resources,
	}
}

// buildEnvs creates the environment variables map passed to SkyPilot for
// post-provision onboarding (nodeadm, SSM registration, etc.).
func (o *Onboarder) buildEnvs(spec adapters.NodeSpec, clusterName string) map[string]string {
	envs := map[string]string{}

	// AWS / EKS configuration.
	if o.config.AWSRegion != "" {
		envs["AWS_REGION"] = o.config.AWSRegion
	}
	if o.config.EKSClusterName != "" {
		envs["CLUSTER_NAME"] = o.config.EKSClusterName
	}

	// Kubernetes version.
	if spec.K8sVersion != "" {
		envs["K8S_VERSION"] = spec.K8sVersion
	}

	// SSM activation — spec-level overrides global config.
	ssmID := o.config.SSMActivationID
	if spec.SSMActivationID != "" {
		ssmID = spec.SSMActivationID
	}
	ssmCode := o.config.SSMActivationCode
	if spec.SSMActivationCode != "" {
		ssmCode = spec.SSMActivationCode
	}
	if ssmID != "" {
		envs["SSM_ACTIVATION_ID"] = ssmID
	}
	if ssmCode != "" {
		envs["SSM_ACTIVATION_CODE"] = ssmCode
	}

	// SkyPilot metadata.
	envs["SKYPILOT_CLUSTER_NAME"] = clusterName
	envs["SKYPILOT_CLOUD"] = spec.Cloud
	envs["SKYPILOT_GPU_TYPE"] = spec.GPUType
	envs["SKYPILOT_GPU_COUNT"] = fmt.Sprintf("%d", spec.GPUCount)

	return envs
}

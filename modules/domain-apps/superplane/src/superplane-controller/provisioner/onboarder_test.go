package provisioner

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/adapters"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
)

// mockSkyClient implements SkyClient for testing.
type mockSkyClient struct {
	healthResp  *skypilot.HealthResponse
	healthErr   error
	launchReqID string
	launchErr   error
	launchReq   skypilot.LaunchRequest // captured request
	statusResp  []skypilot.ClusterInfo
	statusErr   error
	statusNames []string // captured cluster names

	// Stream control: events and optional error.
	streamEvents []skypilot.StreamEvent
	streamErr    error
}

func (m *mockSkyClient) Health(ctx context.Context) (*skypilot.HealthResponse, error) {
	if m.healthErr != nil {
		return nil, m.healthErr
	}
	if m.healthResp != nil {
		return m.healthResp, nil
	}
	return &skypilot.HealthResponse{Status: "healthy", Version: "0.12.0"}, nil
}

func (m *mockSkyClient) Launch(ctx context.Context, req skypilot.LaunchRequest) (string, error) {
	m.launchReq = req
	return m.launchReqID, m.launchErr
}

func (m *mockSkyClient) StreamProgress(ctx context.Context, requestID string) (<-chan skypilot.StreamEvent, <-chan error) {
	eventCh := make(chan skypilot.StreamEvent, len(m.streamEvents)+1)
	errCh := make(chan error, 1)

	go func() {
		defer close(eventCh)
		defer close(errCh)

		for _, ev := range m.streamEvents {
			select {
			case eventCh <- ev:
			case <-ctx.Done():
				errCh <- ctx.Err()
				return
			}
		}
		if m.streamErr != nil {
			errCh <- m.streamErr
		}
	}()

	return eventCh, errCh
}

func (m *mockSkyClient) Status(ctx context.Context, clusterNames ...string) ([]skypilot.ClusterInfo, error) {
	m.statusNames = clusterNames
	return m.statusResp, m.statusErr
}

// --- Tests ---

func TestOnboard_Success(t *testing.T) {
	mock := &mockSkyClient{
		launchReqID: "req-abc-123",
		streamEvents: []skypilot.StreamEvent{
			{Event: "message", Data: "Launching cluster..."},
			{Event: "message", Data: "Provisioning resources..."},
			{Event: skypilot.StreamEventTypeComplete, Data: "Cluster is ready", IsTerminal: true},
		},
		statusResp: []skypilot.ClusterInfo{
			{
				Name:   "sp-test-1",
				Status: skypilot.ClusterStatusUp,
				Handle: skypilot.ClusterHandle{
					ClusterName: "sp-test-1",
					HeadIP:      "54.123.45.67",
				},
			},
		},
	}

	o := NewOnboarder(OnboarderConfig{
		EKSClusterName:    "test-cluster",
		AWSRegion:         "us-west-2",
		SSMActivationID:   "ssm-123",
		SSMActivationCode: "ssm-code-abc",
	}, WithSkyClient(mock))

	spec := adapters.NodeSpec{
		Cloud:      "aws",
		GPUType:    "A10G",
		GPUCount:   1,
		DiskSizeGB: 256,
		K8sVersion: "1.33",
	}

	var lines []string
	result, err := o.Onboard(context.Background(), spec, "sp-test-1", func(line string) {
		lines = append(lines, line)
	})

	if err != nil {
		t.Fatalf("Onboard() error = %v", err)
	}
	if !result.Success {
		t.Errorf("expected success, got failure: %s", result.Error)
	}
	if result.PublicIP != "54.123.45.67" {
		t.Errorf("expected IP 54.123.45.67, got %s", result.PublicIP)
	}
	if result.ClusterName != "sp-test-1" {
		t.Errorf("expected cluster name sp-test-1, got %s", result.ClusterName)
	}
	if result.RequestID != "req-abc-123" {
		t.Errorf("expected request ID req-abc-123, got %s", result.RequestID)
	}
	if len(lines) == 0 {
		t.Error("expected output lines")
	}

	// Verify launch request was built correctly.
	if mock.launchReq.ClusterName != "sp-test-1" {
		t.Errorf("expected launch cluster_name=sp-test-1, got %s", mock.launchReq.ClusterName)
	}
	if mock.launchReq.IdleMinutesToAutostop == nil || *mock.launchReq.IdleMinutesToAutostop != DefaultIdleMinutesToAutostop {
		t.Errorf("expected idle_minutes=%d, got %v", DefaultIdleMinutesToAutostop, mock.launchReq.IdleMinutesToAutostop)
	}

	// Verify task resources.
	resources, ok := mock.launchReq.Task["resources"].(map[string]interface{})
	if !ok {
		t.Fatal("expected resources map in task")
	}
	if resources["cloud"] != "aws" {
		t.Errorf("expected cloud=aws, got %v", resources["cloud"])
	}
	if resources["accelerators"] != "A10G:1" {
		t.Errorf("expected accelerators=A10G:1, got %v", resources["accelerators"])
	}

	// Verify envs include onboarding config.
	envs := mock.launchReq.Envs
	if envs["AWS_REGION"] != "us-west-2" {
		t.Errorf("expected AWS_REGION=us-west-2, got %s", envs["AWS_REGION"])
	}
	if envs["CLUSTER_NAME"] != "test-cluster" {
		t.Errorf("expected CLUSTER_NAME=test-cluster, got %s", envs["CLUSTER_NAME"])
	}
	if envs["SSM_ACTIVATION_ID"] != "ssm-123" {
		t.Errorf("expected SSM_ACTIVATION_ID=ssm-123, got %s", envs["SSM_ACTIVATION_ID"])
	}
}

func TestOnboard_HealthCheckFailure(t *testing.T) {
	mock := &mockSkyClient{
		healthErr: fmt.Errorf("connection refused"),
	}

	o := NewOnboarder(OnboarderConfig{
		EKSClusterName: "test-cluster",
		AWSRegion:      "us-east-1",
	}, WithSkyClient(mock))

	spec := adapters.NodeSpec{
		Cloud:    "aws",
		GPUType:  "T4",
		GPUCount: 1,
	}

	result, err := o.Onboard(context.Background(), spec, "sp-fail-health", nil)
	if err != nil {
		t.Fatalf("Onboard() unexpected error = %v", err)
	}
	if result.Success {
		t.Error("expected failure result")
	}
	if !strings.Contains(result.Error, "health check failed") {
		t.Errorf("expected health check error, got: %s", result.Error)
	}
}

func TestOnboard_LaunchFailure(t *testing.T) {
	mock := &mockSkyClient{
		launchErr: fmt.Errorf("insufficient capacity"),
	}

	o := NewOnboarder(OnboarderConfig{
		EKSClusterName: "test-cluster",
		AWSRegion:      "us-east-1",
	}, WithSkyClient(mock))

	spec := adapters.NodeSpec{
		Cloud:    "aws",
		GPUType:  "H100",
		GPUCount: 8,
	}

	result, err := o.Onboard(context.Background(), spec, "sp-fail-launch", nil)
	if err != nil {
		t.Fatalf("Onboard() unexpected error = %v", err)
	}
	if result.Success {
		t.Error("expected failure result")
	}
	if !strings.Contains(result.Error, "launch failed") {
		t.Errorf("expected launch error, got: %s", result.Error)
	}
}

func TestOnboard_StreamError(t *testing.T) {
	mock := &mockSkyClient{
		launchReqID: "req-err-456",
		streamEvents: []skypilot.StreamEvent{
			{Event: "message", Data: "Starting..."},
			{Event: skypilot.StreamEventTypeError, Data: "Resource unavailable", IsTerminal: true},
		},
	}

	o := NewOnboarder(OnboarderConfig{
		EKSClusterName: "test-cluster",
		AWSRegion:      "us-east-1",
	}, WithSkyClient(mock))

	spec := adapters.NodeSpec{
		Cloud:    "nebius",
		GPUType:  "H100",
		GPUCount: 1,
	}

	result, err := o.Onboard(context.Background(), spec, "sp-fail-stream", nil)
	if err != nil {
		t.Fatalf("Onboard() unexpected error = %v", err)
	}
	if result.Success {
		t.Error("expected failure result")
	}
	if !strings.Contains(result.Error, "launch error") {
		t.Errorf("expected stream error, got: %s", result.Error)
	}
}

func TestOnboard_StatusNotFound(t *testing.T) {
	mock := &mockSkyClient{
		launchReqID: "req-nf-789",
		streamEvents: []skypilot.StreamEvent{
			{Event: skypilot.StreamEventTypeComplete, Data: "Done", IsTerminal: true},
		},
		statusResp: []skypilot.ClusterInfo{}, // empty — cluster not found
	}

	o := NewOnboarder(OnboarderConfig{
		EKSClusterName: "test-cluster",
		AWSRegion:      "us-east-1",
	}, WithSkyClient(mock))

	spec := adapters.NodeSpec{
		Cloud:    "aws",
		GPUType:  "A10G",
		GPUCount: 1,
	}

	result, err := o.Onboard(context.Background(), spec, "sp-notfound", nil)
	if err != nil {
		t.Fatalf("Onboard() unexpected error = %v", err)
	}
	if result.Success {
		t.Error("expected failure result")
	}
	if !strings.Contains(result.Error, "not found") {
		t.Errorf("expected 'not found' error, got: %s", result.Error)
	}
}

func TestOnboard_NoSkyClient(t *testing.T) {
	o := NewOnboarder(OnboarderConfig{
		EKSClusterName: "test-cluster",
		AWSRegion:      "us-east-1",
	})

	spec := adapters.NodeSpec{
		Cloud:    "aws",
		GPUType:  "T4",
		GPUCount: 1,
	}

	_, err := o.Onboard(context.Background(), spec, "sp-noclient", nil)
	if err == nil {
		t.Error("expected error for nil sky client")
	}
	if !strings.Contains(err.Error(), "not configured") {
		t.Errorf("expected 'not configured' error, got: %v", err)
	}
}

func TestOnboard_SSMOverrideFromSpec(t *testing.T) {
	mock := &mockSkyClient{
		launchReqID: "req-ssm-over",
		streamEvents: []skypilot.StreamEvent{
			{Event: skypilot.StreamEventTypeComplete, Data: "Done", IsTerminal: true},
		},
		statusResp: []skypilot.ClusterInfo{
			{
				Name:   "sp-ssm-test",
				Status: skypilot.ClusterStatusUp,
				Handle: skypilot.ClusterHandle{HeadIP: "10.0.0.1"},
			},
		},
	}

	o := NewOnboarder(OnboarderConfig{
		EKSClusterName:    "test-cluster",
		AWSRegion:         "us-east-1",
		SSMActivationID:   "global-id",
		SSMActivationCode: "global-code",
	}, WithSkyClient(mock))

	// Spec-level SSM should override global.
	spec := adapters.NodeSpec{
		Cloud:             "aws",
		GPUType:           "A100",
		GPUCount:          1,
		K8sVersion:        "1.33",
		SSMActivationID:   "spec-id",
		SSMActivationCode: "spec-code",
	}

	result, err := o.Onboard(context.Background(), spec, "sp-ssm-test", nil)
	if err != nil {
		t.Fatalf("Onboard() error = %v", err)
	}
	if !result.Success {
		t.Errorf("expected success, got failure: %s", result.Error)
	}

	envs := mock.launchReq.Envs
	if envs["SSM_ACTIVATION_ID"] != "spec-id" {
		t.Errorf("expected spec-level SSM_ACTIVATION_ID=spec-id, got %s", envs["SSM_ACTIVATION_ID"])
	}
	if envs["SSM_ACTIVATION_CODE"] != "spec-code" {
		t.Errorf("expected spec-level SSM_ACTIVATION_CODE=spec-code, got %s", envs["SSM_ACTIVATION_CODE"])
	}
}

func TestBuildTask(t *testing.T) {
	o := NewOnboarder(OnboarderConfig{}, WithSkyClient(&mockSkyClient{}))

	spec := adapters.NodeSpec{
		Cloud:      "nebius",
		GPUType:    "H100",
		GPUCount:   2,
		DiskSizeGB: 512,
		Region:     "eu-north1",
		UseSpot:    true,
	}

	task := o.buildTask(spec)

	resources, ok := task["resources"].(map[string]interface{})
	if !ok {
		t.Fatal("expected resources map in task")
	}
	if resources["cloud"] != "nebius" {
		t.Errorf("expected cloud=nebius, got %v", resources["cloud"])
	}
	if resources["accelerators"] != "H100:2" {
		t.Errorf("expected accelerators=H100:2, got %v", resources["accelerators"])
	}
	if resources["disk_size"] != 512 {
		t.Errorf("expected disk_size=512, got %v", resources["disk_size"])
	}
	if resources["region"] != "eu-north1" {
		t.Errorf("expected region=eu-north1, got %v", resources["region"])
	}
	if resources["use_spot"] != true {
		t.Errorf("expected use_spot=true, got %v", resources["use_spot"])
	}
}

func TestBuildTask_DefaultDiskSize(t *testing.T) {
	o := NewOnboarder(OnboarderConfig{}, WithSkyClient(&mockSkyClient{}))

	spec := adapters.NodeSpec{
		Cloud:    "aws",
		GPUType:  "T4",
		GPUCount: 1,
		// DiskSizeGB not set — should default.
	}

	task := o.buildTask(spec)
	resources := task["resources"].(map[string]interface{})
	if resources["disk_size"] != DefaultDiskSizeGB {
		t.Errorf("expected default disk_size=%d, got %v", DefaultDiskSizeGB, resources["disk_size"])
	}
}

func TestBuildEnvs(t *testing.T) {
	o := NewOnboarder(OnboarderConfig{
		EKSClusterName:    "prod-cluster",
		AWSRegion:         "us-west-2",
		SSMActivationID:   "ssm-global",
		SSMActivationCode: "ssm-global-code",
	}, WithSkyClient(&mockSkyClient{}))

	spec := adapters.NodeSpec{
		Cloud:      "aws",
		GPUType:    "A10G",
		GPUCount:   4,
		K8sVersion: "1.33",
	}

	envs := o.buildEnvs(spec, "sp-cluster-1")

	checks := map[string]string{
		"AWS_REGION":            "us-west-2",
		"CLUSTER_NAME":         "prod-cluster",
		"K8S_VERSION":          "1.33",
		"SSM_ACTIVATION_ID":    "ssm-global",
		"SSM_ACTIVATION_CODE":  "ssm-global-code",
		"SKYPILOT_CLUSTER_NAME": "sp-cluster-1",
		"SKYPILOT_CLOUD":       "aws",
		"SKYPILOT_GPU_TYPE":    "A10G",
		"SKYPILOT_GPU_COUNT":   "4",
	}

	for key, val := range checks {
		if envs[key] != val {
			t.Errorf("expected envs[%q]=%q, got %q", key, val, envs[key])
		}
	}
}

func TestDefaultConfig(t *testing.T) {
	o := NewOnboarder(OnboarderConfig{}, WithSkyClient(&mockSkyClient{}))

	if o.config.Timeout != DefaultTimeout {
		t.Errorf("expected default timeout %v, got %v", DefaultTimeout, o.config.Timeout)
	}
	if o.config.IdleMinutesToAutostop != DefaultIdleMinutesToAutostop {
		t.Errorf("expected default idle minutes %d, got %d", DefaultIdleMinutesToAutostop, o.config.IdleMinutesToAutostop)
	}
}

func TestOnboard_UsesFallbackExternalIP(t *testing.T) {
	mock := &mockSkyClient{
		launchReqID: "req-extip",
		streamEvents: []skypilot.StreamEvent{
			{Event: skypilot.StreamEventTypeComplete, Data: "Done", IsTerminal: true},
		},
		statusResp: []skypilot.ClusterInfo{
			{
				Name:   "sp-extip",
				Status: skypilot.ClusterStatusUp,
				Handle: skypilot.ClusterHandle{
					HeadIP:            "", // No head IP
					StableExternalIPs: []string{"203.0.113.5"},
				},
			},
		},
	}

	o := NewOnboarder(OnboarderConfig{
		EKSClusterName: "test-cluster",
		AWSRegion:      "us-east-1",
	}, WithSkyClient(mock))

	spec := adapters.NodeSpec{
		Cloud:    "aws",
		GPUType:  "T4",
		GPUCount: 1,
	}

	result, err := o.Onboard(context.Background(), spec, "sp-extip", nil)
	if err != nil {
		t.Fatalf("Onboard() error = %v", err)
	}
	if !result.Success {
		t.Errorf("expected success, got failure: %s", result.Error)
	}
	if result.PublicIP != "203.0.113.5" {
		t.Errorf("expected fallback IP 203.0.113.5, got %s", result.PublicIP)
	}
}

func TestOnboard_ClusterNotUp(t *testing.T) {
	mock := &mockSkyClient{
		launchReqID: "req-notup",
		streamEvents: []skypilot.StreamEvent{
			{Event: skypilot.StreamEventTypeComplete, Data: "Done", IsTerminal: true},
		},
		statusResp: []skypilot.ClusterInfo{
			{
				Name:   "sp-notup",
				Status: skypilot.ClusterStatusInit,
				Handle: skypilot.ClusterHandle{HeadIP: "10.0.0.1"},
			},
		},
	}

	o := NewOnboarder(OnboarderConfig{
		EKSClusterName: "test-cluster",
		AWSRegion:      "us-east-1",
	}, WithSkyClient(mock))

	spec := adapters.NodeSpec{
		Cloud:    "aws",
		GPUType:  "T4",
		GPUCount: 1,
	}

	result, err := o.Onboard(context.Background(), spec, "sp-notup", nil)
	if err != nil {
		t.Fatalf("Onboard() unexpected error = %v", err)
	}
	if result.Success {
		t.Error("expected failure for non-UP cluster")
	}
	if !strings.Contains(result.Error, "not UP") {
		t.Errorf("expected 'not UP' error, got: %s", result.Error)
	}
}

func TestOnboard_ContextTimeout(t *testing.T) {
	// Create a mock that blocks on stream (simulated by no terminal event and context timeout).
	mock := &mockSkyClient{
		launchReqID: "req-timeout",
		streamEvents: []skypilot.StreamEvent{
			{Event: "message", Data: "Starting..."},
			// No terminal event — will block until context times out.
		},
	}

	o := NewOnboarder(OnboarderConfig{
		EKSClusterName: "test-cluster",
		AWSRegion:      "us-east-1",
		Timeout:        500 * time.Millisecond, // Very short timeout
	}, WithSkyClient(mock))

	spec := adapters.NodeSpec{
		Cloud:    "aws",
		GPUType:  "T4",
		GPUCount: 1,
	}

	result, err := o.Onboard(context.Background(), spec, "sp-timeout", nil)
	if err != nil {
		t.Fatalf("Onboard() unexpected error = %v", err)
	}
	// Should fail due to context cancellation.
	if result.Success {
		t.Error("expected failure due to timeout")
	}
}

func TestOnboard_StreamChannelError(t *testing.T) {
	mock := &mockSkyClient{
		launchReqID: "req-cherr",
		streamEvents: []skypilot.StreamEvent{
			{Event: "message", Data: "Starting..."},
		},
		streamErr: fmt.Errorf("connection reset"),
	}

	o := NewOnboarder(OnboarderConfig{
		EKSClusterName: "test-cluster",
		AWSRegion:      "us-east-1",
	}, WithSkyClient(mock))

	spec := adapters.NodeSpec{
		Cloud:    "aws",
		GPUType:  "T4",
		GPUCount: 1,
	}

	result, err := o.Onboard(context.Background(), spec, "sp-cherr", nil)
	if err != nil {
		t.Fatalf("Onboard() unexpected error = %v", err)
	}
	if result.Success {
		t.Error("expected failure due to stream error")
	}
	if !strings.Contains(result.Error, "connection reset") {
		t.Errorf("expected 'connection reset' in error, got: %s", result.Error)
	}
}

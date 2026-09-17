package adapters

import (
	"context"
	"fmt"
	"strings"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
)

// buildSkyPilotTask converts a NodeSpec into a SkyPilot task map suitable
// for the LaunchRequest.Task field.
func buildSkyPilotTask(spec NodeSpec) map[string]interface{} {
	resources := map[string]interface{}{
		"cloud":        spec.Cloud,
		"accelerators": fmt.Sprintf("%s:%d", spec.GPUType, spec.GPUCount),
	}
	if spec.DiskSizeGB > 0 {
		resources["disk_size"] = spec.DiskSizeGB
	}
	if spec.Region != "" {
		resources["region"] = spec.Region
	}
	if spec.UseSpot {
		resources["use_spot"] = true
	}

	task := map[string]interface{}{
		"resources": resources,
	}

	// Pass onboarding-related env vars so the setup script can configure
	// the node to join the EKS hybrid cluster.
	envs := map[string]string{}
	if spec.K8sVersion != "" {
		envs["K8S_VERSION"] = spec.K8sVersion
	}
	if spec.ClusterName != "" {
		envs["CLUSTER_NAME"] = spec.ClusterName
	}
	if spec.SSMActivationID != "" {
		envs["SSM_ACTIVATION_ID"] = spec.SSMActivationID
	}
	if spec.SSMActivationCode != "" {
		envs["SSM_ACTIVATION_CODE"] = spec.SSMActivationCode
	}
	if len(envs) > 0 {
		task["envs"] = envs
	}

	return task
}

// mapClusterStatus converts a SkyPilot ClusterStatus to an adapter NodeStatus.
func mapClusterStatus(s skypilot.ClusterStatus) NodeStatus {
	switch s {
	case skypilot.ClusterStatusInit:
		return NodeStatusProvisioning
	case skypilot.ClusterStatusUp:
		return NodeStatusRunning
	case skypilot.ClusterStatusStopped:
		return NodeStatusStopped
	default:
		return NodeStatusUnknown
	}
}

// clusterRegion extracts the region from a ClusterInfo's launched resources.
func clusterRegion(c skypilot.ClusterInfo) string {
	if c.Handle.LaunchedResources != nil {
		return c.Handle.LaunchedResources.Region
	}
	return ""
}

// NewAdaptersFromClient creates all three cloud adapters using the same
// SkyPilot client. This is the typical initialization path for the controller.
func NewAdaptersFromClient(client *skypilot.Client) []CloudAdapter {
	return []CloudAdapter{
		NewNebiusAdapter(client),
		NewLambdaAdapter(client),
		NewAWSAdapter(client),
	}
}

// AdapterForCloud returns the adapter matching the given cloud name,
// or an error if not found.
func AdapterForCloud(adapters []CloudAdapter, cloud string) (CloudAdapter, error) {
	for _, a := range adapters {
		if a.Name() == cloud {
			return a, nil
		}
	}
	return nil, fmt.Errorf("no adapter found for cloud %q", cloud)
}

// isCloudEnabled queries SkyPilot's enabled_clouds endpoint to check if a
// specific cloud provider is enabled. This is used as a fallback when a GPU
// type is not in the static pricing map — if the cloud is enabled in SkyPilot,
// we assume it can provision the GPU type and let SkyPilot handle placement.
func isCloudEnabled(ctx context.Context, client *skypilot.Client, cloudName string) (bool, error) {
	clouds, err := client.EnabledClouds(ctx)
	if err != nil {
		return false, fmt.Errorf("check enabled clouds: %w", err)
	}

	for _, c := range clouds {
		if strings.EqualFold(c.Name, cloudName) && c.Enabled {
			return true, nil
		}
	}
	return false, nil
}

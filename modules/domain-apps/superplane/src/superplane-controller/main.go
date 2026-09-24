// Superplane runs one registration manager. Workspace workers receive only the
// authenticated shared-executor socket; the legacy provider loops are not wired.
package main

import (
	"encoding/json"
	"flag"
	"os"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/execution"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/management"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
)

func main() {
	var preflight, managementOnly bool
	var apiURL, healthAddress, executionTask string
	flag.StringVar(&executionTask, "execution-task-file", os.Getenv("SUPERPLANE_TASK_ASSIGNMENT_FILE"), "Execute one paid pod-local socket assignment.")
	flag.BoolVar(&preflight, "installation-preflight", false, "Report controller support and configured execution capability without contacting Kubernetes.")
	flag.BoolVar(&managementOnly, "management-only", false, "Start the registration manager with optional explicitly configured execution service.")
	flag.StringVar(&apiURL, "control-plane-api-url", os.Getenv("CONTROL_PLANE_API_URL"), "Explicit Superplane API origin.")
	flag.StringVar(&healthAddress, "health-probe-bind-address", ":8081", "Health listener address.")
	opts := zap.Options{}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()
	if executionTask != "" {
		ctx := ctrl.SetupSignalHandler()
		_ = execution.RunTask(ctx, executionTask, os.Getenv("SUPERPLANE_EXECUTION_SOCKET"), os.Getenv("SUPERPLANE_EXECUTION_CREDENTIALS_DIR"))
		// Native Kubernetes sidecar lifetime belongs to the trusted main worker.
		// Stay idle after one attempt; a container restart is never a retry grant.
		<-ctx.Done()
		return
	}
	configured := os.Getenv("SUPERPLANE_EXECUTION_SOCKET") != "" && os.Getenv("SUPERPLANE_EXECUTION_CREDENTIALS_DIR") != "" && os.Getenv("SUPERPLANE_CONTROLLER_INSTANCE_FILE") != ""
	if preflight {
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{
			"controller_management": true, "durable_registry": true, "workspace_observation": true,
			"authenticated_observations": true, "governed_execution_supported": true,
			"governed_provisioning": false, "workspace_ready": false,
		})
		return
	}
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	logger := ctrl.Log.WithName("setup")
	manager, err := management.New(management.Config{
		APIURL: apiURL, OrgID: os.Getenv("SUPERPLANE_ORG_ID"),
		CredentialFile:            os.Getenv("SUPERPLANE_REGISTRY_CREDENTIAL_FILE"),
		WorkspaceCredentialsDir:   os.Getenv("SUPERPLANE_WORKSPACE_CREDENTIALS_DIR"),
		ManagementAPIServer:       os.Getenv("SUPERPLANE_MANAGEMENT_API_SERVER"),
		EnableExecution:           configured,
		ExecutionSocket:           os.Getenv("SUPERPLANE_EXECUTION_SOCKET"),
		ExecutionCredentialsDir:   os.Getenv("SUPERPLANE_EXECUTION_CREDENTIALS_DIR"),
		InstanceFile:              os.Getenv("SUPERPLANE_CONTROLLER_INSTANCE_FILE"),
		ObservationCredentialsDir: os.Getenv("SUPERPLANE_WORKSPACE_OBSERVATIONS_DIR"),
	})
	if err != nil {
		logger.Error(err, "controller configuration refused")
		os.Exit(1)
	}
	logger.Info("starting controller registration manager", "executionConfigured", configured)
	if err := manager.Run(ctrl.SetupSignalHandler(), healthAddress); err != nil {
		logger.Error(err, "controller stopped")
		os.Exit(1)
	}
}

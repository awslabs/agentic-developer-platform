// Package main is the entry point for the Superplane Controller.
//
// The controller runs as a single Deployment in kube-system and manages all
// controller loops: PodWatcher, Provisioner, HealthMonitor, Consolidator, and HeartbeatSender.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"os"
	"time"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/adapters"
	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/controllers"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/management"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/provisioner"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(superplanev1.AddToScheme(scheme))
}

func main() {
	var (
		installationPreflight bool
		managementOnly        bool
		metricsAddr           string
		healthProbeAddr       string
		enableLeaderElection  bool
		skypilotURL           string
		eksClusterName        string
		awsRegion             string
		ssmActivationID       string
		ssmActivationCode     string
		controlPlaneAPIURL    string
		clusterID             string
		heartbeatInterval     time.Duration
	)

	flag.BoolVar(&installationPreflight, "installation-preflight", false, "Report installed production integration capabilities without contacting Kubernetes.")
	flag.BoolVar(&managementOnly, "management-only", false, "Run the authenticated registration manager without workspace execution.")
	flag.StringVar(&metricsAddr, "metrics-bind-address", ":8080", "The address the metric endpoint binds to.")
	flag.StringVar(&healthProbeAddr, "health-probe-bind-address", ":8081", "The address the health probe endpoint binds to.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false,
		"Enable leader election for controller manager. "+
			"Enabling this will ensure there is only one active controller manager.")
	flag.StringVar(&skypilotURL, "skypilot-url", envOrDefault("SKYPILOT_URL", "http://skypilot-api:8000"),
		"SkyPilot API server URL.")
	flag.StringVar(&eksClusterName, "eks-cluster-name", envOrDefault("EKS_CLUSTER_NAME", ""),
		"EKS cluster name for node onboarding.")
	flag.StringVar(&awsRegion, "aws-region", envOrDefault("AWS_REGION", "us-west-2"),
		"AWS region for the EKS cluster.")
	flag.StringVar(&ssmActivationID, "ssm-activation-id", envOrDefault("SSM_ACTIVATION_ID", ""),
		"SSM hybrid activation ID.")
	flag.StringVar(&ssmActivationCode, "ssm-activation-code", envOrDefault("SSM_ACTIVATION_CODE", ""),
		"SSM hybrid activation code.")
	flag.StringVar(&controlPlaneAPIURL, "control-plane-api-url", envOrDefault("CONTROL_PLANE_API_URL", ""),
		"Control plane API URL for heartbeat reporting.")
	flag.StringVar(&clusterID, "cluster-id", envOrDefault("CLUSTER_ID", ""),
		"Cluster identifier for heartbeat reporting. Defaults to EKS cluster name.")
	flag.DurationVar(&heartbeatInterval, "heartbeat-interval", parseDurationOrDefault(envOrDefault("HEARTBEAT_INTERVAL", ""), 30*time.Second),
		"Heartbeat interval. Default 30s.")

	opts := zap.Options{Development: true}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()
	if installationPreflight {
		if managementOnly {
			_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"controller_management": true, "durable_registry": true, "workspace_observation": true, "governed_provisioning": false})
			return
		}
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"authenticated_observations": true, "authenticated_skypilot": true, "governed_provisioning": false})
		os.Exit(2)
	}

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	if managementOnly {
		manager, err := management.New(management.Config{
			APIURL:                  controlPlaneAPIURL,
			OrgID:                   os.Getenv("SUPERPLANE_ORG_ID"),
			CredentialFile:          os.Getenv("SUPERPLANE_REGISTRY_CREDENTIAL_FILE"),
			WorkspaceCredentialsDir: os.Getenv("SUPERPLANE_WORKSPACE_CREDENTIALS_DIR"),
			ManagementAPIServer:     os.Getenv("SUPERPLANE_MANAGEMENT_API_SERVER"),
		})
		if err != nil {
			setupLog.Error(err, "controller management configuration refused")
			os.Exit(1)
		}
		setupLog.Info("starting controller management; workspace execution unavailable")
		if err := manager.Run(ctrl.SetupSignalHandler(), healthProbeAddr); err != nil {
			setupLog.Error(err, "controller management stopped")
			os.Exit(1)
		}
		return
	}

	// A complete installation never starts the legacy unauthenticated path.
	if os.Getenv("SUPERPLANE_INSTALLATION_REQUIRED") == "true" {
		// B's controller execution adapter is not published yet. Authentication
		// to SkyPilot alone is not spending authority. Fail before registering
		// provider-mutating loops; never re-enable direct provisioning as fallback.
		setupLog.Error(nil, "governed controller provisioning adapter is unavailable (B/#4912)")
		os.Exit(1)

	}

	// Create controller manager.
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme: scheme,
		Metrics: metricsserver.Options{
			BindAddress: metricsAddr,
		},
		HealthProbeBindAddress:  healthProbeAddr,
		LeaderElection:          enableLeaderElection,
		LeaderElectionID:        "superplane-controller.superplane.ai",
		LeaderElectionNamespace: os.Getenv("SUPERPLANE_LEADER_NAMESPACE"),
	})
	if err != nil {
		setupLog.Error(err, "unable to create manager")
		os.Exit(1)
	}

	// Initialize SkyPilot client and cloud adapters.
	skyClient := skypilot.NewClient(skypilotURL, skypilot.WithServiceToken(os.Getenv("SKYPILOT_SERVICE_TOKEN")))
	cloudAdapters := adapters.NewAdaptersFromClient(skyClient)

	// Initialize the onboarder.
	onboarder := provisioner.NewOnboarder(provisioner.OnboarderConfig{
		EKSClusterName:    eksClusterName,
		AWSRegion:         awsRegion,
		SSMActivationID:   ssmActivationID,
		SSMActivationCode: ssmActivationCode,
	})

	// Register NodePool reconciler — sets status.phase based on spec validation.
	if err := (&controllers.NodePoolReconciler{
		Client: mgr.GetClient(),
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "NodePool")
		os.Exit(1)
	}

	// Register PodWatcher controller.
	if err := (&controllers.PodWatcherReconciler{
		Client: mgr.GetClient(),
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "PodWatcher")
		os.Exit(1)
	}

	// Register Provisioner controller.
	provisionerReconciler := controllers.NewProvisionerReconciler(
		mgr.GetClient(),
		cloudAdapters,
		onboarder,
	)
	if err := provisionerReconciler.SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "Provisioner")
		os.Exit(1)
	}

	// Register HealthMonitor controller.
	if err := (&controllers.HealthMonitorReconciler{
		Client: mgr.GetClient(),
	}).SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "HealthMonitor")
		os.Exit(1)
	}

	// Register Consolidator as a runnable (it uses a polling loop, not a reconciler).
	consolidator := controllers.NewConsolidator(
		mgr.GetClient(),
		skyClient,
		controllers.ConsolidatorConfig{},
	)
	if err := mgr.Add(consolidator); err != nil {
		setupLog.Error(err, "unable to add runnable", "runnable", "Consolidator")
		os.Exit(1)
	}

	// Register Heartbeat Sender as a runnable (periodic POST to control plane).
	if controlPlaneAPIURL != "" {
		heartbeatClusterID := clusterID
		if heartbeatClusterID == "" {
			heartbeatClusterID = eksClusterName
		}
		heartbeat := &controllers.HeartbeatSender{
			Client:                mgr.GetClient(),
			WorkspaceID:           os.Getenv("WORKSPACE_ID"),
			Credential:            os.Getenv("OBSERVATION_CREDENTIAL"),
			SigningKey:            os.Getenv("OBSERVATION_SIGNING_KEY"),
			RequireAuthentication: os.Getenv("SUPERPLANE_INSTALLATION_REQUIRED") == "true",
			SkyChecker:            &skyPilotHealthAdapter{client: skyClient},
			APIURL:                controlPlaneAPIURL,
			ClusterID:             heartbeatClusterID,
			Interval:              heartbeatInterval,
		}
		if err := mgr.Add(heartbeat); err != nil {
			setupLog.Error(err, "unable to add runnable", "runnable", "HeartbeatSender")
			os.Exit(1)
		}
		setupLog.Info("heartbeat sender registered",
			"apiURL", controlPlaneAPIURL,
			"clusterID", heartbeatClusterID,
			"interval", heartbeatInterval,
		)
	} else {
		setupLog.Info("heartbeat sender disabled (CONTROL_PLANE_API_URL not set)")
	}

	// Health checks.
	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up ready check")
		os.Exit(1)
	}

	setupLog.Info("starting manager",
		"metricsAddr", metricsAddr,
		"healthProbeAddr", healthProbeAddr,
		"leaderElection", enableLeaderElection,
		"skypilotURL", skypilotURL,
	)

	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "problem running manager")
		os.Exit(1)
	}
}

// envOrDefault returns the value of the environment variable named by key,
// or defaultVal if the variable is not set.
func envOrDefault(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

// Verify at compile time that the SkyPilot client satisfies the Consolidator's interface.
var _ controllers.SkyPilotClient = (*skypilot.Client)(nil)

// skyPilotHealthAdapter wraps the SkyPilot client to implement SkyPilotHealthChecker.
type skyPilotHealthAdapter struct {
	client *skypilot.Client
}

func (a *skyPilotHealthAdapter) HealthCheck(ctx context.Context) (bool, error) {
	resp, err := a.client.Health(ctx)
	if err != nil {
		return false, err
	}
	return resp.Status == "healthy", nil
}

// Verify at compile time that skyPilotHealthAdapter satisfies SkyPilotHealthChecker.
var _ controllers.SkyPilotHealthChecker = (*skyPilotHealthAdapter)(nil)

// parseDurationOrDefault parses a duration string, returning defaultVal on error or empty input.
func parseDurationOrDefault(s string, defaultVal time.Duration) time.Duration {
	if s == "" {
		return defaultVal
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		return defaultVal
	}
	return d
}

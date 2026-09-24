package management

import (
	"context"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/controllers"
	authorizationv1 "k8s.io/api/authorization/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/tools/clientcmd"
	runtimeclient "sigs.k8s.io/controller-runtime/pkg/client"
)

// ProviderObservation is a short-lived trusted-service health read. It grants
// no execution authority and cannot substitute for workspace credential checks.
type ProviderObservation struct {
	SkyPilotHealthy bool      `json:"skypilot_healthy"`
	CheckedAt       time.Time `json:"checked_at"`
}

type observedSkyHealth []ProviderObservation

func (observations observedSkyHealth) HealthCheck(context.Context) (bool, error) {
	now := time.Now()
	for _, observation := range observations {
		if observation.SkyPilotHealthy && !observation.CheckedAt.After(now) && now.Sub(observation.CheckedAt) < 30*time.Second {
			return true, nil
		}
	}
	return false, nil
}

func (m *Manager) inspectTarget(ctx context.Context, target Target) string {
	teardownOnly := len(target.Assignments) > 0
	for _, assignment := range target.Assignments {
		teardownOnly = teardownOnly && assignment.Action == "teardown"
	}
	retiring := target.WorkspaceStatus == "Teardown" || target.WorkspaceStatus == "retired"
	if target.WorkspaceStatus == "Deleted" || (retiring && !teardownOnly) {
		return "retired"
	}
	if (!uuidPattern.MatchString(target.ClusterID) && !(target.Provisional && target.ClusterID == "")) || len(validation.IsDNS1123Label(target.Namespace)) > 0 || target.Namespace == "" || target.ClusterARN == "" || target.Endpoint == "" {
		return "registration_incomplete"
	}
	if target.Provisional && (target.WorkspaceStatus != "Provisioning" || target.BootstrapOperationID == "" || target.RegistrationClaim == "" || len(target.Assignments) > 0) {
		return "registration_incomplete"
	}
	if !target.Provisional && target.WorkspaceStatus != "Ready" && target.WorkspaceStatus != "active" && !(retiring && teardownOnly) {
		return "workspace_not_ready"
	}
	if !target.Provisional && target.ClusterStatus != "Ready" && target.ClusterStatus != "Active" {
		return "cluster_not_ready"
	}
	if m.config.WorkspaceCredentialsDir == "" {
		return "credential_unavailable"
	}
	// Read this exact workspace's mounted credential on every reconcile. There
	// is no default loader, ambient KUBECONFIG, in-cluster config, or exec plugin.
	data, err := os.ReadFile(filepath.Join(m.config.WorkspaceCredentialsDir, target.WorkspaceID+".kubeconfig"))
	if err != nil || len(data) > 1<<20 {
		return "credential_unavailable"
	}
	kubeconfig, err := clientcmd.Load(data)
	if err != nil || len(kubeconfig.Clusters) != 1 || len(kubeconfig.AuthInfos) != 1 || len(kubeconfig.Contexts) != 1 {
		return "credential_refused"
	}
	current := kubeconfig.Contexts[kubeconfig.CurrentContext]
	if current == nil || current.Namespace != target.Namespace || kubeconfig.CurrentContext != target.ClusterARN {
		return "credential_refused"
	}
	cluster := kubeconfig.Clusters[current.Cluster]
	auth := kubeconfig.AuthInfos[current.AuthInfo]
	if cluster == nil || auth == nil {
		return "credential_refused"
	}
	u, err := url.Parse(cluster.Server)
	if err != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || cluster.Server != target.Endpoint || strings.TrimRight(cluster.Server, "/") == strings.TrimRight(m.config.ManagementAPIServer, "/") {
		return "credential_refused"
	}
	if cluster.InsecureSkipTLSVerify || cluster.ProxyURL != "" || cluster.TLSServerName != "" || cluster.CertificateAuthority != "" || len(cluster.CertificateAuthorityData) == 0 || auth.Exec != nil || auth.AuthProvider != nil || auth.TokenFile != "" || auth.ClientCertificate != "" || auth.ClientKey != "" || len(auth.ClientCertificateData) != 0 || len(auth.ClientKeyData) != 0 || auth.Username != "" || auth.Password != "" || auth.Impersonate != "" || len(auth.ImpersonateGroups) != 0 || len(auth.ImpersonateUserExtra) != 0 || auth.ImpersonateUID != "" || auth.Token == "" {
		return "credential_refused"
	}
	config, err := clientcmd.NewNonInteractiveClientConfig(*kubeconfig, kubeconfig.CurrentContext, &clientcmd.ConfigOverrides{}, nil).ClientConfig()
	if err != nil {
		return "credential_refused"
	}
	config.Timeout = 5 * time.Second
	config.Proxy = func(*http.Request) (*url.URL, error) { return nil, nil }
	client, err := kubernetes.NewForConfig(config)
	if err != nil {
		return "credential_refused"
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	ns, err := client.CoreV1().Namespaces().Get(ctx, target.Namespace, metav1.GetOptions{})
	if err != nil || ns.Status.Phase != "Active" {
		return "namespace_unavailable"
	}
	rules, err := client.AuthorizationV1().SelfSubjectRulesReviews().Create(ctx, &authorizationv1.SelfSubjectRulesReview{
		Spec: authorizationv1.SelfSubjectRulesReviewSpec{Namespace: target.Namespace},
	}, metav1.CreateOptions{})
	if err != nil || !readOnlyWorkspaceRules(rules.Status) {
		return "credential_not_read_only"
	}
	resources, err := dynamic.NewForConfig(config)
	if err != nil {
		return "credential_refused"
	}
	for _, resource := range []struct{ name, namespace string }{{"nodepools", ""}, {"superplanenodes", target.Namespace}} {
		client := resources.Resource(schema.GroupVersionResource{Group: "superplane.ai", Version: "v1", Resource: resource.name})
		var err error
		if resource.namespace == "" {
			_, err = client.List(ctx, metav1.ListOptions{Limit: 1})
		} else {
			_, err = client.Namespace(resource.namespace).List(ctx, metav1.ListOptions{Limit: 1})
		}
		if err != nil {
			return "workspace_api_unavailable"
		}
	}
	if target.Provisional {
		return "observed_execution_unavailable"
	}
	if m.config.EnableExecution {
		if m.config.ObservationCredentialsDir == "" {
			return "observation_credential_unavailable"
		}
		credential, err := os.ReadFile(filepath.Join(m.config.ObservationCredentialsDir, target.WorkspaceID+".credential"))
		if err != nil || len(credential) < 32 || len(credential) > 8192 {
			return "observation_credential_unavailable"
		}
		signingKey, err := os.ReadFile(filepath.Join(m.config.ObservationCredentialsDir, target.WorkspaceID+".signing-key"))
		if err != nil || len(signingKey) < 32 || len(signingKey) > 8192 {
			return "observation_credential_unavailable"
		}
		scheme := runtime.NewScheme()
		if clientgoscheme.AddToScheme(scheme) != nil || superplanev1.AddToScheme(scheme) != nil {
			return "observation_unavailable"
		}
		scopedClient, err := runtimeclient.New(config, runtimeclient.Options{Scheme: scheme})
		if err != nil {
			return "observation_unavailable"
		}
		heartbeat := &controllers.HeartbeatSender{
			NativeNodes: true,
			SkyChecker:  observedSkyHealth(target.ProviderObservations),
			Client:      scopedClient, Namespace: target.Namespace, WorkspaceID: target.WorkspaceID,
			ClusterID: target.ClusterID, Credential: strings.TrimSpace(string(credential)),
			SigningKey: strings.TrimSpace(string(signingKey)), RequireAuthentication: true,
			APIURL: m.config.APIURL, HTTPClient: m.client,
		}
		if heartbeat.Send(ctx, heartbeat.Collect(ctx)) != nil {
			return "observation_unavailable"
		}
	}
	// These observations prove scoped reads, not mutation/credential-delivery or
	// spending authority. Do not instantiate any legacy controller from here.
	return "observed_execution_unavailable"
}

func readOnlyWorkspaceRules(status authorizationv1.SubjectRulesReviewStatus) bool {
	if status.Incomplete || status.EvaluationError != "" {
		return false
	}
	for _, rule := range status.ResourceRules {
		for _, resource := range rule.Resources {
			// Reading a Secret or asking for a service-account token would hand
			// the manager another identity, potentially with mutation authority.
			if resource == "*" || resource == "secrets" || resource == "serviceaccounts/token" {
				return false
			}
		}
		for _, verb := range rule.Verbs {
			if verb == "get" || verb == "list" || verb == "watch" {
				// GET on exec/proxy subresources is not observation authority.
				if len(rule.APIGroups) != 1 {
					return false
				}
				for _, resource := range rule.Resources {
					coreRead := rule.APIGroups[0] == "" && (resource == "nodes" || resource == "namespaces" || resource == "pods")
					domainRead := rule.APIGroups[0] == "superplane.ai" && (resource == "nodepools" || resource == "superplanenodes")
					if !coreRead && !domainRead {
						return false
					}
				}
				continue
			}
			// Kubernetes grants these non-mutating identity reviews by default.
			selfReview := verb == "create" && len(rule.APIGroups) == 1 && len(rule.Resources) > 0
			for _, resource := range rule.Resources {
				selfReview = selfReview && ((rule.APIGroups[0] == "authorization.k8s.io" && (resource == "selfsubjectaccessreviews" || resource == "selfsubjectrulesreviews")) || (rule.APIGroups[0] == "authentication.k8s.io" && resource == "selfsubjectreviews"))
			}
			if !selfReview {
				return false
			}
		}
	}
	for _, rule := range status.NonResourceRules {
		for _, verb := range rule.Verbs {
			if verb != "get" {
				return false
			}
		}
	}
	return true
}

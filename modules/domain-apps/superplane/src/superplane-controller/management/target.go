package management

import (
	"context"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
)

func (m *Manager) inspectTarget(ctx context.Context, target Target) string {
	if target.WorkspaceStatus == "Teardown" || target.WorkspaceStatus == "Deleted" || target.WorkspaceStatus == "retired" {
		return "retired"
	}
	if !uuidPattern.MatchString(target.ClusterID) || len(validation.IsDNS1123Label(target.Namespace)) > 0 || target.Namespace == "" || target.ClusterARN == "" || target.Endpoint == "" {
		return "registration_incomplete"
	}
	if target.WorkspaceStatus != "Ready" && target.WorkspaceStatus != "active" {
		return "workspace_not_ready"
	}
	if target.ClusterStatus != "Ready" && target.ClusterStatus != "Active" {
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
	if cluster.InsecureSkipTLSVerify || cluster.ProxyURL != "" || cluster.TLSServerName != "" || cluster.CertificateAuthority != "" || len(cluster.CertificateAuthorityData) == 0 || auth.Exec != nil || auth.AuthProvider != nil || auth.TokenFile != "" || auth.ClientCertificate != "" || auth.ClientKey != "" || auth.Username != "" || auth.Password != "" || auth.Impersonate != "" || len(auth.ImpersonateGroups) != 0 || len(auth.ImpersonateUserExtra) != 0 || auth.ImpersonateUID != "" || auth.Token == "" {
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
	resources, err := dynamic.NewForConfig(config)
	if err != nil {
		return "credential_refused"
	}
	for _, resource := range []string{"nodepools", "superplanenodes"} {
		_, err := resources.Resource(schema.GroupVersionResource{Group: "superplane.ai", Version: "v1", Resource: resource}).Namespace(target.Namespace).List(ctx, metav1.ListOptions{Limit: 1})
		if err != nil {
			return "workspace_api_unavailable"
		}
	}
	// These observations prove scoped reads, not mutation/credential-delivery or
	// spending authority. Do not instantiate any legacy controller from here.
	return "observed_execution_unavailable"
}

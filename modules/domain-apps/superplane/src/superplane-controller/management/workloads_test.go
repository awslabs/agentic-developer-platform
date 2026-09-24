package management

import (
	"context"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

func TestWorkloadObservationChecksOriginalUIDAndBoundsLogs(t *testing.T) {
	owner := true
	image := "fixture/batch@sha256:" + strings.Repeat("a", 64)
	deploymentID := "33333333-3333-4333-8333-333333333333"
	digest := strings.Repeat("b", 64)
	job := batchv1.Job{TypeMeta: metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"}, ObjectMeta: metav1.ObjectMeta{Name: "training", Namespace: "tenant-a", UID: "job-uid", Labels: map[string]string{"superplane.io/workspace": workspaceID, "superplane.ai/capacity": "original-capacity"}, Annotations: map[string]string{"superplane.io/deployment": deploymentID, "superplane.io/approved-request": digest}}, Spec: batchv1.JobSpec{Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "workload", Image: image}}}}}, Status: batchv1.JobStatus{Active: 1}}
	pod := corev1.Pod{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Pod"}, ObjectMeta: metav1.ObjectMeta{Name: "training-pod", Namespace: "tenant-a", UID: "pod-uid", OwnerReferences: []metav1.OwnerReference{{Kind: "Job", UID: job.UID, Controller: &owner}}}, Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "workload", Image: image}}}, Status: corev1.PodStatus{Phase: corev1.PodRunning}}
	replaceAfterLog := false
	reads, logReads := 0, 0
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reads++
		if r.Header.Get("Authorization") != "Bearer read-only-token" {
			t.Error("wrong workspace credential")
			w.WriteHeader(403)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		if r.Method != "GET" && r.URL.Path != "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews" {
			t.Error("provider mutation attempted")
			w.WriteHeader(403)
			return
		}
		switch r.URL.Path {
		case "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews":
			_, _ = w.Write([]byte(`{"apiVersion":"authorization.k8s.io/v1","kind":"SelfSubjectRulesReview","status":{"incomplete":false,"resourceRules":[{"apiGroups":[""],"resources":["pods","pods/log"],"verbs":["get","list"]},{"apiGroups":["batch"],"resources":["jobs"],"verbs":["get"]}]}}`))
		case "/apis/batch/v1/namespaces/tenant-a/jobs/training":
			_ = json.NewEncoder(w).Encode(job)
		case "/api/v1/namespaces/tenant-a/pods":
			if r.URL.Query().Get("limit") != "33" || r.URL.Query().Get("labelSelector") != "superplane.io/workspace="+workspaceID+",superplane.ai/capacity=original-capacity" {
				t.Error("unbounded/unscoped pod list")
			}
			_ = json.NewEncoder(w).Encode(corev1.PodList{TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "PodList"}, Items: []corev1.Pod{pod}})
		case "/api/v1/namespaces/tenant-a/pods/training-pod/log":
			logReads++
			if r.URL.Query().Get("tailLines") != "100" || r.URL.Query().Get("limitBytes") != "16384" || r.URL.Query().Get("container") != "workload" {
				t.Error("unbounded log read")
			}
			_, _ = w.Write([]byte("epoch 1\nAuthorization: Bearer hidden-value\napi_key=hidden-key\n"))
			if replaceAfterLog {
				pod.UID = "replacement-pod"
			}
		case "/api/v1/namespaces/tenant-a/pods/training-pod":
			_ = json.NewEncoder(w).Encode(pod)
		default:
			t.Errorf("unexpected workspace path %s", r.URL.Path)
			w.WriteHeader(403)
		}
	}))
	defer server.Close()
	dir := t.TempDir()
	credential := filepath.Join(dir, "registry")
	_ = os.WriteFile(credential, []byte("registry-token-012345678901234567890123456789"), 0600)
	target := Target{WorkspaceID: workspaceID, ClusterID: orgID, Namespace: "tenant-a", WorkspaceStatus: "Ready", ClusterStatus: "Ready", ClusterARN: "arn:aws:eks:us-east-1:123456789012:cluster/workspace", Endpoint: server.URL}
	kubeconfig := clientcmdapi.Config{CurrentContext: target.ClusterARN, Clusters: map[string]*clientcmdapi.Cluster{"target": {Server: server.URL, CertificateAuthorityData: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw})}}, Contexts: map[string]*clientcmdapi.Context{target.ClusterARN: {Cluster: "target", AuthInfo: "reader", Namespace: target.Namespace}}, AuthInfos: map[string]*clientcmdapi.AuthInfo{"reader": {Token: "read-only-token"}}}
	raw, err := clientcmd.Write(kubeconfig)
	if err != nil {
		t.Fatal(err)
	}
	_ = os.WriteFile(filepath.Join(dir, workspaceID+".kubeconfig"), raw, 0600)
	m := &Manager{config: Config{OrgID: orgID, CredentialFile: credential, WorkspaceCredentialsDir: dir, ManagementAPIServer: "https://management.example"}, targets: map[string]Target{workspaceID: target}, snapshot: Snapshot{RegistryReady: true, InstanceID: deploymentID, FenceToken: 7, LastReconciled: time.Now(), LeaseExpiresAt: time.Now().Add(30 * time.Second)}}
	params := url.Values{"workspace_id": {workspaceID}, "deployment_id": {deploymentID}, "operation_id": {"original-operation"}, "kind": {"batch"}, "name": {"training"}, "uid": {"job-uid"}, "plan_digest": {digest}, "image": {image}, "logs": {"true"}, "pod_uid": {"pod-uid"}}
	request := func(auth string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("GET", "/workload-observation?"+params.Encode(), nil)
		r.Header.Set("Authorization", auth)
		w := httptest.NewRecorder()
		m.Handler().ServeHTTP(w, r)
		return w
	}
	if response := request("foreign"); response.Code != 401 || reads != 0 {
		t.Fatal("unauthorized read", response.Code, reads)
	}
	response := request("registry-token-012345678901234567890123456789")
	if response.Code != 200 {
		t.Fatal(response.Code, response.Body.String())
	}
	var result workloadObservation
	if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.State != "running" || len(result.Pods) != 1 || result.Logs == nil || strings.Contains(*result.Logs, "hidden-") || !strings.Contains(*result.Logs, "epoch 1") || result.FenceToken != 7 || logReads != 1 {
		t.Fatal("invalid observation", response.Body.String())
	}
	job.UID = "replacement-job"
	if response := request("registry-token-012345678901234567890123456789"); response.Code != 503 || logReads != 1 {
		t.Fatal("replacement job was read", response.Code, logReads)
	}
	job.UID = types.UID("job-uid")
	replaceAfterLog = true
	if response := request("registry-token-012345678901234567890123456789"); response.Code != 503 || strings.Contains(response.Body.String(), "epoch") {
		t.Fatal("replacement pod log escaped", response.Code)
	}
	before := reads
	m.snapshot.LeaseExpiresAt = time.Now().Add(-time.Second)
	if response := request("registry-token-012345678901234567890123456789"); response.Code != 503 || reads != before {
		t.Fatal("expired manager performed reads", response.Code)
	}
}

func TestWorkloadLogRedactionDoesNotRenderControlSequences(t *testing.T) {
	value := "plain\n\x1b[31m api-key=secret-value ASIAABCDEFGHIJKLMNOP eyJheader.payload.signature\n-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
	result := redactWorkloadLog(value)
	for _, secret := range []string{"\x1b", "secret-value", "ASIAABCDEFGHIJKLMNOP", "eyJheader", "PRIVATE KEY", "\nsecret\n"} {
		if strings.Contains(result, secret) {
			t.Fatalf("secret/control survived: %q", result)
		}
	}
	if !strings.Contains(result, "plain\n") {
		t.Fatal("ordinary output lost")
	}
}

func TestServingObservationRequiresOriginalDeploymentReplicaSetAndImage(t *testing.T) {
	owner, replicas := true, int32(1)
	image, digest := "fixture/serving@sha256:"+strings.Repeat("a", 64), strings.Repeat("b", 64)
	target := Target{WorkspaceID: workspaceID, Namespace: "tenant-a", ClusterID: orgID}
	query := workloadQuery{WorkspaceID: workspaceID, DeploymentID: orgID, OperationID: "operation", Kind: "serving", Name: "model", UID: "deployment-uid", Digest: digest, Image: image}
	deployment := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: query.Name, Namespace: target.Namespace, UID: types.UID(query.UID), Generation: 2, Labels: map[string]string{"superplane.io/workspace": workspaceID, "superplane.ai/capacity": "capacity"}, Annotations: map[string]string{"superplane.io/deployment": orgID, "superplane.io/approved-request": digest}}, Spec: appsv1.DeploymentSpec{Replicas: &replicas, Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "workload", Image: image}}}}}, Status: appsv1.DeploymentStatus{ObservedGeneration: 2, AvailableReplicas: 1}}
	set := &appsv1.ReplicaSet{ObjectMeta: metav1.ObjectMeta{Name: "owned-set", Namespace: target.Namespace, UID: "set-uid", Labels: deployment.Labels, OwnerReferences: []metav1.OwnerReference{{Kind: "Deployment", UID: deployment.UID, Controller: &owner}}}}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "model-pod", Namespace: target.Namespace, UID: "owned-pod", Labels: deployment.Labels, OwnerReferences: []metav1.OwnerReference{{Kind: "ReplicaSet", UID: set.UID, Controller: &owner}}}, Spec: deployment.Spec.Template.Spec, Status: corev1.PodStatus{Phase: corev1.PodRunning}}
	foreign := pod.DeepCopy()
	foreign.Name = "foreign-pod"
	foreign.UID = "foreign-uid"
	foreign.OwnerReferences[0].UID = "foreign-set"
	client := fake.NewSimpleClientset(deployment, set, pod, foreign)
	result, err := observeWorkload(context.Background(), client, target, query)
	if err != nil || result.State != "ready" || len(result.Pods) != 1 || result.Pods[0].UID != "owned-pod" {
		t.Fatalf("ownership projection: %+v %v", result, err)
	}
	// Matching labels alone never authorize a log read from another owner.
	query.Logs = true
	query.PodUID = "foreign-uid"
	if _, err = observeWorkload(context.Background(), client, target, query); err == nil {
		t.Fatal("foreign Pod accepted")
	}
	query.Logs = false
	query.PodUID = ""
	set.OwnerReferences[0].UID = "replacement-deployment"
	if _, err = client.AppsV1().ReplicaSets(target.Namespace).Update(context.Background(), set, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	result, err = observeWorkload(context.Background(), client, target, query)
	if err != nil || len(result.Pods) != 0 {
		t.Fatal("foreign ReplicaSet adopted", err)
	}
	deployment.Spec.Template.Spec.Containers[0].Image = "foreign/image@sha256:" + strings.Repeat("c", 64)
	if _, err = client.AppsV1().Deployments(target.Namespace).Update(context.Background(), deployment, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err = observeWorkload(context.Background(), client, target, query); err == nil {
		t.Fatal("changed workload image accepted")
	}
}

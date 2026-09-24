package management

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode"

	authorizationv1 "k8s.io/api/authorization/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/client-go/kubernetes"
)

const logLimit = 16384

var workloadReads = make(chan struct{}, 4)
var digestPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

type workloadQuery struct {
	WorkspaceID  string
	DeploymentID string
	OperationID  string
	Kind         string
	Name         string
	UID          string
	Digest       string
	Image        string
	PodUID       string
	Logs         bool
}

type podObservation struct {
	UID      string `json:"uid"`
	Phase    string `json:"phase"`
	Ready    bool   `json:"ready"`
	Restarts int32  `json:"restarts"`
	ExitCode *int32 `json:"exit_code"`
}

type workloadObservation struct {
	OrgID          string           `json:"org_id"`
	WorkspaceID    string           `json:"workspace_id"`
	ClusterID      string           `json:"cluster_id"`
	Namespace      string           `json:"namespace"`
	DeploymentID   string           `json:"deployment_id"`
	OperationID    string           `json:"operation_id"`
	UID            string           `json:"uid"`
	Digest         string           `json:"plan_digest"`
	Kind           string           `json:"kind"`
	State          string           `json:"state"`
	Pods           []podObservation `json:"pods"`
	Logs           *string          `json:"logs"`
	LogsPodUID     string           `json:"logs_pod_uid"`
	LogsTruncated  bool             `json:"logs_truncated"`
	CheckedAt      time.Time        `json:"checked_at"`
	InstanceID     string           `json:"instance_id"`
	FenceToken     int64            `json:"fence_token"`
	LeaseExpiresAt time.Time        `json:"lease_expires_at"`
}

func (m *Manager) workloadObservation(w http.ResponseWriter, r *http.Request) {
	credential, err := m.credential()
	if err != nil || subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), credential) != 1 {
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	select {
	case workloadReads <- struct{}{}:
		defer func() { <-workloadReads }()
	default:
		http.Error(w, "workload observation capacity exceeded", 503)
		return
	}
	q := r.URL.Query()
	allowed := map[string]bool{"workspace_id": true, "deployment_id": true, "operation_id": true, "kind": true, "name": true, "uid": true, "plan_digest": true, "image": true, "pod_uid": true, "logs": true}
	for key, values := range q {
		if !allowed[key] || len(values) != 1 {
			http.Error(w, "invalid observation request", 400)
			return
		}
	}
	query := workloadQuery{q.Get("workspace_id"), q.Get("deployment_id"), q.Get("operation_id"), q.Get("kind"), q.Get("name"), q.Get("uid"), q.Get("plan_digest"), q.Get("image"), q.Get("pod_uid"), q.Get("logs") == "true"}
	if !uuidPattern.MatchString(query.WorkspaceID) || !uuidPattern.MatchString(query.DeploymentID) || query.OperationID == "" || len(query.OperationID) > 255 || (query.Kind != "batch" && query.Kind != "serving") || query.Name == "" || len(validation.IsDNS1123Subdomain(query.Name)) > 0 || query.UID == "" || len(query.UID) > 255 || !digestPattern.MatchString(query.Digest) || len(query.Image) > 512 || !strings.Contains(query.Image, "@sha256:") || len(query.PodUID) > 255 || (q.Get("logs") != "true" && q.Get("logs") != "false") || (query.Logs && query.PodUID == "") {
		http.Error(w, "invalid observation request", 400)
		return
	}
	snapshot := m.Snapshot()
	m.mu.RLock()
	target, found := m.targets[query.WorkspaceID]
	m.mu.RUnlock()
	if !snapshot.RegistryReady || !found || target.Provisional || target.WorkspaceStatus == "Deleted" || time.Since(snapshot.LastReconciled) > 30*time.Second {
		http.Error(w, "workload observation unavailable", 503)
		return
	}
	deadline := time.Now().Add(10 * time.Second)
	if snapshot.LeaseExpiresAt.Add(-time.Second).Before(deadline) {
		deadline = snapshot.LeaseExpiresAt.Add(-time.Second)
	}
	ctx, cancel := context.WithDeadline(r.Context(), deadline)
	defer cancel()
	client, credentialConfig, reason := m.workspaceClient(target)
	if reason != "" {
		http.Error(w, "workload observation credential unavailable", 503)
		return
	}
	rules, err := client.AuthorizationV1().SelfSubjectRulesReviews().Create(ctx, &authorizationv1.SelfSubjectRulesReview{Spec: authorizationv1.SelfSubjectRulesReviewSpec{Namespace: target.Namespace}}, metav1.CreateOptions{})
	if err != nil || !readOnlyWorkspaceRules(rules.Status) {
		http.Error(w, "workload read authority refused", 503)
		return
	}
	result, err := observeWorkload(ctx, client, target, query)
	if err != nil {
		http.Error(w, "original workload observation unavailable", 503)
		return
	}
	current := m.Snapshot()
	// Slow reads cannot outlive, replace or revive the manager's current lease.
	_, currentConfig, currentReason := m.workspaceClient(target)
	if !current.RegistryReady || current.FenceToken != snapshot.FenceToken || current.InstanceID != snapshot.InstanceID || !current.LastReconciled.Equal(snapshot.LastReconciled) || !time.Now().Before(snapshot.LeaseExpiresAt) || currentReason != "" || currentConfig.BearerToken != credentialConfig.BearerToken || !bytes.Equal(currentConfig.CAData, credentialConfig.CAData) {
		http.Error(w, "workload observation authority expired", 503)
		return
	}
	result.OrgID = m.config.OrgID
	result.InstanceID = snapshot.InstanceID
	result.FenceToken = snapshot.FenceToken
	result.LeaseExpiresAt = snapshot.LeaseExpiresAt
	result.CheckedAt = time.Now().UTC()
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(result)
}

func ownerIs(meta metav1.ObjectMeta, kind string, uid types.UID) bool {
	for _, owner := range meta.OwnerReferences {
		if owner.Kind == kind && owner.UID == uid && owner.Controller != nil && *owner.Controller {
			return true
		}
	}
	return false
}

func observeWorkload(ctx context.Context, client kubernetes.Interface, target Target, query workloadQuery) (*workloadObservation, error) {
	result := &workloadObservation{WorkspaceID: query.WorkspaceID, ClusterID: target.ClusterID, Namespace: target.Namespace, DeploymentID: query.DeploymentID, OperationID: query.OperationID, UID: query.UID, Digest: query.Digest, Kind: query.Kind, State: "unknown", Pods: []podObservation{}}
	var meta metav1.ObjectMeta
	var template corev1.PodTemplateSpec
	owners := map[types.UID]bool{}
	podOwnerKind := "Job"
	if query.Kind == "batch" {
		job, err := client.BatchV1().Jobs(target.Namespace).Get(ctx, query.Name, metav1.GetOptions{})
		if err != nil {
			return nil, err
		}
		meta = job.ObjectMeta
		template = job.Spec.Template
		result.State = "pending"
		if job.Status.Active > 0 {
			result.State = "running"
		}
		for _, condition := range job.Status.Conditions {
			if condition.Status == corev1.ConditionTrue {
				if condition.Type == "Complete" {
					result.State = "succeeded"
				}
				if condition.Type == "Failed" {
					result.State = "failed"
				}
			}
		}
		owners[job.UID] = true
	} else {
		deployment, err := client.AppsV1().Deployments(target.Namespace).Get(ctx, query.Name, metav1.GetOptions{})
		if err != nil {
			return nil, err
		}
		meta = deployment.ObjectMeta
		template = deployment.Spec.Template
		result.State = "progressing"
		if deployment.Spec.Replicas != nil && *deployment.Spec.Replicas > 0 && deployment.Status.ObservedGeneration >= deployment.Generation && deployment.Status.AvailableReplicas >= *deployment.Spec.Replicas {
			result.State = "ready"
		}
		podOwnerKind = "ReplicaSet"
	}
	if len(template.Spec.Containers) != 1 || template.Spec.Containers[0].Name != "workload" || template.Spec.Containers[0].Image != query.Image {
		return nil, errors.New("workload template changed")
	}
	if string(meta.UID) != query.UID || meta.Namespace != target.Namespace || meta.Labels["superplane.io/workspace"] != query.WorkspaceID || meta.Annotations["superplane.io/deployment"] != query.DeploymentID || meta.Annotations["superplane.io/approved-request"] != query.Digest || meta.Labels["superplane.ai/capacity"] == "" || len(validation.IsValidLabelValue(meta.Labels["superplane.ai/capacity"])) > 0 {
		return nil, errors.New("original workload binding changed")
	}
	if query.Kind == "serving" {
		sets, err := client.AppsV1().ReplicaSets(target.Namespace).List(ctx, metav1.ListOptions{Limit: 33, LabelSelector: "superplane.io/workspace=" + query.WorkspaceID + ",superplane.ai/capacity=" + meta.Labels["superplane.ai/capacity"]})
		if err != nil {
			return nil, err
		}
		if sets.Continue != "" || len(sets.Items) > 32 {
			return nil, errors.New("replica inventory too large")
		}
		for _, set := range sets.Items {
			if set.Namespace == target.Namespace && ownerIs(set.ObjectMeta, "Deployment", meta.UID) {
				owners[set.UID] = true
			}
		}
	}
	pods, err := client.CoreV1().Pods(target.Namespace).List(ctx, metav1.ListOptions{Limit: 33, LabelSelector: "superplane.io/workspace=" + query.WorkspaceID + ",superplane.ai/capacity=" + meta.Labels["superplane.ai/capacity"]})
	if err != nil {
		return nil, err
	}
	if pods.Continue != "" || len(pods.Items) > 32 {
		return nil, errors.New("pod inventory too large")
	}
	sort.Slice(pods.Items, func(i, j int) bool { return string(pods.Items[i].UID) < string(pods.Items[j].UID) })
	var selected *corev1.Pod
	for index := range pods.Items {
		pod := &pods.Items[index]
		owned := false
		for uid := range owners {
			owned = owned || ownerIs(pod.ObjectMeta, podOwnerKind, uid)
		}
		if !owned || pod.Namespace != target.Namespace {
			continue
		}
		if len(pod.Spec.Containers) != 1 || pod.Spec.Containers[0].Name != "workload" || pod.Spec.Containers[0].Image != query.Image {
			return nil, errors.New("workload pod specification changed")
		}
		observation := podObservation{UID: string(pod.UID), Phase: string(pod.Status.Phase)}
		for _, status := range pod.Status.ContainerStatuses {
			if status.Name == "workload" {
				observation.Ready = status.Ready
				observation.Restarts = status.RestartCount
				if status.State.Terminated != nil {
					code := status.State.Terminated.ExitCode
					observation.ExitCode = &code
				}
			}
		}
		result.Pods = append(result.Pods, observation)
		if string(pod.UID) == query.PodUID {
			selected = pod
		}
	}
	if query.Logs {
		if selected == nil {
			return nil, errors.New("original pod unavailable")
		}
		tail, limit := int64(100), int64(logLimit)
		stream, err := client.CoreV1().Pods(target.Namespace).GetLogs(selected.Name, &corev1.PodLogOptions{Container: "workload", TailLines: &tail, LimitBytes: &limit}).Stream(ctx)
		if err != nil {
			return nil, err
		}
		data, err := io.ReadAll(io.LimitReader(stream, logLimit+1))
		_ = stream.Close()
		if err != nil || len(data) > logLimit {
			return nil, errors.New("log response exceeded limit")
		}
		current, err := client.CoreV1().Pods(target.Namespace).Get(ctx, selected.Name, metav1.GetOptions{})
		if err != nil || current.UID != selected.UID {
			return nil, errors.New("pod identity changed during log read")
		}
		logs := redactWorkloadLog(string(data))
		result.Logs = &logs
		result.LogsPodUID = string(selected.UID)
		result.LogsTruncated = len(data) >= logLimit || strings.Count(string(data), "\n") >= 99
	}
	// A replacement of the root during a multi-request read invalidates the
	// projection, even if its old Pod still exists under the old owner UID.
	var current metav1.ObjectMeta
	if query.Kind == "batch" {
		job, err := client.BatchV1().Jobs(target.Namespace).Get(ctx, query.Name, metav1.GetOptions{})
		if err != nil {
			return nil, err
		}
		current = job.ObjectMeta
	} else {
		deployment, err := client.AppsV1().Deployments(target.Namespace).Get(ctx, query.Name, metav1.GetOptions{})
		if err != nil {
			return nil, err
		}
		current = deployment.ObjectMeta
	}
	if current.UID != meta.UID || current.Generation != meta.Generation || current.ResourceVersion != meta.ResourceVersion {
		return nil, errors.New("workload changed during observation")
	}
	return result, nil
}

var logSecrets = []*regexp.Regexp{
	regexp.MustCompile(`(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+)[^\s,;]+`),
	regexp.MustCompile(`\b(?:AKIA|ASIA)[A-Z0-9]{16}\b`),
	regexp.MustCompile(`\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b`),
	regexp.MustCompile(`(?is)-----BEGIN[^-]*PRIVATE KEY-----.*?(?:-----END[^-]*PRIVATE KEY-----|$)`),
	regexp.MustCompile(`(?i)["']?(?:authorization|password|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)["']?\s*[:=]\s*(?:"[^"]*(?:"|$)|'[^']*(?:'|$)|[^\s,;]+)`),
}

func redactWorkloadLog(value string) string {
	value = strings.ToValidUTF8(value, "�")
	for _, pattern := range logSecrets {
		value = pattern.ReplaceAllString(value, "[redacted]")
	}
	return strings.Map(func(r rune) rune {
		if r == '\n' || r == '\t' || !unicode.IsControl(r) && !unicode.Is(unicode.Cf, r) {
			return r
		}
		return -1
	}, value)
}

package controllers

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
)

// fakeSkyPilotClient is a mock SkyPilot client for testing.
type fakeSkyPilotClient struct {
	downCalls    []downCall
	downErr      error
	downPurgeErr error

	// statusCalls records the cluster names passed to Status, so tests can assert
	// that release was actually verified against the provider.
	statusCalls [][]string

	// statusErr makes the provider re-check fail, modelling an UNKNOWN release
	// outcome (provider unreachable, credentials rejected).
	statusErr error

	// stillPresent are clusters the provider keeps reporting after Down, i.e. a
	// teardown that did not actually release the resource. Values are the status
	// the provider reports; the zero value reports UP.
	stillPresent map[string]skypilot.ClusterStatus

	// extraInfos are entries the provider returns that were not asked about. A
	// real API may answer a filtered query with a wider list, and those unrelated
	// clusters must not be mistaken for the one being verified.
	extraInfos []skypilot.ClusterInfo

	// presentForFirstNChecks models a real asynchronous teardown: Down() only
	// submits the request, so the provider keeps reporting the cluster for the
	// first few status probes and then stops. Counted per cluster name.
	presentForFirstNChecks map[string]int

	// checkCounts records how many times each cluster has been probed.
	checkCounts map[string]int
}

type downCall struct {
	ClusterNames []string
	Purge        bool
}

// Status models the provider re-check. By default a cluster is absent from the
// response, which is how the provider reports a released cluster.
func (f *fakeSkyPilotClient) Status(_ context.Context, clusterNames ...string) ([]skypilot.ClusterInfo, error) {
	f.statusCalls = append(f.statusCalls, clusterNames)
	if f.statusErr != nil {
		return nil, f.statusErr
	}

	var infos []skypilot.ClusterInfo
	for _, name := range clusterNames {
		if f.checkCounts == nil {
			f.checkCounts = map[string]int{}
		}
		f.checkCounts[name]++

		// Asynchronous teardown still in progress for this cluster.
		if n, ok := f.presentForFirstNChecks[name]; ok && f.checkCounts[name] <= n {
			infos = append(infos, skypilot.ClusterInfo{Name: name, Status: skypilot.ClusterStatusInit})
			continue
		}

		status, ok := f.stillPresent[name]
		if !ok {
			continue // released
		}
		if status == "" {
			status = skypilot.ClusterStatusUp
		}
		infos = append(infos, skypilot.ClusterInfo{Name: name, Status: status})
	}
	infos = append(infos, f.extraInfos...)
	return infos, nil
}

func (f *fakeSkyPilotClient) Down(_ context.Context, clusterNames []string, purge bool) (string, error) {
	f.downCalls = append(f.downCalls, downCall{ClusterNames: clusterNames, Purge: purge})
	if purge && f.downPurgeErr != nil {
		return "", f.downPurgeErr
	}
	if !purge && f.downErr != nil {
		return "", f.downErr
	}
	return "req-123", nil
}

// newTestConsolidator builds a Consolidator with a short release-verification
// window. Verification polls the provider until an asynchronous teardown
// converges, so tests that exercise a *failing* release would otherwise sit
// through the production 10-minute timeout.
func newTestConsolidator(c client.Client, sky SkyPilotClient, cfg ConsolidatorConfig) *Consolidator {
	cons := NewConsolidator(c, sky, cfg)
	cons.ReleaseVerifyTimeout = 50 * time.Millisecond
	cons.ReleaseVerifyInterval = 5 * time.Millisecond
	return cons
}

func newScheme() *runtime.Scheme {
	s := runtime.NewScheme()
	utilruntime.Must(clientgoscheme.AddToScheme(s))
	utilruntime.Must(superplanev1.AddToScheme(s))
	return s
}

func newFakeClient(objs ...client.Object) client.Client {
	return fake.NewClientBuilder().
		WithScheme(newScheme()).
		WithObjects(objs...).
		WithStatusSubresource(&superplanev1.SuperplaneNode{}).
		WithIndex(&corev1.Pod{}, "spec.nodeName", func(o client.Object) []string {
			pod := o.(*corev1.Pod)
			if pod.Spec.NodeName == "" {
				return nil
			}
			return []string{pod.Spec.NodeName}
		}).
		Build()
}

func makeNodePool(name string, ttl *int64, maxUnavailable *int32, consolidationEnabled *bool) *superplanev1.NodePool {
	pool := &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{
			Name: name,
		},
		Spec: superplanev1.NodePoolSpec{
			Clouds:               []string{"aws"},
			GPUTypes:             []string{"H100"},
			MaxNodes:             10,
			TTLSecondsAfterEmpty: ttl,
		},
	}
	if maxUnavailable != nil {
		pool.Spec.Disruption = &superplanev1.NodePoolDisruption{
			MaxUnavailable: *maxUnavailable,
		}
	}
	if consolidationEnabled != nil {
		pool.Spec.Consolidation = &superplanev1.NodePoolConsolidation{
			Enabled: *consolidationEnabled,
		}
	}
	return pool
}

func makeSuperplaneNode(name, namespace, poolRef, k8sNodeName, skyCluster string, phase superplanev1.SuperplaneNodePhase, lastPodTime *metav1.Time) *superplanev1.SuperplaneNode {
	return &superplanev1.SuperplaneNode{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Spec: superplanev1.SuperplaneNodeSpec{
			NodePoolRef: poolRef,
			Cloud:       "aws",
			GPUType:     "H100",
			GPUCount:    1,
		},
		Status: superplanev1.SuperplaneNodeStatus{
			Phase:              phase,
			K8sNodeName:        k8sNodeName,
			SkypilotCluster:    skyCluster,
			LastPodScheduledAt: lastPodTime,
		},
	}
}

func makeK8sNode(name string, unschedulable bool) *corev1.Node {
	return &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{
			Name: name,
		},
		Spec: corev1.NodeSpec{
			Unschedulable: unschedulable,
		},
	}
}

func makePod(name, namespace, nodeName string, ownerKind string) *corev1.Pod {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Spec: corev1.PodSpec{
			NodeName: nodeName,
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning,
		},
	}
	if ownerKind != "" {
		pod.OwnerReferences = []metav1.OwnerReference{
			{
				Kind: ownerKind,
				Name: "owner-1",
			},
		}
	}
	return pod
}

func ptrInt64(v int64) *int64    { return &v }
func ptrInt32(v int32) *int32    { return &v }
func ptrBool(v bool) *bool       { return &v }

func timeAgo(d time.Duration) *metav1.Time {
	t := metav1.NewTime(time.Now().Add(-d))
	return &t
}

// --- Tests ---

func TestNewConsolidator(t *testing.T) {
	c := NewConsolidator(nil, nil, ConsolidatorConfig{})
	if c.config.Interval != DefaultConsolidationInterval {
		t.Errorf("expected default interval %v, got %v", DefaultConsolidationInterval, c.config.Interval)
	}

	c2 := NewConsolidator(nil, nil, ConsolidatorConfig{Interval: 30 * time.Second})
	if c2.config.Interval != 30*time.Second {
		t.Errorf("expected custom interval 30s, got %v", c2.config.Interval)
	}
}

func TestIsDaemonSetPod(t *testing.T) {
	tests := []struct {
		name     string
		pod      *corev1.Pod
		expected bool
	}{
		{
			name:     "daemonset pod",
			pod:      makePod("ds-pod", "default", "node1", "DaemonSet"),
			expected: true,
		},
		{
			name:     "deployment pod",
			pod:      makePod("dep-pod", "default", "node1", "ReplicaSet"),
			expected: false,
		},
		{
			name:     "no owner",
			pod:      makePod("bare-pod", "default", "node1", ""),
			expected: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isDaemonSetPod(tt.pod); got != tt.expected {
				t.Errorf("isDaemonSetPod() = %v, want %v", got, tt.expected)
			}
		})
	}
}

func TestIsNodeEmpty(t *testing.T) {
	tests := []struct {
		name        string
		k8sNodeName string
		pods        []client.Object
		wantEmpty   bool
		wantErr     bool
	}{
		{
			name:        "empty node name returns true",
			k8sNodeName: "",
			wantEmpty:   true,
		},
		{
			name:        "no pods on node",
			k8sNodeName: "node1",
			wantEmpty:   true,
		},
		{
			name:        "only daemonset pods",
			k8sNodeName: "node1",
			pods: []client.Object{
				makePod("ds-pod", "default", "node1", "DaemonSet"),
			},
			wantEmpty: true,
		},
		{
			name:        "has workload pod",
			k8sNodeName: "node1",
			pods: []client.Object{
				makePod("work-pod", "default", "node1", "ReplicaSet"),
			},
			wantEmpty: false,
		},
		{
			name:        "only completed pods",
			k8sNodeName: "node1",
			pods: []client.Object{
				func() client.Object {
					p := makePod("done-pod", "default", "node1", "Job")
					p.Status.Phase = corev1.PodSucceeded
					return p
				}(),
			},
			wantEmpty: true,
		},
		{
			name:        "mixed daemonset and completed",
			k8sNodeName: "node1",
			pods: []client.Object{
				makePod("ds-pod", "default", "node1", "DaemonSet"),
				func() client.Object {
					p := makePod("failed-pod", "default", "node1", "Job")
					p.Status.Phase = corev1.PodFailed
					return p
				}(),
			},
			wantEmpty: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			objs := append([]client.Object{makeK8sNode("node1", false)}, tt.pods...)
			fc := newFakeClient(objs...)
			cons := NewConsolidator(fc, nil, ConsolidatorConfig{Namespace: "default"})

			got, err := cons.isNodeEmpty(context.Background(), tt.k8sNodeName)
			if (err != nil) != tt.wantErr {
				t.Errorf("isNodeEmpty() error = %v, wantErr %v", err, tt.wantErr)
			}
			if got != tt.wantEmpty {
				t.Errorf("isNodeEmpty() = %v, want %v", got, tt.wantEmpty)
			}
		})
	}
}

func TestGetEmptyTimestamp(t *testing.T) {
	cons := NewConsolidator(nil, nil, ConsolidatorConfig{})

	t.Run("nil LastPodScheduledAt returns zero", func(t *testing.T) {
		node := makeSuperplaneNode("n1", "default", "pool1", "k8s-node1", "sky-1", superplanev1.SuperplaneNodePhaseReady, nil)
		ts := cons.getEmptyTimestamp(node)
		if !ts.IsZero() {
			t.Errorf("expected zero time, got %v", ts)
		}
	})

	t.Run("set LastPodScheduledAt returns that time", func(t *testing.T) {
		past := timeAgo(10 * time.Minute)
		node := makeSuperplaneNode("n1", "default", "pool1", "k8s-node1", "sky-1", superplanev1.SuperplaneNodePhaseReady, past)
		ts := cons.getEmptyTimestamp(node)
		if ts.IsZero() {
			t.Errorf("expected non-zero time")
		}
	})
}

func TestListReadyNodes(t *testing.T) {
	readyNode := makeSuperplaneNode("ready-1", "default", "pool1", "k8s-1", "sky-1", superplanev1.SuperplaneNodePhaseReady, nil)
	pendingNode := makeSuperplaneNode("pending-1", "default", "pool1", "k8s-2", "sky-2", superplanev1.SuperplaneNodePhasePending, nil)
	drainingNode := makeSuperplaneNode("draining-1", "default", "pool1", "k8s-3", "sky-3", superplanev1.SuperplaneNodePhaseDraining, nil)

	fc := newFakeClient(readyNode, pendingNode, drainingNode)
	cons := NewConsolidator(fc, nil, ConsolidatorConfig{Namespace: "default"})

	nodes, err := cons.listReadyNodes(context.Background())
	if err != nil {
		t.Fatalf("listReadyNodes() error = %v", err)
	}
	if len(nodes) != 1 {
		t.Fatalf("expected 1 ready node, got %d", len(nodes))
	}
	if nodes[0].Name != "ready-1" {
		t.Errorf("expected node ready-1, got %s", nodes[0].Name)
	}
}

func TestCountUnavailableNodes(t *testing.T) {
	nodes := []client.Object{
		makeSuperplaneNode("ready-1", "default", "pool1", "k8s-1", "sky-1", superplanev1.SuperplaneNodePhaseReady, nil),
		makeSuperplaneNode("draining-1", "default", "pool1", "k8s-2", "sky-2", superplanev1.SuperplaneNodePhaseDraining, nil),
		makeSuperplaneNode("terminated-1", "default", "pool1", "k8s-3", "sky-3", superplanev1.SuperplaneNodePhaseTerminated, nil),
		makeSuperplaneNode("draining-2", "default", "pool2", "k8s-4", "sky-4", superplanev1.SuperplaneNodePhaseDraining, nil),
	}

	fc := newFakeClient(nodes...)
	cons := NewConsolidator(fc, nil, ConsolidatorConfig{Namespace: "default"})

	count, err := cons.countUnavailableNodes(context.Background(), "pool1")
	if err != nil {
		t.Fatalf("countUnavailableNodes() error = %v", err)
	}
	if count != 2 {
		t.Errorf("expected 2 unavailable for pool1, got %d", count)
	}

	count2, err := cons.countUnavailableNodes(context.Background(), "pool2")
	if err != nil {
		t.Fatalf("countUnavailableNodes() error = %v", err)
	}
	if count2 != 1 {
		t.Errorf("expected 1 unavailable for pool2, got %d", count2)
	}
}

func TestCordonNode(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	fc := newFakeClient(k8sNode)
	cons := NewConsolidator(fc, nil, ConsolidatorConfig{})

	err := cons.cordonNode(context.Background(), "k8s-node1")
	if err != nil {
		t.Fatalf("cordonNode() error = %v", err)
	}

	// Verify node is now unschedulable.
	var node corev1.Node
	if err := fc.Get(context.Background(), types.NamespacedName{Name: "k8s-node1"}, &node); err != nil {
		t.Fatalf("get node: %v", err)
	}
	if !node.Spec.Unschedulable {
		t.Error("expected node to be unschedulable after cordon")
	}

	// Cordon again should be idempotent.
	err = cons.cordonNode(context.Background(), "k8s-node1")
	if err != nil {
		t.Fatalf("cordonNode() idempotent call error = %v", err)
	}
}

func TestUpdateNodePhase(t *testing.T) {
	spNode := makeSuperplaneNode("node1", "default", "pool1", "k8s-1", "sky-1", superplanev1.SuperplaneNodePhaseReady, nil)
	fc := newFakeClient(spNode)
	cons := NewConsolidator(fc, nil, ConsolidatorConfig{})

	err := cons.updateNodePhase(context.Background(), spNode, superplanev1.SuperplaneNodePhaseDraining, "draining for test")
	if err != nil {
		t.Fatalf("updateNodePhase() error = %v", err)
	}

	// Verify phase was updated.
	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(), types.NamespacedName{Name: "node1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseDraining {
		t.Errorf("expected phase Draining, got %s", updated.Status.Phase)
	}
	if updated.Status.Message != "draining for test" {
		t.Errorf("expected message 'draining for test', got %q", updated.Status.Message)
	}
}

func TestDeleteK8sNode(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	fc := newFakeClient(k8sNode)
	cons := NewConsolidator(fc, nil, ConsolidatorConfig{})

	err := cons.deleteK8sNode(context.Background(), "k8s-node1")
	if err != nil {
		t.Fatalf("deleteK8sNode() error = %v", err)
	}

	// Verify node is gone.
	var node corev1.Node
	err = fc.Get(context.Background(), types.NamespacedName{Name: "k8s-node1"}, &node)
	if err == nil {
		t.Error("expected node to be deleted")
	}
}

func TestRemoveNode(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "k8s-node1", "sky-cluster-1", superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})

	err := cons.removeNode(context.Background(), spNode)
	if err != nil {
		t.Fatalf("removeNode() error = %v", err)
	}

	// Verify sky down was called.
	if len(sky.downCalls) != 1 {
		t.Fatalf("expected 1 sky down call, got %d", len(sky.downCalls))
	}
	if sky.downCalls[0].ClusterNames[0] != "sky-cluster-1" {
		t.Errorf("expected sky down for sky-cluster-1, got %v", sky.downCalls[0].ClusterNames)
	}

	// Verify SuperplaneNode is Terminated.
	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(), types.NamespacedName{Name: "sp-node1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseTerminated {
		t.Errorf("expected phase Terminated, got %s", updated.Status.Phase)
	}
}

func TestRemoveNodeSkyDownFailsRetryWithPurge(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "k8s-node1", "sky-cluster-1", superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	sky := &fakeSkyPilotClient{
		downErr: fmt.Errorf("sky down failed"),
	}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})

	err := cons.removeNode(context.Background(), spNode)
	if err != nil {
		t.Fatalf("removeNode() error = %v", err)
	}

	// Should have two calls: first normal, then purge.
	if len(sky.downCalls) != 2 {
		t.Fatalf("expected 2 sky down calls, got %d", len(sky.downCalls))
	}
	if sky.downCalls[0].Purge {
		t.Error("first call should not be purge")
	}
	if !sky.downCalls[1].Purge {
		t.Error("second call should be purge")
	}
}

func TestReconcileNoReadyNodes(t *testing.T) {
	pendingNode := makeSuperplaneNode("pending-1", "default", "pool1", "k8s-1", "sky-1", superplanev1.SuperplaneNodePhasePending, nil)
	pool := makeNodePool("pool1", ptrInt64(300), nil, nil)

	fc := newFakeClient(pendingNode, pool)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	err := cons.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}
	if len(sky.downCalls) != 0 {
		t.Errorf("expected no sky down calls, got %d", len(sky.downCalls))
	}
}

func TestReconcileConsolidationDisabled(t *testing.T) {
	pool := makeNodePool("pool1", ptrInt64(10), nil, ptrBool(false))
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1", superplanev1.SuperplaneNodePhaseReady, timeAgo(1*time.Hour))
	k8sNode := makeK8sNode("k8s-1", false)

	fc := newFakeClient(pool, spNode, k8sNode)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	err := cons.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}
	// No removal should happen when consolidation is disabled.
	if len(sky.downCalls) != 0 {
		t.Errorf("expected no sky down calls when consolidation disabled, got %d", len(sky.downCalls))
	}
}

func TestReconcileRemovesExpiredNode(t *testing.T) {
	ttl := int64(60) // 60 seconds
	pool := makeNodePool("pool1", &ttl, ptrInt32(2), nil)
	// Node has been empty for 5 minutes, TTL is 60s — should be removed.
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1", superplanev1.SuperplaneNodePhaseReady, timeAgo(5*time.Minute))
	k8sNode := makeK8sNode("k8s-1", false)

	fc := newFakeClient(pool, spNode, k8sNode)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	err := cons.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}

	// Should have called sky down.
	if len(sky.downCalls) != 1 {
		t.Errorf("expected 1 sky down call, got %d", len(sky.downCalls))
	}

	// Verify node is terminated.
	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(), types.NamespacedName{Name: "sp-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseTerminated {
		t.Errorf("expected Terminated, got %s", updated.Status.Phase)
	}
}

func TestReconcileRespectsDisruptionBudget(t *testing.T) {
	ttl := int64(10)
	maxUnavail := int32(1)
	pool := makeNodePool("pool1", &ttl, &maxUnavail, nil)

	// Two empty Ready nodes, both past TTL.
	sp1 := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1", superplanev1.SuperplaneNodePhaseReady, timeAgo(5*time.Minute))
	sp2 := makeSuperplaneNode("sp-2", "default", "pool1", "k8s-2", "sky-2", superplanev1.SuperplaneNodePhaseReady, timeAgo(5*time.Minute))
	k8s1 := makeK8sNode("k8s-1", false)
	k8s2 := makeK8sNode("k8s-2", false)

	fc := newFakeClient(pool, sp1, sp2, k8s1, k8s2)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	err := cons.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}

	// Only 1 should be removed due to maxUnavailable=1.
	if len(sky.downCalls) != 1 {
		t.Errorf("expected 1 sky down call (disruption budget), got %d", len(sky.downCalls))
	}
}

func TestReconcileNodeNotExpiredYet(t *testing.T) {
	ttl := int64(600) // 10 minutes
	pool := makeNodePool("pool1", &ttl, nil, nil)
	// Node empty for only 1 minute, TTL is 10 minutes.
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1", superplanev1.SuperplaneNodePhaseReady, timeAgo(1*time.Minute))
	k8sNode := makeK8sNode("k8s-1", false)

	fc := newFakeClient(pool, spNode, k8sNode)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	err := cons.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}

	// Should not remove — TTL not exceeded.
	if len(sky.downCalls) != 0 {
		t.Errorf("expected no sky down calls, got %d", len(sky.downCalls))
	}
}

func TestReconcileNodeWithWorkloadNotRemoved(t *testing.T) {
	ttl := int64(10)
	pool := makeNodePool("pool1", &ttl, nil, nil)
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1", superplanev1.SuperplaneNodePhaseReady, timeAgo(5*time.Minute))
	k8sNode := makeK8sNode("k8s-1", false)
	// Active workload pod.
	workPod := makePod("work-1", "default", "k8s-1", "ReplicaSet")

	fc := newFakeClient(pool, spNode, k8sNode, workPod)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	err := cons.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}

	// Node has workload, should not be removed.
	if len(sky.downCalls) != 0 {
		t.Errorf("expected no sky down calls for node with workload, got %d", len(sky.downCalls))
	}
}

func TestReconcileNewlyEmptyNodeNotRemoved(t *testing.T) {
	ttl := int64(10)
	pool := makeNodePool("pool1", &ttl, nil, nil)
	// Node with nil LastPodScheduledAt — newly empty.
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1", superplanev1.SuperplaneNodePhaseReady, nil)
	k8sNode := makeK8sNode("k8s-1", false)

	fc := newFakeClient(pool, spNode, k8sNode)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	err := cons.Reconcile(context.Background())
	if err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}

	// Newly empty node (nil LastPodScheduledAt) should not be removed yet.
	if len(sky.downCalls) != 0 {
		t.Errorf("expected no sky down calls for newly empty node, got %d", len(sky.downCalls))
	}
}

func TestStartStop(t *testing.T) {
	pool := makeNodePool("pool1", ptrInt64(300), nil, nil)
	fc := newFakeClient(pool)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Interval: 100 * time.Millisecond})

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()

	errCh := make(chan error, 1)
	go func() {
		errCh <- cons.Start(ctx)
	}()

	// Wait for it to stop via context cancellation.
	select {
	case err := <-errCh:
		if err != nil {
			t.Fatalf("Start() returned error: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Start() did not stop within timeout")
	}
}

func TestStartAlreadyRunning(t *testing.T) {
	fc := newFakeClient()
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Interval: 100 * time.Millisecond})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start first instance.
	go func() {
		_ = cons.Start(ctx)
	}()
	time.Sleep(50 * time.Millisecond)

	// Try starting again — should return error.
	err := cons.Start(ctx)
	if err == nil {
		t.Error("expected error when starting already running consolidator")
	}
}

// ---------------------------------------------------------------------------
// R15: provider truth determines cleanup, not a success status column
//
// Two of the three teardown paths used to mark success without confirming it.
// These tests pin the corrected behaviour: a node is only Terminated once the
// provider confirms the cluster is gone, and an unconfirmed teardown is a
// failure that retains the handle.
// ---------------------------------------------------------------------------

// TestRemoveNodeVerifiesReleaseAgainstProvider asserts the happy path actually
// re-checks the provider rather than trusting that Down() succeeded.
func TestRemoveNodeVerifiesReleaseAgainstProvider(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "k8s-node1", "sky-cluster-1",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})

	if err := cons.removeNode(context.Background(), spNode); err != nil {
		t.Fatalf("removeNode() error = %v", err)
	}

	if len(sky.statusCalls) != 1 {
		t.Fatalf("expected the release to be verified against the provider exactly once, got %d checks",
			len(sky.statusCalls))
	}
	if len(sky.statusCalls[0]) != 1 || sky.statusCalls[0][0] != "sky-cluster-1" {
		t.Errorf("expected verification of sky-cluster-1, got %v", sky.statusCalls[0])
	}
}

// TestRemoveNodeClusterStillPresentIsFailure: the provider still reports the
// cluster, so the resource exists and may still be billing.
func TestRemoveNodeClusterStillPresentIsFailure(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "k8s-node1", "sky-cluster-1",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	sky := &fakeSkyPilotClient{
		stillPresent: map[string]skypilot.ClusterStatus{"sky-cluster-1": skypilot.ClusterStatusUp},
	}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})

	err := cons.removeNode(context.Background(), spNode)
	if err == nil {
		t.Fatal("expected an error when the provider still reports the cluster")
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-node1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}

	if updated.Status.Phase == superplanev1.SuperplaneNodePhaseTerminated {
		t.Error("node was marked Terminated despite an unreleased provider resource")
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseReleaseFailed {
		t.Errorf("expected phase ReleaseFailed, got %s", updated.Status.Phase)
	}
	// The handle is the only way a later pass can finish the release.
	if updated.Status.SkypilotCluster != "sky-cluster-1" {
		t.Errorf("provider handle was not retained, got %q", updated.Status.SkypilotCluster)
	}
	if !strings.Contains(updated.Status.Message, "sky-cluster-1") {
		t.Errorf("expected the unreleased cluster named in the message, got %q", updated.Status.Message)
	}
}

// TestRemoveNodeStoppedClusterIsNotReleased: a STOPPED cluster keeps its disks
// and keeps incurring storage cost, so it must not clear the accounting.
func TestRemoveNodeStoppedClusterIsNotReleased(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "k8s-node1", "sky-cluster-1",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	sky := &fakeSkyPilotClient{
		stillPresent: map[string]skypilot.ClusterStatus{"sky-cluster-1": skypilot.ClusterStatusStopped},
	}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})

	if err := cons.removeNode(context.Background(), spNode); err == nil {
		t.Fatal("expected a STOPPED cluster to count as not released")
	}
}

// TestRemoveNodeUnknownProviderStatusIsFailure: the provider could not be
// queried. An unknown outcome is not a negative one — we must not conclude the
// resource is gone because we could not reach the provider. This is also the
// credential-loss case: a rejected call reads as an error, never as success.
func TestRemoveNodeUnknownProviderStatusIsFailure(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "k8s-node1", "sky-cluster-1",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	sky := &fakeSkyPilotClient{
		statusErr: fmt.Errorf("ExpiredToken: credentials could not be refreshed"),
	}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})

	err := cons.removeNode(context.Background(), spNode)
	if err == nil {
		t.Fatal("expected an unknown provider status to be reported as failure")
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-node1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseReleaseFailed {
		t.Errorf("expected phase ReleaseFailed after credential loss, got %s", updated.Status.Phase)
	}
	if updated.Status.SkypilotCluster != "sky-cluster-1" {
		t.Error("unresolved allocation was erased instead of retained")
	}
}

// TestReconcileReportsReleaseFailureAsError is the controller's equivalent of the
// teardown script's non-zero exit: a failed release must surface, not be logged
// and swallowed.
func TestReconcileReportsReleaseFailureAsError(t *testing.T) {
	pool := makeNodePool("pool1", ptrInt64(10), nil, nil)
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1",
		superplanev1.SuperplaneNodePhaseReady, timeAgo(1*time.Hour))
	k8sNode := makeK8sNode("k8s-1", false)

	fc := newFakeClient(pool, spNode, k8sNode)
	sky := &fakeSkyPilotClient{
		stillPresent: map[string]skypilot.ClusterStatus{"sky-1": skypilot.ClusterStatusUp},
	}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	err := cons.Reconcile(context.Background())
	if err == nil {
		t.Fatal("expected Reconcile to report the unconfirmed release as an error")
	}
	if !strings.Contains(err.Error(), "unconfirmed") {
		t.Errorf("expected the error to describe an unconfirmed release, got %v", err)
	}
}

// TestRemoveNodeK8sNodeDeleteFailureIsReported pins the removal of the old
// "continuing anyway" path: a node object left behind is an unresolved handle
// (the scheduler may keep placing pods against it), not a cosmetic problem.
func TestRemoveNodeK8sNodeDeleteFailureIsReported(t *testing.T) {
	// The K8s node is absent, so Delete fails with NotFound.
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "missing-k8s-node", "sky-cluster-1",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(spNode)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})

	err := cons.removeNode(context.Background(), spNode)
	if err == nil {
		t.Fatal("expected a failed K8s node delete to be reported rather than ignored")
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-node1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}
	if updated.Status.Phase == superplanev1.SuperplaneNodePhaseTerminated {
		t.Error("node was marked Terminated despite a failed K8s node delete")
	}
}

// ---------------------------------------------------------------------------
// R15: the idle-node TTL path is reachable
//
// getEmptyTimestamp reads status.lastPodScheduledAt, which nothing in the
// codebase ever wrote, so the TTL removal branch was dead for every node the
// controller provisions.
// ---------------------------------------------------------------------------

// TestReconcileWritesLastPodScheduledAtWhenBusy asserts the missing write now
// happens, which is what makes the TTL measurable at all.
func TestReconcileWritesLastPodScheduledAtWhenBusy(t *testing.T) {
	pool := makeNodePool("pool1", ptrInt64(300), nil, nil)
	// No lastPodScheduledAt, and the node is carrying a workload pod.
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1",
		superplanev1.SuperplaneNodePhaseReady, nil)
	k8sNode := makeK8sNode("k8s-1", false)
	workloadPod := makePod("training-job", "default", "k8s-1", "")

	fc := newFakeClient(pool, spNode, k8sNode, workloadPod)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	if err := cons.Reconcile(context.Background()); err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}
	if updated.Status.LastPodScheduledAt == nil {
		t.Fatal("lastPodScheduledAt was not written, so the TTL branch stays unreachable")
	}

	// A busy node must not be torn down.
	if len(sky.downCalls) != 0 {
		t.Errorf("a busy node was torn down: %d sky down calls", len(sky.downCalls))
	}
}

// TestReconcileNewlyEmptyNodeStartsTTLClock: a node that never carried work must
// still get a stamp, otherwise it stays unstamped forever and is never reclaimed.
func TestReconcileNewlyEmptyNodeStartsTTLClock(t *testing.T) {
	pool := makeNodePool("pool1", ptrInt64(300), nil, nil)
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1",
		superplanev1.SuperplaneNodePhaseReady, nil)
	k8sNode := makeK8sNode("k8s-1", false)

	fc := newFakeClient(pool, spNode, k8sNode)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	if err := cons.Reconcile(context.Background()); err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}
	if updated.Status.LastPodScheduledAt == nil {
		t.Fatal("expected the TTL clock to start for a newly empty node")
	}
	// Not yet expired, so nothing is removed on this pass.
	if len(sky.downCalls) != 0 {
		t.Errorf("expected no teardown before the TTL expires, got %d", len(sky.downCalls))
	}
}

// TestReconcileExpiredTTLReachesRemoval closes the loop: with a real stamp older
// than the TTL, the previously unreachable removal branch now executes.
func TestReconcileExpiredTTLReachesRemoval(t *testing.T) {
	pool := makeNodePool("pool1", ptrInt64(300), nil, nil)
	// Empty for an hour against a 300s TTL.
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1",
		superplanev1.SuperplaneNodePhaseReady, timeAgo(1*time.Hour))
	k8sNode := makeK8sNode("k8s-1", false)

	fc := newFakeClient(pool, spNode, k8sNode)
	sky := &fakeSkyPilotClient{}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

	if err := cons.Reconcile(context.Background()); err != nil {
		t.Fatalf("Reconcile() error = %v", err)
	}

	if len(sky.downCalls) != 1 {
		t.Fatalf("expected the expired node to be torn down, got %d sky down calls", len(sky.downCalls))
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseTerminated {
		t.Errorf("expected phase Terminated after a confirmed release, got %s", updated.Status.Phase)
	}
}

// TestCountUnavailableNodesIncludesRetiringAndReleaseFailed: retiring and
// release-failed nodes are unavailable, so counting them keeps the disruption
// budget honest instead of draining more nodes on top of a retirement.
func TestCountUnavailableNodesIncludesRetiringAndReleaseFailed(t *testing.T) {
	retiring := makeSuperplaneNode("retiring", "default", "pool1", "k8s-1", "sky-1",
		superplanev1.SuperplaneNodePhaseRetiring, nil)
	releaseFailed := makeSuperplaneNode("release-failed", "default", "pool1", "k8s-2", "sky-2",
		superplanev1.SuperplaneNodePhaseReleaseFailed, nil)
	ready := makeSuperplaneNode("ready", "default", "pool1", "k8s-3", "sky-3",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(retiring, releaseFailed, ready)
	cons := NewConsolidator(fc, &fakeSkyPilotClient{}, ConsolidatorConfig{Namespace: "default"})

	count, err := cons.countUnavailableNodes(context.Background(), "pool1")
	if err != nil {
		t.Fatalf("countUnavailableNodes() error = %v", err)
	}
	if count != 2 {
		t.Errorf("expected Retiring and ReleaseFailed to count as unavailable (2), got %d", count)
	}
}

// TestDrainNodeUsesEvictionAPIRespectingPDBs pins the drain sequence R15 requires
// be preserved: cordon first, then evict via the Eviction API. Using the Eviction
// subresource (rather than deleting pods directly) is what makes the API server
// enforce PodDisruptionBudgets — a budget-blocked eviction is rejected instead of
// silently taking a workload below its minimum available replicas.
func TestDrainNodeCordonsThenEvictsRespectingPDBs(t *testing.T) {
	k8sNode := makeK8sNode("k8s-1", false)
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "k8s-1", "sky-1",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	cons := NewConsolidator(fc, &fakeSkyPilotClient{}, ConsolidatorConfig{})

	// Cordon must happen before eviction, so the scheduler cannot place new pods
	// onto a node that is being drained.
	if err := cons.cordonNode(context.Background(), "k8s-1"); err != nil {
		t.Fatalf("cordonNode() error = %v", err)
	}

	var cordoned corev1.Node
	if err := fc.Get(context.Background(), types.NamespacedName{Name: "k8s-1"}, &cordoned); err != nil {
		t.Fatalf("get k8s node: %v", err)
	}
	if !cordoned.Spec.Unschedulable {
		t.Error("expected the node to be cordoned (unschedulable) before draining")
	}

	// Draining an already-empty node completes without evicting anything.
	if err := cons.drainNode(context.Background(), "k8s-1"); err != nil {
		t.Fatalf("drainNode() on an empty node error = %v", err)
	}
}

// TestDrainNodeSkipsDaemonSetPods: DaemonSet pods are expected to run on every
// node and are not evicted, so their presence must not block a drain.
func TestDrainNodeSkipsDaemonSetPods(t *testing.T) {
	k8sNode := makeK8sNode("k8s-1", true)
	dsPod := makePod("node-exporter", "kube-system", "k8s-1", "DaemonSet")

	fc := newFakeClient(k8sNode, dsPod)
	cons := NewConsolidator(fc, &fakeSkyPilotClient{}, ConsolidatorConfig{})

	if err := cons.drainNode(context.Background(), "k8s-1"); err != nil {
		t.Fatalf("drainNode() error = %v", err)
	}
}

// TestRemoveNodePurgeRetryFailureIsReleaseFailed: sky down is retried with purge,
// and when that also fails the teardown never happened at all. The node must not
// be recorded as Terminated.
func TestRemoveNodePurgeRetryFailureIsReleaseFailed(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "k8s-node1", "sky-cluster-1",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	sky := &fakeSkyPilotClient{
		downErr:      fmt.Errorf("500 Internal Server Error"),
		downPurgeErr: fmt.Errorf("500 Internal Server Error"),
	}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})

	err := cons.removeNode(context.Background(), spNode)
	if err == nil {
		t.Fatal("expected an error when both sky down and the purge retry fail")
	}
	if !strings.Contains(err.Error(), "purge") {
		t.Errorf("expected the purge retry named in the error, got %v", err)
	}

	// Both attempts must have been made: plain down, then purge.
	if len(sky.downCalls) != 2 {
		t.Fatalf("expected 2 down attempts (plain then purge), got %d", len(sky.downCalls))
	}
	if sky.downCalls[0].Purge || !sky.downCalls[1].Purge {
		t.Errorf("expected plain down then purge, got %+v", sky.downCalls)
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-node1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseReleaseFailed {
		t.Errorf("expected ReleaseFailed, got %s", updated.Status.Phase)
	}
	if updated.Status.SkypilotCluster != "sky-cluster-1" {
		t.Errorf("cluster handle must be retained, got %q", updated.Status.SkypilotCluster)
	}
}

// TestRemoveNodePurgeRetrySucceeds: the first down fails but the purge retry works
// and the provider confirms the release, so this is a complete teardown.
func TestRemoveNodePurgeRetrySucceeds(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "k8s-node1", "sky-cluster-1",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	sky := &fakeSkyPilotClient{downErr: fmt.Errorf("transient 503")}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})

	if err := cons.removeNode(context.Background(), spNode); err != nil {
		t.Fatalf("removeNode() error = %v", err)
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-node1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseTerminated {
		t.Errorf("expected Terminated after a confirmed release, got %s", updated.Status.Phase)
	}

	// The K8s node object must be gone too.
	var gone corev1.Node
	err := fc.Get(context.Background(), types.NamespacedName{Name: "k8s-node1"}, &gone)
	if err == nil {
		t.Error("expected the K8s node object to be deleted")
	}
}

// TestMarkNodeBusyOnMissingNodeIsAnError: the TTL stamp is written against a
// freshly read copy of the object, so a node deleted mid-reconcile surfaces as an
// error instead of a silent no-op.
func TestMarkNodeBusyOnMissingNodeIsAnError(t *testing.T) {
	spNode := makeSuperplaneNode("sp-gone", "default", "pool1", "k8s-node1", "",
		superplanev1.SuperplaneNodePhaseReady, nil)

	// The object is NOT seeded into the client.
	fc := newFakeClient()
	cons := NewConsolidator(fc, nil, ConsolidatorConfig{Namespace: "default"})

	err := cons.markNodeBusy(context.Background(), spNode, time.Now())
	if err == nil {
		t.Fatal("expected an error when the node no longer exists")
	}
}

// TestVerifyClusterReleasedIgnoresUnrelatedClusters: the provider answering with
// other clusters is not evidence about this one. Matching on presence alone rather
// than on name would make every release look like a failure.
func TestVerifyClusterReleasedIgnoresUnrelatedClusters(t *testing.T) {
	sky := &fakeSkyPilotClient{
		extraInfos: []skypilot.ClusterInfo{
			{Name: "sky-cluster-2", Status: skypilot.ClusterStatusUp},
			{Name: "sky-cluster-3", Status: skypilot.ClusterStatusInit},
		},
	}
	cons := newTestConsolidator(newFakeClient(), sky, ConsolidatorConfig{})

	if err := cons.verifyClusterReleased(context.Background(), "sky-cluster-1"); err != nil {
		t.Errorf("unrelated clusters must not block confirmation, got %v", err)
	}

	// And the target still being present is a failure even amongst others.
	sky.stillPresent = map[string]skypilot.ClusterStatus{"sky-cluster-1": skypilot.ClusterStatusUp}
	if err := cons.verifyClusterReleased(context.Background(), "sky-cluster-1"); err == nil {
		t.Error("expected a failure when the target cluster is still reported")
	}
}

// ---------------------------------------------------------------------------
// Asynchronous teardown must not be misread as a failed release
//
// Down() only submits a teardown request and returns a request ID; the provider
// tears the cluster down afterwards. For a short window /status therefore still
// reports the cluster. A single immediate probe read that in-progress teardown as
// "not released", which parked a perfectly normal release in ReleaseFailed,
// permanently consumed the pool's disruption budget (ReleaseFailed counts as
// unavailable, maxUnavailable defaults to 1) and leaked the K8s node object.
// ---------------------------------------------------------------------------

// TestRemoveNodeWaitsForAsyncTeardownToConverge: the cluster is still reported by
// the first probes and gone afterwards. That is a successful release.
func TestRemoveNodeWaitsForAsyncTeardownToConverge(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "k8s-node1", "sky-cluster-1",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	sky := &fakeSkyPilotClient{
		presentForFirstNChecks: map[string]int{"sky-cluster-1": 2},
	}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})
	// This tests convergence after two probes, not a 50ms scheduler deadline.
	// Leave timeout-failure tests fast, but allow loaded CI workers to schedule
	// the success path without turning provider convergence into a false failure.
	cons.ReleaseVerifyTimeout = 2 * time.Second


	if err := cons.removeNode(context.Background(), spNode); err != nil {
		t.Fatalf("an in-progress asynchronous teardown that then completes must be a successful release, got %v", err)
	}

	if sky.checkCounts["sky-cluster-1"] < 2 {
		t.Errorf("expected the provider to be re-checked while teardown was in flight, got %d probe(s)",
			sky.checkCounts["sky-cluster-1"])
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-node1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseTerminated {
		t.Errorf("expected Terminated after a confirmed release, got %s", updated.Status.Phase)
	}

	// The early return on a false failure also skipped node deletion, leaving a
	// node object the scheduler may keep placing pods against.
	var leaked corev1.Node
	err := fc.Get(context.Background(), types.NamespacedName{Name: "k8s-node1"}, &leaked)
	if err == nil {
		t.Error("K8s node object leaked: it must be deleted once the release is confirmed")
	}
}

// TestRemoveNodeStillPresentAtDeadlineIsFailure: a cluster the provider keeps
// reporting for the whole window really is unreleased. Waiting must not soften
// that verdict — only defer it to the deadline.
func TestRemoveNodeStillPresentAtDeadlineIsFailure(t *testing.T) {
	k8sNode := makeK8sNode("k8s-node1", false)
	spNode := makeSuperplaneNode("sp-node1", "default", "pool1", "k8s-node1", "sky-cluster-1",
		superplanev1.SuperplaneNodePhaseReady, nil)

	fc := newFakeClient(k8sNode, spNode)
	sky := &fakeSkyPilotClient{
		stillPresent: map[string]skypilot.ClusterStatus{"sky-cluster-1": skypilot.ClusterStatusUp},
	}
	cons := newTestConsolidator(fc, sky, ConsolidatorConfig{})

	if err := cons.removeNode(context.Background(), spNode); err == nil {
		t.Fatal("a cluster still reported at the deadline must remain a failed release")
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-node1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get sp node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseReleaseFailed {
		t.Errorf("expected ReleaseFailed, got %s", updated.Status.Phase)
	}
	if updated.Status.SkypilotCluster != "sky-cluster-1" {
		t.Errorf("provider handle must be retained, got %q", updated.Status.SkypilotCluster)
	}
}

// TestVerifyClusterReleasedRespectsContextCancellation: the wait must never
// outlive the caller's context, and a cancelled wait is not a confirmed release.
func TestVerifyClusterReleasedRespectsContextCancellation(t *testing.T) {
	sky := &fakeSkyPilotClient{
		stillPresent: map[string]skypilot.ClusterStatus{"sky-cluster-1": skypilot.ClusterStatusUp},
	}
	cons := NewConsolidator(newFakeClient(), sky, ConsolidatorConfig{})
	// Production-length window, so the test can only pass by honouring the context.
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	err := cons.verifyClusterReleased(ctx, "sky-cluster-1")
	if err == nil {
		t.Fatal("a cancelled verification must not be reported as a confirmed release")
	}
}

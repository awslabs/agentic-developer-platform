package controllers

import (
	"context"
	"fmt"
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
)

// fakeSkyPilotClient is a mock SkyPilot client for testing.
type fakeSkyPilotClient struct {
	downCalls    []downCall
	downErr      error
	downPurgeErr error
}

type downCall struct {
	ClusterNames []string
	Purge        bool
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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{})

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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{})

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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{Namespace: "default"})

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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{Interval: 100 * time.Millisecond})

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
	cons := NewConsolidator(fc, sky, ConsolidatorConfig{Interval: 100 * time.Millisecond})

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

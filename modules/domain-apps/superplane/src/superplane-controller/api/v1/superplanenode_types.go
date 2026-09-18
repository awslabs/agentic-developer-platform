package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SuperplaneNodePhase describes the current phase of a SuperplaneNode.
// +kubebuilder:validation:Enum=Pending;Provisioning;Joining;Ready;Degraded;Retiring;Draining;Terminated;ReleaseFailed;Failed
type SuperplaneNodePhase string

const (
	SuperplaneNodePhasePending      SuperplaneNodePhase = "Pending"
	SuperplaneNodePhaseProvisioning SuperplaneNodePhase = "Provisioning"
	SuperplaneNodePhaseJoining      SuperplaneNodePhase = "Joining"
	SuperplaneNodePhaseReady        SuperplaneNodePhase = "Ready"
	SuperplaneNodePhaseDegraded     SuperplaneNodePhase = "Degraded"

	// SuperplaneNodePhaseRetiring marks capacity that is being released on
	// purpose. It is distinct from Draining, which the health monitor also uses
	// when it replaces a node that failed accidentally. While a node is
	// Retiring, no controller may recreate equivalent capacity for it.
	SuperplaneNodePhaseRetiring SuperplaneNodePhase = "Retiring"

	SuperplaneNodePhaseDraining   SuperplaneNodePhase = "Draining"
	SuperplaneNodePhaseTerminated SuperplaneNodePhase = "Terminated"

	// SuperplaneNodePhaseReleaseFailed marks a node whose teardown failed or
	// could not be confirmed against the provider. Provider resources may still
	// exist and still be billing, so the node is deliberately NOT Terminated:
	// status.skypilotCluster is retained as the handle a later reconciliation
	// needs to finish the release.
	SuperplaneNodePhaseReleaseFailed SuperplaneNodePhase = "ReleaseFailed"

	SuperplaneNodePhaseFailed SuperplaneNodePhase = "Failed"
)

// AnnotationRetirement marks a SuperplaneNode as deliberately retired by an
// owner or operator. It is the durable record of intent: unlike status.phase it
// survives every phase transition, so a controller holding a slightly stale
// cached copy of the node still sees the retirement and declines to recreate
// capacity for it. Any non-empty value means "retired on purpose"; the value
// itself carries the human reason.
const AnnotationRetirement = "superplane.ai/retirement"

// IsRetiring reports whether this node is being deliberately retired, and so
// must not be recreated or counted as usable capacity by any controller.
// Terminated nodes are not retiring: their release already completed.
func (p SuperplaneNodePhase) IsRetiring() bool {
	return p == SuperplaneNodePhaseRetiring
}

// IsDeliberatelyRetiring reports whether this node's capacity is being released
// on purpose, via either the Retiring phase or the retirement annotation.
//
// This is the single check every controller uses before recreating capacity.
// It deliberately accepts both signals: the phase describes where the node is
// in its lifecycle, while the annotation records the owner's intent and remains
// readable even after the phase moves on to Draining or ReleaseFailed.
func (n *SuperplaneNode) IsDeliberatelyRetiring() bool {
	if n == nil {
		return false
	}
	if n.Status.Phase.IsRetiring() {
		return true
	}
	return n.Annotations[AnnotationRetirement] != ""
}

// RetirementReason returns the operator-supplied reason for a deliberate
// retirement, or "" when the node is not annotated as retired.
func (n *SuperplaneNode) RetirementReason() string {
	if n == nil {
		return ""
	}
	return n.Annotations[AnnotationRetirement]
}

// SuperplaneNodeSpec defines the desired state of SuperplaneNode.
type SuperplaneNodeSpec struct {
	// NodePoolRef is the name of the NodePool this node belongs to.
	// +kubebuilder:validation:MinLength=1
	NodePoolRef string `json:"nodePoolRef"`

	// Cloud is the cloud provider where this node is provisioned.
	// +kubebuilder:validation:MinLength=1
	Cloud string `json:"cloud"`

	// GPUType is the type of GPU on this node.
	// +kubebuilder:validation:MinLength=1
	GPUType string `json:"gpuType"`

	// GPUCount is the number of GPUs on this node.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=1
	GPUCount int32 `json:"gpuCount"`

	// Region is the cloud region where this node is provisioned.
	// +optional
	Region string `json:"region,omitempty"`
}

// SuperplaneNodeStatus defines the observed state of SuperplaneNode.
type SuperplaneNodeStatus struct {
	// Phase is the current lifecycle phase of the node.
	// +optional
	Phase SuperplaneNodePhase `json:"phase,omitempty"`

	// K8sNodeName is the name of the corresponding Kubernetes node object.
	// +optional
	K8sNodeName string `json:"k8sNodeName,omitempty"`

	// SkypilotCluster is the name of the SkyPilot cluster managing this node.
	// +optional
	SkypilotCluster string `json:"skypilotCluster,omitempty"`

	// SSMInstanceID is the SSM managed instance ID.
	// +optional
	SSMInstanceID string `json:"ssmInstanceId,omitempty"`

	// PublicIP is the public IP address of the node.
	// +optional
	PublicIP string `json:"publicIP,omitempty"`

	// TunnelIP is the WireGuard tunnel IP of the node.
	// +optional
	TunnelIP string `json:"tunnelIP,omitempty"`

	// HourlyCost is the hourly cost in USD for this node.
	// +optional
	HourlyCost float64 `json:"hourlyCost,omitempty"`

	// ProvisionedAt is the timestamp when the node was provisioned.
	// +optional
	ProvisionedAt *metav1.Time `json:"provisionedAt,omitempty"`

	// LastPodScheduledAt is the timestamp when a pod was last scheduled on this node.
	// +optional
	LastPodScheduledAt *metav1.Time `json:"lastPodScheduledAt,omitempty"`

	// Message is a human-readable message with details about the current phase.
	// +optional
	Message string `json:"message,omitempty"`

	// Conditions represent the latest available observations of the node's state.
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:printcolumn:name="Pool",type=string,JSONPath=`.spec.nodePoolRef`,description="NodePool reference"
// +kubebuilder:printcolumn:name="Cloud",type=string,JSONPath=`.spec.cloud`,description="Cloud provider"
// +kubebuilder:printcolumn:name="GPU",type=string,JSONPath=`.spec.gpuType`,description="GPU type"
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`,description="Current phase"
// +kubebuilder:printcolumn:name="Node",type=string,JSONPath=`.status.k8sNodeName`,description="K8s node name"
// +kubebuilder:printcolumn:name="Cost/Hr",type=number,JSONPath=`.status.hourlyCost`,description="Hourly cost in USD"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// SuperplaneNode is the Schema for the superplanenodes API.
// SuperplaneNode tracks the lifecycle of each provisioned GPU node.
type SuperplaneNode struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   SuperplaneNodeSpec   `json:"spec,omitempty"`
	Status SuperplaneNodeStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// SuperplaneNodeList contains a list of SuperplaneNode.
type SuperplaneNodeList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []SuperplaneNode `json:"items"`
}

func init() {
	SchemeBuilder.Register(&SuperplaneNode{}, &SuperplaneNodeList{})
}

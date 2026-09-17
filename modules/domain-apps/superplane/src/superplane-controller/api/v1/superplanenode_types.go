package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SuperplaneNodePhase describes the current phase of a SuperplaneNode.
// +kubebuilder:validation:Enum=Pending;Provisioning;Joining;Ready;Degraded;Draining;Terminated;Failed
type SuperplaneNodePhase string

const (
	SuperplaneNodePhasePending      SuperplaneNodePhase = "Pending"
	SuperplaneNodePhaseProvisioning SuperplaneNodePhase = "Provisioning"
	SuperplaneNodePhaseJoining      SuperplaneNodePhase = "Joining"
	SuperplaneNodePhaseReady        SuperplaneNodePhase = "Ready"
	SuperplaneNodePhaseDegraded     SuperplaneNodePhase = "Degraded"
	SuperplaneNodePhaseDraining     SuperplaneNodePhase = "Draining"
	SuperplaneNodePhaseTerminated   SuperplaneNodePhase = "Terminated"
	SuperplaneNodePhaseFailed       SuperplaneNodePhase = "Failed"
)

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

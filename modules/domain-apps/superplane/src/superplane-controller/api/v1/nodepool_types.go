package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ConsolidationPolicy defines how consolidation behaves.
// +kubebuilder:validation:Enum=WhenUnderutilized;Never
type ConsolidationPolicy string

const (
	ConsolidationPolicyWhenUnderutilized ConsolidationPolicy = "WhenUnderutilized"
	ConsolidationPolicyNever             ConsolidationPolicy = "Never"
)

// NodePoolConsolidation defines consolidation settings.
type NodePoolConsolidation struct {
	// Enabled controls whether node consolidation is active.
	// +kubebuilder:default=false
	// +optional
	Enabled bool `json:"enabled,omitempty"`

	// Policy defines the consolidation policy.
	// +kubebuilder:default=WhenUnderutilized
	// +optional
	Policy ConsolidationPolicy `json:"policy,omitempty"`
}

// NodePoolTemplate defines the template for provisioned nodes.
type NodePoolTemplate struct {
	// DiskSizeGB is the disk size in GB for provisioned nodes.
	// +kubebuilder:validation:Minimum=50
	// +kubebuilder:default=256
	// +optional
	DiskSizeGB int32 `json:"diskSizeGB,omitempty"`

	// K8sVersion is the Kubernetes version for the node.
	// +kubebuilder:default="1.33"
	// +optional
	K8sVersion string `json:"k8sVersion,omitempty"`

	// Wireguard enables WireGuard tunnel for the node.
	// +kubebuilder:default=true
	// +optional
	Wireguard bool `json:"wireguard,omitempty"`
}

// NodePoolDisruption defines disruption budget settings.
type NodePoolDisruption struct {
	// MaxUnavailable is the maximum number of nodes that can be unavailable during disruption.
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:default=1
	// +optional
	MaxUnavailable int32 `json:"maxUnavailable,omitempty"`
}

// NodePoolSpec defines the desired state of NodePool.
type NodePoolSpec struct {
	// Clouds is the list of clouds to use, in priority order.
	// +kubebuilder:validation:MinItems=1
	Clouds []string `json:"clouds"`

	// GPUTypes is the list of allowed GPU types.
	// +kubebuilder:validation:MinItems=1
	GPUTypes []string `json:"gpuTypes"`

	// MaxNodes is the maximum number of nodes in this pool.
	// +kubebuilder:validation:Minimum=1
	MaxNodes int32 `json:"maxNodes"`

	// MaxCostPerHour is the maximum cost per hour in USD for this pool.
	// +kubebuilder:validation:Minimum=0
	// +optional
	MaxCostPerHour *float64 `json:"maxCostPerHour,omitempty"`

	// PreferSpot indicates whether to prefer spot/preemptible instances.
	// +kubebuilder:default=false
	// +optional
	PreferSpot bool `json:"preferSpot,omitempty"`

	// TTLSecondsAfterEmpty is the duration in seconds after which an empty node is removed.
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:default=300
	// +optional
	TTLSecondsAfterEmpty *int64 `json:"ttlSecondsAfterEmpty,omitempty"`

	// Consolidation defines consolidation settings for the pool.
	// +optional
	Consolidation *NodePoolConsolidation `json:"consolidation,omitempty"`

	// Template defines the template for provisioned nodes.
	// +optional
	Template *NodePoolTemplate `json:"template,omitempty"`

	// MaxConcurrentProvisioning is the maximum number of nodes that can be provisioning at once.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:default=3
	// +optional
	MaxConcurrentProvisioning *int32 `json:"maxConcurrentProvisioning,omitempty"`

	// Disruption defines the disruption budget for the pool.
	// +optional
	Disruption *NodePoolDisruption `json:"disruption,omitempty"`
}

// NodePoolPhase describes the current phase of the NodePool.
// +kubebuilder:validation:Enum=Active;Inactive
type NodePoolPhase string

const (
	NodePoolPhaseActive   NodePoolPhase = "Active"
	NodePoolPhaseInactive NodePoolPhase = "Inactive"
)

// NodePoolStatus defines the observed state of NodePool.
type NodePoolStatus struct {
	// Phase is the current phase of the NodePool.
	// +optional
	Phase NodePoolPhase `json:"phase,omitempty"`

	// ReadyNodes is the number of nodes in Ready state.
	// +optional
	ReadyNodes int32 `json:"readyNodes,omitempty"`

	// ProvisioningNodes is the number of nodes currently being provisioned.
	// +optional
	ProvisioningNodes int32 `json:"provisioningNodes,omitempty"`

	// CurrentCostPerHour is the current total hourly cost for all nodes in this pool.
	// +optional
	CurrentCostPerHour float64 `json:"currentCostPerHour,omitempty"`

	// Conditions represent the latest available observations of the NodePool's state.
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Cluster
// +kubebuilder:printcolumn:name="Clouds",type=string,JSONPath=`.spec.clouds[*]`,description="Allowed clouds"
// +kubebuilder:printcolumn:name="GPU Types",type=string,JSONPath=`.spec.gpuTypes[*]`,description="Allowed GPU types"
// +kubebuilder:printcolumn:name="Max Nodes",type=integer,JSONPath=`.spec.maxNodes`,description="Maximum nodes"
// +kubebuilder:printcolumn:name="Ready",type=integer,JSONPath=`.status.readyNodes`,description="Ready nodes"
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`,description="Current phase"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// NodePool is the Schema for the nodepools API.
// NodePool is cluster-scoped and defines the policy for auto-provisioning GPU nodes.
type NodePool struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   NodePoolSpec   `json:"spec,omitempty"`
	Status NodePoolStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// NodePoolList contains a list of NodePool.
type NodePoolList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []NodePool `json:"items"`
}

func init() {
	SchemeBuilder.Register(&NodePool{}, &NodePoolList{})
}

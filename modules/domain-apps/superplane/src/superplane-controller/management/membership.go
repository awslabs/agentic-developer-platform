package management

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"time"

	authenticationv1 "k8s.io/api/authentication/v1"
	authorizationv1 "k8s.io/api/authorization/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

// MembershipCredential is public database-backed identity, not an authorization
// supplied by the projected file. The registry selects current active revisions
// or a projected revision under the original live provisional bootstrap claim.
type MembershipCredential struct {
	OrgID             string `json:"org_id"`
	WorkspaceID       string `json:"workspace_id"`
	ClusterID         string `json:"cluster_id"`
	ClusterARN        string `json:"cluster_arn"`
	Generation        string `json:"generation"`
	Namespace         string `json:"namespace"`
	NamespaceUID      string `json:"namespace_uid"`
	ServiceAccountUID string `json:"service_account_uid"`
	Revision          int64  `json:"revision"`
	ExpiresAt         string `json:"expires_at"`
	Scope             string `json:"scope"`
}

func (m *Manager) membershipMatches(target Target, config *clientcmdapi.Config) bool {
	member := target.MembershipCredential
	if !target.SharedMembership {
		_, hasMembership := config.Extensions["superplane.aws-e/membership"]
		return member == nil && !hasMembership
	}
	if member == nil || len(config.Extensions) != 1 {
		return false
	}
	expiry, err := time.Parse(time.RFC3339Nano, member.ExpiresAt)
	if err != nil || !time.Now().Before(expiry) || member.OrgID != m.config.OrgID || !uuidPattern.MatchString(member.OrgID) || member.WorkspaceID != target.WorkspaceID || member.ClusterID != target.ClusterID || member.ClusterARN != target.ClusterARN || member.Namespace != target.Namespace || !digestPattern.MatchString(member.Generation) || member.NamespaceUID == "" || member.ServiceAccountUID == "" || member.Revision < 1 || member.Revision > 2147483647 || member.Scope != "reader" {
		return false
	}
	extension, found := config.Extensions["superplane.aws-e/membership"]
	if !found || extension == nil {
		return false
	}
	raw, err := json.Marshal(extension)
	if err != nil {
		return false
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var projected MembershipCredential
	if decoder.Decode(&projected) != nil || decoder.Decode(new(any)) != io.EOF {
		return false
	}
	return projected == *member
}

// The provider authenticates this exact ServiceAccount UID. Combined with the
// issuer's journalled namespace UID and live namespaced reads, this attests the
// namespace incarnation without granting tenant tokens cluster-scoped GET/list.
// Decoding an unverified JWT would not establish this evidence.
func sharedServiceAccount(ctx context.Context, client kubernetes.Interface, target Target) bool {
	member := target.MembershipCredential
	if member == nil || !digestPattern.MatchString(member.Generation) {
		return false
	}
	observed, err := client.AuthenticationV1().SelfSubjectReviews().Create(ctx, &authenticationv1.SelfSubjectReview{}, metav1.CreateOptions{})
	expected := fmt.Sprintf("system:serviceaccount:%s:sp-reader-%s-%d", target.Namespace, member.Generation[:24], member.Revision)
	return err == nil && observed != nil && observed.Status.UserInfo.UID == member.ServiceAccountUID && observed.Status.UserInfo.Username == expected
}

func scopedReaderRules(status authorizationv1.SubjectRulesReviewStatus, shared bool) bool {
	if !shared {
		return readOnlyWorkspaceRules(status)
	}
	if status.Incomplete || status.EvaluationError != "" {
		return false
	}
	for _, rule := range status.ResourceRules {
		if len(rule.APIGroups) != 1 {
			return false
		}
		for _, resource := range rule.Resources {
			group := rule.APIGroups[0]
			read := group == "" && (resource == "pods" || resource == "events" || resource == "resourcequotas") || group == "batch" && resource == "jobs" || group == "superplane.ai" && resource == "superplanenodes"
			selfReview := group == "authorization.k8s.io" && (resource == "selfsubjectaccessreviews" || resource == "selfsubjectrulesreviews") || group == "authentication.k8s.io" && resource == "selfsubjectreviews"
			for _, verb := range rule.Verbs {
				if !(read && (verb == "get" || verb == "list" || verb == "watch")) && !(selfReview && verb == "create") {
					return false
				}
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

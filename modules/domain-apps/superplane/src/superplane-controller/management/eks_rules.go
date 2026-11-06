package management

import (
	"context"
	"net/url"
	"regexp"
	"strings"

	authenticationv1 "k8s.io/api/authentication/v1"
	authorizationv1 "k8s.io/api/authorization/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

const eksRulesError = "webhook authorizer does not support user rule resolution"

var managedEKSARN = regexp.MustCompile(`^arn:(aws|aws-us-gov|aws-cn):eks:([a-z0-9-]+):[0-9]{12}:cluster/[A-Za-z0-9][A-Za-z0-9_-]{0,99}$`)

func nativeEKSReaderIdentity(target Target, user authenticationv1.UserInfo) bool {
	arn := managedEKSARN.FindStringSubmatch(target.ClusterARN)
	u, err := url.Parse(target.Endpoint)
	if target.SharedMembership || len(arn) != 3 || err != nil || u.Scheme != "https" || u.User != nil || u.Port() != "" || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" {
		return false
	}
	suffix := ".eks.amazonaws.com"
	if arn[1] == "aws-cn" {
		suffix += ".cn"
	}
	if !strings.HasSuffix(u.Hostname(), "."+arn[2]+suffix) || !uuidPattern.MatchString(user.UID) || user.Username != "system:serviceaccount:"+target.Namespace+":superplane-controller" || len(user.Groups) != 3 {
		return false
	}
	groups := map[string]bool{"system:authenticated": false, "system:serviceaccounts": false, "system:serviceaccounts:" + target.Namespace: false}
	for _, group := range user.Groups {
		seen, expected := groups[group]
		if !expected || seen {
			return false
		}
		groups[group] = true
	}
	return true
}

// EKS access policies apply to IAM principals. For a provider-authenticated
// native ServiceAccount, the exact EKS webhook RulesFor diagnostic contributes
// no grants. All reported RBAC rules still pass the strict existing allowlist.
func dedicatedReaderRules(ctx context.Context, client kubernetes.Interface, target Target, status authorizationv1.SubjectRulesReviewStatus) bool {
	if scopedReaderRules(status, target.SharedMembership) {
		return true
	}
	if target.SharedMembership || !status.Incomplete || status.EvaluationError != eksRulesError {
		return false
	}
	observed, err := client.AuthenticationV1().SelfSubjectReviews().Create(ctx, &authenticationv1.SelfSubjectReview{}, metav1.CreateOptions{})
	if err != nil || observed == nil || !nativeEKSReaderIdentity(target, observed.Status.UserInfo) {
		return false
	}
	status.Incomplete, status.EvaluationError = false, ""
	return readOnlyWorkspaceRules(status)
}

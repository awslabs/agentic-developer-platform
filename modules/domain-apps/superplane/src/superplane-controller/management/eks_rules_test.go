package management

import (
	"context"
	"errors"
	"testing"

	authenticationv1 "k8s.io/api/authentication/v1"
	authorizationv1 "k8s.io/api/authorization/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	clienttesting "k8s.io/client-go/testing"
)

func TestEKSReaderRulesRequireNativeIdentityAndSafeRBAC(t *testing.T) {
	for _, name := range []string{"valid", "other-endpoint", "other-region", "http", "shared", "iam-user", "wrong-uid", "extra-group", "unknown-error", "multiple-errors", "secret", "mutation", "review-failed"} {
		t.Run(name, func(t *testing.T) {
			target := Target{ClusterARN: "arn:aws:eks:us-east-1:123456789012:cluster/workspace", Endpoint: "https://ABC.gr7.us-east-1.eks.amazonaws.com", Namespace: "superplane-system"}
			user := authenticationv1.UserInfo{Username: "system:serviceaccount:superplane-system:superplane-controller", UID: "22222222-2222-4222-8222-222222222222", Groups: []string{"system:authenticated", "system:serviceaccounts", "system:serviceaccounts:superplane-system"}}
			status := authorizationv1.SubjectRulesReviewStatus{Incomplete: true, EvaluationError: eksRulesError, ResourceRules: []authorizationv1.ResourceRule{{APIGroups: []string{""}, Resources: []string{"nodes"}, Verbs: []string{"list"}}}}
			switch name {
			case "other-endpoint":
				target.Endpoint = "https://workspace.example"
			case "other-region":
				target.Endpoint = "https://ABC.gr7.us-west-2.eks.amazonaws.com"
			case "http":
				target.Endpoint = "http://ABC.gr7.us-east-1.eks.amazonaws.com"
			case "shared":
				target.SharedMembership = true
			case "iam-user":
				user.Username = "arn:aws:iam::123456789012:role/operator"
			case "wrong-uid":
				user.UID = "aws-iam-authenticator:123456789012:role"
			case "extra-group":
				user.Groups = append(user.Groups, "system:masters")
			case "unknown-error":
				status.EvaluationError = "another authorizer failed"
			case "multiple-errors":
				status.EvaluationError += "; another error"
			case "secret":
				status.ResourceRules[0].Resources = []string{"secrets"}
			case "mutation":
				status.ResourceRules[0].Verbs = []string{"patch"}
			}
			client := fake.NewSimpleClientset()
			client.PrependReactor("create", "selfsubjectreviews", func(action clienttesting.Action) (bool, runtime.Object, error) {
				if name == "review-failed" {
					return true, nil, errors.New("unavailable")
				}
				return true, &authenticationv1.SelfSubjectReview{Status: authenticationv1.SelfSubjectReviewStatus{UserInfo: user}}, nil
			})
			if got := dedicatedReaderRules(context.Background(), client, target, status); got != (name == "valid") {
				t.Fatalf("accepted=%v", got)
			}
			if readOnlyWorkspaceRules(status) {
				t.Fatal("strict original checker must continue to refuse incomplete rules")
			}
		})
	}
}

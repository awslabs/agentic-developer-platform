package worker

import (
	"context"
	"testing"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/plugin"
)

func TestNativeTaskRegistrationAndMissingTransactionDenial(t *testing.T) {
	if err := registerNativePlugins(); err != nil {
		t.Fatalf("native plugin registration failed: %v", err)
	}
	for _, provider := range []string{"sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear", "slack"} {
		found, err := plugin.FindPluginNameBySubPkgPath("github.com/apache/devlake/plugins/" + provider + "/models")
		if err != nil || found != provider {
			t.Fatalf("native %s metadata unavailable: %s, %v", provider, found, err)
		}
		if err := invokeNativeTasks(context.Background(), nil, collection.Source{Tool: provider, Scope: "selected"}, 17); err != ErrWork {
			t.Fatalf("native task accepted missing fenced transaction: %s: %v", provider, err)
		}
	}
}

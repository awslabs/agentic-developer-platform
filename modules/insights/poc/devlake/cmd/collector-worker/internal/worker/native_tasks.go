package worker

import (
	"context"
	"database/sql"
	"io"
	"strconv"
	"sync"

	"github.com/apache/devlake/adp/normalization"
	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/core/models"
	"github.com/apache/devlake/core/models/domainlayer/devops"
	"github.com/apache/devlake/core/plugin"
	"github.com/apache/devlake/helpers/pluginhelper/api"
	implcontext "github.com/apache/devlake/impls/context"
	"github.com/apache/devlake/impls/dalgorm"
	"github.com/apache/devlake/impls/logruslog"
	argocdimpl "github.com/apache/devlake/plugins/argocd/impl"
	argocd "github.com/apache/devlake/plugins/argocd/tasks"
	gitlabimpl "github.com/apache/devlake/plugins/gitlab/impl"
	gitlab "github.com/apache/devlake/plugins/gitlab/tasks"
	jenkinsimpl "github.com/apache/devlake/plugins/jenkins/impl"
	jenkins "github.com/apache/devlake/plugins/jenkins/tasks"
	linearimpl "github.com/apache/devlake/plugins/linear/impl"
	linear "github.com/apache/devlake/plugins/linear/tasks"
	pagerdutyimpl "github.com/apache/devlake/plugins/pagerduty/impl"
	pagerduty "github.com/apache/devlake/plugins/pagerduty/tasks"
	slackimpl "github.com/apache/devlake/plugins/slack/impl"
	slack "github.com/apache/devlake/plugins/slack/tasks"
	sonarqubeimpl "github.com/apache/devlake/plugins/sonarqube/impl"
	sonarqube "github.com/apache/devlake/plugins/sonarqube/tasks"
	"github.com/sirupsen/logrus"
	"github.com/spf13/viper"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

var registerNativeOnce sync.Once
var registerNativeErr error

func registerNativePlugins() error {
	registerNativeOnce.Do(func() {
		for _, registration := range []struct {
			name string
			impl plugin.PluginMeta
		}{
			{"sonarqube", sonarqubeimpl.Sonarqube{}},
			{"jenkins", jenkinsimpl.Jenkins{}},
			{"argocd", argocdimpl.ArgoCD{}},
			{"gitlab", gitlabimpl.Gitlab{}},
			{"pagerduty", pagerdutyimpl.PagerDuty{}},
			{"linear", linearimpl.Linear{}},
			{"slack", slackimpl.Slack{}},
		} {
			if err := plugin.RegisterPlugin(registration.name, registration.impl); err != nil {
				registerNativeErr = ErrWork
				return
			}
		}
	})
	return registerNativeErr
}

func invokeNativeTasks(ctx context.Context, transaction *sql.Tx, source collection.Source, nativeID uint64) (result error) {
	defer func() {
		if recover() != nil {
			result = ErrWork
		}
	}()
	if ctx == nil || transaction == nil || nativeID == 0 || source.Scope == "" || ctx.Err() != nil {
		return ErrWork
	}
	if err := registerNativePlugins(); err != nil {
		return err
	}
	connection, err := gorm.Open(postgres.New(postgres.Config{Conn: transaction}),
		&gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		return ErrWork
	}
	logging := logrus.New()
	logging.SetOutput(io.Discard)
	nativeLogger, err := logruslog.NewDefaultLogger(logging)
	if err != nil {
		return ErrWork
	}
	resources := implcontext.NewDefaultBasicRes(viper.New(), nativeLogger, &nativeBufferedDal{dalgorm.NewDalgorm(connection.WithContext(ctx))})
	switch source.Tool {
	case "sonarqube":
		return normalization.SonarProject(ctx, resources, sonarqube.SonarqubeOptions{ConnectionId: nativeID, ProjectKey: source.Scope})
	case "jenkins", "gitlab":
		enricher := api.NewRegexEnricher()
		if enrichError := enricher.TryAdd(devops.PRODUCTION, "a^"); enrichError != nil {
			return ErrWork
		}
		if enrichError := enricher.TryAdd(devops.DEPLOYMENT, "a^"); enrichError != nil {
			return ErrWork
		}
		if source.Tool == "jenkins" {
			return normalization.JenkinsBuilds(ctx, resources, jenkins.JenkinsOptions{ConnectionId: nativeID, JobFullName: source.Scope}, enricher)
		}
		projectID, parseErr := strconv.Atoi(source.Scope)
		if parseErr != nil || projectID < 1 || strconv.Itoa(projectID) != source.Scope {
			return ErrWork
		}
		policy := models.SyncPolicy{TriggerSyncPolicy: models.TriggerSyncPolicy{FullSync: true}}
		return normalization.GitLabPipelines(ctx, resources, gitlab.GitlabOptions{ConnectionId: nativeID, ProjectId: projectID}, enricher, policy)
	case "argocd":
		return normalization.ArgoApplication(ctx, resources, argocd.ArgocdOptions{ConnectionId: nativeID, ApplicationName: source.Scope})
	case "pagerduty":
		return normalization.PagerDutyService(ctx, resources, pagerduty.PagerDutyOptions{ConnectionId: nativeID, ServiceId: source.Scope})
	case "linear":
		return normalization.LinearTeam(ctx, resources, linear.LinearOptions{ConnectionId: nativeID, TeamId: source.Scope})
	case "slack":
		return normalization.SlackChannel(ctx, resources, slack.SlackOptions{ConnectionId: nativeID, ChannelId: source.Scope})
	default:
		return ErrWork
	}
}

// Package normalization runs pinned DevLake tasks on an already-admitted cell DAL.
// It defines no ADP replacement for upstream raw, tool or domain models.
package normalization

import (
	"context"
	"fmt"

	corecontext "github.com/apache/devlake/core/context"
	"github.com/apache/devlake/core/models"
	"github.com/apache/devlake/core/plugin"
	"github.com/apache/devlake/helpers/pluginhelper/api"
	implcontext "github.com/apache/devlake/impls/context"
	gitlab "github.com/apache/devlake/plugins/gitlab/tasks"
	jenkins "github.com/apache/devlake/plugins/jenkins/tasks"
)

// JenkinsBuilds extracts collected raw build responses and invokes the native
// build converter. Only ordinary single-job selection is supported here;
// multibranch's upstream prefix selection needs a separately reviewed boundary.
// The caller supplies the admitted cell, registered upstream plugin metadata,
// provisioned schema and explicit enrichment configuration. No clients are made.
func JenkinsBuilds(ctx context.Context, res corecontext.BasicRes, options jenkins.JenkinsOptions, enrich *api.RegexEnricher) error {
	if err := ready(ctx, res, "jenkins", enrich); err != nil {
		return err
	}
	if options.Class == jenkins.WORKFLOW_MULTI_BRANCH_PROJECT {
		return fmt.Errorf("multibranch normalization is not supported by this single-job entry point")
	}
	validated, err := jenkins.ValidateTaskOptions(&options)
	if err != nil {
		return err
	}
	if !res.GetDal().HasTable("_raw_" + jenkins.RAW_BUILD_TABLE) {
		return fmt.Errorf("Jenkins raw build table is not provisioned")
	}
	data := &jenkins.JenkinsTaskData{Options: validated, RegexEnricher: enrich}
	extract := implcontext.NewStandaloneSubTaskContext(ctx, res, jenkins.ExtractApiBuildsMeta.Name, data, "jenkins", nil)
	if err := jenkins.ExtractApiBuilds(extract); err != nil {
		return err
	}
	convert := implcontext.NewStandaloneSubTaskContext(ctx, res, jenkins.ConvertBuildsToCicdTasksMeta.Name, data, "jenkins", nil)
	return jenkins.ConvertBuildsToCicdTasks(convert)
}

// GitLabPipelines invokes the native detail extractor and pipeline converter.
// The caller owns the sync policy, current authority and commit/export boundary.
// These tasks write native GitlabPipeline, CICDPipeline and SubtaskState rows.
func GitLabPipelines(ctx context.Context, res corecontext.BasicRes, options gitlab.GitlabOptions, enrich *api.RegexEnricher, policy models.SyncPolicy) error {
	if err := ready(ctx, res, "gitlab", enrich); err != nil {
		return err
	}
	if options.ConnectionId == 0 || options.ProjectId <= 0 {
		return fmt.Errorf("a GitLab connection and project are required")
	}
	if !res.GetDal().HasTable("_raw_" + gitlab.RAW_PIPELINE_DETAILS_TABLE) {
		return fmt.Errorf("GitLab raw pipeline detail table is not provisioned")
	}
	data := &gitlab.GitlabTaskData{Options: &options, RegexEnricher: enrich}
	extract := implcontext.NewStandaloneSubTaskContext(ctx, res, gitlab.ExtractApiPipelineDetailsMeta.Name, data, "gitlab", &policy)
	if err := gitlab.ExtractApiPipelineDetails(extract); err != nil {
		return err
	}
	convert := implcontext.NewStandaloneSubTaskContext(ctx, res, gitlab.ConvertDetailPipelineMeta.Name, data, "gitlab", &policy)
	return gitlab.ConvertDetailPipelines(convert)
}

func ready(ctx context.Context, res corecontext.BasicRes, provider string, enrich *api.RegexEnricher) error {
	if ctx == nil || res == nil || res.GetDal() == nil || res.GetLogger() == nil || res.GetConfigReader() == nil || enrich == nil {
		return fmt.Errorf("context, cell resources and enrichment configuration are required")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	// didgen panics without upstream metadata. Register actual plugin impls once
	// at worker startup, before concurrent tasks; do not initialize global APIs.
	name, err := plugin.FindPluginNameBySubPkgPath("github.com/apache/devlake/plugins/" + provider + "/models")
	if err != nil || name != provider {
		return fmt.Errorf("upstream %s plugin metadata is not registered", provider)
	}
	return nil
}

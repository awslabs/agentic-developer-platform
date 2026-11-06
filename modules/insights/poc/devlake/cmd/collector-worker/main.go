package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/apache/devlake/cmd/adp-collector-worker/internal/celldb"
	"github.com/apache/devlake/cmd/adp-collector-worker/internal/collection"
	"github.com/apache/devlake/cmd/adp-collector-worker/internal/worker"
	"github.com/apache/devlake/helpers/pluginhelper/api"
	github "github.com/apache/devlake/plugins/github/models"
	githubtasks "github.com/apache/devlake/plugins/github/tasks"
	jira "github.com/apache/devlake/plugins/jira/models"
	jiratasks "github.com/apache/devlake/plugins/jira/tasks"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, worker.NewGateway, celldb.Open))
}

func run(arguments []string, output, errors io.Writer, gatewayFactory func(string, string) (*worker.Gateway, error), open worker.OpenPool) int {
	flags := flag.NewFlagSet("collector-worker", flag.ContinueOnError)
	flags.SetOutput(errors)
	verifyPackage := flags.Bool("verify-package", false, "print linked source package inventory")
	configuration := flags.String("config", "", "private registered cell and assigned job configuration")
	if flags.Parse(arguments) != nil || flags.NArg() != 0 {
		return 2
	}
	if !*verifyPackage {
		if err := collection.CheckFlags(); err != nil {
			fmt.Fprintln(errors, err)
			return 2
		}
		file, err := os.Open(*configuration)
		if err != nil {
			fmt.Fprintln(errors, "collection_configuration_required")
			return 2
		}
		defer file.Close()
		var config worker.Config
		decoder := json.NewDecoder(io.LimitReader(file, 1024*1024))
		decoder.DisallowUnknownFields()
		if decoder.Decode(&config) != nil || decoder.Decode(new(any)) != io.EOF {
			fmt.Fprintln(errors, "collection_configuration_invalid")
			return 2
		}
		if config.SchemaPath == "" {
			config.SchemaPath = "/usr/share/devlake/schema-manifest.json"
		}
		if config.BuildManifestPath == "" {
			config.BuildManifestPath = "/usr/share/devlake/source-manifest.json"
		}
		gateway, err := gatewayFactory(config.GatewayURL, config.Region)
		if err != nil {
			fmt.Fprintln(errors, "collection_gateway_unavailable")
			return 2
		}
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
		defer cancel()
		result, err := worker.Run(ctx, config, gateway, open)
		if err != nil {
			fmt.Fprintln(errors, "collection_failed")
			return 1
		}
		if json.NewEncoder(output).Encode(result) != nil {
			return 1
		}
		return 0
	}
	inventory := map[string]interface{}{
		"github_model":      github.GithubRepo{}.TableName(),
		"jira_model":        jira.JiraBoard{}.TableName(),
		"raw_collector":     fmt.Sprintf("%T", api.ApiCollector{}),
		"linked_extractors": []string{fmt.Sprintf("%T", githubtasks.ExtractApiIssues), fmt.Sprintf("%T", jiratasks.ExtractIssues)},
	}
	if err := json.NewEncoder(output).Encode(inventory); err != nil {
		fmt.Fprintln(errors, err)
		return 1
	}
	return 0
}

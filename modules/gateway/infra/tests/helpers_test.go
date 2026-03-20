package test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
)

// RequiredTags defines the tags required by infra-tagging-strategy.md
var RequiredTags = []string{
	"Project",
	"Environment",
	"ManagedBy",
	"Owner",
	"CostCenter",
}

// ValidEnvironments defines the valid environment values
var ValidEnvironments = []string{"dev", "test", "prod"}

// NamingConventionPattern validates the bedrockgw-{env}-* naming convention
var NamingConventionPattern = regexp.MustCompile(`^bedrockgw-(dev|test|prod)-[a-z0-9-]+$`)

// GetTestTerraformOptions returns terraform options for testing a module
func GetTestTerraformOptions(t *testing.T, modulePath string, vars map[string]interface{}) *terraform.Options {
	return &terraform.Options{
		TerraformDir: modulePath,
		Vars:         vars,
		NoColor:      true,
	}
}

// ValidateNamingConvention checks if a resource name follows the bedrockgw-{env}-* convention
func ValidateNamingConvention(t *testing.T, resourceName string, env string) bool {
	expectedPrefix := "bedrockgw-" + env + "-"
	return strings.HasPrefix(resourceName, expectedPrefix)
}

// CheckFileExists verifies a file exists at the given path
func CheckFileExists(t *testing.T, filePath string) bool {
	_, err := os.Stat(filePath)
	return err == nil
}

// GetModulePath returns the absolute path to a module
func GetModulePath(t *testing.T, moduleName string) string {
	testDir, err := os.Getwd()
	if err != nil {
		t.Fatalf("Failed to get current working directory: %v", err)
	}
	return filepath.Join(testDir, "..", "modules", moduleName)
}

// ReadTerraformFile reads and returns the content of a terraform file
func ReadTerraformFile(t *testing.T, filePath string) string {
	content, err := os.ReadFile(filePath)
	if err != nil {
		t.Fatalf("Failed to read file %s: %v", filePath, err)
	}
	return string(content)
}

// ContainsTag checks if a terraform file contains a specific tag reference
func ContainsTag(content string, tagName string) bool {
	// Check for tag in tags block or merge with common_tags
	tagPatterns := []string{
		tagName + ` *=`,                  // Direct tag assignment
		`merge\(var\.common_tags`,        // Merge with common_tags
		`var\.common_tags`,               // Reference to common_tags variable
	}
	for _, pattern := range tagPatterns {
		matched, _ := regexp.MatchString(pattern, content)
		if matched {
			return true
		}
	}
	return false
}

// ContainsDefaultTags checks if a terraform file contains default_tags configuration
func ContainsDefaultTags(content string) bool {
	return strings.Contains(content, "default_tags")
}

// VariableExists checks if a variable is declared in the module
func VariableExists(t *testing.T, modulePath string, variableName string) bool {
	variablesPath := filepath.Join(modulePath, "variables.tf")
	if !CheckFileExists(t, variablesPath) {
		return false
	}
	content := ReadTerraformFile(t, variablesPath)
	pattern := `variable "` + variableName + `"`
	return strings.Contains(content, pattern)
}

// OutputExists checks if an output is declared in the module
func OutputExists(t *testing.T, modulePath string, outputName string) bool {
	outputsPath := filepath.Join(modulePath, "outputs.tf")
	if !CheckFileExists(t, outputsPath) {
		return false
	}
	content := ReadTerraformFile(t, outputsPath)
	pattern := `output "` + outputName + `"`
	return strings.Contains(content, pattern)
}

// ResourceExists checks if a resource is declared in the module
func ResourceExists(t *testing.T, modulePath string, resourceType string, resourceName string) bool {
	mainPath := filepath.Join(modulePath, "main.tf")
	if !CheckFileExists(t, mainPath) {
		return false
	}
	content := ReadTerraformFile(t, mainPath)
	pattern := `resource "` + resourceType + `" "` + resourceName + `"`
	return strings.Contains(content, pattern)
}

// ValidateTerraformModule runs terraform init and validate on a module
// This function creates a temporary provider configuration for modules that don't have their own
func ValidateTerraformModule(t *testing.T, modulePath string) {
	// Create a temporary provider configuration for modules that don't have their own
	providerConfigPath := filepath.Join(modulePath, "_test_providers.tf")
	providerConfig := `
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"

  # Skip credentials validation for module validation tests
  skip_credentials_validation = true
  skip_requesting_account_id  = true

  default_tags {
    tags = {
      Project     = "BedrockGateway"
      Environment = "test"
      ManagedBy   = "terraform"
    }
  }
}
`
	// Write the temporary provider config
	err := os.WriteFile(providerConfigPath, []byte(providerConfig), 0644)
	if err != nil {
		t.Fatalf("Failed to create temporary provider config: %v", err)
	}

	// Ensure cleanup of temporary file
	defer func() {
		os.Remove(providerConfigPath)
		// Also clean up .terraform directory created during init
		os.RemoveAll(filepath.Join(modulePath, ".terraform"))
		os.Remove(filepath.Join(modulePath, ".terraform.lock.hcl"))
	}()

	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		NoColor:      true,
	}

	// Initialize and validate the module
	terraform.InitAndValidate(t, terraformOptions)
}

// GetDefaultTestVars returns common test variables for modules
func GetDefaultTestVars(env string) map[string]interface{} {
	return map[string]interface{}{
		"environment": env,
		"name_prefix": "bedrockgw-" + env,
		"common_tags": map[string]interface{}{
			"Project":     "BedrockGateway",
			"Environment": env,
			"ManagedBy":   "terraform",
			"Owner":       "platform-team",
			"CostCenter":  "engineering-" + env,
		},
	}
}

// ParseJSONPolicy parses a JSON IAM policy and returns the unmarshaled data
func ParseJSONPolicy(t *testing.T, policyJSON string) map[string]interface{} {
	var policy map[string]interface{}
	err := json.Unmarshal([]byte(policyJSON), &policy)
	if err != nil {
		t.Fatalf("Failed to parse JSON policy: %v", err)
	}
	return policy
}

// AssertModuleFilesExist checks that all required module files exist
func AssertModuleFilesExist(t *testing.T, modulePath string) {
	requiredFiles := []string{"main.tf", "variables.tf", "outputs.tf"}
	for _, file := range requiredFiles {
		filePath := filepath.Join(modulePath, file)
		assert.True(t, CheckFileExists(t, filePath), "Required file %s should exist in module", file)
	}
}

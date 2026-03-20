package integration

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ecr"
	"github.com/aws/aws-sdk-go-v2/service/elasticache"
	elbv2 "github.com/aws/aws-sdk-go-v2/service/elasticloadbalancingv2"
	"github.com/aws/aws-sdk-go-v2/service/iam"
	"github.com/aws/aws-sdk-go-v2/service/rds"
	"github.com/gruntwork-io/terratest/modules/random"
	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/require"
)

const (
	// AWS Region for tests
	TestAWSRegion = "us-east-1"

	// Test prefix pattern (must follow naming convention from issue)
	TestPrefixPattern = "bedrockgw-test-%s"

	// Resource name patterns that are allowed by IAM permissions
	// IMPORTANT: IAM roles must start with specific prefixes per policy
	AllowedIAMRolePrefix  = "ai-security-"
	AllowedS3BucketPrefix = "ml-security-experiment-"
)

// RequiredTags defines the tags required by infra-tagging-strategy.md
var RequiredTags = map[string]string{
	"Project":   "BedrockGateway",
	"ManagedBy": "terraform",
	"Owner":     "platform-team",
}

// AWSClients holds AWS service clients
type AWSClients struct {
	EC2         *ec2.Client
	RDS         *rds.Client
	ElastiCache *elasticache.Client
	ECR         *ecr.Client
	IAM         *iam.Client
	ELBV2       *elbv2.Client
	Config      aws.Config
}

// TestContext holds all test context including clients and identifiers
type TestContext struct {
	T           *testing.T
	UniqueID    string
	TestPrefix  string
	AWS         *AWSClients
	ResourceLog *ResourceLog
}

// ResourceLog tracks created resources for reporting
type ResourceLog struct {
	Resources []ResourceEntry
}

// ResourceEntry represents a single created resource
type ResourceEntry struct {
	Type      string
	ID        string
	Name      string
	Timestamp time.Time
}

// GenerateUniqueTestPrefix creates a unique test prefix using Terratest's random ID
func GenerateUniqueTestPrefix() string {
	uniqueID := strings.ToLower(random.UniqueId())
	return fmt.Sprintf(TestPrefixPattern, uniqueID)
}

// GetUniqueID extracts just the unique ID portion
func GetUniqueID() string {
	return strings.ToLower(random.UniqueId())
}

// NewAWSClients creates AWS service clients for testing
func NewAWSClients(t *testing.T) *AWSClients {
	ctx := context.Background()

	cfg, err := config.LoadDefaultConfig(ctx, config.WithRegion(TestAWSRegion))
	require.NoError(t, err, "Failed to load AWS config")

	return &AWSClients{
		EC2:         ec2.NewFromConfig(cfg),
		RDS:         rds.NewFromConfig(cfg),
		ElastiCache: elasticache.NewFromConfig(cfg),
		ECR:         ecr.NewFromConfig(cfg),
		IAM:         iam.NewFromConfig(cfg),
		ELBV2:       elbv2.NewFromConfig(cfg),
		Config:      cfg,
	}
}

// NewTestContext creates a new test context with unique identifiers and AWS clients
func NewTestContext(t *testing.T) *TestContext {
	uniqueID := GetUniqueID()
	return &TestContext{
		T:           t,
		UniqueID:    uniqueID,
		TestPrefix:  fmt.Sprintf(TestPrefixPattern, uniqueID),
		AWS:         NewAWSClients(t),
		ResourceLog: &ResourceLog{Resources: []ResourceEntry{}},
	}
}

// LogResource records a resource creation for reporting
func (tc *TestContext) LogResource(resourceType, id, name string) {
	tc.ResourceLog.Resources = append(tc.ResourceLog.Resources, ResourceEntry{
		Type:      resourceType,
		ID:        id,
		Name:      name,
		Timestamp: time.Now(),
	})
}

// WriteResourceLog writes the resource log to a file
func (tc *TestContext) WriteResourceLog(filename string) error {
	var sb strings.Builder
	sb.WriteString("# Resources Created During Integration Test\n")
	sb.WriteString(fmt.Sprintf("# Test Prefix: %s\n", tc.TestPrefix))
	sb.WriteString(fmt.Sprintf("# Timestamp: %s\n\n", time.Now().Format(time.RFC3339)))

	for _, r := range tc.ResourceLog.Resources {
		sb.WriteString(fmt.Sprintf("%s | %s | %s | %s\n",
			r.Type, r.ID, r.Name, r.Timestamp.Format(time.RFC3339)))
	}

	return os.WriteFile(filename, []byte(sb.String()), 0644)
}

// GetModulePath returns the path to a Terraform module
func GetModulePath(moduleName string) string {
	// Integration tests are in infra/tests/integration
	// Modules are in infra/modules/{moduleName}
	return filepath.Join("..", "..", "modules", moduleName)
}

// GetCommonTags returns the common tags required by tagging strategy
func GetCommonTags(environment string) map[string]interface{} {
	return map[string]interface{}{
		"Project":     "BedrockGateway",
		"Environment": environment,
		"ManagedBy":   "terraform",
		"Owner":       "platform-team",
		"CostCenter":  "engineering-test",
	}
}

// GetNetworkingTestVars returns variables for networking module tests
func GetNetworkingTestVars(testPrefix string) map[string]interface{} {
	return map[string]interface{}{
		"environment": "test",
		"aws_region":  TestAWSRegion,
		"vpc_cidr":    "10.99.0.0/16",
		"az_count":    2,
		"name_prefix": testPrefix,
		"common_tags": GetCommonTags("test"),
	}
}

// CreateTerraformOptions creates Terraform options for a module
func CreateTerraformOptions(t *testing.T, modulePath string, vars map[string]interface{}) *terraform.Options {
	return &terraform.Options{
		TerraformDir:    modulePath,
		Vars:            vars,
		NoColor:         true,
	}
}

// CheckTCPConnection tests TCP connectivity to an endpoint
func CheckTCPConnection(t *testing.T, host string, port int, timeout time.Duration) bool {
	address := fmt.Sprintf("%s:%d", host, port)
	conn, err := net.DialTimeout("tcp", address, timeout)
	if err != nil {
		t.Logf("TCP connection to %s failed: %v", address, err)
		return false
	}
	defer conn.Close()
	t.Logf("TCP connection to %s successful", address)
	return true
}

// ValidateTags checks if a resource has the required tags
func ValidateTags(t *testing.T, tags map[string]string) bool {
	for key, expectedValue := range RequiredTags {
		actualValue, exists := tags[key]
		if !exists {
			t.Logf("Missing required tag: %s", key)
			return false
		}
		if actualValue != expectedValue {
			t.Logf("Tag %s has wrong value: expected %s, got %s", key, expectedValue, actualValue)
			return false
		}
	}
	return true
}

// EC2TagsToMap converts EC2 tags to a map
func EC2TagsToMap(tags []ec2types.Tag) map[string]string {
	result := make(map[string]string)
	for _, tag := range tags {
		if tag.Key != nil && tag.Value != nil {
			result[*tag.Key] = *tag.Value
		}
	}
	return result
}

// WaitForResource waits for a condition to be true with polling
func WaitForResource(t *testing.T, checkFunc func() bool, timeout time.Duration, pollInterval time.Duration, resourceDesc string) bool {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if checkFunc() {
			t.Logf("Resource %s is ready", resourceDesc)
			return true
		}
		t.Logf("Waiting for %s... (timeout in %v)", resourceDesc, time.Until(deadline).Round(time.Second))
		time.Sleep(pollInterval)
	}
	t.Logf("Timeout waiting for %s", resourceDesc)
	return false
}

// CleanupResources is a helper that can be deferred to clean up resources
func CleanupResources(t *testing.T, terraformOptions *terraform.Options) {
	t.Log("Starting cleanup of Terraform resources...")
	defer func() {
		if r := recover(); r != nil {
			t.Logf("Recovered from panic during cleanup: %v", r)
		}
	}()

	_, err := terraform.DestroyE(t, terraformOptions)
	if err != nil {
		t.Logf("Warning: Error during terraform destroy: %v", err)
	} else {
		t.Log("Terraform destroy completed successfully")
	}
}

// Note: As of Go 1.20+, the global random number generator is automatically seeded,
// so explicit rand.Seed() is no longer needed.

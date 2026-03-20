package test

import (
	"path/filepath"
	"testing"

	"github.com/gruntwork-io/terratest/modules/terraform"
	"github.com/stretchr/testify/assert"
)

// TestAlbModuleFilesExist verifies all required files exist in the ALB module
func TestAlbModuleFilesExist(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	AssertModuleFilesExist(t, modulePath)
}

// TestAlbModuleValidate validates the ALB module using terraform validate
func TestAlbModuleValidate(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")

	terraformOptions := &terraform.Options{
		TerraformDir: modulePath,
		NoColor:      true,
	}

	terraform.InitAndValidate(t, terraformOptions)
}

// TestAlbModuleRequiredVariables verifies required variables are declared
func TestAlbModuleRequiredVariables(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")

	requiredVars := []string{
		"environment",
		"name_prefix",
		"common_tags",
		"vpc_id",
		"public_subnet_ids",
		"alb_security_group_id",
		"domain_name",
		"certificate_domain",
	}

	for _, varName := range requiredVars {
		assert.True(t, VariableExists(t, modulePath, varName),
			"Required variable '%s' should be declared in ALB module", varName)
	}
}

// TestAlbModuleOutputs verifies required outputs are declared
func TestAlbModuleOutputs(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")

	requiredOutputs := []string{
		"load_balancer_arn",
		"load_balancer_dns_name",
		"load_balancer_zone_id",
		"target_group_arn",
		"target_group_name",
		"https_listener_arn",
		"http_listener_arn",
		"certificate_arn",
		"alb_logs_bucket_name",
	}

	for _, outputName := range requiredOutputs {
		assert.True(t, OutputExists(t, modulePath, outputName),
			"Required output '%s' should be declared in ALB module", outputName)
	}
}

// TestAlbModuleResources verifies essential resources are declared
func TestAlbModuleResources(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")

	// Verify ALB resource exists
	assert.True(t, ResourceExists(t, modulePath, "aws_lb", "main"),
		"Application Load Balancer should be declared")

	// Verify target group exists
	assert.True(t, ResourceExists(t, modulePath, "aws_lb_target_group", "main"),
		"ALB target group should be declared")

	// Verify HTTPS listener exists
	assert.True(t, ResourceExists(t, modulePath, "aws_lb_listener", "https"),
		"HTTPS listener should be declared")

	// Verify HTTP listener exists (for redirect)
	assert.True(t, ResourceExists(t, modulePath, "aws_lb_listener", "http"),
		"HTTP listener should be declared")

	// Verify S3 bucket for logs exists
	assert.True(t, ResourceExists(t, modulePath, "aws_s3_bucket", "alb_logs"),
		"S3 bucket for ALB logs should be declared")
}

// TestAlbModuleTagging verifies tagging compliance
func TestAlbModuleTagging(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check that common_tags is used for tagging
	assert.Contains(t, content, "var.common_tags",
		"Module should use common_tags variable for tagging")

	// Check for ALB-specific tags as per tagging strategy
	assert.Contains(t, content, "Service = \"load-balancer\"",
		"ALB resources should have Service = load-balancer tag")
	assert.Contains(t, content, "Public  = \"true\"",
		"ALB should have Public = true tag")
}

// TestAlbModuleNamingConvention verifies resource naming follows convention
func TestAlbModuleNamingConvention(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Check for name_prefix usage
	assert.Contains(t, content, "${var.name_prefix}",
		"Resources should use name_prefix variable for naming")

	// Verify ALB naming
	assert.Contains(t, content, "${var.name_prefix}-alb",
		"ALB should follow naming convention bedrockgw-{env}-alb")

	// Verify target group naming
	assert.Contains(t, content, "${var.name_prefix}-tg",
		"Target group should follow naming convention")

	// Verify listener naming
	assert.Contains(t, content, "${var.name_prefix}-listener-https",
		"HTTPS listener should follow naming convention")
}

// TestAlbModuleHTTPSConfiguration verifies HTTPS listener configuration
func TestAlbModuleHTTPSConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify HTTPS port
	assert.Contains(t, content, "port              = \"443\"",
		"HTTPS listener should listen on port 443")

	// Verify HTTPS protocol
	assert.Contains(t, content, "protocol          = \"HTTPS\"",
		"HTTPS listener should use HTTPS protocol")

	// Verify SSL policy
	assert.Contains(t, content, "ssl_policy",
		"HTTPS listener should have SSL policy configured")

	// Verify modern TLS policy is used
	assert.Contains(t, content, "ELBSecurityPolicy-TLS13",
		"HTTPS listener should use TLS 1.3 security policy")

	// Verify certificate is configured
	assert.Contains(t, content, "certificate_arn",
		"HTTPS listener should have certificate configured")
}

// TestAlbModuleHTTPRedirect verifies HTTP to HTTPS redirect
func TestAlbModuleHTTPRedirect(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify HTTP port
	assert.Contains(t, content, "port              = \"80\"",
		"HTTP listener should listen on port 80")

	// Verify redirect action
	assert.Contains(t, content, "type = \"redirect\"",
		"HTTP listener should use redirect action")

	// Verify redirect to HTTPS
	assert.Contains(t, content, "protocol    = \"HTTPS\"",
		"HTTP listener should redirect to HTTPS")

	// Verify 301 redirect
	assert.Contains(t, content, "status_code = \"HTTP_301\"",
		"HTTP to HTTPS redirect should use 301 status code")
}

// TestAlbModuleACMCertificate verifies ACM certificate configuration
func TestAlbModuleACMCertificate(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify ACM certificate data source
	assert.Contains(t, content, "data \"aws_acm_certificate\"",
		"Module should look up ACM certificate")

	// Verify certificate domain variable is used
	assert.Contains(t, content, "var.certificate_domain",
		"Certificate lookup should use certificate_domain variable")

	// Verify certificate status filter
	assert.Contains(t, content, "statuses    = [\"ISSUED\"]",
		"Certificate lookup should filter for ISSUED status")
}

// TestAlbModuleTargetGroupConfiguration verifies target group configuration
func TestAlbModuleTargetGroupConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify target type is IP (for EKS pods)
	assert.Contains(t, content, "target_type = \"ip\"",
		"Target group should use IP target type for EKS pods")

	// Verify target port
	assert.Contains(t, content, "port     = 8080",
		"Target group should use port 8080")

	// Verify health check is configured
	assert.Contains(t, content, "health_check",
		"Target group should have health check configured")

	// Verify health check path
	assert.Contains(t, content, "path                = \"/health\"",
		"Health check should use /health path")
}

// TestAlbModuleAccessLogsConfiguration verifies access logs configuration
func TestAlbModuleAccessLogsConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify access logs are enabled
	assert.Contains(t, content, "access_logs",
		"ALB should have access logs configured")

	// Verify access logs are enabled
	assert.Contains(t, content, "enabled = true",
		"ALB access logs should be enabled")

	// Verify S3 bucket is used for logs
	assert.Contains(t, content, "bucket  = aws_s3_bucket.alb_logs.bucket",
		"ALB should log to S3 bucket")
}

// TestAlbModuleS3BucketConfiguration verifies S3 bucket for logs
func TestAlbModuleS3BucketConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify S3 bucket versioning
	assert.Contains(t, content, "aws_s3_bucket_versioning",
		"S3 bucket should have versioning configured")

	// Verify S3 bucket encryption
	assert.Contains(t, content, "aws_s3_bucket_server_side_encryption_configuration",
		"S3 bucket should have encryption configured")

	// Verify S3 bucket lifecycle
	assert.Contains(t, content, "aws_s3_bucket_lifecycle_configuration",
		"S3 bucket should have lifecycle configuration")

	// Verify S3 bucket policy
	assert.Contains(t, content, "aws_s3_bucket_policy",
		"S3 bucket should have bucket policy for ALB access")

	// Verify S3 bucket naming follows convention
	assert.Contains(t, content, "ml-security-experiment-${var.name_prefix}-alb-logs",
		"S3 bucket should follow naming convention")
}

// TestAlbModuleDeletionProtection verifies deletion protection configuration
func TestAlbModuleDeletionProtection(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify deletion protection is environment-aware
	assert.Contains(t, content, "enable_deletion_protection = var.environment == \"prod\"",
		"ALB deletion protection should be enabled for prod only")
}

// TestAlbModuleWAFConfiguration verifies WAF configuration
func TestAlbModuleWAFConfiguration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify WAF Web ACL resource exists
	assert.Contains(t, content, "aws_wafv2_web_acl",
		"Module should support WAF Web ACL")

	// Verify WAF association exists
	assert.Contains(t, content, "aws_wafv2_web_acl_association",
		"Module should associate WAF with ALB")

	// Verify WAF is optional
	assert.Contains(t, content, "var.enable_waf",
		"WAF should be optionally enabled via variable")
}

// TestAlbModuleOptionalVariables verifies optional variables have defaults
func TestAlbModuleOptionalVariables(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	variablesPath := filepath.Join(modulePath, "variables.tf")
	content := ReadTerraformFile(t, variablesPath)

	// Check for create_route53_record default
	assert.Contains(t, content, "default     = false",
		"Optional variables should have defaults")

	// Check enable_waf default
	assert.Contains(t, content, "enable_waf",
		"enable_waf variable should be declared")
}

// TestAlbModuleRoute53Configuration verifies Route53 configuration
func TestAlbModuleRoute53Configuration(t *testing.T) {
	t.Parallel()

	modulePath := GetModulePath(t, "alb")
	mainPath := filepath.Join(modulePath, "main.tf")
	content := ReadTerraformFile(t, mainPath)

	// Verify Route53 record is conditional
	assert.Contains(t, content, "var.create_route53_record",
		"Route53 record should be conditional")

	// Verify Route53 alias record
	assert.Contains(t, content, "aws_route53_record",
		"Module should support Route53 record")

	// Verify alias configuration
	assert.Contains(t, content, "alias",
		"Route53 record should use alias for ALB")
}

# CloudWatch Dashboards

The latency dashboard is now managed by Terraform via `infra/modules/cloudwatch-dashboard/`.

The `latency-dashboard.json` file is kept as a reference but the canonical source
is the Terraform module. When you deploy a new environment, the dashboard is
created automatically with the correct resource names (CloudFront distribution ID,
ALB ARN suffix, EKS cluster name, etc.).

## ALB ARN Suffix

The ALB is created dynamically by the EKS Ingress controller, not by Terraform.
After the ALB is created, set `alb_arn_suffix` in your environment's `terraform.tfvars`:

```hcl
# Format: app/<alb-name>/<alb-id>
alb_arn_suffix = "app/k8s-bedrockg-bedrockg-96a0136fc5/a04d4e1ab78a9b6c"
```

You can find this value from the ALB ARN:
```bash
# Full ARN: arn:aws:elasticloadbalancing:us-east-1:<gateway-account-id>:loadbalancer/app/k8s-bedrockg-bedrockg-96a0136fc5/a04d4e1ab78a9b6c
# Suffix:   app/k8s-bedrockg-bedrockg-96a0136fc5/a04d4e1ab78a9b6c
```

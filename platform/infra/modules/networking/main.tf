# Data sources for availability zones
data "aws_availability_zones" "available" {
  state = "available"
}

# VPC
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-vpc"
  })
}

# Internet Gateway
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-igw"
  })
}

# Public Subnets
resource "aws_subnet" "public" {
  count = var.az_count

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index + 1)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = merge(var.common_tags, {
    Name                     = "${var.name_prefix}-public-${substr(data.aws_availability_zones.available.names[count.index], -1, 1)}"
    "kubernetes.io/role/elb" = "1"
    Type                     = "public"
  })
}

# Private Subnets
resource "aws_subnet" "private" {
  count = var.az_count

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = merge(var.common_tags, {
    Name                              = "${var.name_prefix}-private-${substr(data.aws_availability_zones.available.names[count.index], -1, 1)}"
    "kubernetes.io/role/internal-elb" = "1"
    Type                              = "private"
  })
}

# Elastic IPs for NAT Gateways
# When single_nat_gateway is true, only create 1 EIP; otherwise create one per AZ
resource "aws_eip" "nat" {
  count = var.single_nat_gateway ? 1 : var.az_count

  domain = "vpc"

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-eip-nat-${count.index + 1}"
  })

  depends_on = [aws_internet_gateway.main]
}

# NAT Gateways
# When single_nat_gateway is true, only create 1 NAT gateway in the first public subnet
resource "aws_nat_gateway" "main" {
  count = var.single_nat_gateway ? 1 : var.az_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-nat-${count.index + 1}"
  })

  depends_on = [aws_internet_gateway.main]
}

# Route Tables - Public
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-rt-public"
    Type = "public"
  })
}

# Route Tables - Private
# All private route tables point to the single NAT gateway when single_nat_gateway is true
resource "aws_route_table" "private" {
  count = var.az_count

  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = var.single_nat_gateway ? aws_nat_gateway.main[0].id : aws_nat_gateway.main[count.index].id
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-rt-private-${count.index + 1}"
    Type = "private"
  })
}

# Route Table Associations - Public
resource "aws_route_table_association" "public" {
  count = var.az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Route Table Associations - Private
resource "aws_route_table_association" "private" {
  count = var.az_count

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# Security Group - ALB
# Issue #133: SECURITY FIX - Restrict ALB ingress based on alb_internal setting
# For internal ALB: Only allow traffic from VPC CIDR
# For internet-facing ALB: Use alb_ingress_cidr_blocks (or VPC CIDR if empty)
locals {
  # Determine which CIDR blocks to allow based on configuration
  # If alb_ingress_cidr_blocks is set, use it; otherwise use VPC CIDR for internal ALB
  alb_allowed_cidrs = length(var.alb_ingress_cidr_blocks) > 0 ? var.alb_ingress_cidr_blocks : [var.vpc_cidr]
}

resource "aws_security_group" "alb" {
  name        = "${var.name_prefix}-sg-alb"
  description = "Security group for Application Load Balancer"
  vpc_id      = aws_vpc.main.id

  # Issue #133: SECURITY FIX - Removed 0.0.0.0/0 ingress
  # ALB is now internal, only allowing traffic from VPC CIDR by default
  ingress {
    description = var.alb_internal ? "HTTPS from VPC (internal ALB)" : "HTTPS from allowed CIDRs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = local.alb_allowed_cidrs
  }

  ingress {
    description = var.alb_internal ? "HTTP from VPC (internal ALB, redirect to HTTPS)" : "HTTP from allowed CIDRs"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = local.alb_allowed_cidrs
  }

  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-sg-alb"
    Service  = "load-balancer"
    Internal = tostring(var.alb_internal)
  })
}

# Security Group - EKS
resource "aws_security_group" "eks" {
  name        = "${var.name_prefix}-sg-eks"
  description = "Security group for EKS cluster and nodes"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTP from ALB"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description = "Node to node communication"
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    self        = true
  }

  ingress {
    description = "Pod to pod communication"
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.common_tags, {
    Name                                                   = "${var.name_prefix}-sg-eks"
    "kubernetes.io/cluster/${var.name_prefix}-eks-cluster" = "owned"
  })
}

# Security Group - RDS
resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-sg-rds"
  description = "Security group for RDS PostgreSQL"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL from EKS"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.eks.id]
  }

  egress {
    description = "No outbound traffic allowed"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = []
  }

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-sg-rds"
    Service  = "database"
    DataType = "tenant-data"
  })
}

# Security Group - Redis (ElastiCache)
resource "aws_security_group" "redis" {
  name        = "${var.name_prefix}-sg-redis"
  description = "Security group for ElastiCache Redis"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Redis from EKS"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.eks.id]
  }

  egress {
    description = "No outbound traffic allowed"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = []
  }

  tags = merge(var.common_tags, {
    Name     = "${var.name_prefix}-sg-redis"
    Service  = "cache"
    DataType = "ephemeral"
  })
}

# VPC Endpoints for AWS services (optional but recommended)
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"

  route_table_ids = concat([aws_route_table.public.id], aws_route_table.private[*].id)

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-vpce-s3"
  })
}

resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.eks.id]
  private_dns_enabled = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-vpce-ecr-dkr"
  })
}

resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.eks.id]
  private_dns_enabled = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-vpce-ecr-api"
  })
}

# ---------------------------------------------------------------------------
# VPC Endpoints — Security Group for Interface Endpoints
# ---------------------------------------------------------------------------
# Dedicated SG allowing HTTPS inbound from EKS nodes/pods only.
# Issue: #1160 (sec/H5 — restrict runner egress to VPC endpoints)
resource "aws_security_group" "vpc_endpoints" {
  name        = "${var.name_prefix}-sg-vpce"
  description = "Security group for VPC interface endpoints — allows HTTPS from EKS"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTPS from EKS nodes and pods"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.eks.id]
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-sg-vpce"
    Service = "vpc-endpoints"
  })
}

# ---------------------------------------------------------------------------
# VPC Endpoints (Interface) — AWS services used by agent runner pods
# ---------------------------------------------------------------------------
# These endpoints allow runner pods to reach AWS services via private IPs
# within the VPC CIDR, enabling NetworkPolicy CIDR-based egress restriction.
# Issue: #1160 (sec/H5)

resource "aws_vpc_endpoint" "sts" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.sts"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-vpce-sts"
  })
}

resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-vpce-secretsmanager"
  })
}

resource "aws_vpc_endpoint" "bedrock_runtime" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-vpce-bedrock-runtime"
  })
}

resource "aws_vpc_endpoint" "sqs" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.sqs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-vpce-sqs"
  })
}

resource "aws_vpc_endpoint" "execute_api" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.execute-api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-vpce-execute-api"
  })
}

# ---------------------------------------------------------------------------
# VPC Endpoint (Gateway) — DynamoDB
# ---------------------------------------------------------------------------
# Gateway endpoints are free and route via route tables (no SG needed).
# Used by agent pods for correlation-pointer writes.
# Issue: #1160 (sec/H5)

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"

  route_table_ids = aws_route_table.private[*].id

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-vpce-dynamodb"
  })
}

# NOTE: bedrock-agentcore VPC endpoint is not yet available in all regions.
# When it becomes available, add it here following the same pattern as
# bedrock_runtime above. Until then, bedrock-agentcore traffic routes via NAT
# gateway and will need an explicit CIDR allowlist in the NetworkPolicy (PR 2).
# Tracked as follow-up in #1160.

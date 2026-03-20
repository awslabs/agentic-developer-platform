environment = "dev"
aws_region  = "us-east-1"

vpc_cidr           = "10.0.0.0/16"
availability_zones = ["us-east-1a", "us-east-1b", "us-east-1c"]

eks_node_instance_types = ["m5.large", "m5.xlarge"]
eks_node_desired_size   = 2
eks_node_min_size       = 1
eks_node_max_size       = 10

# Disposable e2e host: one EC2 instance running an Environment ActiveGate plus the fake Cohesity
# cluster on its own 127.0.0.1. Own state, own VPC - nothing here depends on netapp_test.
#
# No inbound rules anywhere: the ActiveGate only dials out to the tenant, the fake cluster only
# listens on loopback, and shell access is SSM Session Manager rather than SSH.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.prefix
      ManagedBy = "terraform"
      Purpose   = "cohesity-extension-e2e"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

# Ubuntu 24.04 rather than netapp_test's AL2023: its python3 is 3.12, and the fake cluster needs
# >= 3.11 (datetime.UTC). AL2023 ships 3.9. The SSM agent is preinstalled on Canonical's AMIs.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

locals {
  extension_ca_pem = file(var.extension_ca_cert_path)
  tenant_url       = trimsuffix(trimsuffix(var.dt_environment_url, "/"), "/api")
}

# --- Network -----------------------------------------------------------------
# A public subnet only for outbound: tenant, apt, GitHub, SSM. The security group has no ingress.

resource "aws_vpc" "main" {
  cidr_block           = "10.99.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.prefix}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.prefix}-igw" }
}

resource "aws_subnet" "main" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.99.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.prefix}-subnet" }
}

resource "aws_route_table" "main" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${var.prefix}-rt" }
}

resource "aws_route_table_association" "main" {
  subnet_id      = aws_subnet.main.id
  route_table_id = aws_route_table.main.id
}

resource "aws_security_group" "host" {
  name        = "${var.prefix}-host"
  description = "e2e ActiveGate + fake Cohesity: outbound only, no inbound at all."
  vpc_id      = aws_vpc.main.id

  egress {
    description = "ActiveGate to tenant, package installs, git clone, SSM"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.prefix}-host" }
}

# --- Installer token ---------------------------------------------------------
# Held in SSM Parameter Store rather than templated into user-data, so it is not readable by
# anyone with ec2:DescribeInstanceAttribute and does not sit in the instance's user-data forever.
# It is still in the local terraform state - which is why *.tfstate* is gitignored.

resource "aws_ssm_parameter" "installer_token" {
  name        = "/${var.prefix}/dt-installer-token"
  description = "Dynatrace installer (PaaS) token for the e2e ActiveGate. Read once at boot."
  type        = "SecureString"
  value       = var.dt_paas_token
}

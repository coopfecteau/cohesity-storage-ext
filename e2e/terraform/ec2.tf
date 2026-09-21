# --- IAM: Session Manager for a shell, plus read access to exactly one parameter -------------

resource "aws_iam_role" "host" {
  name = "${var.prefix}-host"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "read_installer_token" {
  name = "${var.prefix}-read-installer-token"
  role = aws_iam_role.host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.installer_token.arn
      },
      {
        # SecureString with the AWS-managed aws/ssm key; only usable through SSM itself.
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" }
        }
      }
    ]
  })
}

resource "aws_iam_instance_profile" "host" {
  name = "${var.prefix}-host"
  role = aws_iam_role.host.name
}

# --- The host ------------------------------------------------------------------

resource "aws_instance" "host" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.main.id
  vpc_security_group_ids = [aws_security_group.host.id]
  iam_instance_profile   = aws_iam_instance_profile.host.name

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  user_data = templatefile("${path.module}/userdata/host.sh.tftpl", {
    region               = var.region
    token_parameter_name = aws_ssm_parameter.installer_token.name
    dt_url               = local.tenant_url
    ag_group             = var.activegate_group
    extension_ca_pem     = trimspace(local.extension_ca_pem)
    repo_url             = var.repo_url
    repo_ref             = var.repo_ref
  })
  # A changed ref or CA means a different test bed; rebuild rather than drift.
  user_data_replace_on_change = true

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  lifecycle {
    precondition {
      # The CA certificate is public and belongs in user-data. Its private key must never go
      # there - this catches pointing extension_ca_cert_path at ca.key or developer.pem.
      condition     = can(regex("-----BEGIN CERTIFICATE-----", local.extension_ca_pem)) && !strcontains(local.extension_ca_pem, "PRIVATE KEY")
      error_message = "extension_ca_cert_path must be the PUBLIC CA certificate (ca.pem), not a key or a fused key+certificate."
    }
  }

  depends_on = [aws_iam_role_policy.read_installer_token]

  tags = { Name = "${var.prefix}-host" }
}

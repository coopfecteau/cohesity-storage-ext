output "instance_id" {
  value = aws_instance.host.id
}

output "ssm_session" {
  description = "Shell on the host. No SSH, no inbound ports."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.host.id}"
}

output "bootstrap_log" {
  description = "Run inside the session to see how far boot got."
  value       = "sudo tail -n 100 /var/log/cohesity-e2e-bootstrap.log"
}

output "fake_cluster_logs" {
  value = "sudo journalctl -u cohesity-fake -n 50 --no-pager"
}

output "monitoring_scope" {
  description = "Scope e2e/loop.py assigns the monitoring configuration to."
  value       = "ag_group-${var.activegate_group}"
}

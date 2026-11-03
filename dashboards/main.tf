terraform {
  required_version = "= 1.17.0"
  required_providers {
    grafana = { source = "grafana/grafana", version = "= 3.25.9" }
  }
}
# GRAFANA_URL and GRAFANA_AUTH are read by the provider, never checked in.
provider "grafana" {}
variable "clickhouse_host" {
  type    = string
  default = "127.0.0.1"
}
variable "clickhouse_port" {
  type    = number
  default = 18123
}
variable "clickhouse_password" {
  type      = string
  sensitive = true
}
variable "prometheus_url" {
  type    = string
  default = "http://127.0.0.1:19090"
}
resource "grafana_data_source" "clickhouse" {
  type = "grafana-clickhouse-datasource"
  name = "PathWatch dedicated home ClickHouse"
  uid  = "pathwatch-clickhouse"
  json_data_encoded = jsonencode({
    host     = var.clickhouse_host, port = var.clickhouse_port, protocol = "http"
    username = "pathwatch_grafana", defaultDatabase = "pathwatch"
    secure   = false, tlsSkipVerify = false, queryTimeout = "10", dialTimeout = "5"
  })
  secure_json_data_encoded = jsonencode({ password = var.clickhouse_password })
}
resource "grafana_data_source" "prometheus" {
  type = "prometheus"
  name = "PathWatch SLO"
  uid  = "pathwatch-prometheus"
  url  = var.prometheus_url
}
resource "grafana_dashboard" "pathwatch" {
  config_json = file("${path.module}/pathwatch.json")
  depends_on  = [grafana_data_source.clickhouse, grafana_data_source.prometheus]
}

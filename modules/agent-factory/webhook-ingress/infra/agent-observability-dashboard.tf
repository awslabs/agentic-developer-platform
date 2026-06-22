# =============================================================================
# Agent Observability Dashboard — business + application telemetry lens
# =============================================================================
# Consumes the OTel logs exported by the ADOT Collector (otel-collector.tf) to
# surface cost, runs, tokens, and tool-health metrics sliced by tenant, user,
# persona, and model. All widgets are Logs Insights queries against the OTel
# log group — not CloudWatch metric widgets — because EMF NoDimensionRollup
# prevents clean roll-ups at the metric level.
#
# Gated by var.enable_agent_otel — no dashboard without the telemetry source.
#
# Naming: adp-<env>-agent-observability (application/business lens)
# Companion: adp-<env>-operations-centre (infra/platform lens, #919)
#
# Issue: #1680
# Telemetry source: #1630
# =============================================================================

resource "aws_cloudwatch_dashboard" "agent_observability" {
  count = var.enable_agent_otel ? 1 : 0

  dashboard_name = "${local.name_prefix}-agent-observability"

  dashboard_body = jsonencode({
    widgets = [
      # -----------------------------------------------------------------
      # Row 1: Cost by tenant | Cost by persona
      # -----------------------------------------------------------------
      {
        type   = "log"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Cost (USD) by tenant"
          view   = "bar"
          query  = "SOURCE '${var.otel_collector_log_group}/logs' | filter @message like /api_request/ | parse @message /\"tenant.id\":\"(?<tenant>[^\"]+)\"/ | parse @message /\"cost_usd\":(?<cost>[0-9.]+)/ | stats sum(cost) as cost_usd by tenant | sort cost_usd desc"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Cost (USD) by persona"
          view   = "bar"
          query  = "SOURCE '${var.otel_collector_log_group}/logs' | filter @message like /api_request/ | parse @message /\"agent.persona\":\"(?<persona>[^\"]+)\"/ | parse @message /\"cost_usd\":(?<cost>[0-9.]+)/ | stats sum(cost) as cost_usd by persona | sort cost_usd desc"
        }
      },

      # -----------------------------------------------------------------
      # Row 2: Agent runs by persona | Cost over time (hourly, by persona)
      # -----------------------------------------------------------------
      {
        type   = "log"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Agent runs (distinct sessions) by persona"
          view   = "bar"
          query  = "SOURCE '${var.otel_collector_log_group}/logs' | parse @message /\"agent.persona\":\"(?<persona>[^\"]+)\"/ | parse @message /\"session.id\":\"(?<sess>[^\"]+)\"/ | stats count_distinct(sess) as runs by persona | sort runs desc"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Cost over time (hourly, by persona)"
          view   = "line"
          query  = "SOURCE '${var.otel_collector_log_group}/logs' | filter @message like /api_request/ | parse @message /\"agent.persona\":\"(?<persona>[^\"]+)\"/ | parse @message /\"cost_usd\":(?<cost>[0-9.]+)/ | stats sum(cost) as cost_usd by bin(1h), persona"
        }
      },

      # -----------------------------------------------------------------
      # Row 3: Tokens by model | Runs & cost by user
      # -----------------------------------------------------------------
      {
        type   = "log"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Tokens (in+out) by model"
          view   = "bar"
          query  = "SOURCE '${var.otel_collector_log_group}/logs' | filter @message like /api_request/ | parse @message /\"model\":\"(?<model>[^\"]+)\"/ | parse @message /\"input_tokens\":(?<in>[0-9]+)/ | parse @message /\"output_tokens\":(?<out>[0-9]+)/ | stats sum(in) as input_tokens, sum(out) as output_tokens by model"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Runs & cost by user"
          view   = "bar"
          query  = "SOURCE '${var.otel_collector_log_group}/logs' | filter @message like /api_request/ | parse @message /\"enduser.id\":\"(?<user>[^\"]+)\"/ | parse @message /\"cost_usd\":(?<cost>[0-9.]+)/ | parse @message /\"session.id\":\"(?<sess>[^\"]+)\"/ | stats sum(cost) as cost_usd, count_distinct(sess) as runs by user | sort cost_usd desc"
        }
      },

      # -----------------------------------------------------------------
      # Row 4: Tool calls success/failure | Tool errors by type
      # -----------------------------------------------------------------
      {
        type   = "log"
        x      = 0
        y      = 18
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Tool calls: success vs failure"
          view   = "bar"
          query  = "SOURCE '${var.otel_collector_log_group}/logs' | filter @message like /tool_result/ | parse @message /\"success\":\"(?<ok>[a-z]+)\"/ | stats count(*) as calls by ok"
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 18
        width  = 12
        height = 6
        properties = {
          region = var.aws_region
          title  = "Tool errors by type"
          view   = "bar"
          query  = "SOURCE '${var.otel_collector_log_group}/logs' | filter @message like /tool_result/ and @message like /error_type/ | parse @message /\"error_type\":\"(?<err>[^\"]+)\"/ | parse @message /\"tool_name\":\"(?<tool>[^\"]+)\"/ | stats count(*) as errors by err, tool | sort errors desc"
        }
      },
    ]
  })
}

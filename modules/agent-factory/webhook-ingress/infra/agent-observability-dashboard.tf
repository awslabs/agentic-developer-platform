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
          title  = "Tokens by model (all buckets)"
          view   = "table"
          # All four token buckets + total. cache_read is typically the bulk
          # under prompt caching, so input+output alone drastically undercounts.
          # NOTE: 'output' is a reserved word in Logs Insights -> alias output_tokens.
          # 'sum(a)+sum(b)' is rejected in stats -> compute per-event total in a
          # 'fields' step first, then sum that.
          query = "SOURCE '${var.otel_collector_log_group}/logs' | filter @message like /api_request/ | parse @message /\"model\":\"(?<model>[^\"]+)\"/ | parse @message /\"input_tokens\":(?<inp>[0-9]+)/ | parse @message /\"output_tokens\":(?<out>[0-9]+)/ | parse @message /\"cache_read_tokens\":(?<cr>[0-9]+)/ | parse @message /\"cache_creation_tokens\":(?<cc>[0-9]+)/ | fields (inp+out+cr+cc) as tot | stats sum(inp) as input_tokens, sum(out) as output_tokens, sum(cr) as cache_read_tokens, sum(cc) as cache_create_tokens, sum(tot) as total_tokens by model | sort total_tokens desc"
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

      # -----------------------------------------------------------------
      # Row 5: Per-run detail table (full width) — one row per run with
      # run id, user (ADP owner UUID), all token buckets, start, duration,
      # cost, api calls. Clicking a row -> "View in Logs Insights" filters
      # that session.id, giving the run's full event stream (#1680 req #6).
      # user_id = ADP enduser.id (GitHub-login enrichment is a follow-up;
      # the SDK telemetry carries no GitHub id today).
      # -----------------------------------------------------------------
      {
        type   = "log"
        x      = 0
        y      = 24
        width  = 24
        height = 8
        properties = {
          region = var.aws_region
          title  = "Per-run detail - id, user, tokens, start, duration, cost (click row -> Logs Insights for that run)"
          view   = "table"
          query  = "SOURCE '${var.otel_collector_log_group}/logs' | filter @message like /api_request/ | parse @message /\"session.id\":\"(?<run>[^\"]+)\"/ | parse @message /\"enduser.id\":\"(?<user_id>[^\"]+)\"/ | parse @message /\"input_tokens\":(?<inp>[0-9]+)/ | parse @message /\"output_tokens\":(?<out>[0-9]+)/ | parse @message /\"cache_read_tokens\":(?<cr>[0-9]+)/ | parse @message /\"cost_usd\":(?<cost>[0-9.]+)/ | stats earliest(@timestamp) as start_time, (latest(@timestamp)-earliest(@timestamp))/1000 as duration_sec, sum(inp) as input_tokens, sum(out) as output_tokens, sum(cr) as cache_tokens, sum(cost) as cost_usd, count(*) as api_calls by run, user_id | sort cost_usd desc"
        }
      },

      # -----------------------------------------------------------------
      # Row 6: helper — how to view a single run's full logs
      # -----------------------------------------------------------------
      {
        type   = "text"
        x      = 0
        y      = 32
        width  = 24
        height = 3
        properties = {
          markdown = "### View a single run's logs\nClick any row in the **Per-run detail** table above -> **View in Logs Insights**, or paste this to see the full ordered event stream for one run:\n```\nfilter @message like /SESSION_ID/ | sort @timestamp asc\n```\nReplace `SESSION_ID` with the **run** value (session.id). `user_id` is the ADP owner UUID (GitHub-login enrichment is a follow-up — the SDK telemetry carries no GitHub id today). Widen the dashboard time range to see more runs."
        }
      },
    ]
  })
}

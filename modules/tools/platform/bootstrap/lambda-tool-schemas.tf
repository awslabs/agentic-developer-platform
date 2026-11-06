data "aws_lambda_function" "shared_tools" {
  count         = var.interceptor_lambda_arn == "" ? 0 : 1
  function_name = local.shared_tools_function_name
}

locals {
  shared_tools_function_name = var.shared_tools_function_name != "" ? var.shared_tools_function_name : "adp-${var.environment}-shared-tools-shared"
  shared_tools_role_arn      = "arn:aws:iam::${var.aws_account_id}:role/${local.shared_tools_function_name}-lambda"
  shared_tools_targets = var.interceptor_lambda_arn == "" ? {} : {
    for name in keys(local.lambda_tool_specs) : name => {
      name             = name
      lambda_arn       = data.aws_lambda_function.shared_tools[0].arn
      tool_name        = ""
      tool_description = ""
      input_properties = []
    }
  }
  tool_targets = merge({ for target in var.targets : target.name => target }, local.shared_tools_targets)

  lambda_tool_fields = {
    request_id    = { type = "string", description = "Stable UUID for the original operation" }
    execution_id  = { type = "string", description = "Owned tool execution ID" }
    session_id    = { type = "string", description = "Owned provider session handle" }
    query         = { type = "string", description = "Bounded web search query" }
    code          = { type = "string", description = "Python source to execute" }
    language      = { type = "string", description = "Execution language (python)" }
    provider_id   = { type = "string", description = "Original provider execution ID" }
    path          = { type = "string", description = "Path to a file created in the session" }
    artifact_id   = { type = "string", description = "Owned result artifact handle" }
    offset        = { type = "integer", description = "Byte offset for bounded artifact reads" }
    url           = { type = "string", description = "URL to open in the browser" }
    view_id       = { type = "string", description = "Current authorized browser view ID" }
    action        = { type = "string", description = "Browser action on the current view" }
    candidate_id  = { type = "string", description = "Candidate observed in this view" }
    seconds       = { type = "integer", description = "Bounded wait duration" }
    adp_assertion = { type = "string", description = "Interceptor-verified principal assertion" }
  }

  lambda_tool_specs = {
    websearch = {
      search          = ["execution_id", "request_id", "query"]
      operation_get   = ["execution_id", "request_id"]
      artifact_read   = ["execution_id", "artifact_id", "offset"]
      execution_close = ["execution_id"]
    }
    code-interpreter = {
      start           = ["execution_id", "request_id"]
      execute         = ["execution_id", "request_id", "session_id", "code", "language"]
      result          = ["execution_id", "request_id", "session_id", "provider_id"]
      file            = ["execution_id", "request_id", "session_id", "path"]
      close           = ["execution_id", "session_id"]
      operation_get   = ["execution_id", "request_id"]
      artifact_read   = ["execution_id", "artifact_id", "offset"]
      execution_close = ["execution_id"]
    }
    browser = {
      start           = ["execution_id", "request_id", "url"]
      step            = ["execution_id", "request_id", "session_id", "view_id", "action", "candidate_id", "url", "seconds"]
      inspect         = ["execution_id", "request_id", "session_id", "view_id"]
      close           = ["execution_id", "request_id", "session_id"]
      operation_get   = ["execution_id", "request_id"]
      artifact_read   = ["execution_id", "artifact_id", "offset"]
      execution_close = ["execution_id"]
    }
  }

  lambda_tool_schemas = {
    for target, tools in local.lambda_tool_specs : target => [
      for tool, fields in tools : {
        name        = tool
        description = "${replace(target, "_", " ")} ${replace(tool, "_", " ")}"
        properties = concat([for field in fields : merge(local.lambda_tool_fields[field], {
          name     = field
          required = !contains(["offset", "candidate_id", "url", "seconds"], field) || target == "browser" && tool == "start" && field == "url"
        })], [merge(local.lambda_tool_fields.adp_assertion, { name = "adp_assertion", required = false })])
      }
    ]
  }
}

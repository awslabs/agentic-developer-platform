def handler(event, context):
    return {"interceptorOutputVersion": "1.0", "mcp": {
        "transformedGatewayResponse": {"statusCode": 403, "body": {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32000, "message": "Interceptor not released"},
        }}
    }}

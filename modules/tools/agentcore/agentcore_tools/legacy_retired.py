"""Refuse every invocation of the state-retained Browser REST Lambda."""


def lambda_handler(event, context):
    return {"statusCode": 404, "body": "Not Found"}

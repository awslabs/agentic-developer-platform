#!/usr/bin/env python3
"""Read a protected task's durable SQS acknowledgement; never dispatch or delete."""
import argparse
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

def private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('x') as stream:
        path.chmod(0o600)
        json.dump(value, stream, indent=2)


def acknowledgement_receipt(row, *, invocation_id, sqs_message_id, pod_uid):
    if row.get('pk') != 'PODTASK#' + pod_uid or row.get('sk') != 'DELIVERY':
        raise ValueError('acknowledgement belongs to another worker')
    if row.get('state') != 'acknowledged' or row.get('invocation_id') != invocation_id or row.get('sqs_message_id') != sqs_message_id:
        raise ValueError('no acknowledgement for the published invocation and SQS message')
    if not re.fullmatch(r'[0-9a-f]{64}', str(row.get('receipt_handle_sha256', ''))):
        raise ValueError('receipt hash is missing')
    if any(key in row for key in ('receipt', 'body', 'queue_url')):
        raise ValueError('acknowledged task retained reusable credentials or task content')
    for key, expected in (('ack_attempts', 1), ('sqs_retry_attempts', 0), ('sqs_http_status', 200)):
        if type(row.get(key)) not in (int, Decimal) or row[key] != expected:
            raise ValueError('single clean DeleteMessage is not established: ' + key)
    if not isinstance(row.get('sqs_request_id'), str) or not row['sqs_request_id'].strip():
        raise ValueError('AWS response request ID is missing')
    stamp = row.get('acknowledged_at')
    if type(stamp) not in (int, Decimal) or stamp <= 0 or int(stamp) != stamp:
        raise ValueError('acknowledgement time is missing')
    return dict(run_id=invocation_id, message_id=sqs_message_id, pod_uid=pod_uid,
                receipt_handle_digest='sha256:' + row['receipt_handle_sha256'],
                delete_calls=1, delete_succeeded=True, sqs_request_id=row['sqs_request_id'],
                acknowledged_at=datetime.fromtimestamp(int(stamp), timezone.utc).isoformat(),
                observed_at=datetime.now(timezone.utc).isoformat(),
                observed_by='consistent DynamoDB task-delivery read: one reservation, zero SDK retries and successful SQS response')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('ledger', 'identity', 'dispatch-intent', 'out'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--table', required=True)
    args = parser.parse_args()
    ledger = json.loads(args.ledger.read_text())
    identity = json.loads(args.identity.read_text())['expected_identity']
    intent = json.loads(args.dispatch_intent.read_text())
    request = intent['envelope']['payload']['control_evaluation']
    if (identity['run_id'] != ledger['run_id'] or identity['nonce'] != ledger['run_nonce']
            or request['run_nonce'] != ledger['run_nonce'] or intent['state'] != 'published'):
        raise ValueError('fixture identity differs from the published dispatch')
    owned = [r for r in ledger['k8s'] if r.get('kind') == 'Pod' and r.get('uid') == identity['pod_uid']
             and r.get('name') == identity['pod_name'] and r.get('namespace') == identity['namespace']
             and r.get('created_by_this_run') is True]
    if len(owned) != 1 or args.table != intent['authority_table']:
        raise ValueError('worker or authority table is not owned by this fixture')
    import boto3
    from boto3.dynamodb.types import TypeDeserializer
    session = boto3.Session(region_name=args.region)
    if session.client('sts').get_caller_identity()['Account'] != ledger['account_id']:
        raise ValueError('AWS account differs from the fixture ledger')
    raw = session.client('dynamodb').get_item(TableName=args.table, ConsistentRead=True,
        Key={'pk': {'S': 'PODTASK#' + identity['pod_uid']}, 'sk': {'S': 'DELIVERY'}}).get('Item', {})
    decoder = TypeDeserializer()
    receipt = acknowledgement_receipt({k: decoder.deserialize(v) for k, v in raw.items()},
        invocation_id=intent['envelope']['message_id'], sqs_message_id=intent['sqs_message_id'], pod_uid=identity['pod_uid'])
    private_json(args.out, receipt)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

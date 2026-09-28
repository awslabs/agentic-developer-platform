"""Missing or ambiguous transport observations cannot establish one clean ack."""
import runpy
from pathlib import Path
import pytest

collect = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'wave3/collect_ack_receipt.py'))['acknowledgement_receipt']


def row():
    return dict(pk='PODTASK#pod', sk='DELIVERY', state='acknowledged', invocation_id='run', sqs_message_id='message',
                receipt_handle_sha256='a'*64, ack_attempts=1, sqs_retry_attempts=0, sqs_http_status=200,
                sqs_request_id='aws-response', acknowledged_at=1790269900)


@pytest.mark.parametrize('key,value', [('pk','PODTASK#foreign'),('invocation_id','foreign'),('sqs_message_id','foreign'),
    ('state','acking'),('receipt_handle_sha256',''),('ack_attempts',2),('ack_attempts',True),('sqs_retry_attempts',1),
    ('sqs_retry_attempts',None),('sqs_http_status',500),('sqs_request_id',None),('acknowledged_at',None),('receipt','secret')])
def test_incomplete_or_ambiguous_ack_refused(key, value):
    record=row();record[key]=value
    with pytest.raises(ValueError):
        collect(record, invocation_id='run', sqs_message_id='message', pod_uid='pod')

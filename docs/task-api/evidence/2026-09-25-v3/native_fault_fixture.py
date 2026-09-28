"""Bounded storage/transport qualification; no provider, public identity, or KEDA execution.
Run locally with --moto, or from deployed gateway with --apply.
Canonical acceptance work index shard is isolated before commit to prevent production
recovery discovering fixtures. Direct canonical recovery lease is exercised instead.
Every exact written key is journaled before mutation; finally removes only those keys.
"""
import argparse,copy,json,uuid,hashlib,sys
from datetime import UTC,datetime,timedelta
from concurrent.futures import ThreadPoolExecutor
import boto3
from botocore.exceptions import ClientError
from src.tasks.store import TaskStore,AcceptanceRequest,TaskStoreError,WorkLeaseConflictError,StaleAttemptError,TaskState
from src.tasks.records import task_authority_partition,task_policy_sort_key,task_run_grant_sort_key,payload_digest,WORK_SHARD_ATTRIBUTE

P=argparse.ArgumentParser();P.add_argument('--apply',action='store_true');P.add_argument('--moto',action='store_true');args=P.parse_args()
if not (args.apply or args.moto):
 print(__doc__);sys.exit(0)
context=None
if args.moto:
 from moto import mock_aws
 context=mock_aws();context.start()
D=boto3.client('dynamodb',region_name='us-east-1');Q=boto3.client('sqs',region_name='us-east-1')
if args.moto:
 for table,pk,sk in [('fixture-request','event_id','arrived_at'),('fixture-authority','pk','sk')]:
  D.create_table(TableName=table,BillingMode='PAY_PER_REQUEST',KeySchema=[{'AttributeName':pk,'KeyType':'HASH'},{'AttributeName':sk,'KeyType':'RANGE'}],AttributeDefinitions=[{'AttributeName':pk,'AttributeType':'S'},{'AttributeName':sk,'AttributeType':'S'}])
 base=TaskStore(table_name='fixture-request',authority_table_name='fixture-authority',dynamodb_client=D)
else:
 assert boto3.client('sts').get_caller_identity()['Account']=='879318057152'
 base=TaskStore(dynamodb_client=D)
uid=uuid.uuid4().hex;tenant='task-v3-native-'+uid;principal='fixture-'+uid;capacity=hashlib.sha256(uid.encode()).hexdigest();now=datetime.now(UTC).replace(microsecond=0)
ledger={};queue=None;Qname=None;results=[]
def emit(kind,**fields): print(json.dumps({'at':datetime.now(UTC).isoformat(),'kind':kind,**fields},default=str),flush=True)
def key_for(table,item):return {k:item[k] for k in (('event_id','arrived_at') if table==base.table_name else ('pk','sk'))}
def track(table,key):
 code=json.dumps([table,key],sort_keys=True)
 if code not in ledger:ledger[code]=(table,key);emit('cleanup_ledger',table=table,key=key)
class Client:
 fault=None
 def __getattr__(self,name):return getattr(D,name)
 def transact_write_items(self,**kw):
  kw=copy.deepcopy(kw)
  for tx in kw['TransactItems']:
   for kind,op in tx.items():
    if kind=='ConditionCheck':continue
    item=op.get('Item',op.get('Key'));track(op['TableName'],key_for(op['TableName'],item))
    if kind=='Put' and WORK_SHARD_ATTRIBUTE in item:item[WORK_SHARD_ATTRIBUTE]={'S':'native-fixture#'+uid}
  fault=self.fault;self.fault=None
  if fault=='before':raise ClientError({'Error':{'Code':'InternalServerError','Message':'injected before commit'}},'TransactWriteItems')
  r=D.transact_write_items(**kw)
  if fault=='after':raise ClientError({'Error':{'Code':'InternalServerError','Message':'injected lost commit response'}},'TransactWriteItems')
  return r
C=Client();S=TaskStore(table_name=base.table_name,authority_table_name=base.authority_table_name,dynamodb_client=C,clock=lambda:now)
def check(label,predicate,**evidence):
 assert predicate,label
 results.append(label);emit('PASS',check=label,**evidence)
def refused(label,fn,typ):
 try:fn()
 except typ:check(label,True);return
 raise AssertionError(label+' was permitted')

NOW=now
CAPACITY_SCOPE_HASH=capacity
def _request(**overrides) -> AcceptanceRequest:
    defaults = {
        "task_id": f"tsk_{uuid.uuid4()}",
        "invocation_id": str(uuid.uuid4()),
        "dispatch_id": str(uuid.uuid4()),
        "tenant": "tenant-a",
        "canonical_principal": "svc-principal-1",
        "idempotency_key": "key-1",
        "persona": "agent-task-investigator",
        "request_payload": {"instructions": "investigate", "inputs": {"a": 1}},
        "deadline_at": NOW + timedelta(hours=1),
        "policy_version": 1,
        "capacity_scope_hash": CAPACITY_SCOPE_HASH,
        "capacity_limit": 20,
        "capacity_reservation_id": str(uuid.uuid4()),
    }
    values = {**defaults, **overrides}
    values.setdefault("input_reference", {"record_type": "TASK", "input_digest": payload_digest(values["request_payload"])})
    values.setdefault(
        "envelope",
        {
            "kind": "adp.task",
            "schema_version": "1.0",
            "task_id": values["task_id"],
            "invocation_id": values["invocation_id"],
            "message_id": values["invocation_id"],
            "persona": values["persona"],
            "dispatch_id": values["dispatch_id"],
            "request_digest": payload_digest(values["request_payload"]),
            "input_ref": values["input_reference"],
            "assignment_ref": {
                "grant_pk": task_authority_partition(values["tenant"]),
                "grant_sk": task_run_grant_sort_key(invocation_id=values["invocation_id"], generation=values.get("generation", 1)),
                "generation": values.get("generation", 1),
            },
        },
    )
    values.setdefault(
        "immutable_input",
        {
            "instructions": values["request_payload"]["instructions"],
            "inputs": values["request_payload"].get("inputs", {}),
            "input_digest": values["input_reference"]["input_digest"],
        },
    )
    values.setdefault(
        "model_binding",
        {
            "model_id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "transport": "anthropic_messages",
            "model_policy_version": "mp-test",
            "request_shape_version": "rs-test",
            "pricing_evidence_version": "pe-test",
            "invocability_verified": True,
        },
    )
    values.setdefault(
        "run_limits",
        {
            "max_turns": 8,
            "max_output_tokens_per_turn": 4096,
            "max_usd": 1,
            "deadline_at": values["deadline_at"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    values.setdefault("grant_reference", values["envelope"]["assignment_ref"]["grant_sk"])
    return AcceptanceRequest(**values)

try:
 emit('scope',fixture_id=uid,request_table=S.table_name,authority_table=S.authority_table_name,isolation='nonproduction recovery shard; private FIFO queue; no worker/model/public credentials')
 for item in [
  {'pk':{'S':task_authority_partition(tenant)},'sk':{'S':task_policy_sort_key(principal)},'status':{'S':'active'},'version':{'N':'1'},'personas':{'SS':['agent-task-investigator']}},
  {'pk':{'S':'TASK_CAPACITY#'+capacity},'sk':{'S':'ACTIVE'},'active_count':{'N':'0'},'capacity_limit':{'N':'20'},'reservations':{'M':{}}}]:
  key=key_for(S.authority_table_name,item);track(S.authority_table_name,key)
  D.put_item(TableName=S.authority_table_name,Item=item,ConditionExpression='attribute_not_exists(pk)')
 def request():return _request(tenant=tenant,canonical_principal=principal,idempotency_key=str(uuid.uuid4()))
 pre=request();C.fault='before'
 refused('precommit failure is not accepted',lambda:S.accept(pre),TaskStoreError)
 check('precommit leaves no task',S.read_task(pre.task_id) is None)
 accepted=S.accept(pre);check('subsequent bounded acceptance succeeds',accepted.task_id==pre.task_id)
 post=request();C.fault='after';a=S.accept(post)
 check('lost commit response resolves durable same task',a.task_id==post.task_id and S.read_task(post.task_id) is not None)
 with ThreadPoolExecutor(max_workers=2) as pool:aa=list(pool.map(lambda _:S.accept(post),range(2)))
 check('concurrent idempotency has one task',{v.task_id for v in aa}=={post.task_id})
 rec=S._lease_recovery(record=S.resolve_work(post.dispatch_id),now=now+timedelta(seconds=1),lease_seconds=1)
 check('native recovery lease acquired',bool(rec))
 check('competing recovery lease denied',S._lease_recovery(record=S.resolve_work(post.dispatch_id),now=now+timedelta(seconds=1),lease_seconds=1) is None)
 pub=S.claim_dispatch(dispatch_id=post.dispatch_id,now=now+timedelta(seconds=1),lease_seconds=1)
 refused('competing publisher denied',lambda:S.claim_dispatch(dispatch_id=post.dispatch_id,now=now+timedelta(seconds=1)),WorkLeaseConflictError)
 Qname='task-v3-native-'+uid+'.fifo';emit('cleanup_queue_name',queue_name=Qname);queue=Q.create_queue(QueueName=Qname,Attributes={'FifoQueue':'true','ContentBasedDeduplication':'false','MessageRetentionPeriod':'60','VisibilityTimeout':'1'})['QueueUrl'];emit('cleanup_queue',queue=queue)
 body=json.dumps(pub['envelope'],sort_keys=True);send={'QueueUrl':queue,'MessageBody':body,'MessageGroupId':uid,'MessageDeduplicationId':post.dispatch_id}
 first=Q.send_message(**send)
 # Actual first send succeeds; caller deliberately loses response and later retries
 # with the same protected envelope and dedup ID after its publication lease expires.
 new=S.claim_dispatch(dispatch_id=post.dispatch_id,now=now+timedelta(seconds=3),lease_seconds=45)
 check('expired publication lease reclaims identical envelope',new['envelope']==pub['envelope'])
 refused('stale publisher cannot settle after lease takeover',lambda:S.settle_dispatch(dispatch_id=post.dispatch_id,lease_token=pub['lease_token'],publication_outcome='confirmed',sqs_message_id=first['MessageId'],now=now+timedelta(seconds=3)),WorkLeaseConflictError)
 second=Q.send_message(**send);emit('send_responses',first_id=first['MessageId'],retry_id=second['MessageId'])
 S.settle_dispatch(dispatch_id=post.dispatch_id,lease_token=new['lease_token'],publication_outcome='confirmed',sqs_message_id=second['MessageId'],now=now+timedelta(seconds=3))
 check('confirmed publication durably queues same task',S.read_task(post.task_id)['state']=='queued')
 msgs=Q.receive_message(QueueUrl=queue,MaxNumberOfMessages=2,WaitTimeSeconds=1).get('Messages',[])
 check('duplicate sends yield single delivery',len(msgs)==1 and msgs[0]['Body']==body)
 Q.change_message_visibility(QueueUrl=queue,ReceiptHandle=msgs[0]['ReceiptHandle'],VisibilityTimeout=0)
 again=Q.receive_message(QueueUrl=queue,MaxNumberOfMessages=2,WaitTimeSeconds=1).get('Messages',[])
 check('unacknowledged delivery redelivers same message',len(again)==1 and again[0]['MessageId']==msgs[0]['MessageId'])
 Q.delete_message(QueueUrl=queue,ReceiptHandle=again[0]['ReceiptHandle'])
 check('lost delete response does not fabricate another delivery',not Q.receive_message(QueueUrl=queue,WaitTimeSeconds=1).get('Messages'))
 old=str(uuid.uuid4());newid=str(uuid.uuid4())
 S.bind_runtime_attempt(task_id=pre.task_id,invocation_id=pre.invocation_id,generation=1,runtime_attempt_id=old,expected_version=1)
 S.bind_runtime_attempt(task_id=pre.task_id,invocation_id=pre.invocation_id,generation=1,runtime_attempt_id=newid,expected_version=2,expected_runtime_attempt_id=old)
 refused('replaced attempt cannot report',lambda:S.append_report(task_id=pre.task_id,invocation_id=pre.invocation_id,generation=1,runtime_attempt_id=old,report_id=str(uuid.uuid4()),kind='progress.updated',data={'message':'late','stage':'analysis'}),StaleAttemptError)
 refused('replaced attempt cannot complete',lambda:S.transition(task_id=pre.task_id,expected_version=3,target_state=TaskState.COMPLETED,generation=1,invocation_id=pre.invocation_id,runtime_attempt_id=old),StaleAttemptError)
 from types import SimpleNamespace
 from src.tasks.task_commands import TaskCommands
 from src.tasks.errors import TaskApiError
 identity=SimpleNamespace(task_id=pre.task_id,invocation_id=pre.invocation_id,generation=1,runtime_attempt_id=old,tenant=tenant,canonical_principal=principal)
 refused('replaced attempt cannot acknowledge delivery',lambda:TaskCommands(S).settlement(identity,{'stop_evidence':{'child_exit_confirmed':True,'workload_terminated':True},'queue_ack_status':'confirmed'}),TaskApiError)
 from src.tasks.records import task_run_partition,run_sort_key
 prefix=run_sort_key(invocation_id=pre.invocation_id,generation=1)+'#ATTEMPT#'
 h1=S._get(task_run_partition(pre.task_id),prefix+old);h2=S._get(task_run_partition(pre.task_id),prefix+newid)
 check('explicit attempt history preserves predecessor and versions',h1 is not None and h2 is not None and h1['previous_runtime_attempt_id'] is None and h2['previous_runtime_attempt_id']==old and [h1['task_version'],h2['task_version']]==[2,3])
 check('attempt replacement preserves task and run identity',S.read_task(pre.task_id)['invocation_id']==pre.invocation_id and S.read_task(pre.task_id)['runtime_attempt_id']==newid)
 emit('summary',status='PASS',checks=len(results),limitations=['No production scheduled recovery wake-up or public identity qualification in this isolated lane','No model effects dispatched','Queue deletion fault is transport evidence, not runtime acknowledgment authority proof'])
finally:
 cleanup_errors=[]
 if Qname:
  try:
   if not queue:queue=Q.get_queue_url(QueueName=Qname)['QueueUrl']
   Q.delete_queue(QueueUrl=queue);emit('queue_deleted',queue=queue)
  except Exception as exc:cleanup_errors.append({'queue_name':Qname,'error':str(exc)})
 for table,key in reversed(list(ledger.values())):
  try:D.delete_item(TableName=table,Key=key)
  except Exception as exc:cleanup_errors.append({'table':table,'key':key,'error':str(exc)})
 missing=True
 for table,key in ledger.values():
  try:
   if D.get_item(TableName=table,Key=key,ConsistentRead=True).get('Item'):missing=False
  except Exception as exc:missing=False;cleanup_errors.append({'table':table,'key':key,'read_error':str(exc)})
 emit('cleanup',keys=len(ledger),all_owned_keys_absent=missing,errors=cleanup_errors)
 if context:context.stop()
 assert missing and not cleanup_errors

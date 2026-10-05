"""Bounded production Redis Lua qualification in a unique fixture namespace.
--fake uses fakeredis/Lua only. --apply uses the configured existing IAM/TLS
Redis factory. No Task records, public credentials, provider calls or shared quota
keys are modified. Journals all exact keys before mutation, cleans independently.
"""
import argparse,asyncio,json,uuid,time
from datetime import datetime,UTC
from src.tasks.stream_leases import RedisStreamRegistry,LEASE_SECONDS,_digest
from src.tasks.errors import TaskApiError
from src.shared.config import get_settings
from src.shared.redis_client import create_redis_client

parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--fake',action='store_true');args=parser.parse_args()
def emit(kind,**kw):print(json.dumps({'at':datetime.now(UTC).isoformat(),'kind':kind,**kw},default=str),flush=True)
async def main():
 if not(args.apply or args.fake):print(__doc__);return
 if args.fake:
  import fakeredis.aioredis
  server=fakeredis.FakeServer();clients=[fakeredis.aioredis.FakeRedis(server=server) for _ in range(2)]
 else:
  settings=get_settings();assert settings.redis_url
  clients=[create_redis_client(settings.redis_url,socket_timeout=2,socket_connect_timeout=2) for _ in range(2)]
 namespace='task-v3-fixture-'+uuid.uuid4().hex;registries=[RedisStreamRegistry(c,namespace) for c in clients];keys=set();leases=[];passed=[]
 emit('scope',namespace=namespace,redis_key_prefix=registries[0].prefix,client_count=2,lease_seconds=LEASE_SECONDS,model_calls=0,task_writes=0)
 def check(name,value,**details):assert value,name;passed.append(name);emit('PASS',check=name,**details)
 async def acquire(task,principal,index=0):
  r=registries[index];tenant='fixture'
  planned=[r.prefix+':environment',r.prefix+':principal:'+_digest(tenant,principal),r.prefix+':task:'+_digest(tenant,task)]
  for key in planned:
   if key not in keys:keys.add(key);emit('cleanup_key',key=key)
  try:
   lease=await r.acquire_lease(tenant_id=tenant,principal_id=principal,task_id=task)
  except TaskApiError as exc:
   assert exc.status==429,f'unexpected admission error {exc.status}'
   return None
  leases.append(lease);return lease
 async def release_all():
  await asyncio.gather(*(lease.release() for lease in leases))
  check('all current environment memberships released '+str(len(passed)),await clients[0].zcard(registries[0].prefix+':environment')==0)
 try:
  results=await asyncio.gather(*(acquire('taskcap','principal',i%2) for i in range(5)))
  winners=[v for v in results if v];check('concurrent independent clients enforce 2 per task',len(winners)==2)
  await winners[0].release();replacement=await acquire('taskcap','principal',1);check('released slot can be reacquired cross client',replacement is not None)
  await winners[0].release();check('stale duplicate release preserves replacement',await clients[0].zscore(replacement.keys[0],replacement.token) is not None)
  await release_all()
  results=await asyncio.gather(*(acquire('principal-task-'+str(i),'same-principal',i%2) for i in range(13)))
  check('independent clients enforce 10 per principal',sum(v is not None for v in results)==10)
  await release_all()
  results=await asyncio.gather(*(acquire('env-task-'+str(i),'env-principal-'+str(i),i%2) for i in range(36)))
  check('independent clients enforce 32 per environment',sum(v is not None for v in results)==32)
  await release_all()
  live=await acquire('renew-live','renew-principal');check('current native lease renews',await live.renew())
  # Remove exactly one owned scope membership to simulate partial eviction.
  await clients[1].zrem(live.keys[1],live.token)
  check('missing-scope renewal cannot resurrect ownership',not await live.renew() and await clients[0].zscore(live.keys[1],live.token) is None)
  await release_all()
  expired=await acquire('expired','expiry-principal')
  started=time.monotonic();emit('waiting_for_real_expiry',seconds=LEASE_SECONDS+0.25)
  await asyncio.sleep(LEASE_SECONDS+0.25)
  check('local expired lease cannot renew',not await expired.renew(),elapsed_seconds=time.monotonic()-started)
  # Force only local deadline forward so production Lua independently evaluates
  # the actual expired Redis score; this must still refuse remote resurrection.
  expired.deadline=time.monotonic()+LEASE_SECONDS
  check('Redis clock independently refuses expired token',not await expired.renew())
  renewed=await acquire('expired','expiry-principal',1);check('expiry frees quota to independent client',renewed is not None)
  await expired.release();check('old expired-token release preserves new token',await clients[0].zscore(renewed.keys[0],renewed.token) is not None)
  await release_all()
  emit('summary',status='PASS',checks=len(passed),limitations=['Private namespace real Redis Lua/connection qualification; public gateway cross-replica behavior separately qualified','Does not change deployment quota state or exercise provider workloads'])
 finally:
  errors=[]
  for key in sorted(keys):
   try:await clients[0].delete(key)
   except Exception as exc:errors.append({'key':key,'error':str(exc)})
  remaining=[]
  for key in sorted(keys):
   try:
    if await clients[0].exists(key):remaining.append(key)
   except Exception as exc:errors.append({'key':key,'read_error':str(exc)})
  emit('cleanup',exact_keys=len(keys),remaining=remaining,errors=errors)
  for c in clients:await c.aclose()
  assert not remaining and not errors
asyncio.run(main())

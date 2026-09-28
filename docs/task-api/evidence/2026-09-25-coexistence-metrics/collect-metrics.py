import boto3, datetime as dt, json, pathlib, concurrent.futures
root=pathlib.Path(__file__).parent
cw=boto3.client('cloudwatch',region_name='us-east-1')
windows={'baseline':('2026-09-25T05:10:00+00:00','2026-09-25T05:12:00+00:00'),'during':('2026-09-25T05:12:00+00:00','2026-09-25T05:30:00+00:00')}
specs=[('AWS/Lambda',m,[{'Name':'FunctionName','Value':'adp-dev-github-webhook'}],['Sum']) for m in ['Invocations','Errors','Throttles']]
specs += [('AWS/SQS','ApproximateAgeOfOldestMessage',[{'Name':'QueueName','Value':'adp-dev-agent-submit.fifo'}],['Maximum','Average'])]
specs += [('AWS/DynamoDB','SuccessfulRequestLatency',[{'Name':'TableName','Value':'adp-dev-webhook-events'},{'Name':'Operation','Value':'GetItem'}],['Maximum','Average'])]
def get(args):
 phase,window,spec=args;ns,metric,dims,stats=spec
 response=cw.get_metric_statistics(Namespace=ns,MetricName=metric,Dimensions=dims,StartTime=dt.datetime.fromisoformat(window[0]),EndTime=dt.datetime.fromisoformat(window[1]),Period=60,Statistics=stats)
 return {'phase':phase,'window':window,'namespace':ns,'metric':metric,'dimensions':dims,'datapoints':sorted(response['Datapoints'],key=lambda x:x['Timestamp'])}
with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
 results=list(pool.map(get,[(p,w,s) for p,w in windows.items() for s in specs]))
out={'captured_at':dt.datetime.now(dt.timezone.utc).isoformat(),'limitations':['Shared service aggregates include unrelated traffic; missing datapoints are not zero. Baseline window includes previously accepted Task activity; admission itself was disabled for the ten baseline probes. During window includes queueing and held Task execution, not continuous concurrent model requests.'], 'metrics':results}
(root/'cloudwatch-comparison.json').write_text(json.dumps(out,indent=2,default=str)+'\n')
for r in results: print(r['phase'],r['metric'],r['datapoints'])

import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { HostBridge, frame } from '../src/protocol.mjs';
import { normalizeRequest, streamedMessage, startProxy } from '../src/model-proxy.mjs';
import { groundedReport, runCyber, TOOL_NAMES, cyberTools } from '../src/driver.mjs';
const start = () => ({ task_id: 'tsk_' + randomUUID(), instructions: 'Investigate', inputs: { url: 'https://example.com' }, limits: { max_turns: 4, max_output_tokens_per_turn: 2000 } });
const report = () => ({ summary: 'Evidence insufficient', findings: [], uncertainties: ['No external analysis available'], recommendations: [], evidence_refs: [] });
const tick = () => new Promise(resolve => setImmediate(resolve));
test('SDK normalization strips transport/cache metadata and preserves tool semantics', () => {
  const input = {model:'untrusted', max_tokens:5000, stream:true, metadata:{user_id:'legacy'}, messages:[{role:'assistant',content:[{type:'tool_use',id:'t1',name:'triage',input:{x:1},cache_control:{type:'ephemeral'}}]}],system:[{type:'text',text:'sys',cache_control:{type:'ephemeral'}}],tools:[{name:'triage',input_schema:{type:'object'},cache_control:{type:'ephemeral'}}]};
  const result = normalizeRequest(input, 1000);
  assert.equal(result.max_tokens,1000); assert.equal(result.sdk_request.model,undefined);
  assert.equal(result.sdk_request.messages[0].content[0].cache_control,undefined);
  assert.deepEqual(result.sdk_request.messages[0].content[0].input,{x:1});
  assert.throws(() => normalizeRequest({messages:[{role:'user',content:[{type:'image'}]}]},1000));
  assert.throws(() => normalizeRequest({...input,tools:[{type:'web_search_20250305'}]},1000));
});
test('SSE contains tool JSON, real usage and terminal markers', () => {
  const result = streamedMessage({content:[{type:'tool_use',id:'t1',name:'triage',input:{x:1}}],stop_reason:'tool_use',usage:{input_tokens:7,output_tokens:9}});
  assert.match(result,/input_json_delta/); assert.match(result,/"input_tokens":7/); assert.match(result,/event: message_stop/);
});
test('host serializes model and broker calls and correlates operation', async () => {
  const writes=[]; const bridge = new HostBridge(start(),value=>writes.push(value));
  const model=bridge.model({messages:[]},100); const cyber=bridge.cyber('enrich',{sha256:'a'.repeat(64)});
  await tick(); assert.equal(writes.length,1);
  bridge.receive(frame('model.result',bridge.start.task_id,{turn_id:writes[0].turn_id,operation_status:'pending'}));
  await tick(); assert.equal(writes.length,1);
  bridge.receive(frame('model.result',bridge.start.task_id,{turn_id:writes[0].turn_id,operation_status:'confirmed',content:[{type:'text',text:'ok'}],stop_reason:'end_turn'}));
  await model; await tick(); assert.equal(writes.length,2);
  assert.throws(()=>bridge.receive({...writes[1],type:'tool.result',tool:'cyber.static',operation_status:'confirmed'}));
  bridge.receive({...writes[1],type:'tool.result',operation_status:'confirmed',result:{},artifact:{artifact_id:'art_evidence'}});
  await cyber; assert.equal(bridge.evidence.get('art_evidence').source,'artifact');
});
test('cancel interrupts blocked tool and input, binding cancellation command', async()=>{
  const bridge=new HostBridge(start(),()=>{}); const pending=bridge.ask('Need evidence');
  const command_id=randomUUID(); bridge.receive(frame('cancel',bridge.start.task_id,{command_id,intentional:true}));
  await assert.rejects(pending); assert.equal(bridge.cancelCommand,command_id); assert.equal(bridge.controller.signal.aborted,true);
});
test('unknown model outcome aborts proxy and never fabricates usage',async()=>{
  const bridge=new HostBridge(start(),value=>queueMicrotask(()=>bridge.receive(frame('model.result',bridge.start.task_id,{turn_id:value.turn_id,operation_status:'unknown'}))));
  const proxy=await startProxy(bridge,{maxTokens:100});
  try { const response=await fetch(proxy.url+'/v1/messages',{method:'POST',headers:{authorization:'Bearer '+proxy.token},body:JSON.stringify({messages:[{role:'user',content:'hello'}]})}); assert.equal(response.status,502);assert.equal(bridge.controller.signal.aborted,true); } finally {await proxy.close();}
});
test('report does not accept invented provenance or discard unsupported uncertainty',()=>{
  const value=report();value.findings=[{statement:'x'.repeat(2000),evidence_refs:['fake']}];value.evidence_refs=[{ref:'fake',source:'artifact',artifact_id:'fake'}];
  const actual=groundedReport(value,new Map());assert.equal(actual.findings.length,0);assert.equal(actual.evidence_refs.length,0);assert.equal(actual.uncertainties.slice(1).join(''),'Unsupported finding: '+'x'.repeat(2000));
});
test('driver has exact MCP-only SDK policy and closes session/proxy',async()=>{
  const bridge=new HostBridge(start(),()=>{});let closed=0;let options;
  const result=await runCyber(bridge.start,bridge,{proxyFactory:async()=>({url:'http://127.0.0.1:1',token:'local',close:async()=>closed++}),sdkQuery:args=>{options=args.options;bridge.report=report();return {async *[Symbol.asyncIterator](){yield {type:'result',subtype:'success',is_error:false};},close(){closed++;}};}});
  assert.equal(result.summary,'Evidence insufficient');assert.equal(closed,2);assert.deepEqual(options.tools,[]);assert.deepEqual(options.settingSources,[]);assert.equal(options.persistSession,false);assert.deepEqual(options.allowedTools,TOOL_NAMES);assert.equal(options.env.AWS_ACCESS_KEY_ID,undefined);assert.notEqual(options.env.HOME,process.env.HOME);assert.equal(options.cwd,options.env.HOME);assert.equal(existsSync(options.env.HOME),false);assert.equal((await options.canUseTool('Bash',{})).behavior,'deny');
});
test('skill enum rejects traversal at schema boundary',()=>{
  const tools=cyberTools(new HostBridge(start(),()=>{}));const skill=tools.find(t=>t.name==='read_skill');assert.throws(()=>skill.inputSchema.name.parse('../../secrets'));
});
test('queued jobs prevent report until terminal poll; mutation receipts deduplicate',async()=>{
 const bridge=new HostBridge(start(),()=>{});let calls=0;
 bridge.cyber=async operation=>{calls++;return {operation_status:'confirmed',result:{job_id:'job1',status:operation==='result'?'completed':'pending'}};};
 const tools=cyberTools(bridge);const tool=name=>tools.find(value=>value.name===name);
 const payload={sample_s3_uri:'s3://owned/sample'};
 await tool('triage').handler(payload);await tool('triage').handler(payload);assert.equal(calls,1);
 assert.equal((await tool('submit_report').handler(report())).isError,true);assert.equal(bridge.report,null);
 await tool('result').handler({job_id:'job1'});assert.equal(calls,2);
 await tool('submit_report').handler(report());assert.ok(bridge.report);
});
test('input replay does not lose command provenance or mint another turn',async()=>{
 const writes=[];const bridge=new HostBridge(start(),value=>writes.push(value));const input=bridge.ask('Needed');
 const turn=frame('turn',bridge.start.task_id,{turn_id:randomUUID(),messages:[{command_id:randomUUID(),text:'supplied'}]});
 bridge.receive(turn);assert.equal(await input,'supplied');bridge.receive(turn);
 assert.equal(bridge.nextTurn,turn.turn_id);assert.equal(bridge.evidence.get('follow_up_input.'+turn.messages[0].command_id).source,'follow_up_input');
 assert.throws(()=>bridge.receive({...turn,messages:[{...turn.messages[0],text:'changed'}]}));
});
test('secondary SDK abort preserves original unknown model failure',()=>{
 const bridge=new HostBridge(start(),()=>{});const original=new Error('model_outcome_unknown');bridge.fail(original);bridge.fail(new Error('cancelled'));assert.equal(bridge.failure,original);
});

test('tool citations let an aliased report correct exact references before acceptance',async()=>{
 const writes=[];const bridge=new HostBridge(start(),value=>writes.push(value));
 const tools=cyberTools(bridge);const tool=name=>tools.find(value=>value.name===name);
 const pending=tool('result').handler({job_id:'job1'});
 while (!writes.some(value=>value.type==='tool.request')) await tick();
 const citation={ref:'art_evidence',source:'artifact',artifact_id:'art_evidence'};
 bridge.receive({...writes.find(value=>value.type==='tool.request'),type:'tool.result',operation_status:'confirmed',result:{job_id:'job1',status:'completed'},artifact:{artifact_id:'art_evidence'}});
 const receipt=JSON.parse((await pending).content[0].text);assert.deepEqual(receipt.evidence_refs,[citation]);
 const value={...report(),evidence_refs:[{...citation,ref:'triage_result'}],findings:[{statement:'Observed file type',evidence_refs:['triage_artifact_art_evidence']}]};
 const rejected=await tool('submit_report').handler(value);assert.equal(rejected.isError,true);assert.equal(bridge.report,null);
 const feedback=JSON.parse(rejected.content[0].text);assert.ok(feedback.evidence_refs.some(ref=>JSON.stringify(ref)===JSON.stringify(citation)));
 value.evidence_refs=[citation];value.findings[0].evidence_refs=['art_evidence'];
 assert.deepEqual(JSON.parse((await tool('submit_report').handler(value)).content[0].text),{accepted:true});
 assert.deepEqual(bridge.report,value);
});

test('Archive submission polls accepted work without replaying the query',async()=>{
 const bridge=new HostBridge(start(),()=>{});const calls=[];const waits=[];
 bridge.cyber=async(operation,payload)=>{calls.push(operation);return {operation_status:'confirmed',result:{scan_id:'a'.repeat(64),status:calls.length<3?'pending':'completed',view_id:'view1'}};};
 const tools=cyberTools(bridge,{sleep:async ms=>waits.push(ms)});
 const payload={url:'https://example.com'};
 const result=await tools.find(t=>t.name==='common_crawl_scan').handler(payload);
 assert.deepEqual(calls,['common_crawl_scan','common_crawl_result','common_crawl_result']);
 assert.deepEqual(waits,[2000,4000]);assert.equal(JSON.parse(result.content[0].text).result.view_id,'view1');
 await tools.find(t=>t.name==='common_crawl_scan').handler(payload);assert.equal(calls.length,3);
});

test('SDK model proxy preserves bounded inline tool images and rejects remote URLs',()=>{
 const image={type:'image',source:{type:'base64',media_type:'image/jpeg',data:'/9j/Zml4dHVyZQ=='}};
 const body={messages:[{role:'user',content:[{type:'tool_result',tool_use_id:'toolu_1',content:[image]}]}]};
 assert.deepEqual(normalizeRequest(body,100).sdk_request.messages[0].content[0].content,[image]);
 assert.throws(()=>normalizeRequest({messages:[{role:'user',content:[{type:'tool_result',tool_use_id:'toolu_1',content:[{type:'image',source:{type:'url',url:'https://untrusted.example'}}]}]}]},100));
});

test('report derives metadata from exact finding citations but refuses an unknown citation',async()=>{
 const bridge=new HostBridge(start(),()=>{});
 const citation={ref:'art_known',source:'artifact',artifact_id:'art_known'};
 bridge.evidence.set(citation.ref,citation);
 const submit=cyberTools(bridge).find(t=>t.name==='submit_report');
 const input={summary:'Observed a page',findings:[{statement:'Example page',evidence_refs:['art_known']}]};
 assert.equal(JSON.parse((await submit.handler(input)).content[0].text).accepted,true);
 assert.deepEqual(bridge.report.evidence_refs,[citation]);
 assert.deepEqual(bridge.report.uncertainties,[]);
 bridge.report=null;
 const bad={...input,findings:[{statement:'Example page',evidence_refs:['art_typo']}]};
 assert.equal((await submit.handler(bad)).isError,true);
 assert.equal(bridge.report,null);
});

test('accepted report is acknowledged without another model call or fabricated usage',async()=>{
 const bridge=new HostBridge(start(),()=>{});bridge.report=report();bridge.model=()=>{throw new Error('must not invoke provider after report');};
 const proxy=await startProxy(bridge,{maxTokens:100});
 try {
  const response=await fetch(proxy.url+'/v1/messages',{method:'POST',headers:{authorization:'Bearer '+proxy.token},body:JSON.stringify({messages:[{role:'user',content:'tool report accepted'}]})});
  const value=await response.json();assert.equal(response.status,200);assert.equal(value.stop_reason,'end_turn');assert.deepEqual(value.content,[]);assert.equal(value.usage,undefined);assert.equal(bridge.failure,null);
 } finally {await proxy.close();}
});
test('last two turns require report with an earlier cleanup warning',async()=>{
 const bridge=new HostBridge(start(),()=>{});const requests=[];
 bridge.model=async (request)=>{requests.push(request);return {turn_id:randomUUID(),content:[],stop_reason:'end_turn'};};
 const proxy=await startProxy(bridge,{maxTokens:100,maxTurns:3,finalReportTool:'mcp__cyber__submit_report'});
 try {
  for(let i=0;i<3;i++) {
   const response=await fetch(proxy.url+'/v1/messages',{method:'POST',headers:{authorization:'Bearer '+proxy.token},body:JSON.stringify({messages:[{role:'user',content:'investigate'}],tools:[{name:'mcp__cyber__submit_report',input_schema:{type:'object'}}]})});assert.equal(response.status,200);
  }
  assert.match(requests[0].system.at(-1).text,/close browser sessions/);assert.equal(requests[0].tool_choice,undefined);
  for (const request of requests.slice(1)) assert.deepEqual(request.tool_choice,{type:'tool',name:'mcp__cyber__submit_report',disable_parallel_tool_use:true});
 } finally {await proxy.close();}
});

test('oversized tool history compacts previews without changing instructions or call IDs',()=>{
 const body={messages:[{role:'user',content:'Keep these instructions'},...Array.from({length:10},(_,i)=>({role:'user',content:[{type:'tool_result',tool_use_id:'tool_'+i,content:[{type:'text',text:JSON.stringify({artifact_id:'art_'+i,result:'x'.repeat(8000)})}]}]}))],tools:[{name:'mcp__cyber__submit_report',input_schema:{type:'object'}}]};
 const before=JSON.stringify(body);const {sdk_request}=normalizeRequest(body,100);
 assert.ok(Buffer.byteLength(JSON.stringify(sdk_request))<61440);
 assert.equal(JSON.stringify(body),before);assert.equal(sdk_request.messages[0].content,body.messages[0].content);
 assert.deepEqual(sdk_request.tools,body.tools);
 assert.match(sdk_request.messages[1].content[0].content[0].text,/context_notice/);
 for(let i=0;i<10;i++) {assert.equal(sdk_request.messages[i+1].content[0].tool_use_id,'tool_'+i);assert.match(JSON.stringify(sdk_request.messages[i+1]),new RegExp('art_'+i));}
 assert.throws(()=>normalizeRequest({messages:[{role:'user',content:'x'.repeat(70000)}]},100),/frame bound/);
});

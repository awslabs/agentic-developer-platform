/** Actual built child, shared parser, SDK and framed host exchange. No provider. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { createInterface } from 'node:readline';
import { randomUUID } from 'node:crypto';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';

test('built Cyber entrypoint carries frozen grants through to SDK and final report', {timeout:45000}, async()=>{
 const home=await mkdtemp(tmpdir()+'/task-entry-host-');
 const child=spawn(process.execPath,[fileURLToPath(new URL('../dist/index.js',import.meta.url)),'--embedded'],{
  env:{PATH:process.env.PATH,HOME:home,TMPDIR:home,LANG:'C.UTF-8',LC_ALL:'C.UTF-8',ADP_TASK_NETWORK:'host-mediated-sdk'},stdio:['pipe','pipe','pipe']});
 const exit=once(child,'exit');let stderr='';child.stderr.on('data',b=>{stderr+=b;});
 const task_id='tsk_'+randomUUID();const send=(type,fields={})=>child.stdin.write(JSON.stringify({protocol_version:1,type,request_id:randomUUID(),task_id,...fields})+'\n');
 const timer=setTimeout(()=>child.kill('SIGKILL'),35000);let ready=false,models=0,result;
 try {
  send('start',{invocation_id:randomUUID(),generation:1,runtime_attempt_id:randomUUID(),instructions:'Submit a report with the supplied context.',inputs:{url:'https://example.com'},artifacts:[],tool_grants:['cyber.browser_start','cyber.browser_close'],limits:{max_turns:400,max_output_tokens_per_turn:8001}});
  for await(const line of createInterface({input:child.stdout})) {
   const frame=JSON.parse(line);
   const validation=spawnSync('python3',['-c',
     'import sys,json;sys.path.insert(0,sys.argv[1]);from lib.task_protocol import validate_child_frame;f=json.load(sys.stdin);validate_child_frame(f,f["task_id"])',
     fileURLToPath(new URL('../../../agent-worker-image',import.meta.url))],{input:JSON.stringify(frame),encoding:'utf8'});
   assert.equal(validation.status,0,validation.stderr);
   if(frame.type==='ready')ready=true;
   if(frame.type==='error')assert.fail(JSON.stringify(frame));
   if(frame.type==='model.request'){
    models++;const names=frame.sdk_request.tools.map(t=>t.name);
    assert.ok(names.includes('mcp__cyber__browser_start'));
    assert.ok(!names.includes('mcp__cyber__url_analysis'));
    send('model.result',{turn_id:frame.turn_id,operation_status:'confirmed',content:[{type:'tool_use',id:'toolu_final',name:'mcp__cyber__submit_report',input:{summary:'Supplied URL recorded; live evidence was not requested in this test.',findings:[],uncertainties:['Scripted provider response'],recommendations:[]}}],stop_reason:'tool_use',usage:{input_tokens:100,output_tokens:50}});
   }
   if(frame.type==='result')result=frame.report;
  }
  const [code]=await exit;
  assert.equal(code,0,stderr);assert.ok(ready);assert.equal(models,1);assert.match(result.summary,/Supplied URL/);
 } finally {clearTimeout(timer);child.kill('SIGKILL');await rm(home,{recursive:true,force:true});}
});

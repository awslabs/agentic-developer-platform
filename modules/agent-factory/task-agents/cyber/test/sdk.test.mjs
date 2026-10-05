/** Real SDK subprocess + in-process MCP; scripted model, no provider connection. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { HostBridge } from '../src/protocol.mjs';
import { runCyber } from '../src/driver.mjs';
test('real SDK executes exact MCP submit_report through loopback model adapter', {timeout:45000},async()=>{
 const home=await mkdtemp(tmpdir()+'/task-cyber-sdk-');const previous=process.env.HOME;process.env.HOME=home;
 const start={task_id:'tsk_'+randomUUID(),instructions:'Submit an uncertainty report.',inputs:{},limits:{max_turns:4,max_output_tokens_per_turn:2000}};
 const bridge=new HostBridge(start,()=>{});let calls=0;
 const report={summary:'No analysis requested',findings:[],uncertainties:['Scripted model test only'],recommendations:[],evidence_refs:[]};
 bridge.model=async request=>{
  calls++;assert.ok(request.tools.every(tool=>tool.name.startsWith('mcp__cyber__')));
  return {turn_id:randomUUID(),operation_status:'confirmed',content:calls===1?[{type:'tool_use',id:'toolu_test1',name:'mcp__cyber__submit_report',input:report}]:[{type:'text',text:'Report submitted.'}],stop_reason:calls===1?'tool_use':'end_turn',usage:{input_tokens:100,output_tokens:50}};
 };
 const timer=setTimeout(()=>bridge.fail(new Error('SDK smoke timeout')),35000);
 try {assert.deepEqual(await runCyber(start,bridge),report);assert.equal(calls,1);}finally{clearTimeout(timer);process.env.HOME=previous;await rm(home,{recursive:true,force:true});}
});

test('real SDK accepts a grounded report submitted on the final permitted turn', {timeout:45000},async()=>{
 const start={task_id:'tsk_'+randomUUID(),instructions:'Submit the report.',inputs:{},limits:{max_turns:1,max_output_tokens_per_turn:2000}};
 const bridge=new HostBridge(start,()=>{});let calls=0;
 bridge.model=async()=>{
  calls++;assert.equal(calls,1,'no extra model call is permitted');
  return {turn_id:randomUUID(),operation_status:'confirmed',content:[{type:'tool_use',id:'toolu_final',name:'mcp__cyber__submit_report',input:{summary:'No evidence requested',findings:[]}}],stop_reason:'tool_use',usage:{input_tokens:100,output_tokens:50}};
 };
 const result=await runCyber(start,bridge);
 assert.equal(result.summary,'No evidence requested');assert.deepEqual(result.evidence_refs,[]);assert.equal(calls,1);
});

test('real SDK accepts bounded screenshot tool evidence', {timeout:45000}, async()=>{
 const start={task_id:'tsk_'+randomUUID(),tool_grants:['cyber.browser_inspect'],instructions:'Inspect the screenshot then submit a report.',inputs:{},limits:{max_turns:4,max_output_tokens_per_turn:2000}};
 const bridge=new HostBridge(start,()=>{});let calls=0;
 bridge.cyber=async()=>({operation_status:'confirmed',artifact:{artifact_id:'art_image'},result:{image:{media_type:'image/png',data:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5V8AAAAASUVORK5CYII='}}});
 bridge.model=async request=>{
  calls++;
  return {turn_id:randomUUID(),operation_status:'confirmed',content:[{type:'tool_use',id:'toolu_'+calls,name:calls===1?'mcp__cyber__browser_inspect':'mcp__cyber__submit_report',input:calls===1?{session_id:'a'.repeat(64),section:'screenshot'}:{summary:'Screenshot inspected',findings:[]}}],stop_reason:'tool_use',usage:{input_tokens:100,output_tokens:50}};
 };
 try {await runCyber(start,bridge);assert.equal(calls,2);} catch(e) {throw bridge.failure || e;}
});

test('real SDK repairs malformed report within the reserved turns', {timeout:45000},async()=>{
 const start={task_id:'tsk_'+randomUUID(),instructions:'Submit a report.',inputs:{},limits:{max_turns:2,max_output_tokens_per_turn:2000}};
 const bridge=new HostBridge(start,()=>{});let calls=0;
 bridge.model=async request=>{
  calls++;assert.equal(request.tool_choice.name,'mcp__cyber__submit_report');
  return {turn_id:randomUUID(),operation_status:'confirmed',content:[{type:'tool_use',id:'toolu_repair'+calls,name:'mcp__cyber__submit_report',input:{summary:'Evidence unavailable',findings:[],uncertainties:calls===1?'["Unavailable"]}\n':['Unavailable']}}],stop_reason:'tool_use',usage:{input_tokens:100,output_tokens:50}};
 };
 const result=await runCyber(start,bridge);assert.deepEqual(result.uncertainties,['Unavailable']);assert.equal(calls,2);
});

test('SDK recovers from a model naming an ungranted tool without dispatching it', {timeout:45000}, async()=>{
 const start={task_id:'tsk_'+randomUUID(),instructions:'Inspect if authorized, otherwise report the limitation.',inputs:{url:'https://example.com'},tool_grants:['cyber.browser_start'],limits:{max_turns:4,max_output_tokens_per_turn:2000}};
 const bridge=new HostBridge(start,()=>{});let calls=0;
 bridge.cyber=async()=>{assert.fail('ungranted operation reached the host');};
 bridge.model=async request=>{
  calls++;
  assert.ok(!request.tools.some(tool=>tool.name==='mcp__cyber__url_analysis'));
  if(calls===2) assert.ok(request.messages.some(message => Array.isArray(message.content) && message.content.some(item => item.type === 'tool_result' && item.tool_use_id === 'toolu_permission1' && item.is_error === true)));
  return {turn_id:randomUUID(),operation_status:'confirmed',content:[{type:'tool_use',id:'toolu_permission'+calls,name:calls===1?'mcp__cyber__url_analysis':'mcp__cyber__submit_report',input:calls===1?{url:'https://example.com'}:{summary:'Live evidence unavailable',findings:[],uncertainties:['Requested operation was not authorized']}}],stop_reason:'tool_use',usage:{input_tokens:100,output_tokens:50}};
 };
 const result=await runCyber(start,bridge);assert.equal(calls,2);assert.equal(result.summary,'Live evidence unavailable');
});

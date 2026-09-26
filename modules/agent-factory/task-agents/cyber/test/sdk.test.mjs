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

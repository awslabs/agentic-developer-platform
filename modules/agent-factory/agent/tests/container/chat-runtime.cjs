const assert = require('node:assert/strict');
const Module = require('node:module');
const fs = require('node:fs');
assert.equal(process.getuid(), 10001);
assert.equal(process.getgid(), 10001);
const path = '/app/dist/complex-task-chat/';
const failure = process.env.FIXTURE_FAILURE === '1';
const responses = [], deleted = [];
const task = {task_id:'fixture-task',session_id:'fixture-session',message_id:'fixture-message',message:'fixture question',agent_type:'developer',user_id:'fixture-user',tenant_id:'fixture-tenant',channel:'websocket',connection_id:'fixture-connection'};
function replace(file, exports) { const id = require.resolve(path+file); require.cache[id] = {id, filename:id,loaded:true,exports}; }
const realSqs = require(path+'sqs-client.js');
replace('sqs-client.js', {...realSqs, SqsClient: class {
 async receive(){return [{Body:JSON.stringify(task),ReceiptHandle:'fixture-receipt'}]}
 async sendResponse(r){responses.push(r)}
 async sendAgUiEvent(){}
 async sendProgress(){}
 async deleteMessage(r){deleted.push(r)}
}});
// Cloud transport is mocked; the packaged orchestration, persona, context,
// memory, tool assembly, response formatting and failure handling run unchanged.
replace('bedrock-routing.js',{withChatBedrockRouting:async(task,run)=>run()});
replace('run-query.js',{runQuery:async args=>{assert.equal(args.userMessage,task.message);assert.ok(args.systemPrompt.length>0);if(failure)throw new Error('fixture provider unavailable');return {text:'fixture-ok',tokens:{input:1,output:1},turnCount:1};}});
process.on('exit',()=>{
 assert.equal(responses.length,1);
 assert.equal(responses[0].task_id,task.task_id);
 assert.equal(responses[0].session_id,task.session_id);
 assert.equal(responses[0].status,failure?'failed':'completed');
 if(!failure)assert.equal(responses[0].text,'fixture-ok');
 assert.deepEqual(deleted,failure?[]:['fixture-receipt']);
 assert.ok(!JSON.stringify(responses).includes(process.env.FIXTURE_SECRET));
 fs.writeFileSync('/tmp/result.json',JSON.stringify({mode:failure?'failure':'success',assertions:'passed'}));
});
require(path+'complex-task-chat-agent.js');

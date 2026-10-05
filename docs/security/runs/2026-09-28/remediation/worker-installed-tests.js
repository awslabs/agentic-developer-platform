const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const { hasRepositoryWritePermission, parsePlanApproval } = require('/app/dist/utils/comment-authority');
const { parseReassessmentResponse } = require('/app/dist/reassessment');
const memory = require('/app/dist/memory');
(async () => {
  assert.notEqual(process.getuid(), 0);
  for (const permission of ['admin','maintain','write','read','triage','none','unknown']) {
    global.fetch = async url => {
      assert.equal(url, 'https://api.github.com/repos/owner/repo/collaborators/alice/permission');
      return {ok:true,json:async()=>({permission})};
    };
    assert.equal(await hasRepositoryWritePermission('owner','repo','alice','synthetic'), ['admin','maintain','write'].includes(permission));
  }
  global.fetch = async () => { throw Error('synthetic failure'); };
  assert.equal(await hasRepositoryWritePermission('owner','repo','alice','synthetic'), false);
  assert.equal(await hasRepositoryWritePermission('owner','repo','alice',''), false);
  assert.equal(await hasRepositoryWritePermission('owner','repo','app[bot]','synthetic'), false);
  assert.deepEqual(parsePlanApproval('/approve current','current'), {approved:true,feedback:''});
  for (const body of ['/approve old','/approve current extra','> /approve current']) assert.equal(parsePlanApproval(body,'current'), null);
  for (const body of ['/approved','/skip-anything','> /approve','/action 1 trailing']) assert.equal(parseReassessmentResponse(body).action,'unknown');
  assert.equal(parseReassessmentResponse('/approve').action,'approve_all');
  const root=fs.mkdtempSync('/tmp/worker-security-');
  const git=(cwd,...args)=>execFileSync('git',args,{cwd,encoding:'utf8',stdio:['ignore','pipe','pipe'],env:{...process.env,GIT_CONFIG_NOSYSTEM:'1',GIT_CONFIG_GLOBAL:'/dev/null'}}).trim();
  const remote=path.join(root,'remote.git'),seed=path.join(root,'seed'),work=path.join(root,'work');
  git(root,'init','--bare','--initial-branch=main',remote);git(root,'init','--initial-branch=main',seed);
  git(seed,'config','user.name','Synthetic');git(seed,'config','user.email','fixture@example.invalid');
  fs.writeFileSync(path.join(seed,'README.md'),'source');git(seed,'add','.');git(seed,'commit','-m','source');git(seed,'remote','add','origin',remote);git(seed,'push','origin','main');
  git(seed,'checkout','--orphan','adp');git(seed,'rm','-rf','.');
  const dir=path.join(seed,'agent_context/components/general');fs.mkdirSync(dir,{recursive:true});
  const names=['$(touch memory-injected).md','`touch memory-injected`.md','quote" space.md','line\nbreak.md'];
  names.forEach((name,i)=>fs.writeFileSync(path.join(dir,name),'literal-'+i));
  git(seed,'add','.');git(seed,'commit','-m','fixture');git(seed,'push','origin','adp');
  git(root,'clone','--depth=1',pathToFileURL(remote).href,work);
  memory.configureMemory({cwd:work,issueNumber:'42',agentType:'reviewer',log:()=>{}});
  const result=await memory.readComponentContext('general');for(let i=0;i<names.length;i++)assert.ok(result.includes('literal-'+i));
  assert.equal(fs.existsSync(path.join(work,'memory-injected')),false);
  console.log(JSON.stringify({uid:process.getuid(),permission_matrix:'passed',lookup_failure:'denied',plan_binding:'passed',whole_commands:'passed',literal_git_filenames:'passed',shell_marker_created:false,network:'none',credentials:'synthetic only'}));
})().catch(e=>{console.error(e);process.exitCode=1;});

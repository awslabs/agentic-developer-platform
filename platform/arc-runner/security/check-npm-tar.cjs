const fs=require('fs'),path=require('path'),os=require('os'),zlib=require('zlib'),assert=require('assert');
const tar=require(process.argv[2]);
(async()=>{
 const root=fs.mkdtempSync('/tmp/tar-security-');
 try {
  const src=path.join(root,'src');fs.mkdirSync(src);fs.writeFileSync(path.join(src,'payload'),Buffer.alloc(8*1024*1024));
  const chunks=[];for await(const chunk of tar.c({cwd:src},['payload']))chunks.push(chunk);
  const bomb=path.join(root,'bounded-fixture.tgz');fs.writeFileSync(bomb,zlib.gzipSync(Buffer.concat(chunks)));
  const dest=path.join(root,'dest');fs.mkdirSync(dest);let rejected=false;
  try {await tar.x({file:bomb,cwd:dest,strict:true});} catch(e) {assert.match(String(e),/max decompression ratio exceeded/);rejected=true;}
  assert(rejected,'default decompression ratio accepted bounded high-expansion fixture');
  fs.writeFileSync(path.join(src,'payload'),'valid package control');
  const control=path.join(root,'control.tgz');await tar.c({cwd:src,gzip:true,file:control},['payload']);
  const good=path.join(root,'good');fs.mkdirSync(good);await tar.x({file:control,cwd:good,strict:true});assert.equal(fs.readFileSync(path.join(good,'payload'),'utf8'),'valid package control');
  console.log('Default decompression bound rejects fixture; valid archive extracts');
 } finally {fs.rmSync(root,{recursive:true,force:true});}
})().catch(e=>{console.error(e.message);process.exitCode=1;});

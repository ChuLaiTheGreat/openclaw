// Exact built Gateway, authenticated native webhook and real durable ingress.
// All identities, Bot API responses and preparer effects are synthetic.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import {spawn,execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {DatabaseSync} from 'node:sqlite';
const exec=promisify(execFile);
const P=path.resolve('.artifacts/native-negative');fs.mkdirSync(P,{recursive:true});
const save=(name,value)=>fs.writeFileSync(path.join(P,name),JSON.stringify(value,null,2));
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const calls=[];let gateway,server,passed=false,exitCode=null;
const names=['state','workspace','plugin'];for(const n of names)fs.mkdirSync(path.join(P,n),{recursive:true});
fs.writeFileSync(path.join(P,'workspace/AGENTS.md'),'Isolated synthetic channel fixture. No external actions.\n');
const plugin=`import fs from 'node:fs';
const c={hooks:0,lookups:0,positive:0};
export default {id:'negative-proof',register(api){
 api.on('before_channel_ingress',async(event,context)=>{
  c.hooks++;
  const m=event.providerUpdate.message;
  await fetch('http://127.0.0.1:19413/bot888888:fixture/getFile',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({file_id:m.voice?.file_id??'unexpected'})});
  c.lookups++;
  if(event.providerUpdate.update_id!==803005)throw Error('DENIED_PREPARER_REACHED_PRIVATE_EFFECT');
  c.positive++;
  const timer=setInterval(()=>context.heartbeat(),context.heartbeatIntervalMs);timer.unref();
  try { await new Promise((resolve,reject)=>{
   const aborted=()=>{fs.writeFileSync(${JSON.stringify(path.join(P,'positive-aborted.json'))},JSON.stringify({aborted:true,counters:c}));reject(Error('SYNTHETIC_POSITIVE_SCOPE_CLOSED'));};
   if(context.abortSignal.aborted)aborted();else context.abortSignal.addEventListener('abort',aborted,{once:true});
  }); } finally {clearInterval(timer);}
 },{priority:10000,timeoutMs:600000});
 api.registerGatewayMethod('negativeProof.status',({respond})=>respond(true,{head:process.env.GIT_COMMIT,registered:true,counters:c}),{scope:'operator.read'});
}};
`;
fs.writeFileSync(path.join(P,'plugin/index.mjs'),plugin);
fs.writeFileSync(path.join(P,'plugin/package.json'),JSON.stringify({name:'negative-proof',version:'1.0.0',openclaw:{extensions:['./index.mjs']}}));
fs.writeFileSync(path.join(P,'plugin/openclaw.plugin.json'),JSON.stringify({id:'negative-proof',name:'Synthetic native gate proof',activation:{onStartup:true},configSchema:{type:'object',properties:{},additionalProperties:false}}));
const cfg={gateway:{mode:'local',port:18792,bind:'loopback',auth:{mode:'none'},controlUi:{enabled:false}},logging:{level:'debug',file:path.join(P,'native-gateway.log')},
 models:{providers:{openai:{api:'openai-chatgpt-responses',auth:'oauth',baseUrl:'http://127.0.0.1:19412',request:{allowPrivateNetwork:true},models:[{id:'gpt-test',name:'Unconnected synthetic model',api:'openai-chatgpt-responses',contextWindow:128000,maxTokens:1024}]}}},
 agents:{defaults:{model:{primary:'openai/gpt-test',fallbacks:[]},models:{'openai/gpt-test':{agentRuntime:{id:'openclaw'}}},heartbeat:{every:'0m'}},entries:{main:{workspace:path.join(P,'workspace')}}},
 channels:{telegram:{enabled:true,botToken:'888888:fixture',apiRoot:'http://127.0.0.1:19413',dmPolicy:'allowlist',allowFrom:['123456'],webhookUrl:'https://synthetic.invalid/telegram-webhook',webhookPath:'/telegram-webhook',webhookSecret:'synthetic-gate-secret',legacyWebhook:false,streaming:{mode:'off'}}},
 commands:{native:true,text:true,config:false,allowFrom:{telegram:['999999']}},messages:{inbound:{debounceMs:0}},
 plugins:{enabled:true,allow:['openai','telegram','negative-proof'],slots:{memory:'none'},load:{paths:[path.resolve('dist/extensions/openai'),path.resolve('dist/extensions/telegram'),path.join(P,'plugin')]},entries:{openai:{enabled:true},telegram:{enabled:true},'negative-proof':{enabled:true,hooks:{allowConversationAccess:true}}}},browser:{enabled:false},cron:{enabled:false}};
assert.notEqual(Number(cfg.channels.telegram.botToken.split(':')[0]),123456);
fs.writeFileSync(path.join(P,'state/openclaw.json'),JSON.stringify(cfg,null,2));
const env={...process.env,OPENCLAW_STATE_DIR:path.join(P,'state'),OPENCLAW_CONFIG_PATH:path.join(P,'state/openclaw.json'),OPENCLAW_BUNDLED_PLUGINS_DIR:path.resolve('dist/extensions')};
function rows(){
 const found=[];
 function visit(dir){for(const f of fs.readdirSync(dir,{withFileTypes:true})){const file=path.join(dir,f.name);if(f.isDirectory())visit(file);else if(/\.(sqlite|db)$/.test(file)){let db;try{db=new DatabaseSync(file,{readOnly:true});if(db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_ingress_events'").get())found.push(...db.prepare('SELECT event_id,status,last_error,failed_reason FROM channel_ingress_events').all());}catch{}finally{db?.close();}}}}
 visit(path.join(P,'state'));return found;
}
async function rpc(label){
 const r=await exec('node',['dist/entry.js','gateway','call','negativeProof.status','--json','--timeout','30000','--params','{}'],{env,maxBuffer:4*1024**2,timeout:45000});
 save(label+'-cli.json',r);
 for(let i=0;i<r.stdout.length;i++)if(r.stdout[i]==='{'){try{return JSON.parse(r.stdout.slice(i));}catch{}}
 throw Error('Missing native RPC result; never replay an effect');
}
async function admit(update,label){
 save(label+'-intent.json',{update,at:Date.now(),synthetic:true});
 const r=await fetch('http://127.0.0.1:18792/telegram-webhook',{method:'POST',headers:{'content-type':'application/json','x-telegram-bot-api-secret-token':'synthetic-gate-secret'},body:JSON.stringify(update)});
 save(label+'-admission.json',{status:r.status,headers:Object.fromEntries(r.headers),body:await r.text()});
 assert.equal(r.status,200);assert.equal(r.headers.get('x-openclaw-delivery-accepted'),'durable');
}
async function settle(id){
 const deadline=Date.now()+60000;
 while(Date.now()<deadline){if(rows().some(r=>Number(r.event_id)===id&&r.status==='completed'))return;await pause(200);}
 throw Error('No native terminal completion for '+id);
}
try{
 server=http.createServer(async(req,res)=>{
  const parts=[];for await(const p of req)parts.push(p);
  let fields={};try{fields=JSON.parse(Buffer.concat(parts).toString()||'{}');}catch{}
  const method=req.url.startsWith('/file/')?'file-download':req.url.split('/').at(-1);
  calls.push({method,fields,at:Date.now()});let result=true;
  if(method==='getMe')result={id:888888,is_bot:true,first_name:'Synthetic',username:'synthetic_gate_fixture_bot',can_join_groups:true,can_read_all_group_messages:false,supports_inline_queries:false};
  if(method==='getWebhookInfo')result={url:'',pending_update_count:0};
  if(method==='getFile')result={file_id:fields.file_id,file_unique_id:'fixture',file_path:'synthetic.ogg',file_size:2048};
  if(method==='sendMessage'||method==='editMessageText')result={message_id:999999,date:Math.floor(Date.now()/1000),chat:{id:Number(fields.chat_id??123456),type:'private'},text:fields.text??''};
  res.writeHead(method==='file-download'?403:200,{'content-type':'application/json'}).end(JSON.stringify({ok:method!=='file-download',result}));
 });
 await new Promise(resolve=>server.listen(19413,'127.0.0.1',resolve));
 const fd=fs.openSync(path.join(P,'gateway.log'),'w');gateway=spawn('node',['dist/entry.js','gateway','--port','18792'],{env,stdio:['ignore',fd,fd]});fs.closeSync(fd);
 gateway.once('exit',code=>{exitCode=code;});
 const deadline=Date.now()+240000;
 while(Date.now()<deadline&&!calls.some(c=>c.method==='setWebhook')){assert.equal(exitCode,null,'Gateway stopped before native webhook readiness');await pause(500);}
 assert(calls.some(c=>c.method==='setWebhook'),'Native Telegram webhook never registered');
 const before=await rpc('initial');assert(before.registered&&before.head===process.env.GIT_COMMIT);assert.equal(before.counters.hooks,0);
 const scenarios=[['denied-actor',803001,654321,null],['denied-text-command',803002,123456,'/status'],['denied-enabled-caption',803003,123456,'/reset'],['denied-disabled-caption',803004,123456,'/config show']];
 for(const [label,id,actor,command] of scenarios){
  const m={message_id:id,date:Math.floor(Date.now()/1000),chat:{id:actor,type:'private',first_name:'Synthetic'},from:{id:actor,is_bot:false,first_name:'Synthetic'}};
  if(label==='denied-text-command'){m.text=command;m.entities=[{type:'bot_command',offset:0,length:command.length}];}
  else{m.voice={file_id:'fixture-'+id,file_unique_id:'fixture-'+id,duration:1,file_size:2048,mime_type:'audio/ogg'};if(command){m.caption=command;m.caption_entities=[{type:'bot_command',offset:0,length:command.split(' ')[0].length}];}}
  await admit({update_id:id,message:m},label);await settle(id);const current=await rpc(label);
  assert.equal(current.counters.hooks,0);assert.equal(current.counters.lookups,0);assert(!calls.some(c=>['getFile','file-download'].includes(c.method)));
  save(label+'-result.json',{passed:true,head:process.env.GIT_COMMIT,queue:rows(),counters:current.counters,privateLookupBeforeDenial:false});
 }
 const id=803005;
 await admit({update_id:id,message:{message_id:id,date:Math.floor(Date.now()/1000),chat:{id:123456,type:'private',first_name:'Synthetic'},from:{id:123456,is_bot:false,first_name:'Synthetic'},voice:{file_id:'positive-fixture',file_unique_id:'positive-fixture',duration:1,file_size:2048,mime_type:'audio/ogg'},caption:'Ordinary admitted synthetic voice'}},'positive');
 let positive;const end=Date.now()+60000;
 while(Date.now()<end){positive=await rpc('positive');if(positive.counters.positive===1)break;await pause(500);}
 assert.equal(positive.counters.positive,1);assert.equal(positive.counters.hooks,1);assert.equal(positive.counters.lookups,1);
 assert.equal(calls.filter(c=>c.method==='getFile').length,1);assert(!calls.some(c=>c.method==='file-download'));
 save('positive-observed.json',{head:process.env.GIT_COMMIT,counters:positive.counters,scope:'Reachability control only; deliberately held before final dispatch. Not transcription/model/Telegram acceptance.'});
 passed=true;
}finally{
 if(gateway&&exitCode===null){gateway.kill('SIGTERM');const deadline=Date.now()+80000;while(exitCode===null&&Date.now()<deadline)await pause(200);if(exitCode===null){gateway.kill('SIGKILL');await new Promise(r=>gateway.once('exit',r));}}
 if(server)await new Promise(r=>server.close(r));
 save('terminal.json',{passed,gatewayExit:exitCode,queue:rows(),calls});
}
assert(passed);assert.equal(exitCode,0,'Native Gateway did not settle cleanly');
assert(fs.existsSync(path.join(P,'positive-aborted.json')),'Native caller scope did not close the positive control');
const aborted=JSON.parse(fs.readFileSync(path.join(P,'positive-aborted.json'),'utf8'));assert(aborted.aborted);
save('result.json',{admissionInvariantPassed:true,head:process.env.GIT_COMMIT,negativeCases:4,positiveHookReached:true,positiveFinalAcceptance:false,nativeCallerScopeClosed:true,gatewayExit:exitCode,liveTelegram:false,realModel:false});
console.log(JSON.stringify({nativeNegativePassed:true,negativeCases:4,positiveHookReached:true,gatewayExit:exitCode}));

const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const fields = new Map();
function element() { return {children:[], handlers:{}, attributes:{}, textContent:'', disabled:false,
  append(...items){this.children.push(...items)}, replaceChildren(){this.children=[]},
  setAttribute(name, value){this.attributes[name]=String(value)},
  addEventListener(event, fn){this.handlers[event]=fn}}; }
const get = id => { if(!fields.has(id)) fields.set(id,element()); return fields.get(id); };
const device = {device_id:'a'.repeat(32),name:'Windows',last_observed_state:'ready',checked_at:null};
const snapshot = {schema_version:1,engine_state:'ready',setup:{phase:'new'},capabilities:{},active_resources:{},devices:[device],device_registry_state:'read',observed_at:new Date().toISOString()};
let calls = 0, response;
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../desktop/ui/app.js'),'utf8'), {
  document:{getElementById:get,createElement:element},
  window:{__TAURI__:{core:{invoke:async (command,args)=>{
    if(command==='management_snapshot') return JSON.stringify(snapshot);
    if(command==='management_startup_status') return JSON.stringify({schema_version:1,state:'not_installed',observed_at:snapshot.observed_at});
    assert.equal(command,'management_device_check'); assert.equal(args.deviceId,device.device_id);
    calls++; if(response instanceof Error) throw response; return JSON.stringify(response);
  }}}}
});
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  const row=get('devices').children[0], button=row.children[1], output=row.children[2];
  assert.match(row.textContent,/現在の接続は未確認/);
  assert.equal(output.attributes.role,'status'); assert.equal(output.attributes['aria-live'],'polite');
  response={schema_version:1,device_id:device.device_id,state:'ready',evidence:'authorized_catalog',observed_at:snapshot.observed_at};
  await button.handlers.click(); assert.match(output.textContent,/実操作は未確認/); assert.equal(button.disabled,false);
  response={...response,device_id:'b'.repeat(32)};
  await button.handlers.click(); assert.match(output.textContent,/確認できませんでした/);
  response=new Error('private diagnostic');
  await button.handlers.click(); assert.equal(calls,3); assert.doesNotMatch(output.textContent,/private/);
  // Disabled buttons mostly mean "not applicable", not "busy"; status text reports progress.
  assert.doesNotMatch(fs.readFileSync(path.join(__dirname,'../desktop/ui/style.css'),'utf8'),/cursor:\s*wait/);
  console.log('management device UI passed');
})().catch(error=>{console.error(error);process.exitCode=1});

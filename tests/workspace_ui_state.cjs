// Execute the packaged UI script with a minimal host/DOM, exercising real RPC replies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const html = fs.readFileSync(process.argv[2], 'utf8');
let script = html.split('<script>')[1].split('</script>')[0];
script = script.replace('  controls();\n  if(window.parent',
  '  globalThis.testUI={state,call,resolveMutation,listDevices,selectDevice};return;\n  if(window.parent');
const elements = new Map();
let listener;
let responder;
let calls = 0;
const parent = {postMessage(packet) {
  calls++;
  queueMicrotask(() => listener({source:parent,origin:'https://host.example',data:{
    jsonrpc:'2.0',id:packet.id,result:responder(packet)
  }}));
}};
const context = vm.createContext({
  crypto:webcrypto, TextEncoder, setTimeout, clearTimeout,
  document:{getElementById(id) {
    if(!elements.has(id)) elements.set(id,{hidden:true,dataset:{},value:'draft'});
    return elements.get(id);
  },querySelectorAll:()=>[]},
  window:{parent,addEventListener:(_, callback)=>{listener=callback;}}
});
vm.runInContext(script,context);
const ui = context.testUI;
ui.state.ready=true;
ui.state.tools=new Set(['files_write','operations_get']);
function completed(packet,data) {
  return {structuredContent:{operation_id:packet.params._meta[
    'io.github.meteosimaji.anywhere-computer/operation_id'],state:'completed',data}};
}
(async () => {
  responder=packet=>completed(packet,{sha256:'invalid'});
  await assert.rejects(ui.call('files_write',{path:'/fixture',text:'draft'},{mutation:true}));
  const pending=ui.state.mutation;
  assert.ok(pending, 'Malformed completed write must retain the recovery guard');
  assert.equal(calls,1);
  responder=packet=>completed(packet,{operation_id:'different',state:'completed',data:{sha256:'a'.repeat(64)}});
  await assert.rejects(ui.resolveMutation());
  assert.equal(ui.state.mutation,pending,'Wrong operation cannot release the guard');
  responder=packet=>completed(packet,{operation_id:pending.operationId,state:'completed',data:{sha256:'bad'}});
  await assert.rejects(ui.resolveMutation());
  assert.equal(ui.state.mutation,pending,'Malformed recorded outcome cannot release the guard');
  responder=packet=>completed(packet,{operation_id:pending.operationId,state:'running'});
  await ui.resolveMutation();
  assert.equal(ui.state.mutation,pending);
  ui.state.current={kind:'text',complete:true,text:'old',hash:'b'.repeat(64)};
  responder=packet=>completed(packet,{operation_id:pending.operationId,state:'completed',data:{sha256:'a'.repeat(64)}});
  await ui.resolveMutation();
  assert.equal(ui.state.mutation,null);
  assert.equal(ui.state.current.hash,'a'.repeat(64));
  assert.equal(ui.state.current.text,'draft');
  assert.equal(ui.state.dirty,false);
  assert.equal(calls,5,'Recovery must only query, never resend the write');
  ui.state.tools.delete('operations_get');
  await assert.rejects(ui.call('files_write',{path:'/fixture',text:'draft'},{mutation:true}));
  assert.equal(calls,5,'No write may be dispatched without recovery permission');
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{devices:[]}}});
  await assert.rejects(ui.listDevices());
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{device_id:'remote',tools:[]}}});
  await assert.rejects(ui.selectDevice('remote'));
  assert.equal(ui.state.target,'local','Mismatched discovery must not change target');
  process.stdout.write('workspace mutation recovery: passed\n');
})().catch(error=>{console.error(error);process.exitCode=1;});

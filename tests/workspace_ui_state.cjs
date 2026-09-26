// Execute the packaged UI script with a minimal host/DOM, exercising real RPC replies.
process.stderr.write('workspace fixture: node entered\n');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const html = fs.readFileSync(process.argv[2], 'utf8');
let script = html.split('<script>')[1].split('</script>')[0];
script = script.replace('  controls();\n  if(window.parent',
  '  globalThis.testUI={state,pending,call,openPath,resolveMutation,listDevices,selectDevice,setupCall,planSetup,confirmSetup,controls};return;\n  if(window.parent');
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
const body = {};
const documentStub = {body, activeElement:body, getElementById(id) {
    if(!elements.has(id)) elements.set(id,{hidden:true,dataset:{},value:'draft',textContent:'',children:[],
      replaceChildren(){this.children=[];},append(...items){this.children.push(...items);},setAttribute(){},removeAttribute(){},
      focus(){documentStub.activeElement=this;},closest(selector){
        if(selector==='nav') return id.endsWith('-tab')?{}:null;
        if(selector==='[hidden]') {
          const parent={path:'path-form',editor:'text-panel','setup-resource':'setup'}[id];
          return parent && elements.get(parent).hidden?elements.get(parent):null;
        }
        return null;
      },
      showModal(){this.open=true;},close(){this.open=false;}});
    return elements.get(id);
  },querySelectorAll:()=>[],createElement:()=>({append(){},setAttribute(){},replaceChildren(){}}),createTextNode:text=>({textContent:text})};
const context = vm.createContext({
  crypto:webcrypto, TextEncoder, setTimeout, clearTimeout,
  document:documentStub,
  window:{parent,addEventListener:(_, callback)=>{listener=callback;}}
});
const settle = () => new Promise(resolve=>setTimeout(resolve,0));
const summaryText = () => elements.get('setup-summary').children.map(item=>item.textContent).join('\n');
vm.runInContext(script,context);
const ui = context.testUI;
process.stderr.write('workspace fixture: script loaded\n');
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
  process.stderr.write('workspace fixture: mutation recovery checked\n');
  ui.state.tools.delete('operations_get');
  await assert.rejects(ui.call('files_write',{path:'/fixture',text:'draft'},{mutation:true}));
  assert.equal(calls,5,'No write may be dispatched without recovery permission');
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{devices:[]}}});
  await assert.rejects(ui.listDevices());
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{device_id:'remote',tools:[]}}});
  await assert.rejects(ui.selectDevice('remote'));
  assert.equal(ui.state.target,'local','Mismatched discovery must not change target');
  ui.state.rootTools=new Set(['connection_setup_status','connection_setup_plan','connection_setup_confirm']);
  const configuration={resource:'https://fixture.example/mcp',owner:'owner',client:'anywhere-native',device:'b'.repeat(32),port:8768,scopes:['files_write'],redirects:['http://127.0.0.1/callback']};
  const review={phase:'review',plan_id:'c'.repeat(64),configuration};
  responder=packet=>completed(packet,review);
  await ui.setupCall('connection_setup_plan',{resource:configuration.resource,mode:'files'});
  assert.equal(ui.state.setup.plan_id,review.plan_id);
  assert.equal(elements.get('setup-mode').value,'files');
  assert.match(summaryText(),/許可する操作\n1件（ファイル・文書の変更を含む）/,'Review must describe actual scopes');
  elements.get('setup-resource').oninput();
  assert.equal(ui.state.setup.plan_id,null,'Editing a draft must invalidate the reviewed plan');
  assert.equal(elements.get('setup-confirm').disabled,true);
  await assert.rejects(ui.confirmSetup());
  await ui.setupCall('connection_setup_status');
  let before=calls;
  ui.state.target='remote';
  await assert.rejects(ui.confirmSetup());
  assert.equal(calls,before,'Setup must never be routed to another device');
  ui.state.target='local';
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{...review,phase:'configured'}}});
  await assert.rejects(ui.confirmSetup());
  assert.equal(ui.state.setupUncertain,true);
  before=calls;
  await assert.rejects(ui.confirmSetup());
  await assert.rejects(ui.setupCall('connection_setup_plan',{}));
  assert.equal(calls,before,'Uncertain setup must not be resent or replaced');
  responder=packet=>completed(packet,{phase:'configured',plan_id:null,configuration:{}});
  await assert.rejects(ui.setupCall('connection_setup_status'));
  assert.equal(ui.state.setupUncertain,true,'Malformed status must not release setup guard');
  responder=packet=>{
    assert.equal(packet.params.name,'connection_setup_status','Setup recovery must inspect setup, not operations_get');
    return completed(packet,{phase:'configured',plan_id:null,configuration});
  };
  await ui.setupCall('connection_setup_status');
  assert.equal(ui.state.setupUncertain,false);
  assert.equal(elements.get('setup-confirm').disabled,true);
  assert.equal(ui.state.mutation,null,'Setup must not enter file-operation recovery');
  assert.match(elements.get('setup-status').textContent,/認証・起動・接続確認/);
  responder=packet=>completed(packet,{...review,configuration:{...configuration,scopes:['terminal_start','codex_plugin_call','devices_call']}});
  await ui.setupCall('connection_setup_status');
  assert.equal(elements.get('setup-mode').value,'custom','A custom grant must not be shown as the full preset');
  assert.match(summaryText(),/許可する操作\n3件（コマンド実行・ほかのPluginの呼出し・登録端末の操作を含む）/);
  responder=packet=>completed(packet,{...review,configuration:{...configuration,scopes:['codex_plugin_call','mcp_call','devices_call']}});
  await ui.setupCall('connection_setup_status');
  assert.equal(elements.get('setup-mode').value,'custom');
  assert.match(summaryText(),/3件（ほかのPluginの呼出し・MCPサービスの呼出し・登録端末の操作を含む）/,'The absent terminal scope must not hide service calls');
  responder=packet=>completed(packet,{...review,configuration:{...configuration,scopes:['terminal_start']}});
  await ui.setupCall('connection_setup_status');
  assert.equal(elements.get('setup-mode').value,'custom');
  assert.match(summaryText(),/1件（コマンド実行を含む）/,'A terminal-only grant must not claim plugin or device access');
  const scopeCases=[
    [['files_edit','documents_write'],'2件（ファイル・文書の変更を含む）'],
    [['terminal_input'],'1件（コマンド実行を含む）'],
    [['mcp_session_open'],'1件（コマンド実行を含む）'],
    [['gui_click','gui_native_set_value'],'2件（アプリの操作を含む）'],
    [['browser_click','browser_fill'],'2件（Webページの操作を含む）'],
    [['settings_update','processes_stop'],'2件（共有設定の変更・プロセスの停止を含む）'],
    [['terminal_stop','mcp_session_close'],'2件（プロセスの停止を含む）'],
    [['subchat_delete'],'1件（会話の非表示を含む）'],
    [['subchat_send','subchat_message','subchat_delete'],'3件（Chatへの送信・会話の非表示を含む）'],
    [['terminal_output','gui_native_observe','browser_observe'],'3件。操作一覧で個別に確認できます。'],
  ];
  for(const [scopes,expected] of scopeCases) {
    responder=packet=>completed(packet,{...review,configuration:{...configuration,scopes}});
    await ui.setupCall('connection_setup_status');
    assert.ok(summaryText().includes(expected),`Scope summary for ${scopes.join(', ')}`);
    assert.equal(elements.get('setup-scopes').children.length,scopes.length,'Exact scopes remain available in details');
  }
  before=calls;await assert.rejects(ui.planSetup(),/許可する操作を選び直して/);
  assert.equal(calls,before,'A custom grant must not silently change to a broader preset');
  assert.match(html,/<option value="all">すべての操作（コマンド実行・ほかのPlugin・登録端末の操作を含む）<\/option>/);
  const chatConfig={...configuration,client:'anywhere-chatgpt',redirects:['https://chatgpt.com/connector_platform_oauth_redirect']};
  responder=packet=>completed(packet,{...review,configuration:chatConfig});
  await ui.setupCall('connection_setup_status');
  assert.equal(elements.get('setup-kind').value,'chatgpt');
  assert.equal(elements.get('setup-client').disabled,true);
  responder=packet=>{
    assert.equal(packet.params.arguments.client_kind,'chatgpt');
    assert.equal(Object.hasOwn(packet.params.arguments,'client'),false,'Preset must not send a conflicting manual client');
    return completed(packet,{...review,configuration:chatConfig});
  };
  await ui.planSetup();
  elements.get('setup-kind').value='native';
  elements.get('setup-kind').oninput();
  assert.equal(ui.state.setup.plan_id,null,'Changing the app invalidates confirmation');
  assert.equal(elements.get('setup-client').disabled,false);
  elements.get('setup-client').value='chosen-native-client';
  responder=packet=>{
    assert.equal(packet.params.arguments.client_kind,'native');
    assert.equal(packet.params.arguments.client,'chosen-native-client');
    return completed(packet,{...review,configuration});
  };
  await ui.planSetup();
  ui.state.rootTools.clear();ui.controls();
  assert.equal(elements.get('setup-tab').hidden,true);
  before=calls;await assert.rejects(ui.setupCall('connection_setup_status'));
  assert.equal(calls,before,'Missing local setup capability must prevent dispatch');
  ui.state.tools=new Set(['files_info','documents_read','documents_preview']);
  responder=packet=>{
    const name=packet.params.name;
    if(name==='files_info') return completed(packet,{directory:false,size:100});
    if(name==='documents_read') return completed(packet,{
      sha256:'a'.repeat(64),offset:0,next_offset:null,truncated:false,entries:[{text:'Document text'}]
    });
    if(name==='documents_preview') return {structuredContent:{
      operation_id:packet.params._meta['io.github.meteosimaji.anywhere-computer/operation_id'],
      state:'failed',error:'Document preview is unavailable on this device.',
      data:{error_code:'preview_unavailable',dispatched:false}
    }};
    throw new Error('Unexpected preview fixture tool: '+name);
  };
  for(const extension of ['docx','xlsx','pptx']) {
    await ui.openPath('/fixture.'+extension);
    assert.equal(ui.state.current.kind,'document');
    assert.match(elements.get('document').textContent,/Document text/);
    assert.match(elements.get('notice').textContent,/抽出した文字情報を表示しています/);
    assert.equal(elements.get('notice').dataset.error,'false');
  }
  assert.match(html,/<dialog id="discard" aria-labelledby="discard-title" aria-describedby="discard-description">/);
  assert.match(html,/<h2 id="discard-title">編集中の内容があります<\/h2><p id="discard-description">/);
  assert.match(html,/<h1 id="workspace-heading" tabindex="-1">/);
  const heading=elements.get('workspace-heading');
  ui.state.tools=new Set(['settings_get']);
  responder=packet=>completed(packet,{default_shell:null,file_read_line_limit:200,file_write_line_limit:1000});
  documentStub.activeElement=elements.get('settings-tab');
  elements.get('settings-tab').onclick();await settle();
  assert.equal(documentStub.activeElement,heading,'A completed screen switch moves focus to the main heading');
  ui.state.rootTools=new Set(['connection_setup_status']);
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{}}});
  documentStub.activeElement=elements.get('setup-tab');
  elements.get('setup-tab').onclick();await settle();
  assert.equal(documentStub.activeElement,heading,'The setup heading receives focus even when status fails');
  assert.equal(elements.get('setup').hidden,false);
  assert.equal(elements.get('setup-status').dataset.error,'true');
  documentStub.activeElement=elements.get('setup-resource');
  elements.get('files-tab').onclick();
  assert.equal(documentStub.activeElement,heading,'Leaving setup moves focus out of its now-hidden input');
  responder=packet=>completed(packet,{default_shell:null,file_read_line_limit:200,file_write_line_limit:1000});
  documentStub.activeElement=elements.get('path');
  elements.get('settings-tab').onclick();await settle();
  assert.equal(documentStub.activeElement,heading,'Leaving files moves focus out of the hidden path input');
  documentStub.activeElement=elements.get('path');
  elements.get('files-tab').onclick();
  assert.equal(documentStub.activeElement,elements.get('path'),'Focus is never pulled out of a field in use');
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{}}});
  documentStub.activeElement=elements.get('settings-tab');
  elements.get('settings-tab').onclick();await settle();
  assert.equal(elements.get('welcome').hidden,false,'Failed settings load keeps the current screen');
  assert.equal(documentStub.activeElement,heading,'Failed settings load moves navigation focus to the current heading');
  assert.equal(elements.get('notice').dataset.error,'true');
  before=calls;
  ui.state.current={kind:'text',complete:true,text:'old'};ui.state.dirty=true;
  documentStub.activeElement=elements.get('settings-tab');
  elements.get('settings-tab').onclick();await settle();
  assert.equal(elements.get('discard').open,true,'Unsaved edits must be confirmed before switching');
  assert.equal(documentStub.activeElement,elements.get('settings-tab'),'Pending edits keep focus in place');
  assert.equal(calls,before,'Switching is not dispatched while the edit is unconfirmed');
  elements.get('keep').onclick();
  assert.equal(elements.get('discard').open,false);
  assert.equal(ui.state.dirty,true,'Keeping the edit preserves the draft');
  ui.state.dirty=false;
  assert.equal(ui.pending.size,0,'All RPC replies must release their pending requests');
  process.stderr.write('workspace fixture: all assertions completed\n');
  process.stdout.write('workspace mutation recovery: passed\n');
})().catch(error=>{console.error(error);process.exitCode=1;});

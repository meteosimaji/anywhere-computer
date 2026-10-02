// Execute the packaged UI script with a minimal host/DOM, exercising real RPC replies.
process.stderr.write('workspace fixture: node entered\n');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto,createHash} = require('node:crypto');
const html = fs.readFileSync(process.argv[2], 'utf8');
let script = html.split('<script>')[1].split('</script>')[0];
script = script.replace('  controls();\n  if(window.parent',
  '  globalThis.testUI={state,pending,call,openPath,resolveMutation,listDevices,selectDevice,setupCall,planSetup,confirmSetup,controls,consume};return;\n  if(window.parent');
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
  crypto:webcrypto, TextEncoder, setTimeout, clearTimeout, atob, btoa, Blob, URL,
  document:documentStub,
  window:{parent,addEventListener:(type, callback)=>{if(type==='message') listener=callback;},matchMedia:()=>({matches:false})}
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
    ...['browser_key','browser_drag','browser_hover','browser_select','browser_scroll',
        'browser_file_upload','browser_download','browser_dialog_handle','browser_tab_close']
      .map(scope=>[[scope],'1件（Webページの操作を含む）']),
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
  // An unsaved setup draft survives same-target status refreshes and notifications.
  const draftUrl='https://draft.example/mcp';
  const editDraft=()=>{elements.get('setup-resource').value=draftUrl;elements.get('setup-resource').oninput();};
  const sameReview=packet=>completed(packet,{...review,configuration});
  const notification={structuredContent:{state:'completed',data:{workspace:{view:'connection'}}},
    _meta:{workspaceTools:['connection_setup_status','connection_setup_plan','connection_setup_confirm']}};
  documentStub.getElementById('setup').hidden=false;
  // Each reproduction runs independently so one failure cannot hide the other.
  const failures=[];
  for(const refresh of ['status_refresh','same_target_notification']) {
    try {
      editDraft();
      assert.equal(ui.state.setup.plan_id,null);
      responder=sameReview;
      if(refresh==='status_refresh') await ui.setupCall('connection_setup_status');
      else await ui.consume(notification);
      assert.equal(elements.get('setup-resource').value,draftUrl,`${refresh} must keep the unsaved URL`);
      assert.equal(ui.state.setup.plan_id,null,`${refresh} must not revive the invalidated plan`);
      assert.equal(elements.get('setup-review').hidden,true,`${refresh} must not show the old review`);
      assert.equal(elements.get('setup-confirm').disabled,true);
      before=calls;await assert.rejects(ui.confirmSetup());
      assert.equal(calls,before,'An invalidated plan cannot be saved: no save RPC');
    } catch(error) {
      failures.push(refresh);process.stderr.write(`workspace fixture: ${refresh} failed: ${error.message}\n`);
    }
    // Back to the adopted value through the form, as a person would retype it.
    elements.get('setup-resource').value=configuration.resource;
  }
  assert.deepEqual(failures,[],'Setup draft reproductions must pass independently');
  // Another target is never navigated to silently; leaving setup uses the discard dialog.
  editDraft();before=calls;
  await ui.consume({...notification,structuredContent:{state:'completed',data:{workspace:{view:'settings'}}}});
  assert.equal(calls,before,'A different view must not replace the draft');
  assert.equal(elements.get('setup-resource').value,draftUrl);
  elements.get('files-tab').onclick();
  assert.equal(elements.get('discard').open,true,'Leaving a setup draft needs confirmation');
  elements.get('keep').onclick();
  assert.equal(elements.get('setup-resource').value,draftUrl);
  elements.get('setup-tab').onclick();await settle();
  assert.equal(elements.get('discard').open,false,'Refreshing the open setup screen is not a move');
  assert.equal(elements.get('setup-resource').value,draftUrl);
  // A lost reply still resolves through status without losing the draft or resending.
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:review}});
  await assert.rejects(ui.setupCall('connection_setup_plan',{resource:draftUrl,mode:'files'}));
  assert.equal(ui.state.setupUncertain,true);
  responder=sameReview;
  await ui.setupCall('connection_setup_status');
  assert.equal(ui.state.setupUncertain,false,'Status still resolves an unconfirmed save');
  assert.equal(elements.get('setup-resource').value,draftUrl);
  assert.equal(ui.state.setup.plan_id,null);
  // The draft is adopted only from this request's own matching plan reply.
  elements.get('setup-mode').value='files';
  responder=packet=>completed(packet,{...review,configuration:{...configuration,resource:'https://other.example/mcp'}});
  await ui.planSetup();
  assert.equal(elements.get('setup-resource').value,draftUrl,'A plan reply for other values must not replace the draft');
  assert.equal(ui.state.setup.plan_id,null);
  responder=packet=>completed(packet,{...review,configuration:{...configuration,resource:draftUrl,client:'chosen-native-client'}});
  await ui.planSetup();
  assert.equal(ui.state.setup.plan_id,review.plan_id,'A matching reply to the user\'s own plan is adopted');
  assert.equal(elements.get('setup-resource').value,draftUrl);
  editDraft();
  elements.get('setup-resource').value=draftUrl+'/2';elements.get('setup-resource').oninput();
  // Backend phases that no longer accept edits: the report is shown as is, the draft is kept.
  for(const [phase,title] of [['configured','保存済みの接続設定'],['conflict','保存済みの接続設定'],['saving','保存中の接続設定']]) {
    responder=packet=>completed(packet,{phase,plan_id:phase==='saving'?review.plan_id:null,configuration});
    await ui.setupCall('connection_setup_status');
    assert.equal(ui.state.setup.phase,phase,'The reported phase is not disguised');
    assert.equal(elements.get('setup-resource').value,draftUrl+'/2',`${phase} must keep the unsaved URL`);
    assert.equal(elements.get('setup-form').hidden,false,'The kept draft stays visible');
    assert.equal(elements.get('setup-resource').readOnly,true,'The kept draft is selectable but not editable');
    assert.equal(elements.get('setup-resource').disabled,false);
    assert.equal(elements.get('setup-plan').disabled,true);
    assert.equal(elements.get('setup-confirm').disabled,true);
    assert.equal(elements.get('setup-review-title').textContent,title);
    assert.ok(summaryText().includes(configuration.resource),'The summary is the backend configuration');
    assert.match(elements.get('setup-status').textContent,/入力中の内容は保存されていません/);
    before=calls;await assert.rejects(ui.confirmSetup());
    assert.equal(calls,before,`${phase}: the old plan cannot be saved`);
  }
  // Explicit discard replaces the draft with the backend's reported state.
  elements.get('files-tab').onclick();
  assert.equal(elements.get('discard').open,true);
  elements.get('discard-go').onclick();
  assert.equal(elements.get('setup-resource').value,configuration.resource,'Explicit discard adopts the backend state');
  assert.equal(elements.get('setup-resource').readOnly,false);
  // A confirm reply replaces the draft only when it is the configuration the user reviewed.
  const multi={...configuration,scopes:['files_write','files_edit'],redirects:['http://127.0.0.1/callback','http://localhost/callback']};
  const showReview=async config=>{
    responder=packet=>completed(packet,{...review,configuration:config});
    await ui.setupCall('connection_setup_status');
    assert.equal(ui.state.setup.phase,'review');
  };
  // As a person would: leave setup; a held or edited draft must raise the discard dialog.
  const leaveDraft=expectDialog=>{
    elements.get('files-tab').onclick();
    assert.equal(elements.get('discard').open,expectDialog,'discard dialog for an unsaved draft');
    if(expectDialog) elements.get('discard-go').onclick();
  };
  const confirmWith=async(reply,midFlightDraft=null)=>{
    responder=packet=>{if(midFlightDraft!==null) elements.get('setup-resource').value=midFlightDraft;return completed(packet,reply(packet));};
    before=calls;await ui.confirmSetup();assert.equal(calls,before+1);
  };
  // A normal submit, without any further edit, must not lose the reviewed form when the save did not land as reviewed.
  const other={...multi,resource:'https://other.example/mcp'};
  for(const [label,reply,shown] of [
    ['conflict',{phase:'conflict',plan_id:null,configuration:other},'https://other.example/mcp'],
    ['different configured',{phase:'configured',plan_id:null,configuration:{...multi,scopes:['files_write']}},multi.resource],
    ['invalid',{phase:'invalid',plan_id:null,configuration:null},null],
  ]) {
    await showReview(multi);
    await confirmWith(()=>reply);
    assert.equal(ui.state.setup.phase,reply.phase,`${label}: the backend phase is shown as reported`);
    assert.equal(elements.get('setup-resource').value,multi.resource,`${label}: the reviewed form is kept`);
    assert.equal(elements.get('setup-resource').readOnly,true);
    assert.equal(elements.get('setup-form').hidden,false);
    if(shown) assert.ok(summaryText().includes(shown),`${label}: the summary is the backend configuration`);
    assert.match(elements.get('setup-status').textContent,/確認していた内容を入力欄に残しています/);
    assert.equal(ui.state.setupUncertain,false);
    before=calls;await assert.rejects(ui.confirmSetup());
    assert.equal(calls,before,`${label}: no new save`);
    await ui.setupCall('connection_setup_status'); // a refresh keeps it too
    assert.equal(elements.get('setup-resource').value,multi.resource,`${label}: status refresh keeps the form`);
    leaveDraft(true);
    if(reply.configuration) assert.equal(elements.get('setup-resource').value,reply.configuration.resource,`${label}: discard shows the saved state`);
  }
  // An unknown reply (wrong operation, no edit) holds the reviewed form too; status never discards it.
  for(const [label,reply] of [
    ['conflict',{phase:'conflict',plan_id:null,configuration:other}],
    ['different configured',{phase:'configured',plan_id:null,configuration:{...multi,scopes:['files_write']}}],
    ['invalid',{phase:'invalid',plan_id:null,configuration:null}],
  ]) {
    await showReview(multi);
    responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{}}});
    await assert.rejects(ui.confirmSetup());
    assert.equal(ui.state.setupUncertain,true);
    before=calls;await assert.rejects(ui.confirmSetup());
    assert.equal(calls,before,`${label}: an unknown save is not resent`);
    responder=packet=>completed(packet,reply);
    await ui.setupCall('connection_setup_status');
    assert.equal(ui.state.setupUncertain,false,`${label}: status resolves the unknown save`);
    assert.equal(ui.state.setup.phase,reply.phase);
    assert.equal(elements.get('setup-resource').value,multi.resource,`${label}: the reviewed form survives status`);
    assert.equal(elements.get('setup-resource').readOnly,true);
    before=calls;await assert.rejects(ui.confirmSetup());
    assert.equal(calls,before,`${label}: no save RPC`);
    await ui.setupCall('connection_setup_status');
    assert.equal(elements.get('setup-resource').value,multi.resource,`${label}: a second status keeps it`);
    leaveDraft(true);
  }
  // The same configuration found by status is adopted without any new save.
  for(const extra of [{shared_agent_directory:'/different-fixture'}, {subchat:{provider:'fixture-different'}}]) {
    await showReview(multi);
    responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{}}});
    await assert.rejects(ui.confirmSetup());
    responder=packet=>completed(packet,{phase:'configured',plan_id:null,configuration:{...multi,...extra}});
    await ui.setupCall('connection_setup_status');
    assert.equal(ui.state.setupHeld,true,'Other saved configuration fields cannot confirm this reviewed save');
    leaveDraft(true);
  }
  await showReview(multi);
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{}}});
  await assert.rejects(ui.confirmSetup());
  responder=packet=>completed(packet,{phase:'saving',plan_id:review.plan_id,configuration:multi});
  await ui.setupCall('connection_setup_status');
  assert.equal(ui.state.setupHeld,true,'Saving is not confirmation that the reviewed config was saved');
  responder=packet=>completed(packet,{phase:'conflict',plan_id:review.plan_id,configuration:other});
  await ui.setupCall('connection_setup_status');
  assert.equal(elements.get('setup-resource').value,multi.resource,'A pending save must not erase the reviewed form');
  leaveDraft(true);
  await showReview(multi);
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{}}});
  await assert.rejects(ui.confirmSetup());
  before=calls;responder=packet=>completed(packet,{phase:'configured',plan_id:null,configuration:multi});
  await ui.setupCall('connection_setup_status');
  assert.equal(calls,before+1,'Only the status query was sent');
  assert.equal(ui.state.setup.phase,'configured');
  assert.equal(ui.state.setupUncertain,false);
  leaveDraft(false);
  await showReview(multi);
  await confirmWith(()=>({phase:'conflict',plan_id:null,configuration:other}),draftUrl);
  assert.equal(elements.get('setup-resource').value,draftUrl,'A conflict reply must not replace an edited draft');
  assert.ok(summaryText().includes('https://other.example/mcp'),'The conflicting backend configuration is shown');
  leaveDraft(true);
  await showReview(multi);
  await confirmWith(()=>({phase:'configured',plan_id:null,
    configuration:{...multi,scopes:[...multi.scopes].reverse(),redirects:[...multi.redirects].reverse()}}));
  assert.equal(elements.get('setup-resource').value,multi.resource,'The reviewed configuration, in any list order, is adopted');
  assert.equal(ui.state.setup.phase,'configured');
  // Status that reveals a lost save resolves uncertainty but is not the user's own reply.
  await showReview(multi);
  responder=packet=>({structuredContent:{operation_id:'wrong',state:'completed',data:{}}});
  await assert.rejects(ui.confirmSetup());
  assert.equal(ui.state.setupUncertain,true);
  elements.get('setup-resource').value=draftUrl;
  responder=packet=>completed(packet,{phase:'configured',plan_id:null,configuration:multi});
  await ui.setupCall('connection_setup_status');
  assert.equal(ui.state.setupUncertain,false);
  assert.equal(elements.get('setup-resource').value,draftUrl,'Restoring status does not adopt over the draft');
  leaveDraft(true);
  // A plan reply must also match the client the user asked for.
  const planReply=(client,redirects)=>packet=>completed(packet,{...review,configuration:{...configuration,resource:draftUrl,client,redirects}});
  const chatgptRedirect='https://chatgpt.com/connector_platform_oauth_redirect';
  for(const [kind,reply,adopted] of [
    ['chatgpt',planReply('anywhere-native',['http://127.0.0.1/callback']),false],
    ['chatgpt',planReply('anywhere-chatgpt',[chatgptRedirect,'http://127.0.0.1/callback']),false],
    ['chatgpt',planReply('anywhere-chatgpt',[chatgptRedirect]),true],
    ['native',planReply('other-client',['http://127.0.0.1/callback']),false],
    ['native',planReply('chosen-native-client',['http://127.0.0.1/callback']),true],
  ]) {
    await showReview(configuration);
    elements.get('setup-resource').value=draftUrl;
    elements.get('setup-kind').value=kind;elements.get('setup-client').value='chosen-native-client';elements.get('setup-mode').value='files';
    responder=reply;await ui.planSetup();
    assert.equal(ui.state.setup.plan_id,adopted?review.plan_id:null,`${kind} plan reply adopted=${adopted}`);
    if(!adopted) assert.equal(elements.get('setup-resource').value,draftUrl);
    elements.get('setup-resource').value=draftUrl;leaveDraft(!adopted);
  }
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
  const png=Buffer.from([137,80,78,71,13,10,26,10,1]);
  const image={type:'image',mimeType:'image/png',data:png.toString('base64')};
  const imageSummary={type:'image',mimeType:'image/png',bytes:png.length,sha256:createHash('sha256').update(png).digest('hex')};
  function previewResponse(packet,routed=false) {
    const page={sha256:'a'.repeat(64),page:1,pages:1,mime_type:'image/png',rendered:true,content:[{...imageSummary}]};
    return {...completed(packet,routed?{device_id:'remote',tool:'documents_preview',result:page}:page),content:[{type:'text',text:'preview metadata'},{...image}]};
  }
  for(const target of ['local','remote']) {
    ui.state.target=target;
    responder=packet=>previewResponse(packet,target==='remote');
    const page=await ui.call('documents_preview',{path:'/fixture.docx'});
    assert.equal(page.data_base64,image.data,'Workspace restores the same native PNG');
    for(const change of [
      result=>result.content.pop(),
      result=>{const data=target==='local'?result.structuredContent.data:result.structuredContent.data.result;data.content=null;data.data_base64=image.data;},
      result=>result.content.push({...image}),
      result=>result.content[1].data=Buffer.from('not a PNG').toString('base64'),
      result=>result.content[1].data+='\n',
      result=>result.content[1].mimeType='image/jpeg',
      result=>{const data=target==='local'?result.structuredContent.data:result.structuredContent.data.result;data.content[0].sha256='0'.repeat(64);},
      result=>{const data=target==='local'?result.structuredContent.data:result.structuredContent.data.result;data.content[0].bytes++;},
      result=>{const data=target==='local'?result.structuredContent.data:result.structuredContent.data.result;data.content[0].extra='untrusted';},
      result=>result.structuredContent.operation_id='wrong',
      ...(target==='remote'?[
        result=>result.structuredContent.data.device_id='other',
        result=>result.structuredContent.data.tool='files_read',
      ]:[]),
    ]) {
      responder=packet=>{const result=previewResponse(packet,target==='remote');change(result);return result;};
      await assert.rejects(ui.call('documents_preview',{path:'/fixture.docx'}));
    }
  }
  ui.state.target='local';
  for(const legacy of [false,true]) {
    responder=packet=>{
      if(packet.params.name==='files_info') return completed(packet,{directory:false,size:100});
      if(packet.params.name==='documents_read') return completed(packet,{sha256:'a'.repeat(64),offset:0,next_offset:null,truncated:false,entries:[{text:'Kept text'}]});
      const result=previewResponse(packet);
      if(legacy){delete result.structuredContent.data.content;result.structuredContent.data.data_base64=image.data;result.content=[];}
      return result;
    };
    await ui.openPath('/fixture.docx');
    assert.equal(elements.get('image').hidden,false,'Native and older engines both display preview');
    assert.equal(elements.get('formatted-preview').hidden,false);
    assert.equal(elements.get('formatted-preview').open,true,'A desktop preview opens with its format');
    assert.ok(elements.get('image').src.startsWith('blob:'));
    assert.equal(elements.get('image').alt,'文書の1ページ目');
    assert.match(elements.get('document').textContent,/Kept text/);
    assert.equal(elements.get('more-preview').hidden,true);
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

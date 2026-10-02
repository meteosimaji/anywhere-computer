const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const fields = new Map();
function element() { return {children:[], handlers:{}, attributes:{}, textContent:'', disabled:false,
  append(...items){this.children.push(...items)}, replaceChildren(){this.children=[]},
  setAttribute(name, value){this.attributes[name]=String(value)},
  addEventListener(event, fn){
    const previous = this.handlers[event];
    this.handlers[event] = previous
      ? (...args) => Promise.all([previous(...args), fn(...args)]) : fn;
  }}; }
const get = id => { if(!fields.has(id)) fields.set(id,element()); return fields.get(id); };
const device = {device_id:'a'.repeat(32),name:'Windows',last_observed_state:'ready',checked_at:null};
const snapshot = {schema_version:1,engine_state:'ready',setup:{phase:'new'},capabilities:{},active_resources:{},devices:[device],device_registry_state:'read',observed_at:new Date().toISOString()};
snapshot.capability_diagnostics = {
  browser_isolated: {running_implementation: 'present', acceptance: 'not_verified'},
  media_preview: {runtime_available: true, connection_authorization: 'not_granted'},
  office_rendered_preview: {runtime_available: false},
  codex_skills: {acceptance: 'not_verified'},
  gui_native: {helper: 'verified_available', os_permission: 'not_checked'},
};
snapshot.update_blocker_details = [{
  resource: 'browser_sessions', id: 'fixture-browser', state: 'closing',
  reason: 'cleanup_in_progress', inspect_tool: 'browser_tabs',
}];
let calls = 0, starts = 0, response, startResponse;
const observations = [];
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../desktop/ui/app.js'),'utf8'), {
  document:{getElementById:get,createElement:element},
  window:{__TAURI__:{core:{invoke:async (command,args)=>{
    if(command==='management_snapshot') {observations.push(command);return JSON.stringify(snapshot);}
    if(command==='management_start') {
      if (startResponse) {starts++; return JSON.stringify({...startResponse, snapshot});}
      starts++; snapshot.engine_state = 'ready';
      return JSON.stringify({state:'ready',snapshot});
    }
    if(command==='management_startup_status') {observations.push(command);return JSON.stringify({schema_version:1,state:'not_installed',observed_at:snapshot.observed_at});}
    assert.equal(command,'management_device_check'); assert.equal(args.deviceId,device.device_id);
    calls++; if(response instanceof Error) throw response; return JSON.stringify(response);
  }}}}
});
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(get('startup-enable').hidden,false);
  assert.equal(get('startup-disable').hidden,true);
  const features = get('features').children.map(child => child.textContent).join('\n');
  for (const label of ['独立ブラウザ', '音声・動画の部分プレビュー', '文書の画像プレビュー', 'Codex Skills']) {
    assert.ok(features.includes(label), `Missing capability: ${label}`);
  }
  assert.match(features, /配布物との一致を確認/);
  assert.doesNotMatch(features, /署名済み/);
  assert.match(features, /接続認可：未認可/);
  assert.match(features, /受入：未検証/);
  const work = get('work').children.map(child => child.textContent).join('\n');
  assert.match(work, /fixture-browser/); // Keep known blockers if counts are unavailable.
  assert.match(work, /browser_tabsで状態照会可能/);
  observations.length = 0;
  await get('refresh').handlers.click();
  assert.deepEqual(observations.sort(), ['management_snapshot', 'management_startup_status']);
  const row=get('devices').children[0], button=row.children[1], output=row.children[2];
  assert.equal(row.children[0].children[0].textContent, device.name);
  assert.match(row.children[0].children[1].textContent,/現在の接続は未確認/);
  assert.match(row.children[0].children[1].textContent,/前回：ready \/ 観測なし/);
  assert.equal(output.attributes.role,'status'); assert.equal(output.attributes['aria-live'],'polite');
  response={schema_version:1,device_id:device.device_id,state:'ready',evidence:'authorized_catalog',observed_at:snapshot.observed_at};
  await button.handlers.click(); assert.match(output.textContent,/実操作は未確認/); assert.equal(button.disabled,false);
  response={...response,device_id:'b'.repeat(32)};
  await button.handlers.click(); assert.match(output.textContent,/確認できませんでした/);
  response=new Error('private diagnostic');
  await button.handlers.click(); assert.equal(calls,3); assert.doesNotMatch(output.textContent,/private/);
  snapshot.engine_state = 'stale_endpoint';
  await get('refresh').handlers.click();
  assert.equal(get('start').disabled, false);
  await get('start').handlers.click();
  assert.equal(starts, 1);
  assert.equal(get('start').disabled, true);
  assert.match(get('status').textContent, /エンジンの応答を確認/);
  for (const [code, description] of [
    ['timeout', /時間内/], ['io_error', /入出力エラー/],
    ['invalid_state', /必要な状態/], ['start_failed', /起動処理/],
  ]) {
    snapshot.engine_state = 'stopped';
    startResponse = {state:'not_confirmed', failure:{stage:'engine_start', code}};
    await get('start').handlers.click();
    assert.match(get('action').textContent, description);
    assert.equal(get('start').disabled, false);
  }
  startResponse = undefined;
  snapshot.engine_state = 'catalog_unavailable';
  snapshot.catalog_state = 'unavailable';
  await get('refresh').handlers.click();
  assert.match(get('engine').textContent, /エンジンは応答/);
  assert.match(get('action').textContent, /ツール一覧を取得できません/);
  assert.match(get('identity').children.map(child => child.textContent).join('\n'),
    /MCPツール一覧\n取得できません/);
  assert.equal(get('start').disabled, true);
  for (const state of ['unresponsive', 'identity_mismatch']) {
    snapshot.engine_state = state;
    await get('refresh').handlers.click();
    assert.equal(get('start').disabled, true);
  }
  // Disabled buttons mostly mean "not applicable", not "busy"; status text reports progress.
  assert.doesNotMatch(fs.readFileSync(path.join(__dirname,'../desktop/ui/style.css'),'utf8'),/cursor:\s*wait/);
  console.log('management device UI passed');
})().catch(error=>{console.error(error);process.exitCode=1});

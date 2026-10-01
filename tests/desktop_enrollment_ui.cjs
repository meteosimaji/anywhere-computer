const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const fields = new Map();
const get = id => {
  if (!fields.has(id)) fields.set(id, {value:'', textContent:'', hidden:false, disabled:false, handlers:{}, addEventListener(event, fn) {this.handlers[event]=fn;}, focus() {this.focused=true;}, select() {this.selected=true;}});
  return fields.get(id);
};
const calls = [];
let response;
let pending;
const copied = [];
let clipboardFailure = false;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../desktop/ui/enrollment.js'), 'utf8'), {
  document:{getElementById:get},
  navigator:{clipboard:{writeText:async value=>{
    if(clipboardFailure) throw new Error('clipboard unavailable');
    copied.push(value);
  }}},
  window:{__TAURI__:{core:{invoke:async (command, args) => {
    calls.push({command,args}); if(response instanceof Error) throw response;
    if(pending) await pending;
    return JSON.stringify(response);
  }}}}
});
const click = method => get(`enrollment-${method}`).handlers.click();
const snapshot = phase => ({schema_version:1,authorization:{phase},registration:null,connection_state:'not_checked'});
(async () => {
  response=snapshot('new'); await click('progress');
  assert.equal(get('enrollment-start').disabled,false);
  assert.equal(get('enrollment-start').hidden,false);
  assert.equal(get('enrollment-poll').hidden,true);
  assert.equal(get('enrollment-register').hidden,true);
  let release;
  pending=new Promise(resolve=>{release=resolve;});
  const waitingStart=click('start');
  assert.equal(get('enrollment-start').disabled,true);
  assert.equal(get('enrollment-start').hidden,false);
  release(); await waitingStart; pending=null;
  response=snapshot('waiting'); response.authorization.user_code='CODE'; response.authorization.verification_uri='https://auth.example';
  await click('start');
  assert.equal(get('enrollment-code').value,'CODE');
  assert.equal(get('enrollment-poll').disabled,false);
  assert.equal(get('enrollment-poll').hidden,false);
  assert.equal(get('enrollment-start').hidden,true);
  assert.equal(get('enrollment-open_page').disabled,false);
  await click('open_page');
  assert.equal(calls.at(-1).args.method,'open_page');
  const beforeCopy=calls.length;
  await get('enrollment-copy-code').handlers.click();
  assert.deepEqual(copied,['CODE']);
  assert.equal(calls.length,beforeCopy); // Copy does not poll or restart authorization.
  clipboardFailure=true;
  await get('enrollment-copy-code').handlers.click();
  assert.equal(get('enrollment-code').focused,true);
  assert.equal(get('enrollment-code').selected,true);
  assert.match(get('enrollment-result').textContent,/コピーできません/);
  assert.equal(get('enrollment-copy-code').disabled,false);
  response=new Error('synthetic transport loss'); await click('poll');
  assert.equal(calls.length,beforeCopy+1); // no automatic resend
  assert.equal(get('enrollment-poll').disabled,true);
  assert.equal(get('enrollment-code').value,'');
  assert.equal(get('enrollment-copy-code').disabled,true);
  assert.equal(get('enrollment-open_page').hidden,true);
  response=snapshot('grant_saved'); await click('progress');
  assert.equal(get('enrollment-register').hidden,false);
  assert.equal(get('enrollment-register').disabled,true); // Empty name keeps the next action visible.
  get('enrollment-name').value='日本語 PC 🚀'; get('enrollment-name').handlers.input();
  assert.equal(get('enrollment-register').disabled,false);
  response=snapshot('grant_saved'); response.registration={name:'日本語 PC 🚀',device:null};
  await click('register');
  assert.deepEqual(calls.at(-1).args.name,'日本語 PC 🚀');
  assert.equal(get('enrollment-name').disabled,true);
  assert.equal(get('enrollment-register').disabled,false);
  response.authorization.phase='credential_error'; await click('progress');
  assert.equal(get('enrollment-register').disabled,true);
  assert.equal(get('enrollment-register').hidden,true);
  assert.equal(get('enrollment-name').value,'日本語 PC 🚀');
  assert.equal(get('enrollment-name').disabled,true);
  assert.match(get('enrollment-state').textContent,/保持/);
  assert.match(get('enrollment-state').textContent,/認証情報を利用できません/);
  assert.equal(get('enrollment-restart').disabled,true);
  assert.equal(get('enrollment-reauthorize').disabled,true);
  response.can_reauthorize=true; await click('progress');
  assert.equal(get('enrollment-reauthorize').disabled,false);
  assert.equal(get('enrollment-reauthorize').hidden,false);
  assert.equal(get('enrollment-recovery-help').hidden,false);
  response.authorization.phase='waiting'; response.can_reauthorize=false;
  response.authorization.user_code='NEW-CODE';
  response.authorization.verification_uri='https://auth.example';
  await click('reauthorize');
  assert.equal(calls.at(-1).args.method,'reauthorize');
  assert.equal(get('enrollment-code').value,'NEW-CODE');
  assert.equal(get('enrollment-reauthorize').disabled,true);
  assert.equal(get('enrollment-register').disabled,true);
  response.authorization.phase='grant_saved'; await click('progress');
  assert.equal(get('enrollment-register').disabled,false);
  response.registration.device={state:'registered'}; await click('register');
  assert.equal(get('enrollment-register').disabled,true);
  assert.match(get('enrollment-state').textContent,/未確認/);
  assert.equal(get('enrollment-register').hidden,true);
  assert.equal(get('enrollment-cleanup').disabled,true);
  response.can_cleanup=true; await click('progress');
  assert.equal(get('enrollment-cleanup').disabled,false);
  response.can_cleanup=false; await click('cleanup');
  assert.equal(calls.at(-1).args.method,'cleanup');
  assert.equal(get('enrollment-cleanup').disabled,true);
  response=snapshot('cancelled'); await click('progress');
  assert.equal(get('enrollment-start').disabled,true); // initial start is separate from explicit restart
  assert.equal(get('enrollment-restart').disabled,false);
  response=snapshot('waiting'); await click('restart');
  assert.equal(calls.at(-1).args.method,'restart');
  assert.equal(get('enrollment-restart').disabled,true);
  response=snapshot('credential_error'); await click('progress');
  assert.equal(get('enrollment-retry_save').disabled,true);
  response.authorization.can_retry_save=true; await click('progress');
  assert.equal(get('enrollment-retry_save').disabled,false);
  for (const phase of ['constructor', 'toString', '__proto__']) {
    response=snapshot(phase); await click('progress');
    assert.equal(get('enrollment-state').textContent,'現在の登録状態は未確認です');
    assert.equal(get('enrollment-code-area').hidden,true);
    assert.equal(get('enrollment-retry_save').disabled,true);
  }
  console.log('Enrollment UI state and recovery checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

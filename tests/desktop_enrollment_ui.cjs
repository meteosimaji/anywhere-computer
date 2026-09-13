const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const fields = new Map();
const get = id => {
  if (!fields.has(id)) fields.set(id, {value:'', textContent:'', hidden:false, disabled:false, handlers:{}, addEventListener(event, fn) {this.handlers[event]=fn;}});
  return fields.get(id);
};
const calls = [];
let response;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../desktop/ui/enrollment.js'), 'utf8'), {
  document:{getElementById:get},
  window:{__TAURI__:{core:{invoke:async (command, args) => {
    calls.push({command,args}); if(response instanceof Error) throw response;
    return JSON.stringify(response);
  }}}}
});
const click = method => get(`enrollment-${method}`).handlers.click();
const snapshot = phase => ({schema_version:1,authorization:{phase},registration:null,connection_state:'not_checked'});
(async () => {
  response=snapshot('new'); await click('progress');
  assert.equal(get('enrollment-start').disabled,false);
  response=snapshot('waiting'); response.authorization.user_code='CODE'; response.authorization.verification_uri='https://auth.example';
  await click('start');
  assert.equal(get('enrollment-code').value,'CODE');
  assert.equal(get('enrollment-poll').disabled,false);
  response=new Error('synthetic transport loss'); await click('poll');
  assert.equal(calls.length,3); // no automatic resend
  assert.equal(get('enrollment-poll').disabled,true);
  assert.equal(get('enrollment-code').value,'');
  response=snapshot('grant_saved'); await click('progress');
  get('enrollment-name').value='日本語 PC 🚀'; get('enrollment-name').handlers.input();
  assert.equal(get('enrollment-register').disabled,false);
  response=snapshot('grant_saved'); response.registration={name:'日本語 PC 🚀',device:null};
  await click('register');
  assert.deepEqual(calls.at(-1).args.name,'日本語 PC 🚀');
  assert.equal(get('enrollment-name').disabled,true);
  assert.equal(get('enrollment-register').disabled,false);
  response.authorization.phase='credential_error'; await click('progress');
  assert.equal(get('enrollment-register').disabled,true);
  assert.equal(get('enrollment-name').value,'日本語 PC 🚀');
  assert.equal(get('enrollment-name').disabled,true);
  assert.match(get('enrollment-state').textContent,/保持/);
  assert.match(get('enrollment-state').textContent,/認証情報を利用できません/);
  assert.equal(get('enrollment-restart').disabled,true);
  assert.equal(get('enrollment-reauthorize').disabled,true);
  response.can_reauthorize=true; await click('progress');
  assert.equal(get('enrollment-reauthorize').disabled,false);
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
  console.log('Enrollment UI state and recovery checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});

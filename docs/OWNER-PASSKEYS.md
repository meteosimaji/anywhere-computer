# Owner passkeys for HTTP OAuth consent

Owner passkeys are an optional second way to approve an HTTP OAuth consent page. The
existing owner password remains available. A passkey approves only the pending
request displayed in that browser: it does not create a grant on its own, increase
scopes, or re-enable a revoked device.

## Enroll

Configure the HTTP service and initialize the owner password first. From an
interactive terminal on the owner computer, run:

```text
anywhere owner-passkey-enroll
```

Enter the current Anywhere Computer owner password. Open the printed HTTPS
registration URL in a browser. The link expires after five minutes and can
complete one registration. The browser and authenticator must support WebAuthn
with user verification (PIN, fingerprint, or equivalent). The passkey is bound
to the service's HTTPS origin, RP ID, owner, and selected device. The public
`/authorize` page never enrolls credentials.

To store the passkey on a phone, open that same registration page on the owner
computer and choose “Register on your phone”. Scan the displayed QR with your
phone camera, open the HTTPS link, and register there. The QR is generated locally
by the service from the existing, locally authorized one-use link; no QR service
receives it. The browser registration challenge and its observer cookie expire
after three minutes even if the original link's five-minute lifetime has not ended.

The computer checks the result every two seconds while displaying the QR. After
WebAuthn verification and confirmed credential-store save, it removes the QR and
shows “Passkey registered. You can close this page.” It also clears the form's
ticket and removes the ticket from the current URL. The phone's registration
remains one credential; observing its result never creates a second credential.
This status request is bound to the original browser cookie, CSRF value, ticket,
and same-origin POST. It returns only waiting or registered, without credential
IDs or public keys.

This QR is an ordinary web link to the registration page, not a passkey sign-in
prompt; the browser's own passkey dialogs and any phone QR/Bluetooth step on the
consent page are not drawn by this app. Clicking “Register passkey” shows a waiting
state, ignores repeat clicks and, if the browser dialog is cancelled, restores the
controls without saving or posting anything. After the browser answers, the form
posts once; extra clicks or Enter while the server verifies are ignored. When the QR
is chosen, the form is replaced by the QR and a status line. The phone's page and
the computer's “Passkey registered.” view (heading and title) appear only after the
verified save described here; the phone page says the computer updates after it
confirms the result, not within a fixed time.

A consumed ticket alone does not prove successful registration. Storage or network
errors do not produce a completion message; temporary observation errors retry
without registering again. Expiry, a new locally issued ticket, an owner reset,
or an HTTP service restart can stop observation. Check `owner-passkey-list` locally
before issuing another link when completion is uncertain. A key removed before
the result check is not reported as registered.

Leaving the page pauses observation. If the browser restores the same page from
its history cache, observation resumes when the phone QR had already been chosen;
the original deadline remains unchanged. Registration completion or expiry stays
terminal, and responses from the previous paused poll are ignored.

Register a second passkey while the first and the password are available if
you want another recovery route. A synced passkey may work on multiple devices
according to the passkey provider, but it remains one credential in Anywhere
Computer's list. Keep the owner password available during migration.

## List or remove

Run `anywhere owner-passkey-list` locally to see credential IDs and labels.
Run `anywhere owner-passkey-remove --credential-id ID` in an interactive terminal
and enter the current owner password to remove one. Removing it prevents future
consent approvals with that credential, including an approval whose assertion
was verified but whose authorization code has not yet been issued. Code issuance
is ordered with credential removal under the passkey store lock. If another
operation holds that lock at the final check, consent fails promptly and the
client must start a new connection request. Existing OAuth
grants remain until separately revoked.

At most 20 passkeys can be enrolled. A registration link that meets this cap shows
“Passkey limit reached” and tells the owner to list the keys, remove one with its
credential ID (the page never picks which key) and issue a new link; it does not
suggest an owner reset. The separate lifetime limit of 128 registrations is cleared
only by an owner reset, and its page says so.

## Lost credentials and reset

If a passkey is lost, approve with the owner password or another enrolled
passkey, then remove the lost credential locally. If the owner password is also
forgotten and no usable passkey remains, stop the HTTP service and run the
existing local `anywhere owner-reset` procedure. That procedure revokes the
device's grants, disables it, and clears all enrolled passkeys. Re-enroll
passkeys after establishing a new owner password and enabling fresh consent.
If the native credential store cannot confirm passkey deletion, owner reset
leaves a durable `reset_pending` gate and the device disabled. `http-enable`
refuses to reenable it. Restore credential-store access, rerun `owner-reset`,
then enable the device only after the reset completes. An error from reset is
not evidence that old passkeys were removed.

The native credential store holds credential IDs, public keys, counters, and
labels. Private passkey keys stay with the authenticator or passkey provider.
Short-lived enrollment ticket hashes reside only in the private HTTP state
directory and are durably consumed before a passkey is written or removed by
owner reset. If credential storage fails after consumption, the owner must
issue a new registration link locally; the old link cannot be retried.
The native credential record also saves the digests and expiry times of
tickets used for successful registrations in the same write as each
new passkey. If an OS crash restores a deleted ticket file while keeping that
credential write, registration and ticket validity checks still reject the
old link. Later counter updates and credential removal preserve these records.
They are retained even after expiry so a system clock rollback cannot revive
an old link. After 128 successful registrations, owner reset is required;
that reset changes the salted owner verifier before clearing the records.
Each ticket is also bound to the owner password verifier present at issuance.
Changing the owner password invalidates previously issued registration links
without removing already enrolled passkeys.

## Browser verification

The integration test `tests/test_owner_passkeys_browser.py` uses an isolated
Chromium virtual authenticator and a routed HTTPS origin. It exercises the
registration and consent page JavaScript, browser form submission, WebAuthn
user verification, cookie and CSRF binding, and the resulting authorization
code. Its owner credentials and authorization database live in a temporary
test directory; it never uses a personal passkey or native credential store.
Run it with `uv run pytest -q tests/test_owner_passkeys_browser.py` on a host
with Google Chrome installed. A missing browser causes a reported skip.

`tests/test_authorization_ui_browser.py` renders the consent page with 123
requested tools at desktop, 390px and 320px widths (reachable controls, bounded
complete list, no horizontal overflow, 44px touch targets, no CSP violations) and
uses a page-local stub of `navigator.credentials` for unanswered/cancelled passkey
and registration dialogs, checking duplicate-click blocking and no retry or post.
It also holds the real backend's registration response (verification and save have
already happened) while pressing Enter and clicking again, to prove exactly one POST
and the real verified-success view; page-side timers report state because Playwright
calls block while a form navigation is pending. Expiry and Deny while a passkey
prompt is open are exercised with late credential resolution and rejection. A
1280x700 window must scroll rather than clip the decision column; 390px and 320px
keyboard reachability is checked for every decision.

A second test uses separate computer and phone browser contexts. It injects a
failed status response, verifies that the QR stays visible, registers through the
phone's virtual authenticator, then checks the computer's completion message,
removed QR, cleared ticket and URL. This establishes the browser flow with an
isolated authenticator; physical phone camera and password-manager acceptance
remain separate checks.
The test also dispatches persisted pagehide/pageshow lifecycle events before QR
selection and during observation. It preserves the same script and DOM to exercise
history-cache restoration; it does not establish each browser's cache eligibility.

# Passwordless owner setup with Windows Hello (draft)

This is an alternative first-owner setup for the HTTP service on Windows. It
uses a browser-created platform passkey for connection approval. The browser
requests user verification; choose **Windows Hello on this device** and complete
the Windows prompt yourself. The application never receives your face,
fingerprint, or Windows PIN.

The existing `owner-init` password flow and password-backed passkey enrollment
remain available. These instructions apply only to a new HTTP state directory.

## First setup

1. Configure the HTTP service and its exact HTTPS `/mcp` resource with
   `anywhere http-configure`. The HTTPS hostname must also route the OAuth and
   `/owner-passkey` paths to this same service. Use a separate, protected state
   directory if another Anywhere Computer owner already exists.
2. In an interactive terminal **on the Windows computer running the service**,
   run `anywhere owner-passkey-init --state-dir STATE_DIR`. This command stores
   a passwordless owner binding in the OS credential store and prints a
   one-use registration URL. It refuses a password-backed owner, an already
   enrolled passkey, or an active grant. Keep the URL private.
3. Start `anywhere http-serve --state-dir STATE_DIR` and make the configured
   HTTPS origin available. Open the registration URL in a browser on that
   Windows computer. If the link expires before the service is ready, run
   `owner-passkey-init` again locally to replace it.
4. Select **Register passkey**, choose Windows Hello in the browser prompt, and
   verify with your face, fingerprint, or PIN. Confirm that the page reports
   registration. `anywhere owner-passkey-list --state-dir STATE_DIR` can confirm
   the enrolled key without showing its private key.
5. Start the client connection and approve its requested scopes with the
   enrolled passkey. In this mode, there is no owner password or password
   approval button.

The registration URL is a short-lived bearer capability. The local command is
the trusted bootstrap boundary; the public HTTP service cannot issue it. The
server verifies a one-use ticket, an exact HTTPS origin and relying party ID,
a fresh WebAuthn challenge, and user verification before storing the public
key. Browser platform selection asks for an authenticator on the current
device. It does not cryptographically attest that the selected provider is
Windows Hello, so confirm the provider shown by Windows yourself.

## Recovery

Only one owner passkey can be enrolled in this mode. If it is lost, stop the
HTTP service and run `anywhere owner-passkey-reset --state-dir STATE_DIR` in an
interactive local Windows terminal. Type `REVOKE` to confirm. This rotates the
owner binding, revokes all grants, clears enrolled passkeys, and disables the
device. Then run `owner-passkey-init`, register a new Windows Hello passkey,
and run `anywhere http-enable --state-dir STATE_DIR`. Every client must connect
and be approved again. A failed or uncertain reset leaves the device disabled.

This draft has automated unit and synthetic WebAuthn coverage. It still needs
an end-to-end registration and approval test with a real Windows Hello prompt
and a real HTTPS route before deployment.

# Consent form Origin regression

The real ChatGPT enrollment reached the owner's form, but submission twice ended
at ERR_HTTP_RESPONSE_CODE_FAILURE in the Codex in-app browser. Enrollment and
an authenticated ChatGPT tool call remain incomplete.

The consent page set Referrer-Policy: no-referrer while its POST handler strictly
required the public Origin. A disposable loopback HTML form tested using the
actual in-app browser submitted Origin: null with this policy. With same-origin,
the identical form submitted Origin: http://127.0.0.1:18879. No password, cookie,
or authorization grant was used in this reproduction.

Changed the consent page policy to same-origin: same-origin form POSTs retain
Origin, while cross-origin referrers are suppressed. Origin/CSRF/cookie checks
remain enforced; an explicit null-origin regression test rejects that input.
The reproduction proves the browser/header incompatibility, not successful
ChatGPT authorization after the fix; owner interaction is still required.

Reference: https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Referrer-Policy

## Callback and visibility follow-up

Real enrollment also exposed a second issue: form-action 'self' blocked the
cross-origin OAuth redirect after successful password verification. Before the
fix, the local database contained four issued grants/codes but zero tokens.
The policy now allows only self and the validated registered callback path;
unrepresentable authorities are rejected and query data is excluded from CSP.
ChatGPT's settings subsequently showed the connected app and its tool schemas.

Added an explicit password display/hide button, hidden by default and on submit,
pagehide, or backgrounding. Its fixed inline script is authorized by a SHA-256
CSP hash; no external dependency or unrestricted inline script is enabled.
The real in-app browser verified input type text/password transitions using a
non-secret preview fixture. The temporary preview server was stopped.

Validation: 568 passed, 5 skipped; Ruff and mypy (58 source files) passed.
Live ChatGPT message submitted in conversation 6aa0f2d5-060c-83ee-ba40-655465212442;
response/tool execution acceptance remains pending at this record's writing.

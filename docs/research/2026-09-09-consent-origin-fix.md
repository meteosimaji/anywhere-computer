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

"""Offline markup and styles shared by the browser authorization pages.

Everything here is static presentation: system fonts and inline CSS only, so the pages
need no remote assets and the existing CSP (`style-src 'unsafe-inline'`) holds. The design
keeps a single readable decision flow: one accent colour for actions, separate colours for
error/success/warning text, one decision surface with clear spacing. Callers escape dynamic text;
nothing in this module verifies or decides anything.
"""

_CSS = """
:root{color-scheme:light;--ink:#1c2128;--muted:#57606a;--line:#d0d7de;--accent:#4566a8;
--accent-dark:#38558f;--warn:#7a4200;--warn-line:#c27c0e;--bad:#a0201c;--good:#146c3a;
--focus:#0b57d0}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;scroll-padding-block:16px}
body{margin:0;background:#f4f4f4;color:var(--ink);
font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
font-size:.9em;overflow-wrap:anywhere}
.page{max-width:720px;margin:48px auto;padding:32px;background:#fff;
border:1px solid #e0e0e0;border-radius:14px}
.page.narrow{max-width:600px}
.service{margin:40px 0 0;color:var(--muted);font-size:.875rem}
h1{margin:0 0 16px;font-size:1.75rem;line-height:1.3;font-weight:600;letter-spacing:-.02em}
h2{margin:0 0 8px;font-size:1.1rem;line-height:1.4;font-weight:600}
p{margin:0 0 12px}
.muted{color:var(--muted)}
.fine{margin-top:16px;color:var(--muted);font-size:.875rem}
a{color:var(--accent)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.consent{display:grid;gap:20px}
.auth form{max-width:480px}
.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 24px;margin:16px 0 0}
.facts dt{color:var(--muted)}
.facts dd{margin:0;overflow-wrap:break-word}
.abilities{margin:0 0 12px;padding:0 0 0 20px;display:grid;gap:4px 32px;
grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}
.abilities li::first-letter{text-transform:uppercase}
.abilities li::marker{color:var(--muted)}
.scope-notice{margin:12px 0;padding:0 0 0 12px;border-left:3px solid var(--warn-line)}
.scope-note{margin:12px 0;color:var(--muted)}
details{margin:0;border-top:1px solid var(--line)}
details:last-of-type{border-bottom:1px solid var(--line)}
details>summary{display:flex;align-items:center;justify-content:space-between;gap:12px;
min-height:48px;cursor:pointer;font-weight:600;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::after{content:"Show";color:var(--accent);font-weight:600}
details[open]>summary::after{content:"Hide"}
.details-body{padding:0 0 16px}
.tools-scroll{max-height:320px;overflow:auto;padding:4px 0 16px}
.tools-scroll ul{margin:0;padding:0;list-style:none;display:grid;gap:4px 16px;
grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
.tools-scroll li{min-width:0}
.tools-scroll code{overflow-wrap:anywhere}
.tech{margin:0;display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 24px}
.tech dt{color:var(--muted)}
.tech dd{margin:0;overflow-wrap:anywhere}
label{display:block;font-weight:600}
.hint{margin:2px 0 8px;color:var(--muted);font-size:.9rem}
input[type=text],input[type=password]{display:block;width:100%;min-height:48px;padding:10px 12px;
border:1px solid #858b93;border-radius:10px;background:#fff;color:inherit;font:inherit}
input[aria-invalid=true]{border-color:var(--bad)}
.password-row{display:flex;align-items:stretch;margin-bottom:12px}
.password-row input{flex:1;min-width:0;border-top-right-radius:0;border-bottom-right-radius:0}
.btn{appearance:none;display:flex;align-items:center;justify-content:center;width:100%;scroll-margin-block:8px;
min-height:48px;padding:10px 16px;border:1px solid var(--accent);border-radius:10px;
background:var(--accent);color:#fff;font:inherit;font-weight:600;text-align:center;cursor:pointer}
.btn:hover{background:var(--accent-dark);border-color:var(--accent-dark)}
.btn.secondary{background:#f1f2f3;color:var(--ink);border-color:transparent}
.btn.secondary:hover{background:#e6e8eb;border-color:transparent}
.deny .btn{width:auto;background:transparent;border-color:transparent;color:var(--bad);
font-weight:500}
.deny .btn:hover{background:#f4f4f4}
.btn.quiet{width:auto;min-width:64px;border:1px solid #858b93;border-left:0;
border-radius:0 10px 10px 0;background:#f4f4f4;color:var(--ink);font-weight:600}
.btn.quiet:hover{background:#ececec}
.btn:disabled,.btn[aria-disabled=true]{opacity:.65;cursor:not-allowed}
.btn[aria-disabled=true]{cursor:progress}
.or{display:flex;align-items:center;gap:12px;margin:16px 0;color:var(--muted);font-size:.9rem}

.deny{margin-top:16px}
.status{display:flex;gap:10px;align-items:flex-start;margin:12px 0;font-weight:600}
.status:empty{margin:0}
.status[data-state]::before{content:"";flex:none;width:18px;height:18px;margin-top:2px;
border-radius:50%;font-size:12px;line-height:18px;text-align:center;font-weight:800}
.status[data-state=pending]::before{border:3px solid #c3ccd5;border-top-color:var(--accent);
animation:spin .9s linear infinite}
.status[data-state=warning]{color:var(--warn)}
.status[data-state=warning]::before{content:"!";border:1.5px solid var(--warn-line)}
.status[data-state=error]{color:var(--bad)}
.status[data-state=error]::before{content:"!";border:1.5px solid var(--bad)}
.status[data-state=success]{display:block;padding-left:12px;border-left:3px solid var(--good);
color:var(--good);font-weight:500}
@keyframes spin{to{transform:rotate(360deg)}}
.alert{margin:0 0 16px;padding:0 0 0 12px;border-left:3px solid var(--bad);color:var(--bad);
font-weight:600}
.qr{width:min(100%,240px);margin:0 0 12px;padding:8px;border:1px solid var(--line);
border-radius:6px}
.qr svg{display:block;width:100%;height:auto}
.phone{margin-top:8px}
.result{padding-left:16px;border-left:3px solid var(--line)}
.result.bad{border-left-color:var(--bad)}
.result.good{border-left-color:var(--good)}
[hidden]{display:none!important}
@media(max-width:760px){.page{margin:24px 16px}}
@media(max-width:480px){body{background:#fff}.page{margin:0;padding:24px 16px 48px;
border:0;border-radius:0}
.facts,.tech{grid-template-columns:1fr;gap:0}.facts dd,.tech dd{margin-bottom:8px}
.tools-scroll ul{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){.status[data-state=pending]::before{animation:none}}
"""


def document(title: str, body: str, *, narrow: bool = False, script: str = "") -> str:
    """Wrap escaped body markup in the shared shell; the script must be CSP-pinned."""
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title id=page-title>{title}</title><style>{_CSS}</style>"
        f"<main class='page{' narrow' if narrow else ''}'>"
        f"{body}<p class=service>Anywhere Computer</p></main>"
        + (f"<script>{script}</script>" if script else "")
        + "</html>"
    )


def result_page(kind: str, heading: str, *paragraphs: str) -> str:
    """Outcome body ('good' or 'bad'): one concrete h1 and its short paragraphs (escaped HTML)."""
    return (
        f"<section class='result {kind}'><h1>{heading}</h1>"
        + "".join(f"<p>{text}</p>" for text in paragraphs) + "</section>"
    )

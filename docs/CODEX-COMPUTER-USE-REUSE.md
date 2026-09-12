# Installed Codex Computer Use reuse investigation

Local inspection on 2026-09-12. This is evidence about the installed version, not a
supported integration contract or redistribution permission.

## Verified entry point

The installed `computer-use` plugin launcher resolves:

`$CODEX_HOME/computer-use/Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient`

It executes that file with the supplied arguments. The plugin supplies `mcp`.
The executable exists. Its own help explicitly describes the MCP subcommand.
Installed service bundle version: `26.902.1000968`. The separate JavaScript
package shipped in the desktop application reports `@oai/sky` version `0.6.32`.
These are distinct component versions.

## Direct MCP experiment

Using Anywhere Computer's DirectMCPContext with the installed executable and
`mcp`, initialization and tools/list succeeded. The catalog exposed ten tools:
list_apps, get_app_state, click, perform_secondary_action, set_value, select_text,
scroll, drag, press_key, type_text. The temporary client closed successfully.
No Codex model turn was started and no invented request metadata was supplied.

A subsequent list_apps call returned isError=true and a 69-character text result.
The first diagnostic retained only the result type and size, not its error text;
therefore this record cannot attribute that error to a specific cause.
A separate diagnostic attempt to capture the error body instead lost the response
and produced DirectMCPOutcomeUnknown. No write/click/input call was attempted.

After these experiments the existing SkyComputerUseService was running and its
IPC socket existed. Thus absence of the service is not established as the cause.
The native-pipe transport in the shipped JavaScript package sends a client API
version, Codex turn metadata, a deadline, request type and request body. Its service
startup path depends on nodeRepl.launchServices. The application policy wrapper
also has app policy checks and an elicitation interface. These observations explain
potential dependencies; they do not prove which one caused the failed request.

## Integration boundary

The MCP entry point can be discovered and initialized without implementing a new
GUI backend. Actual desktop operation through this direct route is not yet proven.
Do not remove the existing unsupported-context guard based on catalog discovery.
Do not fabricate turn metadata, approvals or host-service capabilities, replace
signed binaries, or modify service authorization to make an experiment pass.

Prefer referencing a user's existing installation to copying the proprietary
application into MIT distribution artifacts. No license granting redistribution
was established by this inspection; package metadata such as private=false is not
a license grant.

Next evidence needed: a supported external-client execution context or documented
host integration, actionable diagnostics for the direct request failure, and a
successful permitted read followed by a disposable-app action through that route.

## Official documentation and local diagnostic follow-up

The official Help Center article “Manage browser and computer use in your Enterprise
workspace” documents per-application policies, saved approvals, and that an approval
cannot override an administrator restriction:
https://help.openai.com/en/articles/20001510-manage-browser-and-computer-use-in-your-enterprise-workspace

This is evidence of policy boundaries, not evidence that this user's failure came
from an enterprise rule. The documentation inspected did not establish a supported
procedure for passing execution authorization from an arbitrary external MCP client
to the installed native service. Absence from this bounded search is not proof that
no such interface exists.

A bounded read of the last 15 minutes of macOS unified logs for the service/client,
filtered for metadata, missing, not allowed and timeout, did not provide a causal
error message. TCC access-result and network task timeout-configuration records were
present. Their numeric values are not interpreted here as a specific permission
failure, nor do ordinary timeout settings prove a request timed out.

The local computer-use config has appearance/localization keys (accentColor,
direction, locale, strings); it is not evidence of an external execution-authorization
setting. No configuration or OS permission was changed.

## Direct registration path correction

The user-level Codex configuration had computer-use enabled, but its command was
`./Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient`
with cwd `.`. That relative path does not exist under the repository or the home
directory used by the app-server bridge; it exists under the installed computer-use
directory. Only the command field was changed to the installed absolute path.
No permission or enabled flag was changed and no credentials were copied.

Before correction the bridge reported failed with zero received tools. Afterward it
received ten tool definitions, but runtime_status still reported failed. Consequently
this correction resolves a concrete path defect, not the entire runtime failure.
Desktop action success is still unverified. The configuration change is local to the
user's Codex installation and is not part of the distributable repository.

## Confirmed service authentication rejection

A subsequent single list_apps probe returned this exact MCP tool error:

`Computer Use server error -10000: Sender process is not authenticated`

The temporary client then closed with cleanup_confirmed=true. This establishes a
sender-process authentication rejection for that request. It does not establish
which credential, code identity, host registration or execution context the service
requires, nor that every earlier timeout had this cause. Do not substitute invented
turn identifiers or altered signing/authorization settings. A supported way to admit
an external client remains necessary before claiming Computer Use operation support.

## Control experiment with another registered MCP

The existing node_repl registration was discovered through PluginContext, its exact
js schema/digest inspected, and called twice within the same ephemeral context.
The first call defined a synthetic variable as 40; the second added 2 and returned 42.
Both returned is_error=false. The context was closed afterward. No installation,
model turn or access to personal files was involved. Together with the successful
openaiDeveloperDocs call, this confirms that existing registered MCP execution and
state retention work for these servers; Computer Use authentication remains separate.

## Live authenticated Chat-plugin entrance

The current production alpha 6 HTTP entrance was invoked directly using its exposed
Anywhere Computer connector tools. Status receipt
`98d2c2b12e264a53baf266ff191a9a9d` confirmed authenticated HTTP, engine instance
`81b2668883ce4258846f6687130fce72`, and 50 tools. This was not the unpublished
55-tool development engine.

A Codex plugin session was opened, node_repl/js was inspected, and two calls returned
40 then 42 while preserving the same JavaScript variable. Receipts:

- Open: `d155968d12e14cb9b123d142f760eae3`
- Catalog: `5776dc6a216f4ce8b44bc737abb7b483`
- First call: `deabd5e68fd543d680b37bfbc01db452`
- Second call: `46a54128fc3a46f3a9e627fca5b3e922`
- Result recovery: `d990382f17dd4624ba0470df568e4b3c`
- Close: `8caba67f4e084b0d874c0d41d0a7f8ed`, cleanup_confirmed=true

No personal file read/write or extra model generation was requested. This verifies
registered node_repl reuse over the live authenticated HTTP entrance, not Computer
Use execution, all installed plugins, or a rendered Chat UI interaction.

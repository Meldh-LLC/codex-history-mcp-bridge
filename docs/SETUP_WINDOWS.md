# First install: Windows + ChatGPT

This guide is for a new installation. Existing installations should use [UPGRADING.md](../UPGRADING.md). Do not recreate a working key, environment, or tunnel for an ordinary source update.

## 1. Check account access before installing

Confirm your ChatGPT account/workspace offers developer mode and a custom MCP connection, and that your OpenAI Platform organization lets you use Secure MCP Tunnel. These controls are not unlocked by this package.

OpenAI's current help describes developer mode under **Settings → Apps → Advanced settings** or **Workspace settings → Apps → Create**, depending on plan and role. Follow the interface available to your account and the [official availability and permissions guide](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt). These labels and access rules can change. There is no guarantee for every personal plan or managed workspace.

Confirm you can create/select a tunnel at [Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels). Do not post its details publicly. An org permission error is not a Python issue.

## 2. Put the source in a clean folder

Extract the release or clone the repository into a dedicated folder, for example `E:\Tools\codex-history-bridge`. Open PowerShell in the folder that contains `server.py` and `setup.ps1`. Either C: or E: can be used. Do not put the source inside your Codex history folder.

Use the same Windows/WSL environment that holds the sessions you intend to read. Native Windows and WSL have different executable paths and home directories; changing the working folder does not move history. The shipped scripts target native Windows. Do not claim a native Windows test validates WSL.

## 3. Check Python and Codex CLI

```powershell
python --version
```

```powershell
codex --version
```

Python 3.10+ is required. The desktop app alone does not establish that the CLI is on PATH. Install the CLI from [OpenAI's Codex documentation](https://developers.openai.com/codex/cli/), reopen PowerShell, and check again.

If a stale or invalid `CODEX_EXECUTABLE` override is causing failures, remove it from the current process before testing discovery:

```powershell
Remove-Item Env:CODEX_EXECUTABLE -ErrorAction SilentlyContinue
```

```powershell
Get-Command codex -All -ErrorAction SilentlyContinue | Select-Object CommandType, Name, Source
```

Only set `CODEX_EXECUTABLE` to an executable that actually exists. Do not copy a sample path literally. A deliberately configured `CODEX_HOME` must identify the history location you intend to expose.

## 4. Install and probe the local bridge

Review the scripts first. When execution policy blocks local scripts, this changes policy for the current PowerShell process only:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

```powershell
.\setup.ps1
```

Setup creates `.venv`, installs the official MCP SDK from `requirements.txt`, and runs the bridge doctor. `Local setup passed` means the local prerequisites passed; it is not a full transcript or tunnel test. To repeat the doctor:

```powershell
.\run-doctor.ps1
```

Preserve the exact error when a check fails. Do not change several components at once.

## 5. Configure Secure MCP Tunnel

Create a named tunnel in the Platform page from step 1. Download the matching **full Windows tunnel client**, including any companion files its release requires, from the supported download on that page. Keep `tunnel-client.exe` and its required companions outside source control. Full-client commands such as `init`, `doctor`, and `run` must be available. The [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) explains the transport and workspace association.

```powershell
.\tunnel-client.exe --version
```

Create a runtime key using the [Platform runtime API-key page](https://platform.openai.com/settings/organization/api-keys). Its principal needs **Tunnels Read + Use** for the selected tunnel. Do not use a Platform admin key as the long-running credential. The tunnel/workspace association must authorize the intended ChatGPT workspace. Follow [OpenAI's onboarding and permission guide](https://github.com/openai/tunnel-client/blob/master/docs/onboarding.md).

Load the key without putting its literal value in a command or script:

```powershell
.\load-runtime-key.ps1
```

This helper prompts with hidden input, then sets `CONTROL_PLANE_API_KEY` for the current PowerShell process. The key still exists in process memory and is inherited by child processes. It is not saved in the repository, a user environment variable, or a file. This is not an encrypted secret vault. Do not use a shared screen or shell transcript for secrets.

Configure once, replacing the placeholder with the tunnel ID you just created:

```powershell
.\configure-tunnel.ps1 -TunnelId "tunnel_YOUR_ID"
```

The script normalizes Windows path separators before building the stdio command. Drive-letter paths do not require a different tunnel profile.

```powershell
.\start-tunnel.ps1
```

Leave the window running. Use only one active stdio tunnel-client instance per tunnel ID; overlapping instances may route calls to different child processes. A restart also loses process-local recovery handles. See [OpenAI's stdio deployment guidance](https://github.com/openai/tunnel-client).

## 6. Connect ChatGPT while the tunnel runs

In the custom MCP connection screen, use the name **Codex History Bridge**, choose **Tunnel**, and select your tunnel. Choose **No authentication** only for this stdio bridge's additional MCP-server login: it has no OAuth implementation. The tunnel is separately authenticated by the runtime credential and workspace association. This is not permission to expose the bridge publicly without access controls.

Scan the tools and save the draft app. Start a new chat, select or mention the app, then use the [connection-check prompt](../PROMPTS.md#0-connection-check-after-setup-or-restart). The bridge should report version 0.3.0 and 11 read-only tools. No resume, execute, write, approve, archive, or delete action should appear.

## 7. Test a non-sensitive project through the normal workflow

Use a synthetic or non-sensitive recorded directory first. Paste the [metadata-only inventory prompt](../PROMPTS.md#3-metadata-only-inventory-of-known-roots) into ChatGPT, replacing the project name and exact root. Confirm that the bridge returns one or more verified `session_ref` values without asking you to copy a raw thread ID.

Then use the [short recent-work prompt](../PROMPTS.md#5-short-recent-work-check). A complete test either reaches terminal pagination for both selected sessions or reports a precise partial/error state. Do not treat the appearance of one expected phrase as proof that pagination completed.

The command-line verifiers accept raw IDs only as advanced local diagnostics. They are unnecessary for ordinary setup and should not appear in user-facing reconstruction prompts. See [Operations](OPERATIONS.md) if a normal reference-based read fails.

## Sources and scope

The OpenAI links above were checked on 2026-09-20. Interface labels and access rules may change. SDK requirement: [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk). This community package and its read-only annotations are not OpenAI approval, an independent security audit, or a guarantee of data confidentiality.

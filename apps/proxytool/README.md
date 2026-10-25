<!-- Project header -->
<div align="left">
  <h1 style="display: inline; position: top; text-decoration: none;">proxytool</h1>
  <img src="../../public/assets/images/proxytool.webp" alt="proxytool logo" width="72" align="left" />
  <p>Proxy Application</p>
</div>

[![test & checks](https://github.com/dave-holden/proxytoolpatch/actions/workflows/test.yml/badge.svg)](https://github.com/dave-holden/proxytoolpatch/actions/workflows/test.yml)
[![publish toolpatch + proxytool](https://github.com/dave-holden/proxytoolpatch/actions/workflows/publish.yml/badge.svg)](https://github.com/dave-holden/proxytoolpatch/actions/workflows/publish.yml)
[![Python 3.10–3.14](https://img.shields.io/badge/python-3.10%E2%80%933.14-blue)](https://www.python.org)
[![code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![PyPI proxytool](https://img.shields.io/badge/PyPI-proxytool-blue)](https://pypi.org/project/proxytool/)
[![PyPI toolpatch](https://img.shields.io/badge/PyPI-toolpatch-blue)](https://pypi.org/project/toolpatch/)

The `apps/proxytool/` directory contains the runnable FastAPI proxy. It sits between an OpenAI-compatible client (such as a coding agent) and a local model server, so the client doesn't have to talk to the model directly.

The app is the HTTP/API layer. The reusable tool-call repair logic lives in [`packages/toolpatch/`](../../packages/toolpatch/README.md).

## Why run it?

Some local models return a tool request as ordinary assistant text instead of the structured `tool_calls` data that a client expects. The request might look like JSON or a model-specific tool-call marker, but the coding agent cannot execute it as a tool.

The proxy is designed to recognise supported forms of this output and repair the hand-off where it can. It is a compatibility aid, not a guarantee that every malformed response can be recovered.

## Request flow

```text
Coding agent / OpenAI-compatible client
                  |
                  v
          proxytool (FastAPI)
                  |
        inspect / normalise response
                  |
                  v
       Local model inference server
       (for example, llama.cpp or MLX)
```

The proxy forwards requests to the configured model server and applies supported compatibility fixes to the request/response flow. Exact behaviour depends on the enabled features and the client/server combination.

## Setup

Use the repository's supported Python environment and dependency workflow. From the repository root, inspect the project configuration (`pyproject.toml` and any example environment files) for the current install and run commands.

At a high level:

1. Install the project's dependencies.
2. Start your local inference server and confirm its OpenAI-compatible endpoint works directly.
3. Configure the proxy's upstream/base URL and any model or alias settings required by your server.
4. Start the proxy using the app's configured entry point.
5. Configure your coding agent to use the proxy endpoint instead of the inference server endpoint.

Keep the original inference-server URL handy. Comparing a direct request with the same request through the proxy helps isolate the cause if something fails.

## Configuration

Use the current app settings and example environment files as the source of truth for names and defaults. Check the supported options, including:

- The upstream model-server URL.
- The model name or aliases exposed to clients.
- Host and port used by the proxy.
- Streaming behaviour.
- Any compatibility switches for a particular backend.

Do not commit API keys or other secrets. Use a local, ignored `.env` file or your usual secret-management method if supported by the app.

## Configure a coding agent

Set the agent's OpenAI-compatible provider/base URL to the address where the proxy is listening. Leave the model and tool definitions unchanged for the first test, unless the backend requires a specific model name.

Then try a small, easy-to-check task:

> List the files and directories in the current working directory.

A successful run should show the agent invoking its filesystem/listing tool and returning the result, rather than merely printing a JSON object that describes a command.

## Troubleshooting

### The agent still prints JSON instead of using a tool

- Confirm the agent is pointed at the proxy, not directly at the model server.
- Check the proxy logs to see whether the response matched a supported repair format.
- Try the same request against the model server directly to establish a baseline.
- Record the model, quantisation, backend and agent versions when reporting the issue.

### Requests fail or the model is unavailable

- Confirm the upstream server is running and its URL is correct.
- Check whether the model name sent by the client is accepted by the upstream server or mapped by an alias.
- Check the proxy logs for upstream HTTP errors and timeouts.

### Streaming behaves differently

Streaming compatibility can depend on both the upstream server and the client. Test with streaming enabled and disabled if the app supports both modes, and include relevant logs when reporting a problem. Avoid sharing tokens, credentials or private prompt content.

## Development

Run the project's configured tests and lint/type checks using its documented development workflow. When adding a repair rule, add a focused test for the input format and the resulting response shape.

## Related documentation

- [Repository overview and screenshots](../../README.md)
- [Reusable tool-patching package](../../packages/toolpatch/README.md)

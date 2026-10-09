# llama-qwen

Qwen tool-calling template-mismatch detector and repairer.

Pure domain logic (no sockets, no event loop): JSON healing, tool-call
extraction from leaked message content, SSE stream healing, and outbound
request normalization. See the workspace root `PLAN.md` for the full design.

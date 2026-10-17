<p align="center">
  <img src="public/assets/images/proxytoolpatch.webp" alt="proxytoolpatch" width="220">
</p>

# proxytoolpatch workspace

## Development

The git hooks (pre-commit / pre-push quality gates: ruff + pytest) are tracked
in `.githooks/` — activate them once per clone:

```bash
./.githooks/install.sh
```

See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for the full local setup
(test matrix, coverage, branch protection).

It maybe better if I write a bullet list of how this cam about:
* I wanted to run an LLM locally for coding
* I work on a MacBook Pro M1 with 32GB RAM
* After research and chats, Qwen was recommend - specifically Qwen2.5
* Installed Llama.cpp for its small footprint
* Downloaded `Qwen2.5-Coder-14B-Instruct-GGUF` via `hf` HuggingFace
* Prompt "list contents of current directory"
* Got the `<tool_call>` mismatch with Pi and OpenCode
---
I can't be the only one but I've had no end of problems getting a Qwen LLM working with my machine, a MacBook Pro M1 with 32GB RAM.
---
Searching for a coding LLM that'll run on my MacBook Pro M1 with 32GB RAM, without freezing it, I was informed that a Qwen LLM was the best, and `Qwen2.5-Coder-14B-Instruct-GGUF` was regularly mentioned.

However, it had better ideas!

Everytime I connected an AI coding-agent CLI, and prompted to review code (or just list the contents of the current directory) it would output a JSON object with the `ls` command in it.
---
researching for the best coder LLM for my old-ish MacBook Pro M1 with 32GB RAM, the answer inevitably came back as being Qwen... a `Qwen2.5-Coder-14B-Instruct-GGUF` to be exact.
---
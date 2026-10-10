# GGUF


## Start the Server

```sh
export QPATH="$(find "$HF_HUB_CACHE/models--Qwen--Qwen2.5-Coder-14B-Instruct-GGUF/snapshots" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
#
llama serve --host localhost --port 8181 \
  --model $QPATH/qwen2.5-coder-14b-instruct-q4_0.gguf \
  --ctx-size 16384 --jinja
```

## Start the Proxy

```sh
uv sync --all-packages        # first time only
LMW_TRACE=1 uv run uvicorn proxy.main:app --host 127.0.0.1 --port 8180
```

# MLX

```sh
export MPATH="$(find "$HF_HUB_CACHE/models--mlx-community--Qwen2.5-Coder-14B-Instruct-4bit/snapshots" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
#
llama serve --host localhost --port 8181 \
  --model $MPATH/qwen2.5-coder-14b-instruct-q4_0.gguf \
  --ctx-size 16384 --jinja
```

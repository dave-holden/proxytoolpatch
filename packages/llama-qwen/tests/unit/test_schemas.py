from llama_qwen.schemas import ChatRequest


def test_chat_request_round_trips_openai_payload():
    payload = {
        "model": "qwen2.5-coder-14b",
        "messages": [{"role": "user", "content": "list files"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ],
        "stream": True,
        "temperature": 0.2,  # extra field must survive
    }
    req = ChatRequest.model_validate(payload)
    assert req.tools is not None
    assert req.tools[0].function.name == "read_file"
    dumped = req.model_dump(exclude_none=True, exclude_unset=True)
    assert dumped["temperature"] == 0.2

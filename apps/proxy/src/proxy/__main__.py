"""`python -m proxy`: role start for dev."""

import uvicorn


def main() -> None:
    uvicorn.run(
        "proxy.main:app",
        host="127.0.0.1",
        port=8180,
        reload=True,
    )


if __name__ == "__main__":
    main()

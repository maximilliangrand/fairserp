import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "fairserp.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()

"""``python -m ag_server``: serve the app with uvicorn (the Docker entry point)."""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "ag_server.app:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - inside the container; only the proxy publishes a port
        port=8000,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()

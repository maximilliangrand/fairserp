"""Run the fairserp HTTP API (fairserp serve, the default) or the MCP server (fairserp mcp)."""

import argparse
import ipaddress
import os
import sys


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    if (_loopback(args.host) and not os.environ.get("FAIRSERP_API_KEYS")
            and "FAIRSERP_AUTH_DISABLED" not in os.environ):
        # Local-only convenience: nobody else can reach a loopback listener.
        os.environ["FAIRSERP_AUTH_DISABLED"] = "true"
        print(f"fairserp: no FAIRSERP_API_KEYS set; auth is off because the server only "
              f"listens on {args.host}.", file=sys.stderr)
    elif (not os.environ.get("FAIRSERP_API_KEYS")
          and os.environ.get("FAIRSERP_AUTH_DISABLED", "").lower() not in ("1", "true")):
        print(f"fairserp: listening on {args.host} with no FAIRSERP_API_KEYS, so every request "
              f"will get 401. Set FAIRSERP_API_KEYS=<key>, or FAIRSERP_AUTH_DISABLED=true "
              f"behind your own auth.", file=sys.stderr)
    print(f"fairserp: playground at http://{args.host}:{args.port}/", file=sys.stderr)
    uvicorn.run("fairserp.main:app", host=args.host, port=args.port, proxy_headers=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fairserp", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    serve_parser = sub.add_parser("serve", help="run the HTTP API (default)")
    serve_parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    serve_parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    sub.add_parser("mcp", help="run the MCP server on stdio")
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("serve", "mcp", "-h", "--help"):
        argv = ["serve", *argv]
    args = parser.parse_args(argv)
    if args.command == "mcp":
        from fairserp.mcp_server import main as mcp_main

        mcp_main()
        return
    serve(args)


if __name__ == "__main__":
    main()

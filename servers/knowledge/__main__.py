"""Entry point for `python -m servers.knowledge`.

Delegates to knowledge_server.main() so the systemd unit
`mcp-server@knowledge` continues to work after the rename
to knowledge_server.py.
"""

from servers.knowledge_server import main

main()

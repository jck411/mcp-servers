"""Entry point for `python -m servers.knowledge_admin`.

Delegates to knowledge_admin_server.main() so the systemd unit
`mcp-server@knowledge_admin` works via the template.
"""

from servers.knowledge_admin_server import main

main()

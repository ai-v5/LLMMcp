"""LLMMcp: an MCP server for Google web search and web fetch.

The package is laid out so each concern has one owner:

* :mod:`llmmcp.settings` resolves configuration, including the corporate proxy.
* :mod:`llmmcp.http` is the only place that opens sockets.
* :mod:`llmmcp.fetch` turns a URL into HTML/text/markdown.
* :mod:`llmmcp.search` holds one module per engine plus the fallback chain.
* :mod:`llmmcp.server` exposes the MCP tools.
"""

__version__ = "0.1.2"

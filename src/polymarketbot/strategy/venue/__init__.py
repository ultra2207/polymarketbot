"""Venue access: the live CLOB client, the websocket feed and read-only views.

These are the only modules in ``strategy`` allowed to perform network I/O.
They never import ``paper`` or ``live``.
"""

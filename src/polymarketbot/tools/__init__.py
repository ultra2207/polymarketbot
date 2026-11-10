"""Operator CLIs run as ``python -m polymarketbot.tools.<tool>``.

Each module in this package is a recurring maintenance entry point: it parses
its own arguments in ``main()`` and returns an exit code, so it can be run by
hand or from a scheduler without importing the module's private helpers.
"""

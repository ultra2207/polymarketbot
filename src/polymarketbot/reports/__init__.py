"""Read-only reports rendered from the engine's recorded state.

Each report module builds its text from an existing database or tape and never
writes to it; the CLI in ``main()`` prints the report and returns an exit code.
"""

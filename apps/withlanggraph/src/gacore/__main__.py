"""Entry point for ``python -m gacore`` (run with PYTHONPATH=src or after installing).

Launches the interactive REPL: a thin human-in-the-loop frontend over the compiled
graph that handles ask_user interrupts by prompting the human and resuming.
"""

from gacore.cli import main

main()

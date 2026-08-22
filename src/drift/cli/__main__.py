"""The ``drift`` command: a single entry point that dispatches to subcommands.

    drift up <model> [--join ...]   Start/join a private swarm in one command (recommended)
    drift down                      Stop DRIFT-LLM servers running on this machine
    drift server <model> ...        The full server with every knob (drift.cli.run_server)
    drift dht ...                   A standalone lightweight DHT bootstrap peer
    drift api <model> ...           An OpenAI-compatible HTTP API backed by the swarm
    drift manifest <file>           Validate and inspect a ModelManifest v1

Each subcommand owns its own argument parsing; this shim just strips the subcommand
name and delegates. Also runnable as ``python -m drift.cli``.
"""

import sys

_COMMANDS = ("up", "down", "server", "dht", "api", "manifest")

_USAGE = """usage: drift <command> [options]

commands:
  up        Start or join a private swarm in one command (recommended)
              first machine:  drift up <model>
              other machines: drift up <model> --join drift://<peer_id>@<host>:<port>
  down      Stop DRIFT-LLM servers running on this machine (drift down --list to preview)
  server    Run a server with the full set of options (advanced)
  dht       Run a standalone DHT bootstrap peer
  api       Serve an OpenAI-compatible HTTP API backed by the swarm (requires drift[api])
  manifest  Validate and inspect a content-addressed ModelManifest v1

Run `drift <command> --help` for command-specific options.
"""


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(_USAGE)
        return 0

    command, rest = argv[0], argv[1:]
    if command not in _COMMANDS:
        sys.stderr.write(f"drift: unknown command {command!r}\n\n{_USAGE}")
        return 2

    # Give the delegated parser a clean argv with a sensible prog name in its --help.
    sys.argv = [f"drift {command}", *rest]
    if command == "up":
        from drift.cli.run_up import main as run
    elif command == "down":
        from drift.cli.run_down import main as run
    elif command == "server":
        from drift.cli.run_server import main as run
    elif command == "api":
        from drift.cli.run_api import main as run
    elif command == "manifest":
        from drift.cli.run_manifest import main as run
    else:  # dht
        from drift.cli.run_dht import main as run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

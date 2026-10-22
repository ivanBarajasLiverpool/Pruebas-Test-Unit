"""Command-line interface for the trusted unit-test generator."""

import argparse
import json
import sys

from .core import AgentError, generate, load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-config")
    validate.add_argument("--config", required=True)
    create = commands.add_parser("generate")
    for name in ("repository", "config", "base", "head", "output"):
        create.add_argument("--" + name, required=True)
    create.add_argument("--feedback")
    create.add_argument("--previous")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-config":
            load_config(args.config)
            result = {"status": "valid"}
        else:
            result = generate(
                repository=args.repository,
                config_path=args.config,
                base=args.base,
                head=args.head,
                output=args.output,
                feedback=args.feedback,
                previous=args.previous,
            )
    except AgentError as exc:
        print(json.dumps({"status": "error", "error": exc.code}), file=sys.stderr)
        return 2
    except (OSError, ValueError, RecursionError):
        print(json.dumps({"status": "error", "error": "invalid_input"}), file=sys.stderr)
        return 2
    summary = {key: value for key, value in result.items() if key != "ambiguity_explanations"}
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

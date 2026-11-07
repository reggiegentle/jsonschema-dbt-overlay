import argparse

from . import SchemaError, check, generate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "check"))
    parser.add_argument("--contract", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        if args.command == "generate":
            for path in generate(args.contract, args.config):
                print(f"Wrote: {path}")
            return 0
        differences = check(args.contract, args.config)
        print("\n".join(differences) if differences else "Overlay is current")
        return int(bool(differences))
    except (SchemaError, KeyError, ValueError, OSError) as exc:
        print(f"Overlay error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

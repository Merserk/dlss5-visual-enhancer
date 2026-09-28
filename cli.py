"""Console entry point used by VE_CLI.exe and the bundled Python runtime."""
import sys

sys.dont_write_bytecode = True

from src.portable import configure_portable_environment

configure_portable_environment()

from src.cli.main import main


if __name__ == "__main__":
    raise SystemExit(main())

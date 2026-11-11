"""Use the reviewed checkout with an isolated interpreter, without installing it.

Run with .venv for configuration/reports, or the pinned SDK interpreter for an
explicit later online run. Importing this entry point does not connect anything.
"""
from pathlib import Path
import sys

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
from online_trader.cli import main

if __name__=='__main__':raise SystemExit(main())

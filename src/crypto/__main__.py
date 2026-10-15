"""Enable `python -m crypto.backtest`."""
import sys

from .backtest import main

sys.exit(main())

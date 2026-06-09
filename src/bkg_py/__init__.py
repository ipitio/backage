"""Python implementations of selected bkg operations.

The Bash entrypoints remain available while individual operations move into
this package.
"""

from .result import ExitStatus

__all__ = ["ExitStatus", "__version__"]

__version__ = "0.0.0"

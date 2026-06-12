"""
Abstract base class for all market data providers.

Any new provider must subclass BaseProvider and implement `fetch_snapshots`.
The scanner only calls that one method, so adding a new source is
a self-contained change.
"""

from abc import ABC, abstractmethod

from market_providers.models import MarketSnapshot


class BaseProvider(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name, e.g. 'polymarket'."""

    @abstractmethod
    def fetch_snapshots(self) -> list[MarketSnapshot]:
        """
        Fetch current market data and return it as normalised MarketSnapshot
        objects.  Must not raise — catch and log internally, return [] on error.
        """

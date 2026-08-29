"""Neo4j driver wrapper with env-based configuration and lifecycle."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Self

from neo4j import Driver, GraphDatabase

from app.storage.errors import ConnectionError

DEFAULT_URI = "bolt://localhost:7687"
DEFAULT_USER = "neo4j"
DEFAULT_PASSWORD = "km_dev_password"


@dataclass(frozen=True)
class Neo4jConfig:
    """Connection settings for the Neo4j driver."""

    uri: str = DEFAULT_URI
    user: str = DEFAULT_USER
    password: str = DEFAULT_PASSWORD

    @classmethod
    def from_env(cls) -> Neo4jConfig:
        """Build config from KM_NEO4J_* env vars, falling back to dev defaults."""
        return cls(
            uri=os.getenv("KM_NEO4J_URI", DEFAULT_URI),
            user=os.getenv("KM_NEO4J_USER", DEFAULT_USER),
            password=os.getenv("KM_NEO4J_PASSWORD", DEFAULT_PASSWORD),
        )


class Neo4jClient:
    """Owns a Neo4j driver and exposes sessions/transactions.

    Use as a context manager to guarantee driver shutdown::

        with Neo4jClient.from_env() as client:
            client.verify_connectivity()
    """

    def __init__(self, config: Neo4jConfig | None = None) -> None:
        self.config = config or Neo4jConfig.from_env()
        self._driver: Driver | None = None

    @classmethod
    def from_env(cls) -> Neo4jClient:
        """Create a client from KM_NEO4J_* environment variables."""
        return cls(Neo4jConfig.from_env())

    @property
    def driver(self) -> Driver:
        """Return the driver, creating it lazily on first use."""
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                self.config.uri,
                auth=(self.config.user, self.config.password),
            )
        return self._driver

    def verify_connectivity(self) -> None:
        """Check the Bolt connection and raise ConnectionError on failure."""
        try:
            self.driver.verify_connectivity()
        except Exception as exc:
            raise ConnectionError(
                f"cannot connect to Neo4j at {self.config.uri}: {exc}"
            ) from exc

    def session(self, **kwargs: Any):
        """Open a new session on the driver."""
        return self.driver.session(**kwargs)

    def close(self) -> None:
        """Close the driver if it was created."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self) -> Self:
        self.verify_connectivity()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

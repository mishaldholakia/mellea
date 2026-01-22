"""Caching strategies."""

import abc
from collections import OrderedDict
from typing import Any


class Cache(abc.ABC):
    """A Cache for storing model state (e.g., kv cache)."""

    # Whenever PEP 695 generics are supported by mypy, we should use them here.

    @abc.abstractmethod
    def put(self, key: str, value: Any):
        """Inserts into the cache. May result in eviction of other cached values."""
        ...

    @abc.abstractmethod
    def get(self, key: str) -> Any | None:
        """Retrieves a value from the cache. Returns `None` if the `id` has no cached value. May impact which cache values are evicted."""
        ...

    @abc.abstractmethod
    def current_size(self) -> int:
        """Returns the number of things currently in the cache. Mostly useful for debugging."""
        ...


class SimpleLRUCache(Cache):
    """A simple [LRU](https://en.wikipedia.org/wiki/Cache_replacement_policies#Least_Recently_Used_(LRU)) cache."""

    def __init__(self, capacity: int):
        """Initializes the LRU cache with a certain capacity.

        The `SimpleLRUCache` either contains a value or it doesn't. There is no cache hierarchy. Take care when choosing `capacity`. In practice usually a small value will be fine, but ideally you should try to choose a capacity based upon your available device memory and the context size of your model.
        """
        self.capacity = capacity
        self.cache: OrderedDict = OrderedDict()

    def current_size(self):
        """Just return the size of the key set. This isn't necessarily safe."""
        return len(self.cache.keys())

    def get(self, key: str) -> Any | None:
        """Gets a value from the cache."""
        if key not in self.cache:
            return None
        else:
            # Move the accessed item to the end (most recent)
            value = self.cache.pop(key)
            self.cache[key] = value
            return value

    def put(self, key: str, value: Any):
        """Put a value into the cache."""
        if key in self.cache:
            # If the key exists, move it to the end (most recent)
            self.cache.pop(key)
        elif len(self.cache) >= self.capacity:
            # If the cache is full, remove the least recently used item
            self.cache.popitem(last=False)
        # Add the new key-value pair to the end (most recent)
        self.cache[key] = value

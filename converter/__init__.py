"""Snowflake semantic view → Sigma data model.

Column formulas use the warehouse column name, relationships point at the real
key, and a semi-additive metric keeps the latest value in each group.
"""

from .build import convert

__all__ = ["convert"]

"""sologsb scheduler service package."""
from .common import MonitorError, MonitorError as ManagerApiError  # noqa: F401  (re-export)

__all__ = ["MonitorError", "ManagerApiError"]

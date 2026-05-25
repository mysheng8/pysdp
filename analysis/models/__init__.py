"""analysis/models/__init__.py — Import all built-in models to trigger @register calls."""
from analysis.models import category_breakdown, top_bottleneck_dcs, label_quality

__all__ = ["category_breakdown", "top_bottleneck_dcs", "label_quality"]

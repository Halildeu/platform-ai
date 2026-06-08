"""Parametric cost model for the #43 performance/cost matrix.

Pure arithmetic, no assumptions baked in: every figure (electricity price, GPU
power draw, hardware cost, amortization horizon, cloud hourly rate, measured
throughput) is supplied by the caller from real numbers. Lets us compare a
local GPU (electricity + amortization) against a cloud GPU on a per-audio-minute
basis once #43 measurement provides the throughput.

Currency-agnostic: feed all money values in the same unit (e.g. TRY).
"""

from __future__ import annotations


def local_cost_per_hour(
    power_w: float,
    electricity_price_per_kwh: float,
    hardware_cost: float = 0.0,
    amortization_hours: float = 0.0,
) -> dict[str, float]:
    """Per-wall-hour cost of running a local GPU.

    electricity = (power_w / 1000) * price_per_kwh
    amortization = hardware_cost / amortization_hours (0 if horizon unset)
    """
    if power_w < 0 or electricity_price_per_kwh < 0:
        raise ValueError("power and price must be non-negative")
    electricity = (power_w / 1000.0) * electricity_price_per_kwh
    amortization = hardware_cost / amortization_hours if amortization_hours > 0 else 0.0
    return {
        "electricity": electricity,
        "amortization": amortization,
        "total": electricity + amortization,
    }


def audio_minutes_per_wall_hour(realtime_factor: float, concurrency: int = 1) -> float:
    """Audio-minutes processed per wall-hour.

    `realtime_factor` (RTF) = processing_time / audio_time. RTF < 1 means faster
    than real time. With `concurrency` parallel streams the throughput scales
    linearly (until GPU saturation — see #42).
    """
    if realtime_factor <= 0 or concurrency < 1:
        raise ValueError("realtime_factor must be > 0 and concurrency >= 1")
    return 60.0 * concurrency / realtime_factor


def cost_per_audio_minute(cost_per_hour: float, audio_min_per_wall_hour: float) -> float:
    """Cost to process one audio-minute given hourly cost and throughput."""
    if audio_min_per_wall_hour <= 0:
        return 0.0
    return cost_per_hour / audio_min_per_wall_hour


def compare_local_vs_cloud(
    *,
    local_cost_per_hour_total: float,
    cloud_cost_per_hour: float,
    audio_min_per_wall_hour: float,
) -> dict[str, object]:
    """Per-audio-minute comparison of a local GPU vs a cloud GPU."""
    local_pm = cost_per_audio_minute(local_cost_per_hour_total, audio_min_per_wall_hour)
    cloud_pm = cost_per_audio_minute(cloud_cost_per_hour, audio_min_per_wall_hour)
    cheaper = "local" if local_pm < cloud_pm else "cloud"
    return {
        "local_per_audio_min": local_pm,
        "cloud_per_audio_min": cloud_pm,
        "delta_per_audio_min": cloud_pm - local_pm,
        "cheaper": cheaper,
    }

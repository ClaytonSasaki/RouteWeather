"""
weather.py
Fetches weather forecasts from Open-Meteo using the ECMWF IFS HRES endpoint.

We use the ECMWF-specific endpoint (/v1/ecmwf) rather than the generic
forecast endpoint because it gives us:
  - Native ECMWF precipitation_type field (authoritative, model-derived)
  - Native snowfall data (not temperature-approximated)
  - Full 9 km IFS HRES resolution (open data since Oct 2025)

Precipitation amounts:
  - rain / showers : liquid inches (actual accumulation)
  - snowfall       : inches of snow (actual accumulation, not LWE)
                     Open-Meteo delivers this in cm; we convert: cm × 0.393701

precipitation_type codes (ECMWF native diagnostic):
  0  = No precipitation
  1  = Rain
  3  = Freezing rain   (supercooled droplets, freeze on contact)
  5  = Snow
  6  = Wet snow        (snow starting to melt)
  7  = Rain/snow mix   (sleet)
  8  = Ice pellets
  12 = Freezing drizzle

API docs: https://open-meteo.com/en/docs/ecmwf-api
"""

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone


# --- Constants ----------------------------------------------------------------

# ECMWF-specific endpoint — gives native precipitation_type and snowfall
OPEN_METEO_URL = "https://api.open-meteo.com/v1/ecmwf"


# --- Precipitation type decoder -----------------------------------------------

# Maps ECMWF native precipitation_type codes to our internal string and label
_PRECIP_TYPE_MAP = {
    0:  (None,             "No precipitation"),
    1:  ("rain",           "Rain"),
    3:  ("freezing_rain",  "Freezing rain"),
    5:  ("snow",           "Snow"),
    6:  ("snow",           "Wet snow"),        # melting snow, treat as snow
    7:  ("sleet",          "Rain/snow mix"),
    8:  ("sleet",          "Ice pellets"),
    12: ("freezing_rain",  "Freezing drizzle"),
}

def decode_precipitation_type(code) -> dict:
    """
    Decode an ECMWF precipitation_type integer into a precip_type string
    and a human-readable condition label.

    precip_type values used downstream:
      None           - no precipitation
      "rain"         - liquid rain
      "showers"      - convective rain showers (from weather_code)
      "freezing_rain"- freezing rain or freezing drizzle
      "snow"         - snow or wet snow
      "snow_showers" - snow showers (from weather_code)
      "sleet"        - rain/snow mix or ice pellets
      "thunderstorm" - thunderstorm (from weather_code)
      "fog"          - fog (from weather_code)
    """
    if code is None:
        return {"precip_type": None, "label": "Unknown"}
    try:
        code = int(code)
    except (TypeError, ValueError):
        return {"precip_type": None, "label": "Unknown"}

    precip_type, label = _PRECIP_TYPE_MAP.get(code, (None, f"Type {code}"))
    return {"precip_type": precip_type, "label": label}


# WMO weather_code → human label for non-precip conditions and refinements
_WMO_LABEL = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Heavy freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight showers", 81: "Moderate showers", 82: "Heavy showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ hail",
}

_WMO_PRECIP_TYPE = {
    45: "fog", 48: "fog",
    80: "showers", 81: "showers", 82: "showers",
    85: "snow_showers", 86: "snow_showers",
    95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
}


# --- Public API ---------------------------------------------------------------

def fetch_weather_for_waypoints(
    waypoints: list[dict],
    token: str,
    repo: str,
    wind_warning_mph: float = 40.0,
) -> list[dict]:
    """Fetch weather for all waypoints in parallel using Open-Meteo ECMWF."""
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(_fetch_one, wp, wind_warning_mph): i
            for i, wp in enumerate(waypoints)
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                waypoints[idx]["weather"] = future.result()
            except Exception as e:
                print(f"[weather] Error for waypoint {idx}: {e}")
                waypoints[idx]["weather"] = _weather_unavailable(str(e))

    return waypoints


# --- Internal helpers ---------------------------------------------------------

def _fetch_one(wp: dict, wind_warning_mph: float) -> dict:
    """Fetch hourly weather from Open-Meteo ECMWF for a single waypoint."""
    lat = wp["lat"]
    lon = wp["lon"]
    arrival_dt: datetime = wp["arrival_dt"]

    if arrival_dt.tzinfo is not None:
        arrival_utc = arrival_dt.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        arrival_utc = arrival_dt

    params = {
        "latitude":           lat,
        "longitude":          lon,
        "hourly":             ",".join([
                                  "temperature_2m",
                                  "precipitation",
                                  "rain",
                                  "showers",
                                  "snowfall",
                                  "precipitation_type",
                                  "weather_code",
                                  "cloud_cover",
                                  "wind_speed_10m",
                                  "wind_direction_10m",
                                  "visibility",
                              ]),
        "temperature_unit":   "fahrenheit",
        "wind_speed_unit":    "mph",
        "precipitation_unit": "inch",
        "forecast_days":      15,
        "timezone":           "UTC",
    }

    resp = requests.get(OPEN_METEO_URL, params=params, timeout=15)

    if resp.status_code != 200:
        return _weather_unavailable(f"Open-Meteo returned HTTP {resp.status_code}")

    data = resp.json()

    if "hourly" not in data:
        return _weather_unavailable("No hourly data in Open-Meteo response.")

    hourly = data["hourly"]
    times  = hourly.get("time", [])

    if not times:
        return _weather_unavailable("Empty time series from Open-Meteo.")

    best_idx = _nearest_hour_index(times, arrival_utc)

    if best_idx is None:
        return _weather_unavailable(
            f"Arrival time {arrival_utc.isoformat()} is outside the 15-day forecast window."
        )

    def val(key, fallback=None):
        arr = hourly.get(key, [])
        if best_idx < len(arr) and arr[best_idx] is not None:
            try:
                return float(arr[best_idx])
            except (TypeError, ValueError):
                return fallback
        return fallback

    temp_f        = val("temperature_2m")
    precip_total  = val("precipitation", 0.0)   # liquid-equivalent total, inches
    rain_in       = val("rain", 0.0)             # liquid rain, inches
    showers_in    = val("showers", 0.0)          # liquid showers, inches
    snowfall_cm   = val("snowfall", 0.0)         # actual snow depth, cm
    precip_type_code = val("precipitation_type") # ECMWF native code
    weather_code  = val("weather_code")          # WMO code (for labels/refinement)
    cloud_pct     = val("cloud_cover", 0.0)
    wind_mph      = val("wind_speed_10m", 0.0)
    wind_deg      = val("wind_direction_10m", 0.0)
    vis_m         = val("visibility", 16000.0)

    # Snowfall: cm of actual snow accumulation → inches of actual snow
    snowfall_in = (snowfall_cm or 0.0) * 0.393701

    vis_mi   = (vis_m or 0.0) / 1609.34
    wind_dir = _degrees_to_compass(wind_deg)

    # Decode precipitation type from ECMWF native codes (most authoritative)
    pt_decoded   = decode_precipitation_type(precip_type_code)
    precip_type  = pt_decoded["precip_type"]
    condition    = pt_decoded["label"]

    # Refine with WMO weather_code for conditions not in precipitation_type:
    # showers, snow showers, thunderstorm, fog are better captured there
    wmo_int = int(weather_code) if weather_code is not None else None
    if wmo_int is not None:
        wmo_override = _WMO_PRECIP_TYPE.get(wmo_int)
        wmo_label    = _WMO_LABEL.get(wmo_int)
        # Use WMO for non-precip refinements (fog, thunderstorm, showers)
        # but don't let WMO override a more specific ECMWF precip_type
        if wmo_override in ("fog", "thunderstorm"):
            precip_type = wmo_override
            condition   = wmo_label
        elif precip_type is None and wmo_override:
            precip_type = wmo_override
            condition   = wmo_label
        elif precip_type is None and wmo_label:
            condition = wmo_label   # use WMO for clear/cloudy descriptions

    return {
        "temp_f":          round(temp_f, 1) if temp_f is not None else None,
        "precip_in_hr":    round(precip_total or 0.0, 4),
        "rain_in_hr":      round(rain_in or 0.0, 4),
        "showers_in_hr":   round(showers_in or 0.0, 4),
        "snowfall_in_hr":  round(snowfall_in, 4),
        "cloud_pct":       round(cloud_pct or 0.0, 1),
        "wind_mph":        round(wind_mph or 0.0, 1),
        "wind_dir":        wind_dir,
        "vis_mi":          round(min(vis_mi, 99.0), 1),
        "wind_warning":    (wind_mph or 0.0) >= wind_warning_mph,
        "precip_type_code":int(precip_type_code) if precip_type_code is not None else None,
        "weather_code":    wmo_int,
        "condition":       condition,
        "precip_type":     precip_type,
        "available":       True,
    }


def _nearest_hour_index(times: list[str], target: datetime) -> int | None:
    best_idx  = None
    best_diff = float("inf")
    for i, t_str in enumerate(times):
        try:
            t = datetime.fromisoformat(t_str)
        except ValueError:
            continue
        diff = abs((t - target).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best_idx  = i
    return best_idx if best_diff <= 12 * 3600 else None


def _weather_unavailable(reason: str) -> dict:
    return {
        "available":       False,
        "reason":          reason,
        "temp_f":          None,
        "precip_in_hr":    0.0,
        "rain_in_hr":      0.0,
        "showers_in_hr":   0.0,
        "snowfall_in_hr":  0.0,
        "cloud_pct":       0.0,
        "wind_mph":        0.0,
        "wind_dir":        "—",
        "vis_mi":          0.0,
        "wind_warning":    False,
        "precip_type_code":None,
        "weather_code":    None,
        "condition":       "Unavailable",
        "precip_type":     None,
    }


def _degrees_to_compass(degrees: float | None) -> str:
    if degrees is None:
        return "—"
    directions = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
                  "S","SSW","SW","WSW","W","WNW","NW","NNW"]
    return directions[round(degrees / 22.5) % 16]

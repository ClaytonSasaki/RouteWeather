"""
routing.py
Handles address geocoding (Nominatim), route fetching (OpenRouteService),
waypoint sampling, and arrival time estimation.
"""

import math
import os
import requests
from datetime import datetime, timedelta


# --- Geocoding ----------------------------------------------------------------

def geocode(address: str) -> tuple[float, float] | None:
    """
    Convert an address string to (longitude, latitude) using Nominatim (OpenStreetMap).
    Free, no API key required. Returns None on failure.

    Nominatim's usage policy requires a descriptive User-Agent with a real
    contact email. Set CONTACT_EMAIL in your .env file -- requests with
    placeholder emails (e.g. example.com) will be blocked with a 403.
    See: https://operations.osmfoundation.org/policies/nominatim/
    """
    contact = os.environ.get("CONTACT_EMAIL", "")
    if not contact:
        raise RuntimeError(
            "CONTACT_EMAIL is not set in your .env file. "
            "Nominatim requires a real contact email in the User-Agent. "
            "Add: CONTACT_EMAIL=you@yourdomain.com"
        )

    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": address,
        "format": "json",
        "limit": 1,
    }
    headers = {"User-Agent": f"RouteWeatherPlanner/1.0 ({contact})"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None
        lon = float(results[0]["lon"])
        lat = float(results[0]["lat"])
        return (lon, lat)
    except Exception as e:
        print(f"[geocode] Error: {e}")
        return None


# --- Routing ------------------------------------------------------------------

def get_route(start: tuple, end: tuple, api_key: str) -> dict | None:
    """
    Get a driving route between two (lon, lat) points using OpenRouteService.
    Returns a dict with:
      - coords: [[lon, lat], ...] full route geometry
      - distance_mi: total distance in miles
      - duration_hrs: estimated driving duration in hours

    Get a free API key at https://openrouteservice.org/dev/#/signup
    Free tier: 2,000 req/day, 40 req/min.
    """
    url = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {
        "coordinates": [list(start), list(end)],
        "instructions": False,
    }

    try:
        resp = requests.post(url, json=body, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        feature  = data["features"][0]
        coords   = feature["geometry"]["coordinates"]
        props    = feature["properties"]["summary"]
        dist_m   = props["distance"]
        dur_s    = props["duration"]

        return {
            "coords":       coords,
            "distance_mi":  dist_m * 0.000621371,
            "duration_hrs": dur_s / 3600,
        }

    except requests.exceptions.HTTPError as e:
        print(f"[get_route] HTTP error: {e} -- {e.response.text}")
        return None
    except Exception as e:
        print(f"[get_route] Error: {e}")
        return None


# --- Geometry helpers ---------------------------------------------------------

def haversine_mi(lon1, lat1, lon2, lat2) -> float:
    """Great-circle distance in miles between two lon/lat points."""
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def interpolate_point(lon1, lat1, lon2, lat2, frac: float) -> tuple[float, float]:
    """Linearly interpolate a point between two coordinates."""
    return (lon1 + (lon2 - lon1) * frac, lat1 + (lat2 - lat1) * frac)


# --- Waypoint sampling -------------------------------------------------------

def sample_waypoints(coords: list, interval_mi: float, total_dist_mi: float) -> list[dict]:
    """
    Walk along the route geometry and pick a waypoint every interval_mi miles.
    Always includes the destination as the final waypoint.
    """
    waypoints = []
    accumulated = 0.0
    next_target = interval_mi
    wp_index = 1

    prev_lon, prev_lat = coords[0]

    for i in range(1, len(coords)):
        cur_lon, cur_lat = coords[i]
        seg_dist = haversine_mi(prev_lon, prev_lat, cur_lon, cur_lat)

        while next_target <= accumulated + seg_dist:
            frac = (next_target - accumulated) / seg_dist
            lon, lat = interpolate_point(prev_lon, prev_lat, cur_lon, cur_lat, frac)
            waypoints.append({
                "lon":   lon,
                "lat":   lat,
                "mile":  round(next_target, 1),
                "label": f"Checkpoint {wp_index} (Mile {round(next_target, 0):.0f})",
            })
            wp_index += 1
            next_target += interval_mi

        accumulated += seg_dist
        prev_lon, prev_lat = cur_lon, cur_lat

    end_lon, end_lat = coords[-1]
    waypoints.append({
        "lon":   end_lon,
        "lat":   end_lat,
        "mile":  round(total_dist_mi, 1),
        "label": f"Destination (Mile {round(total_dist_mi, 0):.0f})",
    })

    return waypoints


# --- Arrival time estimation -------------------------------------------------

def estimate_arrival_times(
    waypoints: list[dict],
    departure_dt: datetime,
    avg_speed_mph: float = 60.0,
) -> list[dict]:
    """
    Estimate the wall-clock arrival time at each waypoint.
    Adds 'arrival_dt' and 'arrival_str' to each waypoint dict.
    """
    for wp in waypoints:
        hours_elapsed = wp["mile"] / avg_speed_mph
        arrival = departure_dt + timedelta(hours=hours_elapsed)
        wp["arrival_dt"]  = arrival
        wp["arrival_str"] = arrival.strftime("%a %b %-d, %I:%M %p")
    return waypoints

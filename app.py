"""
Route Weather Planner
Flask app that shows weather conditions along a driving route,
fetching forecast data from Open-Meteo using the ECMWF IFS model.
"""

import os
import folium
from flask import Flask, render_template, request
from datetime import datetime, timedelta
from dotenv import load_dotenv

from routing import geocode, get_route, sample_waypoints, estimate_arrival_times
from weather import fetch_weather_for_waypoints

load_dotenv()

app = Flask(__name__)

# --- Configuration ------------------------------------------------------------

ORS_API_KEY      = os.environ.get("ORS_API_KEY", "")
WIND_WARNING_MPH = int(os.environ.get("WIND_WARNING_MPH", 40))


# --- Routes -------------------------------------------------------------------

@app.route("/")
def index():
    missing = []
    if not ORS_API_KEY:
        missing.append("ORS_API_KEY")
    return render_template("index.html", missing_keys=missing)


@app.route("/plan", methods=["POST"])
def plan():
    start_addr  = request.form.get("start", "").strip()
    end_addr    = request.form.get("end", "").strip()
    depart_str  = request.form.get("departure", "").strip()
    interval_mi = int(request.form.get("interval", 50))
    avg_speed   = float(request.form.get("avg_speed", 60))

    # -- Validate inputs -------------------------------------------------------
    errors = []
    if not start_addr:
        errors.append("Starting address is required.")
    if not end_addr:
        errors.append("Ending address is required.")
    if not depart_str:
        errors.append("Departure date & time is required.")
    if errors:
        return render_template("index.html", errors=errors, missing_keys=[])

    try:
        departure_dt = datetime.fromisoformat(depart_str)
    except ValueError:
        return render_template("index.html", errors=["Invalid date/time format."], missing_keys=[])

    # -- Geocode ---------------------------------------------------------------
    start_coords = geocode(start_addr)
    end_coords   = geocode(end_addr)

    if not start_coords:
        return render_template("index.html",
                               errors=[f'Could not find address: "{start_addr}"'],
                               missing_keys=[])
    if not end_coords:
        return render_template("index.html",
                               errors=[f'Could not find address: "{end_addr}"'],
                               missing_keys=[])

    # -- Route -----------------------------------------------------------------
    route_data = get_route(start_coords, end_coords, ORS_API_KEY)
    if not route_data:
        return render_template("index.html",
                               errors=["Could not compute route. Check your ORS API key."],
                               missing_keys=[])

    coords_list   = route_data["coords"]
    total_dist_mi = route_data["distance_mi"]
    total_dur_hrs = route_data["duration_hrs"]

    # -- Sample waypoints ------------------------------------------------------
    waypoints = sample_waypoints(coords_list, interval_mi, total_dist_mi)

    # -- Estimate arrival times ------------------------------------------------
    waypoints = estimate_arrival_times(waypoints, departure_dt, avg_speed)

    # -- Fetch weather ---------------------------------------------------------
    waypoints = fetch_weather_for_waypoints(
        waypoints, token="", repo="", wind_warning_mph=WIND_WARNING_MPH
    )

    # -- Build map -------------------------------------------------------------
    map_html = build_map(coords_list, waypoints, start_addr, end_addr)

    eta = departure_dt + timedelta(hours=total_dur_hrs)

    return render_template(
        "result.html",
        map_html=map_html,
        waypoints=waypoints,
        start=start_addr,
        end=end_addr,
        total_mi=round(total_dist_mi, 1),
        duration_hrs=total_dur_hrs,
        departure=departure_dt.strftime("%b %d, %Y at %I:%M %p"),
        eta=eta.strftime("%b %d, %Y at %I:%M %p"),
        wind_mph=WIND_WARNING_MPH,
    )


# --- Helpers ------------------------------------------------------------------

def precip_dot_color(weather: dict) -> str:
    """
    Map precip_type from the WMO weather code to a marker color name.
    Priority: wind warning > thunderstorm > freezing > snow > rain > cloud > clear
    """
    if not weather or not weather.get("available"):
        return "blue"
    if weather.get("wind_warning"):
        return "red"
    pt = weather.get("precip_type")
    return {
        "thunderstorm":  "purple",
        "freezing_rain": "orange",
        "snow":          "blue_snow",
        "snow_showers":  "blue_snow",
        "sleet":         "orange",
        "showers":       "yellow",
        "rain":          "yellow",
        "fog":           "gray",
    }.get(pt, "green" if weather.get("cloud_pct", 0) <= 75 else "gray")


FILL_COLORS = {
    "green":     "#2ECC71",
    "gray":      "#95A5A6",
    "orange":    "#E67E22",
    "yellow":    "#F4D03F",
    "red":       "#E74C3C",
    "purple":    "#9B59B6",
    "blue_snow": "#5DADE2",
    "blue":      "#3498DB",
}


# --- Map Builder --------------------------------------------------------------

def build_map(coords_list, waypoints, start_addr, end_addr):
    lons = [c[0] for c in coords_list]
    lats = [c[1] for c in coords_list]
    center = [(min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2]

    m = folium.Map(location=center, zoom_start=6, tiles="CartoDB positron")

    # Route line
    folium.PolyLine(
        [[c[1], c[0]] for c in coords_list],
        color="#1A73E8", weight=5, opacity=0.85,
        tooltip="Planned Route"
    ).add_to(m)

    # Start / end markers
    folium.Marker(
        location=[coords_list[0][1], coords_list[0][0]],
        popup=folium.Popup(f"<b>Start</b><br>{start_addr}", max_width=220),
        tooltip="Start",
        icon=folium.Icon(color="green", icon="play", prefix="fa"),
    ).add_to(m)

    folium.Marker(
        location=[coords_list[-1][1], coords_list[-1][0]],
        popup=folium.Popup(f"<b>Destination</b><br>{end_addr}", max_width=220),
        tooltip="Destination",
        icon=folium.Icon(color="red", icon="flag-checkered", prefix="fa"),
    ).add_to(m)

    # Weather waypoint markers
    for wp in waypoints:
        lat, lon  = wp["lat"], wp["lon"]
        label     = wp.get("label", f"Mile {wp['mile']}")
        arrival   = wp.get("arrival_str", "")
        weather   = wp.get("weather")

        color      = precip_dot_color(weather)
        fill_color = FILL_COLORS.get(color, "#3498DB")

        if weather and weather.get("available"):
            w = weather
            temp_str    = f"{w['temp_f']:.1f}°F" if w["temp_f"] is not None else "—"
            precip_str  = f"{w['precip_in_hr']:.3f} in/hr"
            snow_str    = f"{w['snowfall_in_hr']:.3f} in/hr"
            cloud_str   = f"{w['cloud_pct']:.0f}%"
            vis_str     = f"{w['vis_mi']:.1f} mi"
            wind_str    = f"{w['wind_mph']:.1f} mph"
            condition   = w.get("condition", "")

            # Build precip rows depending on what's falling
            pt = w.get("precip_type")
            precip_rows = ""
            if pt in ("rain", "showers"):
                precip_rows = f'<tr><td>Rain</td><td style="text-align:right"><b>{precip_str}</b></td></tr>'
            elif pt in ("snow", "snow_showers"):
                precip_rows = f'<tr><td>Snow</td><td style="text-align:right"><b>{snow_str}</b></td></tr>'
            elif pt == "freezing_rain":
                precip_rows = f'<tr><td>Frz. Rain</td><td style="text-align:right"><b>{precip_str}</b></td></tr>'
            elif pt == "sleet":
                precip_rows = (
                    f'<tr><td>Rain</td><td style="text-align:right"><b>{precip_str}</b></td></tr>'
                    f'<tr><td>Snow</td><td style="text-align:right"><b>{snow_str}</b></td></tr>'
                )
            elif pt == "thunderstorm":
                precip_rows = f'<tr><td>Precip</td><td style="text-align:right"><b>{precip_str}</b></td></tr>'
            elif w["precip_in_hr"] > 0:
                precip_rows = f'<tr><td>Precip</td><td style="text-align:right"><b>{precip_str}</b></td></tr>'

            wind_flag = (
                f'<div style="color:#E74C3C;font-weight:bold;margin-top:6px">&#9888; Extreme wind: {w["wind_mph"]:.0f} mph</div>'
                if w.get("wind_warning") else ""
            )

            popup_html = f"""
            <div style="font-family:sans-serif;min-width:200px">
              <div style="font-size:14px;font-weight:bold;margin-bottom:2px">{label}</div>
              <div style="color:#888;font-size:12px;margin-bottom:6px">Arrives ~{arrival}</div>
              <div style="font-size:12px;color:#555;margin-bottom:8px">{condition}</div>
              <table style="font-size:13px;border-collapse:collapse;width:100%">
                <tr><td>Temp</td><td style="text-align:right"><b>{temp_str}</b></td></tr>
                {precip_rows}
                <tr><td>Cloud cover</td><td style="text-align:right"><b>{cloud_str}</b></td></tr>
                <tr><td>Visibility</td><td style="text-align:right"><b>{vis_str}</b></td></tr>
                <tr><td>Wind</td><td style="text-align:right"><b>{wind_str}</b></td></tr>
                <tr><td>Direction</td><td style="text-align:right"><b>{w['wind_dir']}</b></td></tr>
              </table>
              {wind_flag}
            </div>
            """
        else:
            reason = weather.get("reason", "Unavailable") if weather else "Unavailable"
            popup_html = f"""
            <div style="font-family:sans-serif">
              <b>{label}</b><br>
              <span style="color:#888">Arrives ~{arrival}</span><br>
              <span style="color:#aaa;font-size:12px">{reason}</span>
            </div>
            """

        folium.CircleMarker(
            location=[lat, lon],
            radius=9,
            color="white",
            weight=1.5,
            fill=True,
            fill_color=fill_color,
            fill_opacity=0.9,
            popup=folium.Popup(popup_html, max_width=250),
            tooltip=f"{label} — {arrival}",
        ).add_to(m)

    m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])
    return m._repr_html_()


# --- Entry point --------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)

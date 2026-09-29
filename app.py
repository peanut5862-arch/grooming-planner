
import os
import json
import re
import itertools
import urllib.parse
import urllib.request
from datetime import date, datetime, time

import pandas as pd
import streamlit as st

try:
    from supabase import create_client
except Exception:
    create_client = None

st.set_page_config(
    page_title="Mobile Grooming Planner v24.3",
    page_icon="🐾",
    layout="wide",
)

st.title("🐾 Mobile Grooming Planner v24.3")
st.caption("Private client manager + due-date intelligence + weekly routing + real Google Maps drive-time optimization.")

WORKDAYS = {
    "Jen": ["Tuesday", "Wednesday", "Thursday"],
    "Haley": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
}

CLIENT_COLUMNS = [
    "Owner",
    "Dog",
    "Household ID",
    "Phone",
    "Area",
    "Groomer",
    "Last Groom",
    "Frequency Weeks",
    "Service Pattern",
    "Next Service",
    "Bath Frequency Weeks",
    "Last Bath",
    "Groom Frequency Weeks",
    "Last Groom Service",
    "Bath Price",
    "Bath Minutes",
    "Groom Price",
    "Groom Minutes",
    "Price",
    "Minutes",
    "Household Override Minutes",
    "Address",
    "City",
    "State",
    "ZIP",
    "Latitude",
    "Longitude",
    "Last Contacted",
    "Notes",
]

APPT_COLUMNS = [
    "Date",
    "Start Time",
    "End Time",
    "Client",
    "Area",
    "Groomer",
]

# ---------- Secrets / privacy ----------

def get_secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.getenv(name, default)

def require_password():
    expected = get_secret("APP_PASSWORD", "")
    if not expected:
        return

    if st.session_state.get("authenticated"):
        return

    st.info("This planner is password protected.")
    password = st.text_input("App password", type="password")

    if st.button("Unlock"):
        if password == expected:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    st.stop()

require_password()

# ---------- Google Maps ----------

def get_maps_key():
    return get_secret("GOOGLE_MAPS_API_KEY", "")


def get_groomer_home_address(groomer):
    """Read a groomer's private home address from Streamlit Secrets."""
    secret_name = {
        "Jen": "JEN_HOME_ADDRESS",
        "Haley": "HALEY_HOME_ADDRESS",
    }.get(str(groomer or "").strip())

    if not secret_name:
        return ""

    return str(get_secret(secret_name, "") or "").strip()


def get_groomer_home_coords(groomer, maps_key):
    """
    Geocode a groomer's private home address and cache only the coordinates
    in the current Streamlit session. The address itself stays in Secrets.
    """
    if not maps_key:
        return None

    cache = st.session_state.setdefault("groomer_home_coords", {})
    if groomer in cache:
        return cache[groomer]

    address = get_groomer_home_address(groomer)
    if not address:
        return None

    try:
        lat, lon = geocode_address(address, maps_key)
    except Exception:
        return None

    if lat is None or lon is None:
        return None

    cache[groomer] = (float(lat), float(lon))
    return cache[groomer]


def get_json(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))

def geocode_address(full_address, key):
    params = urllib.parse.urlencode({"address": full_address, "key": key})
    result = get_json("https://maps.googleapis.com/maps/api/geocode/json?" + params)

    if result.get("status") != "OK" or not result.get("results"):
        return None, None

    loc = result["results"][0]["geometry"]["location"]
    return loc["lat"], loc["lng"]

def _routes_waypoint(value):
    """
    Build a Routes API waypoint from either:
    - (latitude, longitude), or
    - a saved street-address string.
    """
    if isinstance(value, str):
        address = value.strip()
        if not address:
            return None
        return {"address": address}

    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return {
            "location": {
                "latLng": {
                    "latitude": float(value[0]),
                    "longitude": float(value[1]),
                }
            }
        }

    return None


def route_metrics(origin, destination, key):
    origin_waypoint = _routes_waypoint(origin)
    destination_waypoint = _routes_waypoint(destination)

    if origin_waypoint is None or destination_waypoint is None:
        return None, None

    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    payload = {
        "origin": origin_waypoint,
        "destination": destination_waypoint,
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "units": "IMPERIAL",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }

    result = get_json(
        url,
        headers=headers,
        data=json.dumps(payload).encode("utf-8"),
    )

    routes = result.get("routes", [])
    if not routes:
        return None, None

    r = routes[0]
    miles = r.get("distanceMeters", 0) / 1609.344
    duration = str(r.get("duration", "0s")).replace("s", "")

    try:
        minutes = float(duration) / 60
    except ValueError:
        minutes = None

    return miles, minutes


def _household_client_rows(household_id, owner=None):
    clients_df = st.session_state.get("clients", pd.DataFrame())
    if clients_df is None or clients_df.empty:
        return pd.DataFrame()

    hid = str(household_id or "").strip()

    if hid and hid.lower() not in {"nan", "none"}:
        matches = clients_df[
            clients_df["Household ID"].astype(str).str.strip() == hid
        ].copy()
        if not matches.empty:
            return matches

    # Defensive fallback for older/migrated weekly drafts whose Household ID
    # may not exactly match the current client table.
    owner_text = str(owner or "").strip()
    if owner_text and "Owner" in clients_df.columns:
        matches = clients_df[
            clients_df["Owner"].astype(str).str.strip().str.casefold()
            == owner_text.casefold()
        ].copy()
        if not matches.empty:
            return matches

    return pd.DataFrame()


def household_address(household_id, owner=None):
    rows = _household_client_rows(household_id, owner=owner)
    if rows.empty:
        return ""

    for _, row in rows.iterrows():
        address = client_full_address(row)
        if address:
            return address
    return ""


def household_coords(
    household_id,
    maps_key=None,
    geocode_if_missing=False,
    owner=None,
):
    """
    Return one lat/lon pair for a household when available.

    Geocoding is still attempted so we can cache coordinates, but v24.1 no
    longer requires geocoding to succeed for routing: the Routes API can route
    directly from the saved address string.
    """
    rows = _household_client_rows(household_id, owner=owner)
    if rows.empty:
        return None

    for _, row in rows.iterrows():
        lat = pd.to_numeric(row.get("Latitude"), errors="coerce")
        lon = pd.to_numeric(row.get("Longitude"), errors="coerce")
        if pd.notna(lat) and pd.notna(lon):
            return float(lat), float(lon)

    if not (geocode_if_missing and maps_key):
        return None

    address = household_address(household_id, owner=owner)
    if not address:
        return None

    try:
        lat, lon = geocode_address(address, maps_key)
    except Exception:
        return None

    if lat is None or lon is None:
        return None

    # Cache in session and persist to each dog record in the household.
    clients_df = st.session_state.clients.copy()

    hid = str(household_id or "").strip()
    if hid and hid.lower() not in {"nan", "none"}:
        mask = (
            clients_df["Household ID"].astype(str).str.strip()
            == hid
        )
    else:
        owner_text = str(owner or "").strip()
        mask = (
            clients_df["Owner"].astype(str).str.strip().str.casefold()
            == owner_text.casefold()
        )

    clients_df.loc[mask, "Latitude"] = float(lat)
    clients_df.loc[mask, "Longitude"] = float(lon)
    st.session_state.clients = clients_df

    if supabase_configured():
        for _, dog_row in clients_df[mask].iterrows():
            record_id = dog_row.get("Record ID")
            if (
                record_id
                and str(record_id).strip()
                and str(record_id).strip().lower() != "nan"
            ):
                (
                    get_supabase()
                    .table("dogs")
                    .update({
                        "latitude": float(lat),
                        "longitude": float(lon),
                    })
                    .eq("id", str(record_id))
                    .execute()
                )

    return float(lat), float(lon)


def household_route_point(household_id, maps_key=None, owner=None):
    """
    Return the best routable representation for Google Routes.

    Prefer saved/cached coordinates. If Geocoding cannot produce coordinates,
    fall back to the saved address string and let Routes API geocode it
    directly. This mirrors why an address can work in Google Maps even when
    our separate Geocoding request failed.
    """
    coords = household_coords(
        household_id,
        maps_key=maps_key,
        geocode_if_missing=True,
        owner=owner,
    )
    if coords is not None:
        return coords

    address = household_address(household_id, owner=owner)
    if address:
        return address

    return None


def get_groomer_home_route_point(groomer, maps_key):
    """
    Prefer cached home coordinates, but fall back to the private home address
    string so Routes API can geocode it directly.
    """
    coords = get_groomer_home_coords(groomer, maps_key)
    if coords is not None:
        return coords

    address = get_groomer_home_address(groomer)
    return address or None


def _best_route_order(
    indexes,
    coords_by_index,
    maps_key,
    start_coord=None,
    end_coord=None,
):
    """
    Find the shortest route through the day's clients.

    When home coordinates are supplied, total route cost includes:
      home -> first client -> ... -> last client -> home
    """
    indexes = list(indexes)
    if not indexes:
        return indexes, {}, {}, {}

    pair_metrics = {}
    start_metrics = {}
    end_metrics = {}

    # Client-to-client directed legs.
    for a in indexes:
        for b in indexes:
            if a == b:
                continue
            try:
                miles, minutes = route_metrics(
                    coords_by_index[a],
                    coords_by_index[b],
                    maps_key,
                )
            except Exception:
                miles, minutes = None, None

            if minutes is not None:
                pair_metrics[(a, b)] = (
                    float(miles or 0),
                    float(minutes),
                )

    # Home/start -> each client.
    if start_coord is not None:
        for idx in indexes:
            try:
                miles, minutes = route_metrics(
                    start_coord,
                    coords_by_index[idx],
                    maps_key,
                )
            except Exception:
                miles, minutes = None, None
            if minutes is not None:
                start_metrics[idx] = (
                    float(miles or 0),
                    float(minutes),
                )

    # Each client -> home/end.
    if end_coord is not None:
        for idx in indexes:
            try:
                miles, minutes = route_metrics(
                    coords_by_index[idx],
                    end_coord,
                    maps_key,
                )
            except Exception:
                miles, minutes = None, None
            if minutes is not None:
                end_metrics[idx] = (
                    float(miles or 0),
                    float(minutes),
                )

    def path_minutes(order):
        if not order:
            return 0.0

        total = 0.0

        if start_coord is not None:
            metric = start_metrics.get(order[0])
            if metric is None:
                return float("inf")
            total += metric[1]

        for a, b in zip(order, order[1:]):
            metric = pair_metrics.get((a, b))
            if metric is None:
                return float("inf")
            total += metric[1]

        if end_coord is not None:
            metric = end_metrics.get(order[-1])
            if metric is None:
                return float("inf")
            total += metric[1]

        return total

    best = indexes
    best_minutes = path_minutes(indexes)

    for perm in itertools.permutations(indexes):
        minutes = path_minutes(perm)
        if minutes < best_minutes:
            best = list(perm)
            best_minutes = minutes

    return best, pair_metrics, start_metrics, end_metrics



def _best_timed_route_order(
    indexes,
    pair_metrics,
    start_metrics,
    end_metrics,
    durations,
    locked_times,
    first_arrival_time,
    service_buffer_minutes=0,
    has_home=False,
):
    """
    Choose a feasible route while honoring exact appointment times.

    Special handling:
    - If an exact appointment is at or before the normal first-stop arrival
      time, it is treated as the first stop for that day.
    - Missing return-home data never makes an otherwise valid exact-time
      schedule fail.
    - Missing nonessential route legs are skipped rather than invalidating
      every possible order.
    """
    indexes = list(indexes)
    if not indexes:
        return [], {}, None

    base_first = (
        first_arrival_time.hour * 60
        + first_arrival_time.minute
    )

    # If one or more locks are at/before the normal first arrival, the earliest
    # of those locks must be first. This is the common "customer needs 8:30"
    # case and should never be rejected merely because another permutation
    # could not be fully evaluated.
    forced_first = None
    early_locked = [
        (idx, locked_times[idx])
        for idx in indexes
        if idx in locked_times and locked_times[idx] <= base_first
    ]
    if early_locked:
        forced_first = min(early_locked, key=lambda x: x[1])[0]

    def evaluate(order):
        schedule = {}
        previous = None
        current_end = None
        total_drive = 0.0
        total_wait = 0.0

        for position, idx in enumerate(order):
            locked = locked_times.get(idx)
            duration = int(durations.get(idx, 0) or 0)
            duration += int(service_buffer_minutes)

            if position == 0:
                home_metric = start_metrics.get(idx) if has_home else None
                if home_metric is not None:
                    total_drive += float(home_metric[1])

                if locked is not None:
                    start_min = locked
                else:
                    start_min = base_first
            else:
                metric = pair_metrics.get((previous, idx))
                if metric is None:
                    # This order cannot be safely timed if the travel leg is
                    # unknown.
                    return None

                drive_min = float(metric[1])
                total_drive += drive_min
                earliest = current_end + drive_min

                if locked is not None:
                    if earliest > locked + 0.5:
                        return None
                    total_wait += max(locked - earliest, 0)
                    start_min = locked
                else:
                    start_min = earliest

            end_min = start_min + duration
            schedule[idx] = {
                "start": start_min,
                "end": end_min,
            }
            current_end = end_min
            previous = idx

        finish_with_home = current_end
        if has_home and order:
            metric = end_metrics.get(order[-1])
            if metric is not None:
                total_drive += float(metric[1])
                finish_with_home = current_end + float(metric[1])

        score = (
            float(finish_with_home),
            float(total_wait),
            float(total_drive),
        )
        return score, schedule

    # For a forced first stop, only test orders that begin there.
    if forced_first is not None:
        remaining = [idx for idx in indexes if idx != forced_first]
        permutations = (
            (forced_first,) + perm
            for perm in itertools.permutations(remaining)
        )
    else:
        permutations = itertools.permutations(indexes)

    best_order = None
    best_schedule = {}
    best_score = None

    for perm in permutations:
        result = evaluate(perm)
        if result is None:
            continue
        score, schedule = result
        if best_score is None or score < best_score:
            best_order = list(perm)
            best_schedule = schedule
            best_score = score

    return best_order, best_schedule, best_score



def optimize_week_with_google_maps(
    plan,
    maps_key,
    jen_start,
    haley_start,
    service_buffer_minutes=0,
):
    """
    Optimize each groomer/day using real road drive times.

    If a groomer's home address is present in Streamlit Secrets, route cost and
    timing include leaving home before the first client and returning home after
    the final client.
    """
    if plan is None or plan.empty:
        return plan, []

    optimized = plan.copy()

    for col, default in [
        ("Route Order", 0),
        ("Drive From Previous Min", 0.0),
        ("Drive From Previous Miles", 0.0),
        ("Drive Home After Min", 0.0),
        ("Drive Home After Miles", 0.0),
        ("Leave Home Time", ""),
        ("Locked Time", ""),
        ("Routing Mode", ""),
    ]:
        if col not in optimized.columns:
            optimized[col] = default

    if "Appointment Status" not in optimized.columns:
        optimized["Appointment Status"] = "Scheduled"

    routing_notes = []

    active = optimized[
        (optimized["Owner"].astype(str).str.strip() != "")
        & ~optimized["Appointment Status"]
        .fillna("Scheduled")
        .isin(["Cancelled", "Moved to another week"])
    ].copy()

    for (day_date, groomer), group in active.groupby(
        ["Date", "Groomer"],
        sort=True,
    ):
        if group.empty:
            continue

        coords = {}
        missing = []

        for row_index, row in group.iterrows():
            hid = row.get("Household ID")
            point = household_route_point(
                hid,
                maps_key=maps_key,
                owner=row.get("Owner"),
            )
            if point is None:
                missing.append(
                    str(row.get("Owner", "") or hid or "Unknown client")
                )
            else:
                coords[row_index] = point

        if missing:
            routing_notes.append(
                f"{pd.Timestamp(day_date):%a %b %d} · {groomer}: "
                f"could not route {', '.join(missing)} because an address/coordinate "
                "was unavailable."
            )

        home_address = get_groomer_home_address(groomer)
        home_coord = get_groomer_home_route_point(groomer, maps_key)

        if not home_address:
            routing_notes.append(
                f"{pd.Timestamp(day_date):%a %b %d} · {groomer}: "
                "no private home address is configured, so this day was optimized "
                "between clients only."
            )

        routable = [idx for idx in group.index if idx in coords]
        unroutable = [idx for idx in group.index if idx not in coords]

        if routable:
            (
                _distance_order,
                pair_metrics,
                start_metrics,
                end_metrics,
            ) = _best_route_order(
                routable,
                coords,
                maps_key,
                start_coord=home_coord,
                end_coord=home_coord,
            )
        else:
            pair_metrics = {}
            start_metrics = {}
            end_metrics = {}

        # The configured groomer time is ARRIVAL AT THE FIRST CLIENT.
        first_arrival_time = jen_start if groomer == "Jen" else haley_start

        durations = {}
        locked_times = {}
        for row_index in routable:
            duration = pd.to_numeric(
                optimized.at[row_index, "Minutes"],
                errors="coerce",
            )
            durations[row_index] = (
                int(duration) if pd.notna(duration) else 0
            )
            lock_value = optimized.at[row_index, "Locked Time"]
            lock_minutes = locked_time_minutes(lock_value)
            if lock_minutes is not None:
                locked_times[row_index] = lock_minutes

        if routable:
            if locked_times:
                (
                    best_order,
                    timed_schedule,
                    timed_score,
                ) = _best_timed_route_order(
                    routable,
                    pair_metrics,
                    start_metrics,
                    end_metrics,
                    durations,
                    locked_times,
                    first_arrival_time,
                    service_buffer_minutes=service_buffer_minutes,
                    has_home=home_coord is not None,
                )

                if best_order is None:
                    locked_names = [
                        str(optimized.at[idx, "Owner"] or "client")
                        for idx in routable
                        if idx in locked_times
                    ]
                    raise ValueError(
                        f"{groomer} on {pd.Timestamp(day_date):%A %b %d} "
                        "still cannot reach the locked appointment time(s) after "
                        "trying the available route orders: "
                        + ", ".join(locked_names)
                    )
            else:
                # No exact-time promises on this day: use the normal route
                # optimizer. A flaky/missing pairwise Routes response elsewhere
                # in the week must not block saving an exact time on another day.
                best_order = list(_distance_order)
                timed_schedule = {}
        else:
            best_order = []
            timed_schedule = {}

        # Keep any unroutable appointment after the routable stops instead of
        # silently dropping it. Exact-time locking requires a routable address.
        for idx in unroutable:
            if locked_time_minutes(optimized.at[idx, "Locked Time"]) is not None:
                raise ValueError(
                    f"{optimized.at[idx, 'Owner']} has an exact time but the "
                    "app could not find a usable saved address for that client."
                )

        final_order = list(best_order) + list(unroutable)
        current_time = first_arrival_time
        previous_idx = None

        # Clear old routing values for this groomer/day.
        for row_index in group.index:
            optimized.at[row_index, "Drive Home After Min"] = 0.0
            optimized.at[row_index, "Drive Home After Miles"] = 0.0
            optimized.at[row_index, "Leave Home Time"] = ""

        for position, row_index in enumerate(final_order, start=1):
            drive_miles = 0.0
            drive_minutes = 0.0

            if previous_idx is None and home_coord is not None:
                metric = start_metrics.get(row_index)
                if metric is not None:
                    drive_miles, drive_minutes = metric
            elif previous_idx is not None:
                metric = pair_metrics.get((previous_idx, row_index))
                if metric is not None:
                    drive_miles, drive_minutes = metric

            if row_index in timed_schedule:
                start_minutes = timed_schedule[row_index]["start"]
                end_minutes = timed_schedule[row_index]["end"]
                start_t = minutes_to_time(start_minutes)
                end_t = minutes_to_time(end_minutes)
            else:
                # Normal flexible timing. First stop begins at the configured
                # first-stop arrival time; later stops add actual drive time.
                if previous_idx is not None:
                    current_time = add_minutes_to_time(
                        current_time,
                        int(round(drive_minutes)),
                    )
                duration = pd.to_numeric(
                    optimized.at[row_index, "Minutes"],
                    errors="coerce",
                )
                duration = int(duration) if pd.notna(duration) else 0
                start_t = current_time
                end_t = add_minutes_to_time(
                    start_t,
                    duration + int(service_buffer_minutes),
                )

            # Work backward from the actual first appointment time to calculate
            # the required leave-home time.
            if position == 1 and home_coord is not None:
                optimized.at[row_index, "Leave Home Time"] = format_clock(
                    add_minutes_to_time(
                        start_t,
                        -int(round(drive_minutes)),
                    )
                )

            optimized.at[row_index, "Route Order"] = position
            optimized.at[row_index, "Drive From Previous Min"] = round(
                drive_minutes, 1
            )
            optimized.at[row_index, "Drive From Previous Miles"] = round(
                drive_miles, 1
            )
            optimized.at[row_index, "Routing Mode"] = (
                "Google Maps + Home"
                if row_index in coords and home_coord is not None
                else (
                    "Google Maps"
                    if row_index in coords
                    else "Address missing"
                )
            )
            optimized.at[row_index, "Start Time"] = format_clock(start_t)
            optimized.at[row_index, "End Time"] = format_clock(end_t)

            current_time = end_t
            previous_idx = row_index

        # Add the final client's return-home leg for totals/display.
        if best_order and home_coord is not None:
            last_idx = best_order[-1]
            return_metric = end_metrics.get(last_idx)
            if return_metric is not None:
                return_miles, return_minutes = return_metric
                optimized.at[last_idx, "Drive Home After Min"] = round(
                    return_minutes, 1
                )
                optimized.at[last_idx, "Drive Home After Miles"] = round(
                    return_miles, 1
                )

    return optimized, routing_notes


def google_maps_route_url(day_group, groomer=None):
    """
    Open the displayed route in Google Maps.

    When a private home address exists for the groomer, the Maps route starts
    and ends at home. The private address is read from Secrets and is never
    written into the schedule/database.
    """
    if day_group is None or day_group.empty:
        return ""

    group = day_group.copy()
    if "Route Order" in group.columns and pd.to_numeric(
        group["Route Order"], errors="coerce"
    ).notna().any():
        group["_Route Sort"] = pd.to_numeric(
            group["Route Order"], errors="coerce"
        ).fillna(999)
        group = group.sort_values(
            ["_Route Sort", "Start Time"],
            kind="stable",
        )
    else:
        group["_Start Sort"] = group.get(
            "Start Time",
            pd.Series([""] * len(group), index=group.index),
        ).apply(clock_sort_minutes)
        group = group.sort_values("_Start Sort", kind="stable")

    addresses = []
    for _, row in group.iterrows():
        address = household_address(
            row.get("Household ID"),
            owner=row.get("Owner"),
        )
        if address and address not in addresses:
            addresses.append(address)

    if not addresses:
        return ""

    if not groomer and "Groomer" in group.columns and not group.empty:
        groomer = str(group.iloc[0].get("Groomer", "") or "").strip()

    home_address = get_groomer_home_address(groomer)

    if home_address:
        params = {
            "api": "1",
            "origin": home_address,
            "destination": home_address,
            "waypoints": "|".join(addresses),
            "travelmode": "driving",
        }
    else:
        # Fallback: current phone location -> client route.
        destination = addresses[-1]
        waypoints = addresses[:-1]
        params = {
            "api": "1",
            "destination": destination,
            "travelmode": "driving",
        }
        if waypoints:
            params["waypoints"] = "|".join(waypoints)

    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(
        params,
        safe="|",
    )


# ---------- Data helpers ----------

def normalize_clients(df):
    df = df.copy()

    # Backward compatibility with v5-v7 files.
    # Old "Client" becomes Owner. Old "Pets" becomes Dog.
    aliases = {
        "Client": "Owner",
        "Pets": "Dog",
        "Duration Minutes": "Minutes",
        "Duration": "Minutes",
        "Appointment Minutes": "Minutes",
        "Frequency": "Frequency Weeks",
        "Last Groom Date": "Last Groom",
        "Client Name": "Owner",
        "Pet Names": "Dog",
        "Phone Number": "Phone",
        "Zip": "ZIP",
        "Zip Code": "ZIP",
    }

    for source, target in aliases.items():
        if source in df.columns and target not in df.columns:
            df[target] = df[source]

    for c in CLIENT_COLUMNS:
        if c not in df.columns:
            df[c] = None

    # Default Household ID to owner so multiple dogs belonging to the same
    # owner can be grouped into one appointment.
    df["Household ID"] = df["Household ID"].fillna(df["Owner"])
    df.loc[df["Household ID"].astype(str).str.strip() == "", "Household ID"] = df["Owner"]

    # Preserve the database primary key when rows came from Supabase.
    # Without this, "Save dog edits" cannot update existing rows and would
    # insert a new copy instead.
    keep_columns = CLIENT_COLUMNS.copy()
    if "Record ID" in df.columns:
        keep_columns.append("Record ID")

    df = df[keep_columns]

    for c in ["Last Groom", "Last Contacted", "Last Bath", "Last Groom Service"]:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")

    for c in [
        "Frequency Weeks",
        "Bath Frequency Weeks",
        "Groom Frequency Weeks",
        "Bath Price",
        "Bath Minutes",
        "Groom Price",
        "Groom Minutes",
        "Price",
        "Minutes",
        "Household Override Minutes",
        "Latitude",
        "Longitude",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    return df

def normalize_appointments(df):
    df = df.copy()

    for c in APPT_COLUMNS:
        if c not in df.columns:
            df[c] = None

    df = df[APPT_COLUMNS]
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    return df

def load_sample_clients():
    return normalize_clients(pd.read_csv("sample_clients.csv"))

def load_sample_appointments():
    return normalize_appointments(pd.read_csv("sample_appointments.csv"))

def calculate_due_fields(df, target_date):
    out = df.copy()

    out["Next Due"] = (
        out["Last Groom"]
        + pd.to_timedelta(out["Frequency Weeks"] * 7, unit="D")
    )

    out["Days Until Due"] = (
        out["Next Due"].dt.normalize()
        - pd.Timestamp(target_date)
    ).dt.days

    def due_status(days):
        if pd.isna(days):
            return "Unknown"
        if days < 0:
            return "🔴 Overdue"
        if days <= 7:
            return "🟠 Due this week"
        if days <= 14:
            return "🟡 Due next week"
        return "🟢 Not due yet"

    out["Status"] = out["Days Until Due"].apply(due_status)
    return out

def safe_date_string(value):
    if pd.isna(value):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d")

def clients_for_download(df):
    out = df[CLIENT_COLUMNS].copy()
    out["Last Groom"] = out["Last Groom"].apply(safe_date_string)
    out["Last Contacted"] = out["Last Contacted"].apply(safe_date_string)
    return out

def parse_time(value):
    if pd.isna(value):
        return None

    text = str(value).strip()
    for fmt in ("%I:%M %p", "%H:%M", "%I:%M%p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    return None

def client_full_address(row):
    pieces = [
        str(row.get("Address") or "").strip(),
        str(row.get("City") or "").strip(),
        str(row.get("State") or "").strip(),
        str(row.get("ZIP") or "").strip(),
    ]
    return ", ".join(
        [p for p in pieces if p and p.lower() not in {"nan", "none"}]
    )

def infer_from_question(question, groomers, areas):
    q = question.lower()
    inferred = {
        "groomer": None,
        "area": None,
        "minutes": None,
        "time_hint": None,
    }

    for g in groomers:
        if g.lower() in q:
            inferred["groomer"] = g
            break

    for a in areas:
        if a.lower() in q:
            inferred["area"] = a
            break

    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", q)

    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        ampm = time_match.group(3)

        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

        if 0 <= hour <= 23 and 0 <= minute <= 59:
            inferred["time_hint"] = time(hour, minute)

    minute_match = re.search(
        r"\b(\d{2,3})\s*(?:min|mins|minute|minutes)\b",
        q,
    )
    if minute_match:
        inferred["minutes"] = int(minute_match.group(1))

    if "hour and a half" in q or "90 minute" in q:
        inferred["minutes"] = 90
    elif "two hours" in q or "2 hours" in q:
        inferred["minutes"] = 120
    elif "one hour" in q or "1 hour" in q:
        inferred["minutes"] = 60

    return inferred

def nearest_booked_neighbors(day_appts, target_time):
    before = None
    after = None

    if day_appts.empty:
        return before, after

    timed = day_appts.copy()
    timed["Start Parsed"] = timed["Start Time"].apply(parse_time)
    timed["End Parsed"] = timed["End Time"].apply(parse_time)
    timed = timed.dropna(subset=["Start Parsed", "End Parsed"]).sort_values("Start Parsed")

    for _, row in timed.iterrows():
        if row["End Parsed"] <= target_time:
            before = row["Client"]
        elif row["Start Parsed"] >= target_time and after is None:
            after = row["Client"]

    return before, after

def coords_for(owner_name, clients):
    if not owner_name:
        return None

    if "Owner" not in clients.columns:
        return None

    match = clients[clients["Owner"] == owner_name]
    if match.empty:
        return None

    row = match.iloc[0]

    if pd.isna(row["Latitude"]) or pd.isna(row["Longitude"]):
        return None

    return float(row["Latitude"]), float(row["Longitude"])

def score_candidates(
    candidates,
    target_date,
    groomer,
    area,
    available_minutes,
    before_client,
    after_client,
    clients,
    maps_key,
):
    rows = []
    before_coords = coords_for(before_client, clients)
    after_coords = coords_for(after_client, clients)

    for _, row in candidates.iterrows():
        score = 0
        reasons = []
        days = row["Days Until Due"]

        if pd.notna(days):
            if days < 0:
                score += 60 + min(abs(int(days)), 30)
                reasons.append(f"{abs(int(days))} days overdue")
            elif days <= 7:
                score += 40
                reasons.append("due this week")
            elif days <= 14:
                score += 25
                reasons.append("due next week")
            elif days <= 21:
                score += 10

        assigned_groomer = str(row["Groomer"]).strip()
        if assigned_groomer == groomer:
            score += 30
            reasons.append(f"assigned to {groomer}")
        elif assigned_groomer.lower() == "either":
            score += 20
            reasons.append("either groomer")
        else:
            score -= 100

        if area != "Any":
            if str(row["Area"]).strip().lower() == area.strip().lower():
                score += 35
                reasons.append("same area")
            else:
                score -= 15
                reasons.append("different area")

        if pd.notna(row["Minutes"]) and row["Minutes"] <= available_minutes:
            score += 20
            fill_ratio = float(row["Minutes"]) / max(available_minutes, 1)
            score += int(fill_ratio * 10)
        else:
            score -= 100

        if pd.notna(row["Price"]):
            score += min(int(float(row["Price"]) / 15), 15)

        if pd.notna(row["Last Contacted"]):
            days_since_contact = (
                pd.Timestamp(target_date) - row["Last Contacted"]
            ).days
            if days_since_contact < 3:
                score -= 30
                reasons.append("contacted recently")

        drive_minutes = None
        drive_miles = None

        if (
            maps_key
            and not pd.isna(row["Latitude"])
            and not pd.isna(row["Longitude"])
        ):
            candidate_coords = (
                float(row["Latitude"]),
                float(row["Longitude"]),
            )
            legs = []

            try:
                if before_coords:
                    miles, mins = route_metrics(
                        before_coords,
                        candidate_coords,
                        maps_key,
                    )
                    if mins is not None:
                        legs.append((miles, mins))

                if after_coords:
                    miles, mins = route_metrics(
                        candidate_coords,
                        after_coords,
                        maps_key,
                    )
                    if mins is not None:
                        legs.append((miles, mins))
            except Exception:
                legs = []

            if legs:
                drive_miles = sum(
                    x[0] for x in legs if x[0] is not None
                )
                drive_minutes = sum(
                    x[1] for x in legs if x[1] is not None
                )

                if drive_minutes <= 20:
                    score += 25
                    reasons.append("short drive")
                elif drive_minutes <= 35:
                    score += 10
                elif drive_minutes > 60:
                    score -= 35
                    reasons.append("long drive")

        rows.append({
            **row.to_dict(),
            "Score": score,
            "Drive Miles": drive_miles,
            "Drive Minutes": drive_minutes,
            "Why": ", ".join(reasons[:4]),
        })

    result = pd.DataFrame(rows)

    if result.empty:
        return result

    return result.sort_values(
        ["Score", "Days Until Due", "Price"],
        ascending=[False, True, False],
    )




# ---------- Service helpers ----------

def _safe_weeks(value):
    try:
        if pd.isna(value):
            return None
        value = int(value)
        return value if value > 0 else None
    except Exception:
        return None


def _safe_date(value):
    try:
        if value is None or pd.isna(value):
            return None
        return pd.Timestamp(value)
    except Exception:
        return None


def service_due_dates(row):
    """
    Return calendar due dates for Bath and Groom independently.
    This means a dog can be bath every 4 weeks and groom every 8 weeks,
    instead of merely flipping back and forth after each appointment.
    """
    bath_weeks = _safe_weeks(row.get("Bath Frequency Weeks"))
    groom_weeks = _safe_weeks(row.get("Groom Frequency Weeks"))

    last_bath = _safe_date(row.get("Last Bath"))
    last_groom = _safe_date(row.get("Last Groom Service"))

    # Backward compatibility: existing Last Groom can seed groom history.
    if last_groom is None:
        last_groom = _safe_date(row.get("Last Groom"))

    bath_due = None
    groom_due = None

    if bath_weeks and last_bath is not None:
        bath_due = last_bath + pd.Timedelta(weeks=bath_weeks)

    if groom_weeks and last_groom is not None:
        groom_due = last_groom + pd.Timedelta(weeks=groom_weeks)

    return bath_due, groom_due


def effective_service_for_dog(row, target_date=None):
    """
    Choose the service that is next due by calendar schedule.
    If only one service has a cadence, use that service.
    """
    target = pd.Timestamp(target_date or date.today())
    bath_due, groom_due = service_due_dates(row)

    if bath_due is None and groom_due is None:
        # Backward compatibility with v14 manual Next Service behavior.
        next_service = str(row.get("Next Service", "") or "").strip()
        if next_service in {"Bath", "Groom"}:
            return next_service
        pattern = str(row.get("Service Pattern", "") or "").strip()
        if pattern == "Bath only":
            return "Bath"
        return "Groom"

    if bath_due is None:
        return "Groom"
    if groom_due is None:
        return "Bath"

    # Pick whichever service is due first.
    # If both are due the same day, Groom wins because it is the fuller service.
    if groom_due <= bath_due:
        return "Groom"
    return "Bath"


def next_service_due_date(row):
    service = effective_service_for_dog(row)
    bath_due, groom_due = service_due_dates(row)
    if service == "Bath":
        return bath_due
    return groom_due


def service_price_minutes(row, target_date=None):
    service = effective_service_for_dog(row, target_date=target_date)

    if service == "Bath":
        price = row.get("Bath Price")
        minutes = row.get("Bath Minutes")
    else:
        price = row.get("Groom Price")
        minutes = row.get("Groom Minutes")

    if pd.isna(price) or price is None:
        price = row.get("Price")
    if pd.isna(minutes) or minutes is None:
        minutes = row.get("Minutes")

    return service, price, minutes


def service_status_for_dog(row, target_date):
    due_date = next_service_due_date(row)
    if due_date is None:
        return None, None

    days_until = (pd.Timestamp(due_date).normalize() - pd.Timestamp(target_date).normalize()).days
    return due_date, days_until

# ---------- Household / dog-level helpers ----------

def household_due_table(dog_df, target_date):
    """Aggregate dog-level rows into one schedulable household appointment."""
    output_columns = [
        "Household ID",
        "Owner",
        "Dogs",
        "Area",
        "Groomer",
        "Days Until Due",
        "Status",
        "Price",
        "Minutes",
        "Last Contacted",
        "Latitude",
        "Longitude",
        "Address",
        "City",
        "State",
        "ZIP",
    ]

    due = calculate_due_fields(dog_df, target_date).copy()

    # A brand-new private database is valid and may have zero clients.
    # Return the expected schema so downstream tabs can render cleanly
    # instead of raising KeyError on columns such as "Groomer".
    if due.empty:
        return pd.DataFrame(columns=output_columns)

    rows = []
    for household_id, group in due.groupby("Household ID", dropna=False):
        group = group.copy()

        owner = str(group["Owner"].dropna().iloc[0]) if group["Owner"].notna().any() else ""
        dog_service_labels = []
        for _, dog_row in group.iterrows():
            dog_name = str(dog_row.get("Dog", "") or "").strip()
            service_name = effective_service_for_dog(dog_row, target_date=target_date)
            if dog_name:
                dog_service_labels.append(f"{dog_name} ({service_name})")
        dogs = ", ".join(dog_service_labels)
        area = str(group["Area"].dropna().iloc[0]) if group["Area"].notna().any() else ""
        groomer = str(group["Groomer"].dropna().iloc[0]) if group["Groomer"].notna().any() else ""

        # Household is due by the earliest upcoming service across its dogs.
        service_days = []
        for _, dog_row in group.iterrows():
            _, service_days_until = service_status_for_dog(dog_row, target_date)
            if service_days_until is not None:
                service_days.append(service_days_until)

        if service_days:
            days_until_due = min(service_days)
        else:
            # Fall back to the older single-frequency logic for legacy records.
            days_until_due = (
                group["Days Until Due"].min()
                if group["Days Until Due"].notna().any()
                else None
            )

        if pd.isna(days_until_due):
            status = "Unknown"
        elif days_until_due < 0:
            status = "🔴 Overdue"
        elif days_until_due <= 7:
            status = "🟠 Due this week"
        elif days_until_due <= 14:
            status = "🟡 Due next week"
        else:
            status = "🟢 Not due yet"

        service_values = group.apply(
            lambda row: pd.Series(
                service_price_minutes(row, target_date=target_date),
                index=["Effective Service", "Effective Price", "Effective Minutes"],
            ),
            axis=1,
        )

        override = group["Household Override Minutes"].dropna()
        if not override.empty and float(override.iloc[0]) > 0:
            total_minutes = float(override.iloc[0])
        else:
            total_minutes = pd.to_numeric(
                service_values["Effective Minutes"],
                errors="coerce",
            ).fillna(0).sum()

        total_price = pd.to_numeric(
            service_values["Effective Price"],
            errors="coerce",
        ).fillna(0).sum()

        last_contacted = group["Last Contacted"].max() if group["Last Contacted"].notna().any() else pd.NaT
        lat = group["Latitude"].dropna().iloc[0] if group["Latitude"].notna().any() else None
        lon = group["Longitude"].dropna().iloc[0] if group["Longitude"].notna().any() else None
        address = str(group["Address"].dropna().iloc[0]) if group["Address"].notna().any() else ""
        city = str(group["City"].dropna().iloc[0]) if group["City"].notna().any() else ""
        state = str(group["State"].dropna().iloc[0]) if group["State"].notna().any() else ""
        zip_code = str(group["ZIP"].dropna().iloc[0]) if group["ZIP"].notna().any() else ""

        rows.append({
            "Household ID": household_id,
            "Owner": owner,
            "Dogs": dogs,
            "Area": area,
            "Groomer": groomer,
            "Days Until Due": days_until_due,
            "Status": status,
            "Price": total_price,
            "Minutes": total_minutes,
            "Last Contacted": last_contacted,
            "Latitude": lat,
            "Longitude": lon,
            "Address": address,
            "City": city,
            "State": state,
            "ZIP": zip_code,
        })

    return pd.DataFrame(rows, columns=output_columns)

def household_member_detail(dog_df, household_id):
    group = dog_df[dog_df["Household ID"] == household_id].copy()
    return group[
        [
            "Owner",
            "Dog",
            "Service Pattern",
            "Next Service",
            "Bath Frequency Weeks",
            "Last Bath",
            "Groom Frequency Weeks",
            "Last Groom Service",
            "Bath Minutes",
            "Bath Price",
            "Groom Minutes",
            "Groom Price",
            "Frequency Weeks",
            "Last Groom",
        ]
    ]


# ---------- Weekly route builder ----------

def week_dates(start_date):
    monday = start_date - pd.Timedelta(days=start_date.weekday())
    return [monday + pd.Timedelta(days=i) for i in range(5)]


def add_minutes_to_time(start_time, minutes):
    base = datetime.combine(date.today(), start_time)
    return (base + pd.Timedelta(minutes=int(minutes))).time()

def format_clock(t):
    if t is None:
        return ""
    return datetime.combine(date.today(), t).strftime("%-I:%M %p")


def clock_sort_minutes(value):
    """Convert a displayed clock string like 8:30 AM to minutes after midnight."""
    parsed = parse_time(value)
    if parsed is None:
        return 24 * 60 + 1
    return parsed.hour * 60 + parsed.minute


def minutes_to_time(minutes_value):
    """Convert minutes after midnight to a time object."""
    total = int(round(float(minutes_value))) % (24 * 60)
    return time(total // 60, total % 60)


def locked_time_minutes(value):
    """Return locked appointment time as minutes after midnight, or None."""
    parsed = parse_time(value)
    if parsed is None:
        return None
    return parsed.hour * 60 + parsed.minute

def assign_times_to_weekly_plan(
    weekly_plan,
    jen_start,
    haley_start,
    travel_buffer_minutes=20,
    service_buffer_minutes=0,
):
    """Add estimated start/end times using saved groom duration plus buffers."""
    if weekly_plan.empty:
        return weekly_plan

    plan = weekly_plan.copy()
    plan["Start Time"] = ""
    plan["End Time"] = ""

    valid = plan[plan["Owner"].astype(str).str.strip() != ""].copy()

    for (day_date, groomer), group in valid.groupby(["Date", "Groomer"], sort=True):
        current = jen_start if groomer == "Jen" else haley_start

        # Earliest slots go to the most urgent clients first.
        # Score is already driven primarily by overdue / due status.
        sort_cols = [c for c in ["Score", "Owner"] if c in group.columns]
        if "Score" in sort_cols:
            group = group.sort_values(
                ["Score", "Owner"],
                ascending=[False, True],
                kind="stable",
            )
        else:
            group = group.sort_values(["Owner"], kind="stable")

        for idx in group.index:
            duration = plan.at[idx, "Minutes"]
            duration = int(duration) if pd.notna(duration) else 0

            start_t = current
            end_t = add_minutes_to_time(
                start_t,
                duration + int(service_buffer_minutes),
            )

            plan.at[idx, "Start Time"] = format_clock(start_t)
            plan.at[idx, "End Time"] = format_clock(end_t)

            current = add_minutes_to_time(
                end_t,
                int(travel_buffer_minutes),
            )

    return plan

def render_weekly_cards(valid_plan, daily_capacity, travel_buffer):
    """Compact whole-week-at-a-glance view designed for phones."""
    if valid_plan.empty:
        st.info("No scheduled appointments for this week.")
        return

    sorted_plan = valid_plan.copy()
    sorted_plan["_Start Sort"] = sorted_plan["Start Time"].apply(clock_sort_minutes)
    sorted_plan = sorted_plan.sort_values(
        ["Date", "Groomer", "_Start Sort", "Owner"],
        ascending=[True, True, True, True],
        kind="stable",
    )

    st.markdown("## Week at a glance")

    for (day_date, day_name), day_group in sorted_plan.groupby(
        ["Date", "Day"],
        sort=True,
    ):
        day_revenue = float(day_group["Price"].fillna(0).sum())
        day_minutes = int(day_group["Minutes"].fillna(0).sum())
        day_appts = len(day_group)

        st.markdown(
            f"### {day_name} · {pd.Timestamp(day_date):%b %d}"
            f"  \n{day_appts} appts · {day_minutes} groom min · ${day_revenue:,.0f}"
        )

        for groomer, groomer_group in day_group.groupby("Groomer", sort=False):
            groomer_group = groomer_group.sort_values(
                ["_Start Sort", "Owner"],
                ascending=[True, True],
                kind="stable",
            )
            area = ""
            if groomer_group["Area Cluster"].notna().any():
                area = str(groomer_group["Area Cluster"].dropna().iloc[0])

            st.markdown(
                f"**{groomer}" + (f" · {area}" if area else "") + "**"
            )

            for _, row in groomer_group.iterrows():
                owner = str(row.get("Owner", "") or "").strip()
                dogs = str(row.get("Dogs", "") or "").strip()
                status = str(row.get("Status", "") or "").strip()
                completion_status = str(
                    row.get("Completion Status", "") or ""
                ).strip()
                completed_date = str(
                    row.get("Completed Date", "") or ""
                ).strip()
                appointment_status = str(
                    row.get("Appointment Status", "") or ""
                ).strip()
                status_note = str(
                    row.get("Status Note", "") or ""
                ).strip()

                if completion_status == "Completed":
                    status = (
                        f"✅ Completed {completed_date}"
                        if completed_date
                        else "✅ Completed"
                    )
                elif appointment_status == "Cancelled":
                    status = "❌ Cancelled"
                    if status_note:
                        status += f" · {status_note}"
                elif appointment_status == "Rescheduled":
                    status = "↪️ Rescheduled"
                    if status_note:
                        status += f" · {status_note}"
                elif appointment_status == "Moved to another week":
                    moved_to = str(row.get("Rescheduled To", "") or "").strip()
                    status = (
                        f"↪️ Moved to {moved_to}"
                        if moved_to
                        else "↪️ Moved to another week"
                    )
                    if status_note:
                        status += f" · {status_note}"

                start_time = str(row.get("Start Time", "") or "").strip()
                end_time = str(row.get("End Time", "") or "").strip()
                locked_time = str(row.get("Locked Time", "") or "").strip()
                lock_badge = " 🔒" if locked_time else ""
                minutes = int(row.get("Minutes", 0) or 0)
                price = float(row.get("Price", 0) or 0)

                if not owner:
                    continue

                dog_text = f" / {dogs}" if dogs else ""

                drive_text = ""
                drive_minutes = pd.to_numeric(
                    row.get("Drive From Previous Min"),
                    errors="coerce",
                )
                drive_miles = pd.to_numeric(
                    row.get("Drive From Previous Miles"),
                    errors="coerce",
                )
                if (
                    str(row.get("Routing Mode", "") or "") in {
                        "Google Maps", "Google Maps + Home"
                    }
                    and pd.notna(drive_minutes)
                    and float(drive_minutes) > 0
                ):
                    drive_text = (
                        f" · 🚗 {float(drive_minutes):.0f} min"
                        + (
                            f" / {float(drive_miles):.1f} mi"
                            if pd.notna(drive_miles)
                            else ""
                        )
                    )

                st.markdown(
                    f"- **{start_time}–{end_time}{lock_badge}** · **{owner}{dog_text}** "
                    f"· {minutes} min · ${price:,.0f}{drive_text} · {status}"
                )

            used_minutes = int(groomer_group["Minutes"].fillna(0).sum())
            appt_count = len(groomer_group)

            has_real_routing = (
                "Routing Mode" in groomer_group.columns
                and groomer_group["Routing Mode"]
                .fillna("")
                .isin(["Google Maps", "Google Maps + Home"])
                .any()
            )

            if has_real_routing:
                outbound_and_between_min = float(
                    pd.to_numeric(
                        groomer_group.get(
                            "Drive From Previous Min",
                            pd.Series(dtype=float),
                        ),
                        errors="coerce",
                    ).fillna(0).sum()
                )
                return_home_min = float(
                    pd.to_numeric(
                        groomer_group.get(
                            "Drive Home After Min",
                            pd.Series(dtype=float),
                        ),
                        errors="coerce",
                    ).fillna(0).sum()
                )
                travel_total = int(round(
                    outbound_and_between_min + return_home_min
                ))

                outbound_and_between_miles = float(
                    pd.to_numeric(
                        groomer_group.get(
                            "Drive From Previous Miles",
                            pd.Series(dtype=float),
                        ),
                        errors="coerce",
                    ).fillna(0).sum()
                )
                return_home_miles = float(
                    pd.to_numeric(
                        groomer_group.get(
                            "Drive Home After Miles",
                            pd.Series(dtype=float),
                        ),
                        errors="coerce",
                    ).fillna(0).sum()
                )
                drive_miles = (
                    outbound_and_between_miles + return_home_miles
                )
                open_minutes = max(
                    int(daily_capacity) - used_minutes - travel_total,
                    0,
                )
                home_mode = (
                    "Routing Mode" in groomer_group.columns
                    and groomer_group["Routing Mode"]
                    .fillna("")
                    .eq("Google Maps + Home")
                    .any()
                )
                route_scope = (
                    "home → clients → home"
                    if home_mode
                    else "between client stops"
                )
                leave_home_values = (
                    groomer_group.get(
                        "Leave Home Time",
                        pd.Series([""] * len(groomer_group), index=groomer_group.index),
                    )
                    .fillna("")
                    .astype(str)
                )
                leave_home_values = [
                    value for value in leave_home_values.tolist() if value.strip()
                ]
                leave_home_text = (
                    f" · leave home {leave_home_values[0]}"
                    if leave_home_values
                    else ""
                )

                st.caption(
                    f"{groomer}: {travel_total} drive min · "
                    f"{drive_miles:.1f} mi · {route_scope}"
                    f"{leave_home_text} · "
                    f"{open_minutes} open min remaining"
                )
            else:
                travel_total = max(appt_count - 1, 0) * int(travel_buffer)
                open_minutes = max(
                    int(daily_capacity) - used_minutes - travel_total,
                    0,
                )
                st.caption(
                    f"{groomer}: {open_minutes} open min remaining "
                    f"(using {travel_buffer}-min estimated travel/setup buffers)"
                )

            maps_url = google_maps_route_url(groomer_group, groomer=groomer)
            if maps_url:
                st.link_button(
                    f"Open {groomer} route in Google Maps",
                    maps_url,
                    use_container_width=True,
                )

        st.divider()

    if get_maps_key():
        st.caption(
            "All scheduled workdays are shown together. After you optimize the "
            "week, drive times and route order use Google Maps."
        )
    else:
        st.caption(
            "All scheduled workdays are shown together. Travel/setup time is an "
            "estimate until Google Maps routing is connected."
        )


def schedule_score(row):
    score = 0
    days = row.get("Days Until Due")

    if pd.notna(days):
        if days < 0:
            score += 80 + min(abs(int(days)), 30)
        elif days <= 7:
            score += 50
        elif days <= 14:
            score += 30
        elif days <= 21:
            score += 15
        else:
            score -= 20

    price = row.get("Price")
    if pd.notna(price):
        score += min(int(float(price) / 20), 12)

    return score

def choose_area_for_day(pool):
    if pool.empty:
        return None

    area_summary = (
        pool.groupby("Area", dropna=False)
        .agg(
            Clients=("Owner", "count"),
            Overdue=("Days Until Due", lambda s: int((s < 0).sum())),
            DueSoon=("Days Until Due", lambda s: int((s <= 7).sum())),
            Revenue=("Price", "sum"),
        )
        .reset_index()
    )

    area_summary["Area Score"] = (
        area_summary["Overdue"] * 100
        + area_summary["DueSoon"] * 40
        + area_summary["Clients"] * 15
        + area_summary["Revenue"].fillna(0) / 10
    )

    area_summary = area_summary.sort_values(
        ["Area Score", "Overdue", "DueSoon", "Revenue"],
        ascending=[False, False, False, False],
    )

    return area_summary.iloc[0]["Area"]

def build_week_plan(
    clients_df,
    week_start,
    daily_capacity_minutes=420,
    max_appointments_per_day=4,
    week_overrides=None,
    exclude_household_ids=None,
):
    output_columns = [
        "Household ID",
        "Date",
        "Day",
        "Groomer",
        "Area Cluster",
        "Owner",
        "Dogs",
        "Status",
        "Minutes",
        "Price",
        "Score",
    ]

    # A newly connected private database can legitimately contain zero clients.
    # Return a correctly shaped empty plan instead of trying to filter missing data.
    if clients_df is None or clients_df.empty:
        return pd.DataFrame(columns=output_columns)

    due = household_due_table(clients_df, week_start)

    if due.empty or "Days Until Due" not in due.columns:
        return pd.DataFrame(columns=output_columns)

    due = due[due["Days Until Due"].notna()].copy()

    if due.empty:
        return pd.DataFrame(columns=output_columns)

    days = week_dates(pd.Timestamp(week_start))
    scheduled_households = set()
    results = []
    week_overrides = week_overrides or {}

    # Normal automatic scheduling stays strict: only overdue / due-this-week.
    # A user can still explicitly force a client into this week without changing
    # that client's normal recurring cadence.
    forced_households = {
        str(hid)
        for hid, override in week_overrides.items()
        if bool(override.get("force_include", False))
    }

    exclude_household_ids = {
        str(hid)
        for hid in (exclude_household_ids or set())
        if str(hid).strip()
    }

    # If a household is already on a recently saved earlier week, do not
    # automatically schedule it again. A manual force-include still wins,
    # which supports intentional reschedules.
    eligible_due = due[
        (~due["Household ID"].astype(str).isin(exclude_household_ids))
        | (due["Household ID"].astype(str).isin(forced_households))
    ].copy()

    pool = eligible_due[
        (eligible_due["Days Until Due"] <= 6)
        | (eligible_due["Household ID"].astype(str).isin(forced_households))
    ].copy()
    pool["Weekly Score"] = pool.apply(schedule_score, axis=1)

    def requested_day_for(row):
        household_id = str(row.get("Household ID", "") or "")
        override = week_overrides.get(household_id, {})
        return str(override.get("requested_day", "") or "")

    def response_status_for(row):
        household_id = str(row.get("Household ID", "") or "")
        override = week_overrides.get(household_id, {})
        return str(override.get("response_status", "") or "")

    def requested_groomer_for(row):
        household_id = str(row.get("Household ID", "") or "")
        override = week_overrides.get(household_id, {})
        return str(override.get("requested_groomer", "") or "")

    for day_ts in days:
        day_date = day_ts.date()
        day_name = day_ts.strftime("%A")

        for groomer, allowed_days in WORKDAYS.items():
            if day_name not in allowed_days:
                continue

            groomer_pool = pool[
                (~pool["Household ID"].astype(str).isin(scheduled_households))
                & (pool["Minutes"].notna())
            ].copy()

            if not groomer_pool.empty:
                groomer_pool["_Requested Day"] = groomer_pool.apply(
                    requested_day_for, axis=1
                )
                groomer_pool["_Response Status"] = groomer_pool.apply(
                    response_status_for, axis=1
                )
                groomer_pool["_Requested Groomer"] = groomer_pool.apply(
                    requested_groomer_for, axis=1
                )

                # Honor a one-week groomer choice when supplied. Otherwise use
                # the client's normal Jen / Haley / Either rule.
                normal_match = (
                    (groomer_pool["Groomer"] == groomer)
                    | (groomer_pool["Groomer"].astype(str).str.strip().str.lower() == "either")
                )
                forced_match = groomer_pool["_Requested Groomer"] == groomer
                no_forced_groomer = groomer_pool["_Requested Groomer"] == ""
                groomer_pool = groomer_pool[
                    forced_match | (no_forced_groomer & normal_match)
                ].copy()

                # A one-week client reply overrides the ideal draft without
                # changing the client's recurring schedule.
                groomer_pool = groomer_pool[
                    (groomer_pool["_Response Status"] != "Skip this week")
                    & (
                        (groomer_pool["_Requested Day"] == "")
                        | (groomer_pool["_Requested Day"] == day_name)
                    )
                ].copy()

            if groomer_pool.empty:
                results.append({
                    "Household ID": "",
                    "Date": day_date,
                    "Day": day_name,
                    "Groomer": groomer,
                    "Area Cluster": "",
                    "Owner": "",
                    "Dogs": "",
                    "Status": "",
                    "Minutes": 0,
                    "Price": 0,
                    "Score": 0,
                })
                continue

            # If a client specifically requested this day, anchor the day's
            # route area around that exception first. Otherwise choose normally.
            forced_today = groomer_pool[
                groomer_pool["_Requested Day"] == day_name
            ].copy()

            if not forced_today.empty:
                preferred_area = choose_area_for_day(forced_today)
            else:
                preferred_area = choose_area_for_day(groomer_pool)

            same_area_pool = groomer_pool[
                groomer_pool["Area"].astype(str).str.strip().str.lower()
                == str(preferred_area).strip().lower()
            ].copy()

            # This-week day moves are hard constraints. Keep every client who was
            # explicitly moved to this day even when their saved Area label differs
            # from the day's primary cluster. Then fill remaining capacity from the
            # primary area. This avoids dropping nearby clients just because two
            # neighborhoods have different text labels.
            forced_today = groomer_pool[
                groomer_pool["_Requested Day"] == day_name
            ].copy()

            forced_ids = set(
                forced_today["Household ID"].astype(str).tolist()
            )
            same_area_fill = same_area_pool[
                ~same_area_pool["Household ID"].astype(str).isin(forced_ids)
            ].copy()

            area_pool = pd.concat(
                [forced_today, same_area_fill],
                ignore_index=False,
            ).drop_duplicates(subset=["Household ID"], keep="first")

            # Book urgent clients first, but explicit this-week moves stay first.
            area_pool["_Urgency Tier"] = area_pool["Days Until Due"].apply(
                lambda d: (
                    0 if pd.notna(d) and d < 0
                    else 1 if pd.notna(d) and d <= 7
                    else 2 if pd.notna(d) and d <= 14
                    else 3
                )
            )

            area_pool["_Forced Today"] = (
                area_pool["_Requested Day"] == day_name
            ).astype(int)

            area_pool = area_pool.sort_values(
                ["_Forced Today", "_Urgency Tier", "Days Until Due", "Weekly Score", "Price"],
                ascending=[False, True, True, False, False],
                kind="stable",
            )

            used = 0
            selected = []

            for _, row in area_pool.iterrows():
                mins = int(row["Minutes"]) if pd.notna(row["Minutes"]) else 0
                if mins <= 0:
                    continue
                if len(selected) >= max_appointments_per_day:
                    break
                if used + mins > daily_capacity_minutes:
                    continue

                selected.append(row)
                used += mins
                scheduled_households.add(str(row["Household ID"]))

            if not selected:
                results.append({
                    "Household ID": "",
                    "Date": day_date,
                    "Day": day_name,
                    "Groomer": groomer,
                    "Area Cluster": preferred_area or "",
                    "Owner": "",
                    "Dogs": "",
                    "Status": "",
                    "Minutes": 0,
                    "Price": 0,
                    "Score": 0,
                })
            else:
                for row in selected:
                    results.append({
                        "Household ID": row["Household ID"],
                        "Date": day_date,
                        "Day": day_name,
                        "Groomer": groomer,
                        "Area Cluster": (
                            "Mixed · this-week moves"
                            if len({
                                str(x).strip().lower()
                                for x in forced_today["Area"].dropna().tolist()
                                if str(x).strip()
                            }) > 1
                            else (preferred_area or row["Area"])
                        ),
                        "Owner": row["Owner"],
                        "Dogs": row["Dogs"],
                        "Status": row["Status"],
                        "Minutes": int(row["Minutes"]) if pd.notna(row["Minutes"]) else 0,
                        "Price": float(row["Price"]) if pd.notna(row["Price"]) else 0,
                        "Score": int(row["Weekly Score"]),
                    })

    return pd.DataFrame(results, columns=output_columns)





# ---------- Address helper ----------

def parse_full_address(full_address):
    """
    Accept a pasted US-style address such as:
    123 Main St, Conroe, TX 77301

    Returns street, city, state, zip. If the format is unusual,
    the full text is preserved in Street Address so nothing is lost.
    """
    raw = str(full_address or "").strip()
    if not raw:
        return "", "", "TX", ""

    parts = [p.strip() for p in raw.split(",") if p.strip()]

    # Most common format: street, city, ST ZIP
    if len(parts) >= 3:
        street = ", ".join(parts[:-2]).strip()
        city = parts[-2].strip()
        state_zip = parts[-1].strip()

        match = re.match(r"^([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)$", state_zip)
        if match:
            return street, city, match.group(1).upper(), match.group(2)

        # If state/ZIP isn't perfectly formatted, keep the original safely.
        return raw, "", "TX", ""

    return raw, "", "TX", ""


# ---------- Household ID helpers ----------

def get_or_create_household_id(owner_name, clients_df):
    """
    Reuse an existing household ID for the same owner.
    Otherwise create a short, readable unique ID automatically.
    """
    owner = str(owner_name or "").strip()
    if not owner:
        return ""

    if clients_df is not None and not clients_df.empty:
        same_owner = clients_df[
            clients_df["Owner"].astype(str).str.strip().str.casefold()
            == owner.casefold()
        ]

        if not same_owner.empty:
            existing = (
                same_owner["Household ID"]
                .dropna()
                .astype(str)
                .str.strip()
            )
            existing = existing[existing != ""]
            if not existing.empty:
                return existing.iloc[0]

        used_ids = set(
            clients_df["Household ID"]
            .dropna()
            .astype(str)
            .str.strip()
            .str.upper()
            .tolist()
        )
    else:
        used_ids = set()

    base = re.sub(r"[^A-Za-z0-9]+", "", owner.upper())[:12] or "HOUSE"
    number = 1
    candidate = f"{base}{number:02d}"

    while candidate in used_ids:
        number += 1
        candidate = f"{base}{number:02d}"

    return candidate


# ---------- Persistent private database (Supabase) ----------

DB_DOG_COLUMNS = [
    "id",
    "owner",
    "dog",
    "household_id",
    "phone",
    "area",
    "groomer",
    "last_groom",
    "frequency_weeks",
    "price",
    "minutes",
    "household_override_minutes",
    "address",
    "city",
    "state",
    "zip",
    "latitude",
    "longitude",
    "last_contacted",
    "notes",
]

DB_APPT_COLUMNS = [
    "id",
    "date",
    "start_time",
    "end_time",
    "client",
    "area",
    "groomer",
]

APP_TO_DB_DOG = {
    "Owner": "owner",
    "Dog": "dog",
    "Household ID": "household_id",
    "Phone": "phone",
    "Area": "area",
    "Groomer": "groomer",
    "Last Groom": "last_groom",
    "Frequency Weeks": "frequency_weeks",
    "Service Pattern": "service_pattern",
    "Next Service": "next_service",
    "Bath Frequency Weeks": "bath_frequency_weeks",
    "Last Bath": "last_bath",
    "Groom Frequency Weeks": "groom_frequency_weeks",
    "Last Groom Service": "last_groom_service",
    "Bath Price": "bath_price",
    "Bath Minutes": "bath_minutes",
    "Groom Price": "groom_price",
    "Groom Minutes": "groom_minutes",
    "Price": "price",
    "Minutes": "minutes",
    "Household Override Minutes": "household_override_minutes",
    "Address": "address",
    "City": "city",
    "State": "state",
    "ZIP": "zip",
    "Latitude": "latitude",
    "Longitude": "longitude",
    "Last Contacted": "last_contacted",
    "Notes": "notes",
}

DB_TO_APP_DOG = {v: k for k, v in APP_TO_DB_DOG.items()}

def db_secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.getenv(name, default)


def supabase_project_ref_from_url():
    url = str(db_secret("SUPABASE_URL", "") or "").strip().rstrip("/")
    match = re.match(r"^https://([a-z0-9-]+)\.supabase\.co$", url, re.I)
    return match.group(1) if match else ""

def legacy_key_project_ref():
    """
    Safely decode only the middle JWT payload locally to read the project ref.
    Never logs or displays the key itself.
    """
    import base64
    import json

    key = str(db_secret("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()
    if not key.startswith("eyJ"):
        return ""

    try:
        parts = key.split(".")
        if len(parts) != 3:
            return ""
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        return str(data.get("ref", "") or "")
    except Exception:
        return ""

def supabase_connection_test():
    """
    Actually test the configured credentials. Returns (ok, message).
    No secrets are returned or logged.
    """
    if not supabase_configured():
        return False, "Supabase credentials are missing."

    url_ref = supabase_project_ref_from_url()
    key_ref = legacy_key_project_ref()

    if url_ref and key_ref and url_ref != key_ref:
        return False, (
            f"Project mismatch: URL is for {url_ref}, but the legacy service_role "
            f"key is for {key_ref}."
        )

    try:
        sb = get_supabase()
        sb.table("dogs").select("id").limit(1).execute()
        return True, "Private database connected and verified."
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "Invalid API key" in msg:
            return False, (
                "Supabase rejected the API key (401). Re-copy the URL and service_role "
                "key from the SAME Supabase project."
            )
        return False, f"Database test failed: {msg}"

def supabase_configured():
    return bool(
        db_secret("SUPABASE_URL", "")
        and db_secret("SUPABASE_SERVICE_ROLE_KEY", "")
        and create_client is not None
    )

@st.cache_resource
def get_supabase():
    if not supabase_configured():
        return None
    return create_client(
        db_secret("SUPABASE_URL"),
        db_secret("SUPABASE_SERVICE_ROLE_KEY"),
    )


def load_week_overrides_db(week_key):
    """Load saved one-week scheduling overrides from Supabase."""
    if not supabase_configured():
        return {}

    try:
        rows = (
            get_supabase()
            .table("weekly_overrides")
            .select("household_id,response_status,requested_day,requested_groomer,force_include")
            .eq("week_start", str(week_key))
            .execute()
            .data
            or []
        )
        return {
            str(r.get("household_id")): {
                "response_status": r.get("response_status") or "Not contacted",
                "requested_day": r.get("requested_day") or "",
                "requested_groomer": r.get("requested_groomer") or "",
                "force_include": bool(r.get("force_include", False)),
            }
            for r in rows
            if r.get("household_id")
        }
    except Exception:
        # If the migration has not been run yet, keep session-only behavior.
        return {}


def save_week_override_db(week_key, household_id, override):
    """Persist one household's this-week override."""
    if not supabase_configured():
        return

    payload = {
        "week_start": str(week_key),
        "household_id": str(household_id),
        "response_status": override.get("response_status") or "Not contacted",
        "requested_day": override.get("requested_day") or None,
        "requested_groomer": override.get("requested_groomer") or None,
        "force_include": bool(override.get("force_include", False)),
    }
    (
        get_supabase()
        .table("weekly_overrides")
        .upsert(payload, on_conflict="week_start,household_id")
        .execute()
    )


def clear_week_overrides_db(week_key):
    """Delete only the temporary overrides for one planning week."""
    if not supabase_configured():
        return
    get_supabase().table("weekly_overrides").delete().eq(
        "week_start", str(week_key)
    ).execute()


def _json_safe_value(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _plan_to_json_records(plan):
    if plan is None or plan.empty:
        return []
    records = []
    for _, row in plan.iterrows():
        records.append({
            str(col): _json_safe_value(row.get(col))
            for col in plan.columns
            if not str(col).startswith("_")
        })
    return records


def _settings_to_json(settings):
    return {
        str(key): _json_safe_value(value)
        for key, value in (settings or {}).items()
    }


def save_week_draft_db(week_key, plan, settings, client_fingerprint=""):
    """Persist the exact visible weekly draft so deploys/tab changes cannot rebuild it."""
    if not supabase_configured():
        return

    payload = {
        "week_start": str(week_key),
        "plan_json": _plan_to_json_records(plan),
        "settings_json": _settings_to_json(settings),
        "client_fingerprint": str(client_fingerprint or ""),
        "updated_at": datetime.utcnow().isoformat(),
    }

    (
        get_supabase()
        .table("weekly_drafts")
        .upsert(payload, on_conflict="week_start")
        .execute()
    )


def load_week_draft_db(week_key):
    """Load the exact last-saved weekly draft and its builder settings."""
    if not supabase_configured():
        return None

    try:
        rows = (
            get_supabase()
            .table("weekly_drafts")
            .select("plan_json,settings_json,client_fingerprint,status,confirmed_at")
            .eq("week_start", str(week_key))
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            return None

        saved = rows[0]
        plan_records = saved.get("plan_json") or []
        plan = pd.DataFrame(plan_records)

        if not plan.empty and "Date" in plan.columns:
            parsed_dates = pd.to_datetime(plan["Date"], errors="coerce")
            plan["Date"] = parsed_dates.dt.date

        for col in ["Minutes", "Price", "Score"]:
            if not plan.empty and col in plan.columns:
                plan[col] = pd.to_numeric(plan[col], errors="coerce").fillna(0)

        settings = saved.get("settings_json") or {}
        for key in ["jen_start", "haley_start"]:
            if key in settings and isinstance(settings[key], str):
                parsed = parse_time(settings[key])
                if parsed is not None:
                    settings[key] = parsed

        return {
            "plan": plan,
            "settings": settings,
            "fingerprint": saved.get("client_fingerprint") or "",
            "status": saved.get("status") or "draft",
            "confirmed_at": saved.get("confirmed_at"),
        }
    except Exception:
        return None


def delete_week_draft_db(week_key):
    if not supabase_configured():
        return
    get_supabase().table("weekly_drafts").delete().eq(
        "week_start", str(week_key)
    ).execute()


def set_week_draft_status_db(week_key, status):
    """Mark a saved weekly draft as draft or confirmed."""
    if not supabase_configured():
        return

    payload = {
        "status": str(status),
        "confirmed_at": (
            datetime.utcnow().isoformat()
            if str(status) == "confirmed"
            else None
        ),
        "updated_at": datetime.utcnow().isoformat(),
    }

    (
        get_supabase()
        .table("weekly_drafts")
        .update(payload)
        .eq("week_start", str(week_key))
        .execute()
    )



def load_month_unconfirmed_drafts_db(month_start, month_end):
    """Return saved draft weeks overlapping the selected month."""
    if not supabase_configured():
        return []

    try:
        month_start = pd.Timestamp(month_start).normalize()
        month_end = pd.Timestamp(month_end).normalize()

        query_start = (
            month_start - pd.Timedelta(days=month_start.weekday())
        ).date().isoformat()
        query_end = month_end.date().isoformat()

        return (
            get_supabase()
            .table("weekly_drafts")
            .select("week_start,plan_json,status")
            .gte("week_start", query_start)
            .lte("week_start", query_end)
            .eq("status", "draft")
            .order("week_start")
            .execute()
            .data
            or []
        )
    except Exception:
        return []


def load_month_drafts_db(month_start, month_end):
    """Load all saved weekly drafts that can contain dates inside a month."""
    if not supabase_configured():
        return []

    try:
        month_start = pd.Timestamp(month_start).normalize()
        month_end = pd.Timestamp(month_end).normalize()

        # Include the Monday that may start before the first day of the month.
        query_start = (
            month_start - pd.Timedelta(days=month_start.weekday())
        ).date().isoformat()
        query_end = month_end.date().isoformat()

        return (
            get_supabase()
            .table("weekly_drafts")
            .select("week_start,plan_json,status,confirmed_at")
            .gte("week_start", query_start)
            .lte("week_start", query_end)
            .eq("status", "confirmed")
            .order("week_start")
            .execute()
            .data
            or []
        )
    except Exception:
        return []


def month_plan_dataframe(month_start, month_end):
    """
    Flatten saved weekly drafts for the calendar weeks that overlap a month.

    Important: the monthly page must mirror Weekly Route Builder exactly.
    So if September's last workweek runs into Oct 1-2, those Thu/Fri appointments
    are shown with that week rather than appearing as falsely open.
    """
    month_start = pd.Timestamp(month_start).normalize()
    month_end = pd.Timestamp(month_end).normalize()

    display_start = (
        month_start - pd.Timedelta(days=month_start.weekday())
    ).normalize()
    last_week_monday = (
        month_end - pd.Timedelta(days=month_end.weekday())
    ).normalize()
    display_end = (last_week_monday + pd.Timedelta(days=4)).normalize()

    # Store whole weeks by week-start key. DB gives persistence; current session
    # replaces the DB copy for that same week so the monthly view immediately
    # matches the weekly view after a move/addition.
    week_records = {}

    for saved in load_month_drafts_db(month_start, month_end):
        week_key = str(saved.get("week_start", "") or "")
        if not week_key:
            continue
        week_records[week_key] = list(saved.get("plan_json") or [])

    for week_key, plan in st.session_state.get("week_route_plans", {}).items():
        if plan is None or plan.empty:
            continue

        # Session plans must obey the same confirmed-only rule as database rows.
        if st.session_state.get("week_plan_statuses", {}).get(week_key) != "confirmed":
            continue

        week_ts = pd.to_datetime(week_key, errors="coerce")
        if pd.isna(week_ts):
            continue
        week_ts = week_ts.normalize()

        if week_ts < display_start or week_ts > last_week_monday:
            continue

        week_records[str(week_key)] = [
            row.to_dict()
            for _, row in plan.iterrows()
        ]

    records = []

    for week_key, items in week_records.items():
        for item in items:
            owner = str(item.get("Owner", "") or "").strip()
            household_id = str(item.get("Household ID", "") or "").strip()
            raw_date = item.get("Date")

            if not owner or not household_id or raw_date is None:
                continue

            appt_date = pd.to_datetime(raw_date, errors="coerce")
            if pd.isna(appt_date):
                continue
            appt_date = appt_date.normalize()

            if not (display_start <= appt_date <= display_end):
                continue

            row = dict(item)
            row["Date"] = appt_date.date()
            row["Week Start"] = week_key
            records.append(row)

    if not records:
        return pd.DataFrame(
            columns=[
                "Household ID", "Date", "Day", "Groomer", "Area Cluster",
                "Owner", "Dogs", "Status", "Minutes", "Price", "Start Time",
                "End Time", "Week Start",
            ]
        )

    df = pd.DataFrame(records)

    # Exact duplicate protection only; legitimate multi-dog households are already
    # represented as one household row in the weekly plan.
    dedupe_cols = [
        c for c in ["Week Start", "Household ID", "Date", "Groomer"]
        if c in df.columns
    ]
    if dedupe_cols:
        df = df.drop_duplicates(subset=dedupe_cols, keep="last")

    for col in ["Minutes", "Price"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    return df


def _service_value(row, service, kind):
    """Return price/minutes for a projected Bath or Groom service."""
    if service == "Bath":
        value = row.get("Bath Price") if kind == "price" else row.get("Bath Minutes")
    else:
        value = row.get("Groom Price") if kind == "price" else row.get("Groom Minutes")

    if value is None or pd.isna(value):
        value = row.get("Price") if kind == "price" else row.get("Minutes")

    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _advance_due_into_window(last_date, weeks, window_start):
    """Find the first recurring due date on or after window_start."""
    last_date = _safe_date(last_date)
    weeks = _safe_weeks(weeks)

    if last_date is None or not weeks:
        return None

    due = pd.Timestamp(last_date).normalize() + pd.Timedelta(weeks=weeks)
    step = pd.Timedelta(weeks=weeks)
    window_start = pd.Timestamp(window_start).normalize()

    for _ in range(600):
        if due >= window_start:
            return due
        due += step

    return None



def confirmed_service_history(up_to_date, lookback_days=1095):
    """
    Return the latest confirmed Bath/Groom appointment date by
    (household_id, dog_name, service).

    Uses BOTH:
    - confirmed rows persisted in Supabase
    - confirmed weekly plans in the current Streamlit session

    This guarantees future projections use the exact same confirmed schedule
    the Monthly Planner is showing.
    """
    history = {}
    up_to = pd.Timestamp(up_to_date).normalize()
    query_start_ts = up_to - pd.Timedelta(days=lookback_days)

    saved_weeks = []

    if supabase_configured():
        try:
            db_rows = (
                get_supabase()
                .table("weekly_drafts")
                .select("week_start,plan_json,status")
                .gte("week_start", query_start_ts.date().isoformat())
                .lte("week_start", up_to.date().isoformat())
                .eq("status", "confirmed")
                .execute()
                .data
                or []
            )
            saved_weeks.extend(db_rows)
        except Exception:
            pass

    # Merge confirmed plans currently in session. These are allowed to replace
    # the same DB week because they are what the user is actually seeing now.
    session_confirmed = {}
    statuses = st.session_state.get("week_plan_statuses", {})
    for week_key, plan in st.session_state.get("week_route_plans", {}).items():
        if statuses.get(week_key) != "confirmed":
            continue
        if plan is None or plan.empty:
            continue

        week_ts = pd.to_datetime(week_key, errors="coerce")
        if pd.isna(week_ts):
            continue
        week_ts = week_ts.normalize()
        if week_ts < query_start_ts or week_ts > up_to:
            continue

        session_confirmed[str(week_key)] = {
            "week_start": str(week_key),
            "status": "confirmed",
            "plan_json": [row.to_dict() for _, row in plan.iterrows()],
        }

    # Keep one copy per week, preferring the current session version.
    by_week = {
        str(saved.get("week_start", "")): saved
        for saved in saved_weeks
        if str(saved.get("week_start", "")).strip()
    }
    by_week.update(session_confirmed)

    label_re = re.compile(r"^\s*(.*?)\s*\((Bath|Groom)\)\s*$", re.IGNORECASE)

    def apply_item(item):
        hid = str(item.get("Household ID", "") or "").strip()
        appointment_status = str(
            item.get("Appointment Status", "") or ""
        ).strip()
        if appointment_status in {"Cancelled", "Moved to another week"}:
            return

        completion_status = str(
            item.get("Completion Status", "") or ""
        ).strip()
        raw_date = (
            item.get("Completed Date")
            if completion_status == "Completed"
            else item.get("Date")
        )
        dogs_text = str(item.get("Dogs", "") or "").strip()

        if not hid or not raw_date or not dogs_text:
            return

        appt_date = pd.to_datetime(raw_date, errors="coerce")
        if pd.isna(appt_date):
            return
        appt_date = appt_date.normalize()

        for label in [part.strip() for part in dogs_text.split(",") if part.strip()]:
            match = label_re.match(label)
            if not match:
                continue

            dog_name = match.group(1).strip()
            service = match.group(2).title()
            key = (hid, dog_name.casefold(), service)

            prior = history.get(key)
            if prior is None or appt_date > prior:
                history[key] = appt_date

            # Full groom includes a bath.
            if service == "Groom":
                bath_key = (hid, dog_name.casefold(), "Bath")
                prior_bath = history.get(bath_key)
                if prior_bath is None or appt_date > prior_bath:
                    history[bath_key] = appt_date

    for saved in by_week.values():
        for item in saved.get("plan_json") or []:
            apply_item(item)

    return history


def projected_month_services(dog_df, month_start, month_end, service_history=None):
    """
    Project recurring Bath/Groom work into a future month.
    This is planning information only and does not create appointments.
    """
    columns = [
        "Household ID", "Owner", "Dogs", "Projected Due", "Area", "Groomer",
        "Minutes", "Price",
    ]

    if dog_df is None or dog_df.empty:
        return pd.DataFrame(columns=columns)

    month_start = pd.Timestamp(month_start).normalize()
    month_end = pd.Timestamp(month_end).normalize()
    service_history = service_history or {}
    raw = []

    for _, row in dog_df.iterrows():
        hid = str(row.get("Household ID", "") or "").strip()
        owner = str(row.get("Owner", "") or "").strip()
        dog = str(row.get("Dog", "") or "").strip()
        if not hid or not owner:
            continue

        service_specs = []
        bath_weeks = _safe_weeks(row.get("Bath Frequency Weeks"))
        groom_weeks = _safe_weeks(row.get("Groom Frequency Weeks"))

        if bath_weeks:
            last_bath = row.get("Last Bath")
            confirmed_bath = service_history.get(
                (hid, dog.casefold(), "Bath")
            )
            if confirmed_bath is not None:
                saved_bath = _safe_date(last_bath)
                if saved_bath is None or confirmed_bath > pd.Timestamp(saved_bath):
                    last_bath = confirmed_bath
            service_specs.append(("Bath", last_bath, bath_weeks))

        if groom_weeks:
            last_groom = row.get("Last Groom Service")
            if last_groom is None or pd.isna(last_groom):
                last_groom = row.get("Last Groom")

            confirmed_groom = service_history.get(
                (hid, dog.casefold(), "Groom")
            )
            if confirmed_groom is not None:
                saved_groom = _safe_date(last_groom)
                if saved_groom is None or confirmed_groom > pd.Timestamp(saved_groom):
                    last_groom = confirmed_groom

            service_specs.append(("Groom", last_groom, groom_weeks))

        # Legacy clients without separate Bath/Groom cadence.
        if not service_specs:
            legacy_weeks = _safe_weeks(row.get("Frequency Weeks"))
            legacy_last = row.get("Last Groom")
            legacy_service = effective_service_for_dog(
                row,
                target_date=month_start,
            )

            confirmed_legacy = service_history.get(
                (hid, dog.casefold(), legacy_service)
            )
            if confirmed_legacy is not None:
                saved_legacy = _safe_date(legacy_last)
                if saved_legacy is None or confirmed_legacy > pd.Timestamp(saved_legacy):
                    legacy_last = confirmed_legacy

            if legacy_weeks and _safe_date(legacy_last) is not None:
                service_specs.append(
                    (legacy_service, legacy_last, legacy_weeks)
                )

        for service, last_date, weeks in service_specs:
            due = _advance_due_into_window(last_date, weeks, month_start)
            if due is None:
                continue

            step = pd.Timedelta(weeks=weeks)

            for _ in range(20):
                if due > month_end:
                    break

                raw.append({
                    "Household ID": hid,
                    "Owner": owner,
                    "Dog": dog,
                    "Service": service,
                    "Projected Due": due.date(),
                    "Area": str(row.get("Area", "") or ""),
                    "Groomer": str(row.get("Groomer", "") or ""),
                    "Minutes": _service_value(row, service, "minutes"),
                    "Price": _service_value(row, service, "price"),
                })
                due += step

    if not raw:
        return pd.DataFrame(columns=columns)

    raw_df = pd.DataFrame(raw)

    # If Bath and Groom land on the same day for one dog, the full groom
    # replaces the bath so the visit is not double-counted.
    raw_df["_Service Priority"] = raw_df["Service"].map(
        {"Bath": 1, "Groom": 2}
    ).fillna(0)

    raw_df = raw_df.sort_values(
        ["Household ID", "Dog", "Projected Due", "_Service Priority"],
        ascending=[True, True, True, False],
        kind="stable",
    ).drop_duplicates(
        subset=["Household ID", "Dog", "Projected Due"],
        keep="first",
    )

    rows = []
    for (hid, due_date), group in raw_df.groupby(
        ["Household ID", "Projected Due"],
        sort=True,
    ):
        first = group.iloc[0]
        dog_labels = []

        for _, r in group.iterrows():
            dog_name = str(r.get("Dog", "") or "").strip()
            service = str(r.get("Service", "") or "").strip()
            if dog_name:
                dog_labels.append(f"{dog_name} ({service})")

        rows.append({
            "Household ID": hid,
            "Owner": first.get("Owner", ""),
            "Dogs": ", ".join(dog_labels),
            "Projected Due": due_date,
            "Area": first.get("Area", ""),
            "Groomer": first.get("Groomer", ""),
            "Minutes": int(
                pd.to_numeric(group["Minutes"], errors="coerce")
                .fillna(0)
                .sum()
            ),
            "Price": float(
                pd.to_numeric(group["Price"], errors="coerce")
                .fillna(0)
                .sum()
            ),
        })

    return pd.DataFrame(rows, columns=columns)


def load_households_scheduled_before_week_db(week_key, lookback_weeks=4):
    """
    Return household IDs that are already placed in a saved weekly draft
    shortly before the selected week.

    This prevents a client scheduled this week from being automatically
    suggested again when the user starts planning next week.
    """
    if not supabase_configured():
        return set()

    try:
        selected_monday = pd.Timestamp(week_key).normalize()
        lookback_start = (
            selected_monday - pd.Timedelta(weeks=int(lookback_weeks))
        ).date().isoformat()
        prior_day = (
            selected_monday - pd.Timedelta(days=1)
        ).date().isoformat()

        rows = (
            get_supabase()
            .table("weekly_drafts")
            .select("week_start,plan_json")
            .gte("week_start", lookback_start)
            .lte("week_start", prior_day)
            .execute()
            .data
            or []
        )

        scheduled = set()

        for saved_week in rows:
            for item in saved_week.get("plan_json") or []:
                household_id = str(item.get("Household ID", "") or "").strip()
                owner = str(item.get("Owner", "") or "").strip()
                appointment_status = str(
                    item.get("Appointment Status", "") or ""
                ).strip()

                # Cancelled appointments should not block the client from being
                # scheduled on a later week.
                if appointment_status in {"Cancelled", "Moved to another week"}:
                    continue

                # Blank placeholder rows are not real appointments.
                if household_id and owner:
                    scheduled.add(household_id)

        return scheduled
    except Exception:
        return set()

def clean_scalar(value):
    if value is None:
        return None

    # Streamlit's data editor can return blank cells as empty strings.
    # PostgreSQL date/numeric columns need NULL instead of "".
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        return value

    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()

    if isinstance(value, datetime):
        return value.date().isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass

    return value


def clean_db_field(db_col, value):
    value = clean_scalar(value)

    if value is None:
        return None

    if db_col in {"last_groom", "last_contacted", "last_bath", "last_groom_service"}:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date().isoformat()

    if db_col in {
        "frequency_weeks",
        "bath_frequency_weeks",
        "groom_frequency_weeks",
        "bath_price",
        "bath_minutes",
        "groom_price",
        "groom_minutes",
        "price",
        "minutes",
        "household_override_minutes",
        "latitude",
        "longitude",
    }:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return value


def dog_row_to_db(row):
    payload = {}
    for app_col, db_col in APP_TO_DB_DOG.items():
        payload[db_col] = clean_db_field(db_col, row.get(app_col))
    return payload


def parse_dog_service_labels(dogs_text):
    """
    Parse weekly-plan labels such as:
      Lulu (Bath)
      Dood 1 (Groom), Dood 2 (Groom)
    Returns [(dog_name, service), ...].
    """
    matches = re.findall(
        r"(?:^|,\s*)(.*?)\s*\((Bath|Groom)\)",
        str(dogs_text or ""),
        flags=re.IGNORECASE,
    )
    return [
        (name.strip(), service.title())
        for name, service in matches
        if name.strip()
    ]


def mark_household_services_completed(household_id, dogs_text, completed_date):
    """
    Update the private dog records after a real appointment is completed.

    Bath:
      - updates Last Bath

    Groom:
      - updates Last Groom Service
      - updates legacy Last Groom
      - updates Last Bath too, because a full groom includes a bath
    """
    household_id = str(household_id or "").strip()
    completed_ts = pd.Timestamp(completed_date).normalize()
    completed_iso = completed_ts.date().isoformat()
    services = parse_dog_service_labels(dogs_text)

    if not household_id or not services:
        return 0, []

    updated = st.session_state.clients.copy()
    changed = 0
    missing = []

    for dog_name, service in services:
        mask = (
            updated["Household ID"].astype(str).str.strip().eq(household_id)
            & updated["Dog"].astype(str).str.strip().str.casefold().eq(
                dog_name.casefold()
            )
        )
        matching = updated[mask]

        if matching.empty:
            missing.append(dog_name)
            continue

        for row_index, row in matching.iterrows():
            if service == "Bath":
                updated.at[row_index, "Last Bath"] = completed_ts
                db_updates = {
                    "last_bath": completed_iso,
                }
            else:
                updated.at[row_index, "Last Groom Service"] = completed_ts
                updated.at[row_index, "Last Groom"] = completed_ts
                updated.at[row_index, "Last Bath"] = completed_ts
                db_updates = {
                    "last_groom_service": completed_iso,
                    "last_groom": completed_iso,
                    "last_bath": completed_iso,
                }

            if supabase_configured():
                record_id = row.get("Record ID")
                if record_id and str(record_id).strip().lower() != "nan":
                    (
                        get_supabase()
                        .table("dogs")
                        .update(db_updates)
                        .eq("id", str(record_id))
                        .execute()
                    )

            changed += 1

    st.session_state.clients = normalize_clients(updated)
    return changed, missing


def mark_week_plan_row_completed(
    week_key,
    plan,
    row_index,
    completed_date,
    current_settings,
):
    """
    Mark one weekly appointment completed and persist that status inside
    weekly_drafts.plan_json. This does not move or rebuild the route.
    """
    updated_plan = plan.copy()

    if "Completion Status" not in updated_plan.columns:
        updated_plan["Completion Status"] = ""
    if "Completed Date" not in updated_plan.columns:
        updated_plan["Completed Date"] = ""

    updated_plan.at[row_index, "Completion Status"] = "Completed"
    updated_plan.at[row_index, "Completed Date"] = (
        pd.Timestamp(completed_date).date().isoformat()
    )

    st.session_state.week_route_plans[week_key] = updated_plan

    if supabase_configured():
        save_week_draft_db(
            week_key,
            updated_plan,
            current_settings,
            st.session_state.week_plan_fingerprints.get(week_key, ""),
        )

    return updated_plan



def update_week_appointment_status(
    week_key,
    plan,
    row_index,
    action,
    current_settings,
    note="",
    new_day=None,
    new_groomer=None,
):
    """
    Update one weekly appointment without changing the client's normal cadence.

    Supported actions:
      - Cancelled
      - Rescheduled (within the same selected week)
      - Scheduled (restore)
    """
    updated_plan = plan.copy()

    for col, default in [
        ("Appointment Status", "Scheduled"),
        ("Status Note", ""),
    ]:
        if col not in updated_plan.columns:
            updated_plan[col] = default

    action = str(action or "Scheduled")
    updated_plan.at[row_index, "Appointment Status"] = action
    updated_plan.at[row_index, "Status Note"] = str(note or "").strip()

    if action == "Rescheduled":
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        if new_day not in day_names:
            raise ValueError("Choose a Monday-Friday reschedule day.")

        monday = pd.Timestamp(week_key).normalize()
        target_date = monday + pd.Timedelta(days=day_names.index(new_day))

        updated_plan.at[row_index, "Date"] = target_date.date()
        updated_plan.at[row_index, "Day"] = new_day

        if new_groomer:
            updated_plan.at[row_index, "Groomer"] = new_groomer

        updated_plan.at[row_index, "Area Cluster"] = "Rescheduled this week"

    if action == "Scheduled":
        updated_plan.at[row_index, "Status Note"] = ""

    # Recalculate times so a cancellation or within-week move gives an honest
    # view of the remaining day.
    active_for_times = updated_plan[
        updated_plan["Appointment Status"].fillna("Scheduled") != "Cancelled"
    ].copy()

    active_for_times = assign_times_to_weekly_plan(
        active_for_times,
        jen_start=current_settings["jen_start"],
        haley_start=current_settings["haley_start"],
        travel_buffer_minutes=current_settings["travel_buffer"],
        service_buffer_minutes=current_settings["service_buffer"],
    )

    # Copy recalculated times back to active rows while retaining cancelled rows.
    for active_index, active_row in active_for_times.iterrows():
        for col in ["Start Time", "End Time"]:
            if col in active_for_times.columns:
                updated_plan.at[active_index, col] = active_row.get(col, "")

    if action == "Cancelled":
        updated_plan.at[row_index, "Start Time"] = ""
        updated_plan.at[row_index, "End Time"] = ""

    st.session_state.week_route_plans[week_key] = updated_plan
    st.session_state.week_plan_statuses[week_key] = "draft"

    if supabase_configured():
        save_week_draft_db(
            week_key,
            updated_plan,
            current_settings,
            st.session_state.week_plan_fingerprints.get(week_key, ""),
        )
        set_week_draft_status_db(week_key, "draft")

    return updated_plan



def reschedule_appointment_to_date(
    source_week_key,
    source_plan,
    row_index,
    target_date,
    target_groomer,
    source_settings,
    note="",
):
    """
    Move one appointment to a specific date, including a future week.

    The source week keeps a visible "Moved to another week" history row.
    The destination week receives the real scheduled appointment and is saved
    as a Draft until the user reviews and confirms it.
    """
    target_ts = pd.Timestamp(target_date).normalize()
    target_day = target_ts.strftime("%A")

    if target_day not in {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}:
        raise ValueError("Choose a Monday-Friday date.")

    if target_day not in WORKDAYS.get(target_groomer, []):
        raise ValueError(f"{target_groomer} does not normally work on {target_day}.")

    source_monday = pd.Timestamp(source_week_key).normalize()
    target_monday = target_ts - pd.Timedelta(days=target_ts.weekday())
    target_week_key = target_monday.date().isoformat()

    # Same-week move can use the existing reschedule function.
    if target_week_key == str(source_week_key):
        return {
            "same_week": True,
            "source_plan": update_week_appointment_status(
                source_week_key,
                source_plan,
                row_index,
                "Rescheduled",
                source_settings,
                note=note,
                new_day=target_day,
                new_groomer=target_groomer,
            ),
            "target_week_key": target_week_key,
        }

    source_updated = source_plan.copy()
    for col, default in [
        ("Appointment Status", "Scheduled"),
        ("Status Note", ""),
        ("Rescheduled To", ""),
    ]:
        if col not in source_updated.columns:
            source_updated[col] = default

    moving_row = source_updated.loc[row_index].copy()

    source_updated.at[row_index, "Appointment Status"] = "Moved to another week"
    source_updated.at[row_index, "Status Note"] = str(note or "").strip()
    source_updated.at[row_index, "Rescheduled To"] = target_ts.date().isoformat()
    source_updated.at[row_index, "Start Time"] = ""
    source_updated.at[row_index, "End Time"] = ""

    # Re-time only appointments that remain active in the source week.
    source_active = source_updated[
        ~source_updated["Appointment Status"]
        .fillna("Scheduled")
        .isin(["Cancelled", "Moved to another week"])
    ].copy()

    source_active = assign_times_to_weekly_plan(
        source_active,
        jen_start=source_settings["jen_start"],
        haley_start=source_settings["haley_start"],
        travel_buffer_minutes=source_settings["travel_buffer"],
        service_buffer_minutes=source_settings["service_buffer"],
    )

    for active_index, active_row in source_active.iterrows():
        for col in ["Start Time", "End Time"]:
            if col in source_active.columns:
                source_updated.at[active_index, col] = active_row.get(col, "")

    st.session_state.week_route_plans[source_week_key] = source_updated
    st.session_state.week_plan_statuses[source_week_key] = "draft"

    if supabase_configured():
        save_week_draft_db(
            source_week_key,
            source_updated,
            source_settings,
            st.session_state.week_plan_fingerprints.get(source_week_key, ""),
        )
        set_week_draft_status_db(source_week_key, "draft")

    # Load destination week if it already exists.
    destination_plan = st.session_state.week_route_plans.get(target_week_key)
    destination_settings = st.session_state.week_builder_settings.get(target_week_key)

    if destination_plan is None:
        persisted = load_week_draft_db(target_week_key)
        if persisted is not None:
            destination_plan = persisted["plan"]
            destination_settings = persisted.get("settings") or None
            st.session_state.week_plan_fingerprints[target_week_key] = (
                persisted.get("fingerprint") or ""
            )
        else:
            destination_plan = pd.DataFrame(columns=source_updated.columns)

    if destination_settings is None:
        destination_settings = {
            "daily_capacity": source_settings.get("daily_capacity", 420),
            "max_appointments": source_settings.get("max_appointments", 4),
            "jen_start": source_settings.get("jen_start", time(8, 0)),
            "haley_start": source_settings.get("haley_start", time(8, 0)),
            "travel_buffer": source_settings.get("travel_buffer", 20),
            "service_buffer": source_settings.get("service_buffer", 0),
        }

    st.session_state.week_builder_settings[target_week_key] = destination_settings

    # Prepare the destination appointment.
    moved = moving_row.to_dict()
    moved["Date"] = target_ts.date()
    moved["Day"] = target_day
    moved["Groomer"] = target_groomer
    moved["Appointment Status"] = "Scheduled"
    moved["Status Note"] = (
        f"Rescheduled from {pd.Timestamp(moving_row.get('Date')):%b %d}"
        + (f" · {note}" if str(note or "").strip() else "")
    )
    moved["Rescheduled To"] = ""
    moved["Completion Status"] = ""
    moved["Completed Date"] = ""
    moved["Start Time"] = ""
    moved["End Time"] = ""

    if destination_plan is None or destination_plan.empty:
        destination_plan = pd.DataFrame([moved])
    else:
        destination_plan = destination_plan.copy()
        for col in moved.keys():
            if col not in destination_plan.columns:
                destination_plan[col] = ""

        hid = str(moved.get("Household ID", "") or "")
        existing_mask = destination_plan["Household ID"].astype(str).eq(hid)

        if existing_mask.any():
            existing_index = destination_plan[existing_mask].index[0]
            for col, value in moved.items():
                destination_plan.at[existing_index, col] = value
        else:
            destination_plan = pd.concat(
                [destination_plan, pd.DataFrame([moved])],
                ignore_index=True,
            )

    # Re-time destination week.
    destination_active = destination_plan[
        ~destination_plan.get(
            "Appointment Status",
            pd.Series(["Scheduled"] * len(destination_plan), index=destination_plan.index),
        )
        .fillna("Scheduled")
        .isin(["Cancelled", "Moved to another week"])
    ].copy()

    destination_active = assign_times_to_weekly_plan(
        destination_active,
        jen_start=destination_settings["jen_start"],
        haley_start=destination_settings["haley_start"],
        travel_buffer_minutes=destination_settings["travel_buffer"],
        service_buffer_minutes=destination_settings["service_buffer"],
    )

    for active_index, active_row in destination_active.iterrows():
        for col in ["Start Time", "End Time"]:
            if col in destination_active.columns:
                destination_plan.at[active_index, col] = active_row.get(col, "")

    st.session_state.week_route_plans[target_week_key] = destination_plan
    st.session_state.week_plan_statuses[target_week_key] = "draft"
    st.session_state.week_plan_fingerprints[target_week_key] = schedule_data_fingerprint(
        st.session_state.clients
    )

    if supabase_configured():
        save_week_draft_db(
            target_week_key,
            destination_plan,
            destination_settings,
            st.session_state.week_plan_fingerprints[target_week_key],
        )
        set_week_draft_status_db(target_week_key, "draft")

    return {
        "same_week": False,
        "source_plan": source_updated,
        "destination_plan": destination_plan,
        "target_week_key": target_week_key,
    }


def dogs_db_to_app(rows):
    if not rows:
        return normalize_clients(pd.DataFrame(columns=CLIENT_COLUMNS))

    df = pd.DataFrame(rows)

    for db_col, app_col in DB_TO_APP_DOG.items():
        if db_col in df.columns:
            df[app_col] = df[db_col]

    if "id" in df.columns:
        df["Record ID"] = df["id"]

    return normalize_clients(df)

def load_dogs_from_db():
    sb = get_supabase()
    if sb is None:
        return None

    response = (
        sb.table("dogs")
        .select("*")
        .order("owner")
        .order("dog")
        .execute()
    )
    return dogs_db_to_app(response.data or [])

def insert_dog_db(row_dict):
    sb = get_supabase()
    payload = dog_row_to_db(row_dict)
    return sb.table("dogs").insert(payload).execute()

def upsert_dogs_db(df):
    sb = get_supabase()
    payloads = []

    for _, row in df.iterrows():
        # Streamlit's dynamic editor can include a blank "new row".
        # Never send that placeholder row to Supabase.
        owner = str(row.get("Owner", "") or "").strip()
        dog = str(row.get("Dog", "") or "").strip()

        if not owner and not dog:
            continue

        # Owner and dog are required for a real record.
        if not owner or not dog:
            continue

        payload = dog_row_to_db(row)
        record_id = row.get("Record ID")

        if record_id and str(record_id).strip() and str(record_id).lower() != "nan":
            payload["id"] = str(record_id)

        payloads.append(payload)

    if payloads:
        sb.table("dogs").upsert(payloads).execute()

def delete_dog_ids_db(ids):
    sb = get_supabase()
    for record_id in ids:
        sb.table("dogs").delete().eq("id", str(record_id)).execute()

def remove_exact_duplicate_dogs_db():
    """
    Keep the oldest copy of truly identical dog records and remove only
    exact duplicates. Database IDs/timestamps are ignored when comparing.
    """
    sb = get_supabase()
    response = (
        sb.table("dogs")
        .select("*")
        .order("created_at")
        .execute()
    )
    rows = response.data or []

    compare_fields = list(APP_TO_DB_DOG.values())
    seen = set()
    duplicate_ids = []

    def canonical(value):
        if value is None:
            return None
        if isinstance(value, str):
            return value.strip()
        return value

    for row in rows:
        signature = tuple(canonical(row.get(field)) for field in compare_fields)

        if signature in seen:
            if row.get("id"):
                duplicate_ids.append(str(row["id"]))
        else:
            seen.add(signature)

    delete_dog_ids_db(duplicate_ids)
    return len(duplicate_ids)

def appointments_db_to_app(rows):
    if not rows:
        return normalize_appointments(pd.DataFrame(columns=APPT_COLUMNS))

    df = pd.DataFrame(rows)
    rename = {
        "date": "Date",
        "start_time": "Start Time",
        "end_time": "End Time",
        "client": "Client",
        "area": "Area",
        "groomer": "Groomer",
    }
    df = df.rename(columns=rename)
    return normalize_appointments(df)

def load_appointments_from_db():
    sb = get_supabase()
    if sb is None:
        return None

    response = (
        sb.table("appointments")
        .select("*")
        .order("date")
        .order("start_time")
        .execute()
    )
    return appointments_db_to_app(response.data or [])

def import_dogs_dataframe_to_db(df):
    sb = get_supabase()
    normalized = normalize_clients(df)
    payloads = [dog_row_to_db(row) for _, row in normalized.iterrows()]
    if payloads:
        sb.table("dogs").insert(payloads).execute()
    return len(payloads)

def import_appointments_dataframe_to_db(df):
    sb = get_supabase()
    normalized = normalize_appointments(df)
    payloads = []
    for _, row in normalized.iterrows():
        payloads.append({
            "date": clean_scalar(row.get("Date")),
            "start_time": row.get("Start Time"),
            "end_time": row.get("End Time"),
            "client": row.get("Client"),
            "area": row.get("Area"),
            "groomer": row.get("Groomer"),
        })
    if payloads:
        sb.table("appointments").insert(payloads).execute()
    return len(payloads)


# ---------- Session data ----------

if "clients" not in st.session_state:
    if supabase_configured():
        try:
            loaded = load_dogs_from_db()
            st.session_state.clients = loaded if loaded is not None else load_sample_clients()
        except Exception as exc:
            st.session_state.clients = load_sample_clients()
            st.session_state.db_load_error = str(exc)
    else:
        st.session_state.clients = load_sample_clients()

if "appointments" not in st.session_state:
    if supabase_configured():
        try:
            loaded_appts = load_appointments_from_db()
            st.session_state.appointments = (
                loaded_appts if loaded_appts is not None else load_sample_appointments()
            )
        except Exception as exc:
            st.session_state.appointments = load_sample_appointments()
            st.session_state.db_appt_error = str(exc)
    else:
        st.session_state.appointments = load_sample_appointments()

# ---------- Sidebar: private data ----------

st.sidebar.header("Private data")

if supabase_configured():
    db_ok, db_message = supabase_connection_test()
    if db_ok:
        st.sidebar.success(db_message)
    else:
        st.sidebar.error(db_message)

        url_ref = supabase_project_ref_from_url()
        key_ref = legacy_key_project_ref()
        if url_ref:
            st.sidebar.caption(f"URL project ref: {url_ref}")
        if key_ref:
            st.sidebar.caption(f"Key project ref: {key_ref}")

    if st.sidebar.button("Refresh database"):
        try:
            get_supabase.clear()
            db_ok, db_message = supabase_connection_test()
            if not db_ok:
                st.sidebar.error(db_message)
            else:
                st.session_state.clients = load_dogs_from_db()
                st.session_state.appointments = load_appointments_from_db()
                st.rerun()
        except Exception as exc:
            st.sidebar.error(f"Database refresh failed: {exc}")
else:
    st.sidebar.warning("Database not connected — session/sample mode")


client_upload = st.sidebar.file_uploader(
    "Load your private client CSV",
    type=["csv"],
    key="private_client_upload",
    help="The file is loaded at runtime and is not stored in your public GitHub repository.",
)

if client_upload is not None:
    uploaded_df = normalize_clients(pd.read_csv(client_upload))

    if supabase_configured():
        if st.sidebar.button("Import client file to private database"):
            try:
                count = import_dogs_dataframe_to_db(uploaded_df)
                st.session_state.clients = load_dogs_from_db()
                st.sidebar.success(f"Imported {count} dog records.")
                st.rerun()
            except Exception as exc:
                st.sidebar.error(f"Import failed: {exc}")
    else:
        if st.sidebar.button("Use this client file"):
            st.session_state.clients = uploaded_df
            st.sidebar.success("Private client file loaded for this session.")

appt_upload = st.sidebar.file_uploader(
    "Load appointments CSV",
    type=["csv"],
    key="private_appt_upload",
)

if appt_upload is not None:
    uploaded_appts = normalize_appointments(pd.read_csv(appt_upload))

    if supabase_configured():
        if st.sidebar.button("Import appointments to private database"):
            try:
                count = import_appointments_dataframe_to_db(uploaded_appts)
                st.session_state.appointments = load_appointments_from_db()
                st.sidebar.success(f"Imported {count} appointments.")
                st.rerun()
            except Exception as exc:
                st.sidebar.error(f"Appointment import failed: {exc}")
    else:
        if st.sidebar.button("Use these appointments"):
            st.session_state.appointments = uploaded_appts
            st.sidebar.success("Appointments loaded.")

if not supabase_configured():
    if st.sidebar.button("Reset to sample data"):
        st.session_state.clients = load_sample_clients()
        st.session_state.appointments = load_sample_appointments()
        st.rerun()

maps_key = get_maps_key()

if maps_key:
    st.sidebar.success("Google Maps connected — real routing available")

    configured_homes = [
        groomer
        for groomer in ["Jen", "Haley"]
        if get_groomer_home_address(groomer)
    ]
    if len(configured_homes) == 2:
        st.sidebar.success("Jen + Haley home routing configured")
    elif configured_homes:
        st.sidebar.caption(
            f"Home routing configured for {configured_homes[0]} only."
        )
    else:
        st.sidebar.caption(
            "Home routing not configured yet; routes start between clients."
        )
else:
    st.sidebar.caption("Google Maps not connected yet.")

clients = st.session_state.clients
appointments = st.session_state.appointments


def get_week_plan_for_day(day_value):
    """
    Load the saved weekly plan that contains day_value.
    Prefer the current session copy, otherwise restore the persisted Supabase copy.
    """
    # Today is the first tab, so these weekly-session containers must exist
    # before the later Weekly Route Builder section initializes them.
    if "week_client_responses" not in st.session_state:
        st.session_state.week_client_responses = {}
    if "week_route_plans" not in st.session_state:
        st.session_state.week_route_plans = {}
    if "week_builder_settings" not in st.session_state:
        st.session_state.week_builder_settings = {}
    if "week_plan_fingerprints" not in st.session_state:
        st.session_state.week_plan_fingerprints = {}
    if "week_plan_statuses" not in st.session_state:
        st.session_state.week_plan_statuses = {}

    day_ts = pd.Timestamp(day_value).normalize()
    monday = day_ts - pd.Timedelta(days=day_ts.weekday())
    week_key = monday.date().isoformat()

    default_settings = {
        "daily_capacity": 420,
        "max_appointments": 4,
        "jen_start": time(8, 0),
        "haley_start": time(8, 0),
        "travel_buffer": 20,
        "service_buffer": 0,
    }

    plan = st.session_state.get("week_route_plans", {}).get(week_key)
    settings = st.session_state.get("week_builder_settings", {}).get(week_key)
    status = st.session_state.get("week_plan_statuses", {}).get(week_key)

    if plan is None:
        saved = load_week_draft_db(week_key)
        if saved is not None:
            plan = saved["plan"]
            settings = saved.get("settings") or default_settings.copy()
            status = saved.get("status", "draft")

            st.session_state.week_route_plans[week_key] = plan
            st.session_state.week_builder_settings[week_key] = settings
            st.session_state.week_plan_fingerprints[week_key] = saved.get(
                "fingerprint", ""
            )
            st.session_state.week_plan_statuses[week_key] = status

    if settings is None:
        settings = default_settings.copy()
        st.session_state.week_builder_settings[week_key] = settings

    if plan is None:
        plan = pd.DataFrame()

    return week_key, plan.copy(), settings, status or "draft"


# ---------- Main tabs ----------

today_tab, planner_tab, monthly_tab, weekly_tab, clients_tab, due_tab, export_tab = st.tabs(
    [
        "☀️ Today",
        "📅 Planner",
        "🗓️ Monthly Planner",
        "📆 Weekly Route Builder",
        "👥 Client Manager",
        "⏰ Due List",
        "🔐 Private Data",
    ]
)


# ---------- Today ----------

with today_tab:
    st.markdown("## Today")
    st.caption(
        "Your mobile workday view. See the route, then mark appointments completed, "
        "cancel them, or move them without opening the full weekly builder."
    )

    today_view_date = st.date_input(
        "Day",
        value=date.today(),
        key="today_view_date",
    )

    today_week_key, today_week_plan, today_settings, today_week_status = (
        get_week_plan_for_day(today_view_date)
    )

    if today_week_plan.empty:
        st.info(
            "There is no saved weekly schedule containing this day yet."
        )
        if st.button(
            "Open this week in Weekly Route Builder",
            key=f"today_open_empty_week_{today_week_key}",
        ):
            st.session_state["week_builder_date"] = pd.Timestamp(
                today_week_key
            ).date()
            st.success(
                f"Weekly Route Builder is set to the week of "
                f"{pd.Timestamp(today_week_key):%b %d}. Tap that tab."
            )
    else:
        today_rows = today_week_plan.copy()
        today_dates = pd.to_datetime(
            today_rows.get("Date"),
            errors="coerce",
        )
        today_rows = today_rows[
            today_dates.dt.date == pd.Timestamp(today_view_date).date()
        ].copy()

        if "Appointment Status" not in today_rows.columns:
            today_rows["Appointment Status"] = "Scheduled"
        today_rows["Appointment Status"] = (
            today_rows["Appointment Status"]
            .fillna("")
            .replace("", "Scheduled")
        )

        active_today = today_rows[
            ~today_rows["Appointment Status"].isin(
                ["Cancelled", "Moved to another week"]
            )
        ].copy()

        t1, t2, t3 = st.columns(3)
        t1.metric("Appointments", len(active_today))
        t2.metric(
            "Revenue",
            f"${float(active_today.get('Price', pd.Series(dtype=float)).sum()):,.0f}",
        )
        t3.metric(
            "Groom min",
            int(active_today.get("Minutes", pd.Series(dtype=float)).sum()),
        )

        if today_week_status == "confirmed":
            st.success("This week is CONFIRMED.")
        else:
            st.warning(
                "This week is still a DRAFT. Daily actions will save, but review and "
                "confirm the week when the schedule is final."
            )

        if today_rows.empty:
            st.info("No appointments are scheduled for this day.")
        else:
            today_rows["_Start Sort"] = today_rows.get(
                "Start Time",
                pd.Series([""] * len(today_rows), index=today_rows.index),
            ).apply(clock_sort_minutes)

            today_rows = today_rows.sort_values(
                ["Groomer", "_Start Sort", "Owner"],
                kind="stable",
            )

            for groomer, groomer_rows in today_rows.groupby(
                "Groomer",
                sort=False,
            ):
                st.markdown(f"### {groomer}")

                for row_index, row in groomer_rows.iterrows():
                    owner = str(row.get("Owner", "") or "").strip()
                    dogs = str(row.get("Dogs", "") or "").strip()
                    start = str(row.get("Start Time", "") or "").strip()
                    end = str(row.get("End Time", "") or "").strip()
                    area = str(row.get("Area Cluster", "") or "").strip()
                    price = float(row.get("Price", 0) or 0)
                    minutes = int(row.get("Minutes", 0) or 0)
                    appointment_status = str(
                        row.get("Appointment Status", "") or "Scheduled"
                    ).strip()
                    completion_status = str(
                        row.get("Completion Status", "") or ""
                    ).strip()
                    completed_date = str(
                        row.get("Completed Date", "") or ""
                    ).strip()

                    if completion_status == "Completed":
                        status_label = "✅ Completed"
                        if completed_date:
                            status_label += f" {completed_date}"
                    elif appointment_status == "Cancelled":
                        status_label = "❌ Cancelled"
                    elif appointment_status == "Moved to another week":
                        moved_to = str(row.get("Rescheduled To", "") or "").strip()
                        status_label = (
                            f"↪️ Moved to {moved_to}"
                            if moved_to
                            else "↪️ Moved"
                        )
                    elif appointment_status == "Rescheduled":
                        status_label = "↪️ Rescheduled"
                    else:
                        status_label = "Scheduled"

                    locked_time = str(
                        row.get("Locked Time", "") or ""
                    ).strip()
                    time_text = (
                        f"{start}–{end}" + (" 🔒" if locked_time else "")
                        if start or end
                        else "No time"
                    )
                    dog_text = f" / {dogs}" if dogs else ""

                    with st.expander(
                        f"{time_text} · {owner}{dog_text} · ${price:,.0f}",
                        expanded=False,
                    ):
                        drive_minutes = pd.to_numeric(
                            row.get("Drive From Previous Min"),
                            errors="coerce",
                        )
                        drive_miles = pd.to_numeric(
                            row.get("Drive From Previous Miles"),
                            errors="coerce",
                        )
                        drive_detail = ""
                        if (
                            str(row.get("Routing Mode", "") or "") in {
                            "Google Maps", "Google Maps + Home"
                        }
                            and pd.notna(drive_minutes)
                            and float(drive_minutes) > 0
                        ):
                            drive_detail = (
                                f" · 🚗 {float(drive_minutes):.0f} min"
                                + (
                                    f" / {float(drive_miles):.1f} mi"
                                    if pd.notna(drive_miles)
                                    else ""
                                )
                            )

                        st.write(
                            f"**{status_label}** · {minutes} min"
                            + (f" · {area}" if area else "")
                            + drive_detail
                        )

                        if completion_status == "Completed":
                            st.success("This appointment is already completed.")
                        elif appointment_status in {
                            "Cancelled",
                            "Moved to another week",
                        }:
                            st.caption(
                                "This appointment is no longer active on this day."
                            )
                        else:
                            complete_col, cancel_col = st.columns(2)

                            with complete_col:
                                if st.button(
                                    "✅ Completed",
                                    key=f"today_complete_{today_week_key}_{row_index}",
                                    use_container_width=True,
                                ):
                                    try:
                                        changed, missing = mark_household_services_completed(
                                            row.get("Household ID"),
                                            row.get("Dogs"),
                                            today_view_date,
                                        )
                                        mark_week_plan_row_completed(
                                            today_week_key,
                                            st.session_state.week_route_plans[
                                                today_week_key
                                            ],
                                            row_index,
                                            today_view_date,
                                            today_settings,
                                        )
                                        if missing:
                                            st.warning(
                                                "Completed, but these dog names could "
                                                f"not be matched: {', '.join(missing)}"
                                            )
                                        else:
                                            st.success(
                                                f"Completed. Updated {changed} dog record(s)."
                                            )
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(
                                            f"Could not complete appointment: {exc}"
                                        )

                            with cancel_col:
                                if st.button(
                                    "❌ Cancel",
                                    key=f"today_cancel_{today_week_key}_{row_index}",
                                    use_container_width=True,
                                ):
                                    try:
                                        update_week_appointment_status(
                                            today_week_key,
                                            st.session_state.week_route_plans[
                                                today_week_key
                                            ],
                                            row_index,
                                            "Cancelled",
                                            today_settings,
                                        )
                                        st.success("Cancelled.")
                                        st.rerun()
                                    except Exception as exc:
                                        st.error(
                                            f"Could not cancel appointment: {exc}"
                                        )

                            new_date = st.date_input(
                                "Reschedule to",
                                value=(
                                    pd.Timestamp(today_view_date)
                                    + pd.Timedelta(days=7)
                                ).date(),
                                key=f"today_reschedule_date_{today_week_key}_{row_index}",
                            )

                            current_groomer = str(
                                row.get("Groomer", "") or ""
                            )
                            if current_groomer == "Jen":
                                quick_groomer_choices = ["Jen"]
                            elif current_groomer == "Haley":
                                quick_groomer_choices = ["Haley"]
                            else:
                                quick_groomer_choices = ["Jen", "Haley"]

                            quick_new_groomer = st.selectbox(
                                "Groomer",
                                quick_groomer_choices,
                                key=f"today_reschedule_groomer_{today_week_key}_{row_index}",
                            )

                            if st.button(
                                "↪️ Reschedule",
                                key=f"today_reschedule_{today_week_key}_{row_index}",
                                use_container_width=True,
                            ):
                                try:
                                    result = reschedule_appointment_to_date(
                                        today_week_key,
                                        st.session_state.week_route_plans[
                                            today_week_key
                                        ],
                                        row_index,
                                        new_date,
                                        quick_new_groomer,
                                        today_settings,
                                    )
                                    if result["same_week"]:
                                        st.success(
                                            f"Moved to {pd.Timestamp(new_date):%A, %b %d}."
                                        )
                                    else:
                                        st.session_state[
                                            "pending_week_builder_date"
                                        ] = pd.Timestamp(
                                            result["target_week_key"]
                                        ).date()
                                        st.success(
                                            f"Moved to {pd.Timestamp(new_date):%A, %b %d}. "
                                            "The future week was saved as Draft."
                                        )
                                    st.rerun()
                                except Exception as exc:
                                    st.error(
                                        f"Could not reschedule appointment: {exc}"
                                    )

                today_maps_url = google_maps_route_url(groomer_rows, groomer=groomer)
                if today_maps_url:
                    st.link_button(
                        f"🗺️ Open {groomer} route in Google Maps",
                        today_maps_url,
                        use_container_width=True,
                    )

                st.divider()

        if st.button(
            "Open this week in Weekly Route Builder",
            key=f"today_open_week_{today_week_key}",
        ):
            st.session_state["week_builder_date"] = pd.Timestamp(
                today_week_key
            ).date()
            st.success(
                f"Weekly Route Builder is set to the week of "
                f"{pd.Timestamp(today_week_key):%b %d}. Tap that tab."
            )


# ---------- Planner ----------

with planner_tab:
    target_date = st.date_input(
        "Planning date",
        value=date.today(),
        key="planner_date",
    )

    groomers = sorted(
        clients["Groomer"].dropna().astype(str).unique().tolist()
    )
    areas = sorted(
        clients["Area"].dropna().astype(str).unique().tolist()
    )

    c1, c2, c3 = st.columns(3)

    with c1:
        selected_groomer = st.selectbox(
            "Groomer",
            groomers or ["Jen"],
        )

    with c2:
        selected_area = st.selectbox(
            "Area",
            ["Any"] + areas,
        )

    with c3:
        available_minutes = st.selectbox(
            "Opening length",
            [60, 75, 90, 105, 120, 150, 180],
            index=2,
        )

    day_name = target_date.strftime("%A")

    if day_name not in WORKDAYS.get(selected_groomer, []):
        st.warning(
            f"{selected_groomer} normally does not work on {day_name}."
        )

    st.markdown("### Ask the planner")

    question = st.text_input(
        "Type a normal request",
        placeholder="Jen lost her 1 PM in The Woodlands. Who should I contact?",
    )

    inferred = infer_from_question(question, groomers, areas) if question else {}

    if inferred.get("groomer"):
        selected_groomer = inferred["groomer"]

    if inferred.get("area"):
        selected_area = inferred["area"]

    if inferred.get("minutes"):
        available_minutes = inferred["minutes"]

    if question:
        st.caption(
            f"Understood as: {selected_groomer} • "
            f"{selected_area} • {available_minutes} min"
        )

    due_clients = household_due_table(clients, target_date)

    day_appts = appointments[
        (appointments["Date"] == target_date)
        & (appointments["Groomer"] == selected_groomer)
    ].copy()

    if not day_appts.empty:
        st.markdown("#### Booked that day")
        st.dataframe(
            day_appts[
                ["Start Time", "End Time", "Client", "Area"]
            ],
            use_container_width=True,
            hide_index=True,
        )

    target_time = inferred.get("time_hint") or time(13, 0)

    before_client, after_client = nearest_booked_neighbors(
        day_appts,
        target_time,
    )

    # Primary pool: assigned groomer, fits the opening, due within 3 weeks.
    candidates = due_clients[
        (due_clients["Groomer"] == selected_groomer)
        & (due_clients["Minutes"].notna())
        & (due_clients["Minutes"] <= available_minutes)
        & (due_clients["Days Until Due"].notna())
        & (due_clients["Days Until Due"] <= 21)
    ].copy()

    # If the user asked for a specific area, treat that as a hard filter first.
    if selected_area != "Any":
        candidates = candidates[
            candidates["Area"].astype(str).str.strip().str.lower()
            == selected_area.strip().lower()
        ].copy()

    ranked = score_candidates(
        candidates,
        target_date,
        selected_groomer,
        selected_area,
        available_minutes,
        before_client,
        after_client,
        due_clients,
        maps_key,
    )

    fallback_note = None

    # If no exact-ish match exists, relax the due-date rule first.
    if ranked.empty:
        relaxed = due_clients[
            (due_clients["Groomer"] == selected_groomer)
            & (due_clients["Minutes"].notna())
            & (due_clients["Minutes"] <= available_minutes)
        ].copy()

        if selected_area != "Any":
            relaxed = relaxed[
                relaxed["Area"].astype(str).str.strip().str.lower()
                == selected_area.strip().lower()
            ].copy()

        ranked = score_candidates(
            relaxed,
            target_date,
            selected_groomer,
            selected_area,
            available_minutes,
            before_client,
            after_client,
            due_clients,
            maps_key,
        )

        if not ranked.empty:
            fallback_note = (
                "No clients fit the due-within-3-weeks rule, so these are the "
                "best schedule-fit alternatives."
            )

    # If still empty, allow slightly-too-long appointments as alternatives.
    if ranked.empty:
        relaxed_more = due_clients[
            (due_clients["Groomer"] == selected_groomer)
            & (due_clients["Minutes"].notna())
            & (due_clients["Minutes"] <= available_minutes + 30)
        ].copy()

        if selected_area != "Any":
            relaxed_more = relaxed_more[
                relaxed_more["Area"].astype(str).str.strip().str.lower()
                == selected_area.strip().lower()
            ].copy()

        ranked = score_candidates(
            relaxed_more,
            target_date,
            selected_groomer,
            selected_area,
            available_minutes + 30,
            before_client,
            after_client,
            due_clients,
            maps_key,
        )

        if not ranked.empty:
            fallback_note = (
                "No exact duration match was available, so these include "
                "appointments up to 30 minutes longer."
            )

    st.markdown("### Best clients to contact")

    if ranked.empty:
        st.info(
            "No suggestions yet. Check that your client CSV has values for "
            "Groomer, Minutes, Last Groom, and Frequency Weeks."
        )
    else:
        if fallback_note:
            st.caption(fallback_note)

        top = ranked.head(10).copy()

        display = top[
            [
                "Owner",
                "Dogs",
                "Area",
                "Status",
                "Price",
                "Minutes",
                "Drive Miles",
                "Drive Minutes",
                "Score",
                "Why",
            ]
        ].copy()

        display["Drive Miles"] = pd.to_numeric(
            display["Drive Miles"],
            errors="coerce",
        ).round(1)

        display["Drive Minutes"] = pd.to_numeric(
            display["Drive Minutes"],
            errors="coerce",
        ).round(0)

        st.dataframe(
            display,
            use_container_width=True,
            hide_index=True,
        )

        best = top.iloc[0]

        st.success(
            f"Best match: {best['Owner']} "
            f"({best['Dogs'] or 'pet'}) — {best['Status']} — "
            f"{best['Area']} — ${best['Price']:.0f}"
        )

        suggested_text = (
            f"Hi {best['Owner']}! We had an opening come up on "
            f"{target_date:%A, %B %d}. Would you like to grab it?"
        )

        st.code(suggested_text)

        if st.button(
            f"Mark {best['Owner']} contacted today",
            key="mark_best_contacted",
        ):
            idx = st.session_state.clients[
                st.session_state.clients["Household ID"] == best["Household ID"]
            ].index

            if len(idx):
                st.session_state.clients.loc[
                    idx,
                    "Last Contacted",
                ] = pd.Timestamp(date.today())

                if supabase_configured():
                    try:
                        sb = get_supabase()
                        ids = st.session_state.clients.loc[idx, "Record ID"].dropna().tolist()
                        for record_id in ids:
                            sb.table("dogs").update(
                                {"last_contacted": date.today().isoformat()}
                            ).eq("id", str(record_id)).execute()
                        st.success("Contact date saved.")
                    except Exception as exc:
                        st.error(f"Contact date updated in session but database save failed: {exc}")
                else:
                    st.success("Contact date updated for this session.")



# ---------- Household / dog-level helpers ----------

# ---------- Weekly route builder ----------

if "week_client_responses" not in st.session_state:
    st.session_state.week_client_responses = {}
if "week_route_plans" not in st.session_state:
    st.session_state.week_route_plans = {}
if "week_builder_settings" not in st.session_state:
    st.session_state.week_builder_settings = {}
if "week_plan_fingerprints" not in st.session_state:
    st.session_state.week_plan_fingerprints = {}
if "week_plan_statuses" not in st.session_state:
    st.session_state.week_plan_statuses = {}

def schedule_data_fingerprint(df):
    if df is None or df.empty:
        return "empty"

    cols = [
        c for c in [
            "Owner", "Dog", "Household ID", "Area", "Groomer",
            "Last Groom", "Frequency Weeks", "Service Pattern",
            "Bath Frequency Weeks", "Last Bath",
            "Groom Frequency Weeks", "Last Groom Service",
            "Bath Price", "Bath Minutes", "Groom Price", "Groom Minutes",
            "Price", "Minutes",
        ]
        if c in df.columns
    ]
    snap = df[cols].copy()
    for c in snap.columns:
        snap[c] = snap[c].astype(str)
    snap = snap.sort_values(cols, kind="stable").reset_index(drop=True)
    return str(int(pd.util.hash_pandas_object(snap, index=True).sum()))

# ---------- Monthly Planner ----------

with monthly_tab:
    st.markdown("## Monthly planner")
    st.info(
        "Confirmed weeks are your real schedule. Future recurring work is shown "
        "separately as projected / unscheduled until you build and confirm it."
    )

    saved_month = pd.Timestamp(
        st.session_state.get("monthly_planner_date", date.today())
    ).replace(day=1).date()

    nav1, nav2, nav3 = st.columns([1, 2, 1])

    with nav1:
        if st.button("‹ Previous", key="month_prev", use_container_width=True):
            new_month = (
                pd.Timestamp(saved_month) - pd.offsets.MonthBegin(1)
            ).replace(day=1).date()
            st.session_state["monthly_planner_date"] = new_month
            st.session_state["monthly_month_picker"] = new_month
            st.rerun()

    with nav2:
        st.markdown(
            f"<div style='text-align:center;font-size:1.35rem;font-weight:700;"
            f"padding-top:0.45rem'>{pd.Timestamp(saved_month):%B %Y}</div>",
            unsafe_allow_html=True,
        )

    with nav3:
        if st.button("Next ›", key="month_next", use_container_width=True):
            new_month = (
                pd.Timestamp(saved_month) + pd.offsets.MonthBegin(1)
            ).replace(day=1).date()
            st.session_state["monthly_planner_date"] = new_month
            st.session_state["monthly_month_picker"] = new_month
            st.rerun()

    if "monthly_month_picker" not in st.session_state:
        st.session_state["monthly_month_picker"] = saved_month

    jump_month = st.date_input(
        "Jump to month",
        value=st.session_state["monthly_month_picker"],
        key="monthly_month_picker",
        help="Pick any date in the month you want to view.",
    )

    picked_month = pd.Timestamp(jump_month).replace(day=1).date()
    if picked_month != saved_month:
        st.session_state["monthly_planner_date"] = picked_month
        st.rerun()

    month_start = pd.Timestamp(saved_month).normalize()
    month_end = (month_start + pd.offsets.MonthEnd(1)).normalize()

    month_plan = month_plan_dataframe(month_start, month_end)

    if not month_plan.empty:
        month_dates = pd.to_datetime(month_plan["Date"], errors="coerce")
        month_only = month_plan[
            (month_dates >= month_start)
            & (month_dates <= month_end)
        ].copy()
    else:
        month_only = month_plan.copy()

    if not month_only.empty:
        if "Appointment Status" not in month_only.columns:
            month_only["Appointment Status"] = "Scheduled"
        month_active = month_only[
            ~month_only["Appointment Status"]
            .fillna("Scheduled")
            .isin(["Cancelled", "Moved to another week"])
        ].copy()
    else:
        month_active = month_only.copy()

    confirmed_appts = len(month_active)
    confirmed_revenue = float(
        month_active.get("Price", pd.Series(dtype=float)).sum()
    ) if not month_active.empty else 0.0
    confirmed_minutes = int(
        month_active.get("Minutes", pd.Series(dtype=float)).sum()
    ) if not month_active.empty else 0

    service_history = confirmed_service_history(month_end)

    today_ts = pd.Timestamp(date.today()).normalize()

    if month_end < today_ts:
        projected = pd.DataFrame(
            columns=[
                "Household ID", "Owner", "Dogs", "Projected Due",
                "Area", "Groomer", "Minutes", "Price",
            ]
        )
    else:
        projection_start = max(month_start, today_ts)
        projected = projected_month_services(
            st.session_state.clients,
            projection_start,
            month_end,
            service_history=service_history,
        )

    confirmed_hids = set(
        month_active.get("Household ID", pd.Series(dtype=str))
        .dropna()
        .astype(str)
        .tolist()
    ) if not month_active.empty else set()

    # If a household already has a confirmed appointment somewhere in this
    # month, keep it out of the unscheduled projection list.
    projected_unscheduled = projected[
        ~projected["Household ID"].astype(str).isin(confirmed_hids)
    ].copy() if not projected.empty else projected.copy()

    projected_revenue = float(
        projected_unscheduled.get("Price", pd.Series(dtype=float)).sum()
    ) if not projected_unscheduled.empty else 0.0
    projected_minutes = int(
        projected_unscheduled.get("Minutes", pd.Series(dtype=float)).sum()
    ) if not projected_unscheduled.empty else 0

    st.markdown(f"### {month_start:%B %Y} overview")

    m1, m2 = st.columns(2)
    m1.metric("Confirmed appointments", confirmed_appts)
    m2.metric("Confirmed revenue", f"${confirmed_revenue:,.0f}")

    p1, p2 = st.columns(2)
    p1.metric("Projected unscheduled stops", len(projected_unscheduled))
    p2.metric(
        "Projected unscheduled revenue",
        f"${projected_revenue:,.0f}",
    )

    st.caption(
        f"Confirmed groom time: {confirmed_minutes} min · "
        f"Projected unscheduled time: {projected_minutes} min. "
        "Projections use each dog's recurring cadence. A confirmed full groom resets both the Groom and Bath clocks, because the groom includes the bath."
    )

    st.markdown("### Confirmed schedule")

    if month_plan.empty:
        st.info(
            "No confirmed appointments are on this month yet."
        )

        unconfirmed_weeks = load_month_unconfirmed_drafts_db(
            month_start,
            month_end,
        )

        if unconfirmed_weeks:
            draft_labels = []
            for saved in unconfirmed_weeks:
                week_value = pd.to_datetime(
                    saved.get("week_start"),
                    errors="coerce",
                )
                if not pd.isna(week_value):
                    draft_labels.append(f"{week_value:%b %d}")

            label_text = ", ".join(draft_labels) if draft_labels else "this month"

            st.warning(
                f"You do have saved draft schedule(s) for: {label_text}. "
                "They are hidden here until you open that week in Weekly Route Builder "
                "and tap Confirm this week."
            )
        else:
            st.caption(
                "Build and adjust a week in Weekly Route Builder, then tap Confirm this week."
            )
    else:
        month_plan["_Date Sort"] = pd.to_datetime(
            month_plan["Date"],
            errors="coerce",
        )
        monday_series = (
            month_plan["_Date Sort"]
            - pd.to_timedelta(
                month_plan["_Date Sort"].dt.weekday,
                unit="D",
            )
        )
        month_plan["_Week Monday"] = monday_series.dt.date

        for week_monday, week_group in month_plan.groupby(
            "_Week Monday",
            sort=True,
        ):
            week_monday_ts = pd.Timestamp(week_monday)
            week_revenue = float(
                week_group["Price"].sum()
            ) if "Price" in week_group.columns else 0
            week_count = len(week_group)

            st.markdown(
                f"#### Week of {week_monday_ts:%b %d} "
                f"· {week_count} appts · ${week_revenue:,.0f}"
            )

            for day_offset in range(5):
                day_ts = week_monday_ts + pd.Timedelta(days=day_offset)
                day_rows = week_group[
                    pd.to_datetime(week_group["Date"]).dt.normalize()
                    == day_ts.normalize()
                ].copy()

                if day_rows.empty:
                    st.markdown(
                        f"**{day_ts:%A · %b %d}** — _open_"
                    )
                    continue

                day_rows["_Start Sort"] = day_rows.get(
                    "Start Time",
                    pd.Series(
                        [""] * len(day_rows),
                        index=day_rows.index,
                    ),
                ).apply(clock_sort_minutes)

                day_rows = day_rows.sort_values(
                    ["_Start Sort", "Groomer", "Owner"],
                    kind="stable",
                )

                day_revenue = float(
                    day_rows["Price"].sum()
                ) if "Price" in day_rows.columns else 0

                st.markdown(
                    f"**{day_ts:%A · %b %d}** "
                    f"· {len(day_rows)} appts · ${day_revenue:,.0f}"
                )

                for _, row in day_rows.iterrows():
                    start_time = str(row.get("Start Time", "") or "")
                    owner = str(row.get("Owner", "") or "")
                    dogs = str(row.get("Dogs", "") or "")
                    groomer = str(row.get("Groomer", "") or "")
                    area = str(row.get("Area Cluster", "") or "")
                    price = float(row.get("Price", 0) or 0)
                    completion_status = str(
                        row.get("Completion Status", "") or ""
                    ).strip()
                    appointment_status = str(
                        row.get("Appointment Status", "") or ""
                    ).strip()
                    status_note = str(
                        row.get("Status Note", "") or ""
                    ).strip()

                    details = " · ".join(
                        [part for part in [groomer, area] if part]
                    )
                    time_prefix = (
                        f"{start_time} · " if start_time else ""
                    )
                    dog_text = f" / {dogs}" if dogs else ""

                    if completion_status == "Completed":
                        completed_text = " · ✅ Completed"
                    elif appointment_status == "Cancelled":
                        completed_text = " · ❌ Cancelled"
                    elif appointment_status == "Rescheduled":
                        completed_text = " · ↪️ Rescheduled"
                    elif appointment_status == "Moved to another week":
                        moved_to = str(row.get("Rescheduled To", "") or "").strip()
                        completed_text = (
                            f" · ↪️ Moved to {moved_to}"
                            if moved_to
                            else " · ↪️ Moved to another week"
                        )
                    else:
                        completed_text = ""

                    if status_note and appointment_status in {
                        "Cancelled", "Rescheduled", "Moved to another week"
                    }:
                        completed_text += f" · {status_note}"

                    st.markdown(
                        f"- **{time_prefix}{owner}{dog_text}**"
                        f"{' · ' + details if details else ''} "
                        f"· ${price:,.0f}{completed_text}"
                    )

            st.divider()

    st.markdown("### Projected / unscheduled this month")
    st.caption(
        "These are expected from the recurring service schedules, but they are "
        "not appointments yet. For the current month, past due dates are hidden; "
        "only today forward is projected. Confirmed appointments reset the recurrence clock."
    )

    if projected_unscheduled.empty:
        st.success(
            "No additional recurring clients are currently projected for this month."
        )
    else:
        projected_unscheduled["Projected Due"] = pd.to_datetime(
            projected_unscheduled["Projected Due"],
            errors="coerce",
        ).dt.date

        show_projection = projected_unscheduled[
            [
                "Projected Due",
                "Owner",
                "Dogs",
                "Area",
                "Groomer",
                "Minutes",
                "Price",
            ]
        ].sort_values(
            ["Projected Due", "Area", "Owner"]
        )

        st.dataframe(
            show_projection,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Price": st.column_config.NumberColumn(
                    "Price",
                    format="$%.0f",
                ),
                "Minutes": st.column_config.NumberColumn(
                    "Minutes",
                    format="%d",
                ),
            },
        )

    st.markdown("### Jump to a week")

    first_monday = (
        month_start - pd.Timedelta(days=month_start.weekday())
    )
    last_week_monday = (
        month_end - pd.Timedelta(days=month_end.weekday())
    )

    week_choices = []
    cursor = first_monday
    while cursor <= last_week_monday:
        week_choices.append(cursor.date())
        cursor += pd.Timedelta(days=7)

    chosen_week = st.selectbox(
        "Week",
        week_choices,
        format_func=lambda d: (
            f"Week of {pd.Timestamp(d):%b %d}"
        ),
        key=f"monthly_week_jump_{month_start:%Y_%m}",
    )

    if st.button(
        "Open this week in Weekly Route Builder",
        key=f"monthly_open_week_{month_start:%Y_%m}",
    ):
        st.session_state["week_builder_date"] = chosen_week
        st.success(
            f"Weekly Route Builder is set to the week of "
            f"{pd.Timestamp(chosen_week):%B %d}. "
            "Tap the Weekly Route Builder tab."
        )


with weekly_tab:
    st.info(
        "Schedule-strict mode: this builder only uses overdue clients or clients "
        "due during the selected week. It will not pull not-due clients forward."
    )

    if st.session_state.clients.empty:
        st.info(
            "No private clients are saved yet. Add dogs in Client Manager, "
            "then the weekly route builder will start using them."
        )
    st.markdown("### Build the week automatically")
    st.caption(
        "Creates a Monday–Friday draft using due/overdue status, groomer workdays, "
        "appointment length, area clustering, and revenue."
    )
    st.success(
        "Saved-week mode: once you move or add clients, the exact weekly draft is "
        "stored in the private database. It will not rebuild unless you tap "
        "Generate / rebuild week or Reset this week."
    )
    # Cross-week rescheduling cannot directly change a widget key after that
    # widget has already been created in the same Streamlit run. Store the
    # requested destination temporarily, then apply it here on the next rerun
    # BEFORE the date_input is instantiated.
    pending_week_builder_date = st.session_state.pop(
        "pending_week_builder_date",
        None,
    )
    if pending_week_builder_date is not None:
        st.session_state["week_builder_date"] = pending_week_builder_date

    week_start_input = st.date_input(
        "Week of",
        value=st.session_state.get("week_builder_date", date.today()),
        key="week_builder_date",
    )

    selected_week_monday = pd.Timestamp(week_start_input) - pd.Timedelta(
        days=pd.Timestamp(week_start_input).weekday()
    )
    week_key = selected_week_monday.date().isoformat()

    already_scheduled_households = load_households_scheduled_before_week_db(
        week_key,
        lookback_weeks=4,
    )

    if already_scheduled_households:
        st.caption(
            f"{len(already_scheduled_households)} household(s) already placed in a "
            "recent saved week are being kept out of this week's automatic draft. "
            "You can still manually add one if you intentionally need to reschedule them."
        )

    if week_key not in st.session_state.week_client_responses:
        st.session_state.week_client_responses[week_key] = load_week_overrides_db(week_key)
    week_overrides = st.session_state.week_client_responses[week_key]

    # On a fresh Streamlit session or after a deployment, restore the exact
    # weekly draft from Supabase instead of regenerating it from scratch.
    if week_key not in st.session_state.week_route_plans:
        persisted_draft = load_week_draft_db(week_key)
        if persisted_draft is not None:
            st.session_state.week_route_plans[week_key] = persisted_draft["plan"]
            st.session_state.week_builder_settings[week_key] = persisted_draft["settings"]
            st.session_state.week_plan_fingerprints[week_key] = persisted_draft["fingerprint"]
            st.session_state.week_plan_statuses[week_key] = persisted_draft.get("status", "draft")

    default_settings = {
        "daily_capacity": 420,
        "max_appointments": 4,
        "jen_start": time(8, 0),
        "haley_start": time(8, 0),
        "travel_buffer": 20,
        "service_buffer": 0,
    }
    saved_settings = st.session_state.week_builder_settings.get(
        week_key, default_settings.copy()
    )

    c1, c2 = st.columns(2)

    with c1:
        capacity_options = [300, 360, 420, 480, 540]
        saved_capacity = int(saved_settings.get("daily_capacity", 420))
        daily_capacity = st.selectbox(
            "Approximate grooming minutes available per groomer/day",
            capacity_options,
            index=(
                capacity_options.index(saved_capacity)
                if saved_capacity in capacity_options
                else 2
            ),
            format_func=lambda x: f"{x} minutes ({x/60:.1f} hrs)",
            key=f"daily_capacity_{week_key}",
        )

    with c2:
        appt_options = [3, 4, 5, 6]
        saved_appts = int(saved_settings.get("max_appointments", 4))
        max_appointments = st.selectbox(
            "Maximum appointments per groomer/day",
            appt_options,
            index=(
                appt_options.index(saved_appts)
                if saved_appts in appt_options
                else 1
            ),
            key=f"max_appointments_{week_key}",
        )

    st.markdown("#### Time assumptions")

    t1, t2 = st.columns(2)

    with t1:
        jen_start = st.time_input(
            "Jen first-stop arrival time",
            value=saved_settings.get("jen_start", time(8, 0)),
            key=f"jen_week_start_{week_key}",
        )

    with t2:
        haley_start = st.time_input(
            "Haley first-stop arrival time",
            value=saved_settings.get("haley_start", time(8, 0)),
            key=f"haley_week_start_{week_key}",
        )

    t3, t4 = st.columns(2)

    with t3:
        travel_options = [10, 15, 20, 25, 30, 45]
        saved_travel = int(saved_settings.get("travel_buffer", 20))
        travel_buffer = st.selectbox(
            "Travel / setup buffer",
            travel_options,
            index=(
                travel_options.index(saved_travel)
                if saved_travel in travel_options
                else 2
            ),
            format_func=lambda x: f"{x} min",
            key=f"travel_buffer_{week_key}",
        )

    with t4:
        service_options = [0, 5, 10, 15]
        saved_service = int(saved_settings.get("service_buffer", 0))
        service_buffer = st.selectbox(
            "Extra service buffer per household",
            service_options,
            index=(
                service_options.index(saved_service)
                if saved_service in service_options
                else 0
            ),
            format_func=lambda x: f"{x} min",
            key=f"service_buffer_{week_key}",
        )

    current_settings = {
        "daily_capacity": daily_capacity,
        "max_appointments": max_appointments,
        "jen_start": jen_start,
        "haley_start": haley_start,
        "travel_buffer": travel_buffer,
        "service_buffer": service_buffer,
    }
    st.session_state.week_builder_settings[week_key] = current_settings

    build_col1, build_col2 = st.columns(2)
    with build_col1:
        rebuild_week = st.button(
            "Generate / rebuild week",
            type="primary",
            key=f"rebuild_week_{week_key}",
        )
    with build_col2:
        if st.button(
            "Reset this week",
            key=f"reset_week_{week_key}",
        ):
            st.session_state.week_route_plans.pop(week_key, None)
            st.session_state.week_plan_fingerprints.pop(week_key, None)
            st.session_state.week_plan_statuses.pop(week_key, None)
            st.session_state.week_client_responses[week_key] = {}
            try:
                clear_week_overrides_db(week_key)
                delete_week_draft_db(week_key)
            except Exception as exc:
                st.warning(f"Could not clear saved weekly changes: {exc}")
            st.session_state.week_builder_settings[week_key] = default_settings.copy()
            for key in [
                f"daily_capacity_{week_key}",
                f"max_appointments_{week_key}",
                f"jen_week_start_{week_key}",
                f"haley_week_start_{week_key}",
                f"travel_buffer_{week_key}",
                f"service_buffer_{week_key}",
            ]:
                st.session_state.pop(key, None)
            st.rerun()

    current_status = st.session_state.week_plan_statuses.get(
        week_key,
        "draft",
    )

    if current_status == "confirmed":
        st.success("This week is CONFIRMED and will appear on the Monthly Planner.")
    else:
        st.info(
            "This week is a DRAFT. It will not appear on the Monthly Planner until you confirm it."
        )

    confirm_col1, confirm_col2 = st.columns(2)

    with confirm_col1:
        if current_status != "confirmed":
            if st.button(
                "Confirm this week",
                type="primary",
                key=f"confirm_week_{week_key}",
            ):
                if week_key not in st.session_state.week_route_plans:
                    st.warning("Generate the week first, then confirm it.")
                else:
                    try:
                        set_week_draft_status_db(week_key, "confirmed")
                        st.session_state.week_plan_statuses[week_key] = "confirmed"
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not confirm this week: {exc}")
        else:
            if st.button(
                "Return to draft",
                key=f"unconfirm_week_{week_key}",
            ):
                try:
                    set_week_draft_status_db(week_key, "draft")
                    st.session_state.week_plan_statuses[week_key] = "draft"
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not return this week to draft: {exc}")

    with confirm_col2:
        st.caption(
            "Monthly Planner shows confirmed weeks only. You can keep editing a draft until it is ready."
        )

    current_fingerprint = schedule_data_fingerprint(st.session_state.clients)

    # Generate once for a brand-new week, then keep that draft until the user
    # explicitly rebuilds it. Tab changes no longer reset/recalculate the plan.
    if rebuild_week or week_key not in st.session_state.week_route_plans:
        weekly_plan = build_week_plan(
            st.session_state.clients,
            pd.Timestamp(week_start_input),
            daily_capacity_minutes=daily_capacity,
            max_appointments_per_day=max_appointments,
            week_overrides=week_overrides,
            exclude_household_ids=already_scheduled_households,
        )

        weekly_plan = assign_times_to_weekly_plan(
            weekly_plan,
            jen_start=jen_start,
            haley_start=haley_start,
            travel_buffer_minutes=travel_buffer,
            service_buffer_minutes=service_buffer,
        )

        st.session_state.week_route_plans[week_key] = weekly_plan
        st.session_state.week_plan_fingerprints[week_key] = current_fingerprint
        st.session_state.week_plan_statuses[week_key] = "draft"
        try:
            save_week_draft_db(
                week_key,
                weekly_plan,
                current_settings,
                current_fingerprint,
            )
            set_week_draft_status_db(week_key, "draft")
        except Exception as exc:
            st.error(
                "The week was generated, but it could not be saved permanently: "
                f"{exc}"
            )
    else:
        weekly_plan = st.session_state.week_route_plans[week_key].copy()


    # Real route optimization is optional and explicit so ordinary edits do not
    # unexpectedly rearrange a carefully adjusted week.
    if not weekly_plan.empty:
        st.markdown("### Route optimization")

        if not maps_key:
            st.info(
                "Google Maps routing is not connected yet. Add "
                "GOOGLE_MAPS_API_KEY in Streamlit Secrets to turn on real "
                "drive-time ordering."
            )
        else:
            if st.button(
                "🗺️ Optimize this week with Google Maps",
                key=f"optimize_maps_{week_key}",
                use_container_width=True,
            ):
                try:
                    with st.spinner(
                        "Calculating real drive times and ordering each route..."
                    ):
                        routed_plan, routing_notes = optimize_week_with_google_maps(
                            st.session_state.week_route_plans[week_key],
                            maps_key,
                            jen_start=jen_start,
                            haley_start=haley_start,
                            service_buffer_minutes=service_buffer,
                        )

                    st.session_state.week_route_plans[week_key] = routed_plan
                    st.session_state.week_plan_statuses[week_key] = "draft"
                    weekly_plan = routed_plan

                    save_week_draft_db(
                        week_key,
                        routed_plan,
                        current_settings,
                        st.session_state.week_plan_fingerprints.get(
                            week_key,
                            current_fingerprint,
                        ),
                    )
                    set_week_draft_status_db(week_key, "draft")

                    if routing_notes:
                        for note in routing_notes:
                            st.warning(note)

                    st.success(
                        "Route optimized using Google Maps drive times. "
                        "When a groomer's private home address is configured, "
                        "the route includes home → clients → home. The groomer start "
                        "time is the ARRIVAL time at the first client, and any 🔒 exact "
                        "appointment times are preserved while the other stops route "
                        "around them. Review it, then confirm the week again."
                    )
                    st.rerun()

                except Exception as exc:
                    st.error(
                        "Google Maps could not optimize this week. "
                        f"Check the API key / Routes API settings. Details: {exc}"
                    )

    saved_fingerprint = st.session_state.week_plan_fingerprints.get(week_key)
    if saved_fingerprint and saved_fingerprint != current_fingerprint:
        st.warning(
            "Client data has changed since this weekly draft was generated. "
            "Your saved draft is still shown. Tap Generate / rebuild week when you want to update it."
        )

    # Persist control choices without changing the exact schedule.
    if week_key in st.session_state.week_route_plans:
        try:
            save_week_draft_db(
                week_key,
                st.session_state.week_route_plans[week_key],
                current_settings,
                st.session_state.week_plan_fingerprints.get(week_key, ""),
            )
        except Exception:
            pass

    if weekly_plan.empty:
        st.info("No clients are currently due enough to build a week.")
    else:
        valid = weekly_plan[weekly_plan["Owner"] != ""].copy()

        if "Appointment Status" not in valid.columns:
            valid["Appointment Status"] = "Scheduled"
        valid["Appointment Status"] = (
            valid["Appointment Status"].fillna("").replace("", "Scheduled")
        )

        active_valid = valid[
            ~valid["Appointment Status"].isin(
                ["Cancelled", "Moved to another week"]
            )
        ].copy()

        m1, m2, m3 = st.columns(3)
        m1.metric("Active appointments", len(active_valid))
        m2.metric("Projected revenue", f"${active_valid['Price'].sum():,.0f}")
        m3.metric("Scheduled minutes", int(active_valid["Minutes"].sum()))

        st.markdown("### Weekly schedule")
        render_weekly_cards(
            valid,
            daily_capacity=daily_capacity,
            travel_buffer=travel_buffer,
        )

        st.markdown("### Exact appointment time")
        st.caption(
            "Lock a customer to a promised arrival time. Google Maps will route "
            "the other appointments around it. Choose Flexible to remove the lock."
        )

        exact_time_candidates = active_valid.copy()
        if not exact_time_candidates.empty:
            exact_time_options = {}
            for row_index, row in exact_time_candidates.iterrows():
                appt_date = pd.to_datetime(row.get("Date"), errors="coerce")
                date_label = (
                    appt_date.strftime("%a %b %d")
                    if not pd.isna(appt_date)
                    else str(row.get("Date", ""))
                )
                current_lock = str(row.get("Locked Time", "") or "").strip()
                lock_label = f" · 🔒 {current_lock}" if current_lock else ""
                label = (
                    f"{date_label} · {row.get('Owner', '')} / "
                    f"{row.get('Dogs', '')}{lock_label}"
                )
                exact_time_options[label] = row_index

            selected_exact = st.selectbox(
                "Customer",
                list(exact_time_options.keys()),
                key=f"exact_time_client_{week_key}",
            )
            exact_row_index = exact_time_options[selected_exact]
            exact_row = st.session_state.week_route_plans[week_key].loc[
                exact_row_index
            ]
            existing_lock = parse_time(exact_row.get("Locked Time", ""))
            existing_start = parse_time(exact_row.get("Start Time", ""))
            default_exact_time = (
                existing_lock
                or existing_start
                or (
                    jen_start
                    if str(exact_row.get("Groomer", "")) == "Jen"
                    else haley_start
                )
            )

            exact_mode = st.radio(
                "Time rule",
                ["Flexible", "Exact time"],
                index=1 if existing_lock is not None else 0,
                horizontal=True,
                key=f"exact_time_mode_{week_key}_{exact_row_index}",
            )

            selected_exact_time = None
            if exact_mode == "Exact time":
                selected_exact_time = st.time_input(
                    "Promised arrival time",
                    value=default_exact_time,
                    key=f"exact_time_value_{week_key}_{exact_row_index}",
                )

            if st.button(
                "Save time rule",
                key=f"save_exact_time_{week_key}",
                use_container_width=True,
            ):
                try:
                    plan_with_lock = st.session_state.week_route_plans[
                        week_key
                    ].copy()
                    if "Locked Time" not in plan_with_lock.columns:
                        plan_with_lock["Locked Time"] = ""

                    if exact_mode == "Exact time":
                        plan_with_lock.at[
                            exact_row_index, "Locked Time"
                        ] = format_clock(selected_exact_time)
                    else:
                        plan_with_lock.at[
                            exact_row_index, "Locked Time"
                        ] = ""

                    # Re-optimize immediately when Maps is connected so the
                    # rest of the day moves around the promised time.
                    if maps_key:
                        routed_plan, routing_notes = optimize_week_with_google_maps(
                            plan_with_lock,
                            maps_key,
                            jen_start=jen_start,
                            haley_start=haley_start,
                            service_buffer_minutes=service_buffer,
                        )
                        plan_with_lock = routed_plan
                        for note in routing_notes:
                            st.warning(note)
                    else:
                        # Without Maps, preserve the rule in the weekly draft.
                        # The exact route will be rebuilt once Maps optimization
                        # is available.
                        if exact_mode == "Exact time":
                            duration = pd.to_numeric(
                                plan_with_lock.at[
                                    exact_row_index, "Minutes"
                                ],
                                errors="coerce",
                            )
                            duration = (
                                int(duration) if pd.notna(duration) else 0
                            )
                            plan_with_lock.at[
                                exact_row_index, "Start Time"
                            ] = format_clock(selected_exact_time)
                            plan_with_lock.at[
                                exact_row_index, "End Time"
                            ] = format_clock(
                                add_minutes_to_time(
                                    selected_exact_time,
                                    duration + int(service_buffer),
                                )
                            )

                    st.session_state.week_route_plans[week_key] = plan_with_lock
                    st.session_state.week_plan_statuses[week_key] = "draft"
                    weekly_plan = plan_with_lock

                    save_week_draft_db(
                        week_key,
                        plan_with_lock,
                        current_settings,
                        st.session_state.week_plan_fingerprints.get(
                            week_key,
                            current_fingerprint,
                        ),
                    )
                    set_week_draft_status_db(week_key, "draft")

                    if exact_mode == "Exact time":
                        st.success(
                            f"Exact time saved for "
                            f"{exact_row.get('Owner', '')}: "
                            f"{format_clock(selected_exact_time)}. "
                            "The route was adjusted around it."
                        )
                    else:
                        st.success(
                            "Time lock removed. This appointment is flexible again."
                        )
                    st.rerun()
                except Exception as exc:
                    st.error(
                        "The route could not be rebuilt with the current time "
                        f"locks. Details: {exc}"
                    )

        st.markdown("### Complete an appointment")
        st.caption(
            "Use this after the service is actually finished. It updates each dog's "
            "Last Bath / Last Groom date automatically so future scheduling starts "
            "from what was really done."
        )

        completion_candidates = valid.copy()
        if "Completion Status" in completion_candidates.columns:
            completion_candidates = completion_candidates[
                completion_candidates["Completion Status"].fillna("") != "Completed"
            ].copy()
        if "Appointment Status" in completion_candidates.columns:
            completion_candidates = completion_candidates[
                ~completion_candidates["Appointment Status"]
                .fillna("Scheduled")
                .isin(["Cancelled", "Moved to another week"])
            ].copy()

        if completion_candidates.empty:
            st.success("All appointments in this week are marked completed.")
        else:
            completion_options = {}
            for row_index, row in completion_candidates.iterrows():
                appt_date = pd.to_datetime(
                    row.get("Date"),
                    errors="coerce",
                )
                date_label = (
                    appt_date.strftime("%a %b %d")
                    if not pd.isna(appt_date)
                    else str(row.get("Date", ""))
                )
                label = (
                    f"{date_label} · {row.get('Start Time', '')} · "
                    f"{row.get('Owner', '')} / {row.get('Dogs', '')}"
                )
                completion_options[label] = row_index

            selected_completion = st.selectbox(
                "Appointment",
                list(completion_options.keys()),
                key=f"complete_appointment_{week_key}",
            )
            completion_row_index = completion_options[selected_completion]
            completion_row = valid.loc[completion_row_index]

            default_completed_date = pd.to_datetime(
                completion_row.get("Date"),
                errors="coerce",
            )
            if pd.isna(default_completed_date):
                default_completed_date = pd.Timestamp(date.today())

            completion_date = st.date_input(
                "Date service was completed",
                value=default_completed_date.date(),
                key=f"completed_date_{week_key}_{completion_row_index}",
            )

            if st.button(
                "✅ Mark appointment completed",
                type="primary",
                key=f"mark_completed_{week_key}",
            ):
                try:
                    changed, missing = mark_household_services_completed(
                        completion_row.get("Household ID"),
                        completion_row.get("Dogs"),
                        completion_date,
                    )

                    weekly_plan = mark_week_plan_row_completed(
                        week_key,
                        st.session_state.week_route_plans[week_key],
                        completion_row_index,
                        completion_date,
                        current_settings,
                    )

                    if missing:
                        st.warning(
                            "Appointment was marked completed, but I could not match "
                            f"these dog name(s) to the client records: {', '.join(missing)}."
                        )
                    elif changed:
                        st.success(
                            f"Completed. Updated {changed} dog record(s) and their "
                            "future recurrence dates."
                        )
                    else:
                        st.warning(
                            "Appointment was marked completed, but no dog records were updated."
                        )

                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not mark this appointment completed: {exc}")

        st.markdown("### Cancel or reschedule")
        st.caption(
            "Choose an exact date. You can move the appointment later this week "
            "or to a completely different future week."
        )

        status_candidates = valid.copy()
        if "Completion Status" in status_candidates.columns:
            status_candidates = status_candidates[
                status_candidates["Completion Status"].fillna("") != "Completed"
            ].copy()
        if "Appointment Status" in status_candidates.columns:
            status_candidates = status_candidates[
                status_candidates["Appointment Status"]
                .fillna("Scheduled") != "Moved to another week"
            ].copy()

        if status_candidates.empty:
            st.caption("No unfinished appointments are available to change.")
        else:
            status_options = {}
            for status_index, status_row in status_candidates.iterrows():
                status_date = pd.to_datetime(
                    status_row.get("Date"),
                    errors="coerce",
                )
                status_date_label = (
                    status_date.strftime("%a %b %d")
                    if not pd.isna(status_date)
                    else str(status_row.get("Date", ""))
                )
                status_label = (
                    f"{status_date_label} · {status_row.get('Start Time', '')} · "
                    f"{status_row.get('Owner', '')} / {status_row.get('Dogs', '')}"
                )
                status_options[status_label] = status_index

            selected_status_appt = st.selectbox(
                "Appointment to change",
                list(status_options.keys()),
                key=f"status_appointment_{week_key}",
            )
            status_row_index = status_options[selected_status_appt]
            selected_status_row = valid.loc[status_row_index]

            status_action = st.selectbox(
                "What changed?",
                [
                    "Cancel appointment",
                    "Reschedule to another date",
                    "Restore to scheduled",
                ],
                key=f"status_action_{week_key}",
            )

            status_note = st.text_input(
                "Note (optional)",
                placeholder="Client sick, out of town, requested another date, etc.",
                key=f"status_note_{week_key}",
            )

            new_date = None
            new_groomer = None

            if status_action == "Reschedule to another date":
                original_date = pd.to_datetime(
                    selected_status_row.get("Date"),
                    errors="coerce",
                )
                if pd.isna(original_date):
                    original_date = pd.Timestamp(date.today())

                suggested_date = (
                    original_date + pd.Timedelta(days=7)
                ).date()

                rs1, rs2 = st.columns(2)

                with rs1:
                    new_date = st.date_input(
                        "New appointment date",
                        value=suggested_date,
                        key=f"reschedule_date_{week_key}_{status_row_index}",
                    )

                with rs2:
                    normal_groomer = str(
                        selected_status_row.get("Groomer", "") or ""
                    )
                    if normal_groomer == "Jen":
                        reschedule_groomer_choices = ["Jen"]
                    elif normal_groomer == "Haley":
                        reschedule_groomer_choices = ["Haley"]
                    else:
                        reschedule_groomer_choices = ["Jen", "Haley"]

                    new_groomer = st.selectbox(
                        "Groomer",
                        reschedule_groomer_choices,
                        key=f"reschedule_groomer_{week_key}",
                    )

                selected_day_name = pd.Timestamp(new_date).strftime("%A")
                if selected_day_name in {"Saturday", "Sunday"}:
                    st.warning("Choose a Monday-Friday appointment date.")
                elif selected_day_name not in WORKDAYS.get(new_groomer, []):
                    st.warning(
                        f"{new_groomer} does not normally work on {selected_day_name}."
                    )
                else:
                    target_monday = (
                        pd.Timestamp(new_date)
                        - pd.Timedelta(days=pd.Timestamp(new_date).weekday())
                    )
                    if target_monday.date().isoformat() != week_key:
                        st.info(
                            f"This will move the appointment into the week of "
                            f"{target_monday:%b %d}. That destination week will be saved "
                            "as a Draft for you to review and confirm."
                        )

            if st.button(
                "Save appointment change",
                key=f"save_status_change_{week_key}",
            ):
                try:
                    if status_action == "Cancel appointment":
                        weekly_plan = update_week_appointment_status(
                            week_key,
                            st.session_state.week_route_plans[week_key],
                            status_row_index,
                            "Cancelled",
                            current_settings,
                            note=status_note,
                        )
                        st.success(
                            "Cancelled. It no longer counts toward route time or revenue."
                        )

                    elif status_action == "Reschedule to another date":
                        selected_day_name = pd.Timestamp(new_date).strftime("%A")
                        if selected_day_name in {"Saturday", "Sunday"}:
                            st.error("Choose a Monday-Friday appointment date.")
                            st.stop()
                        if selected_day_name not in WORKDAYS.get(new_groomer, []):
                            st.error(
                                f"{new_groomer} does not normally work on {selected_day_name}."
                            )
                            st.stop()

                        result = reschedule_appointment_to_date(
                            week_key,
                            st.session_state.week_route_plans[week_key],
                            status_row_index,
                            new_date,
                            new_groomer,
                            current_settings,
                            note=status_note,
                        )
                        weekly_plan = result["source_plan"]

                        if result["same_week"]:
                            st.success(
                                f"Moved to {pd.Timestamp(new_date):%A, %b %d}. "
                                "Review and confirm this week again."
                            )
                        else:
                            st.session_state["pending_week_builder_date"] = pd.Timestamp(
                                result["target_week_key"]
                            ).date()
                            st.success(
                                f"Moved to {pd.Timestamp(new_date):%A, %b %d}. "
                                f"The week of {pd.Timestamp(result['target_week_key']):%b %d} "
                                "is now a Draft and will open next so you can review it."
                            )

                    else:
                        weekly_plan = update_week_appointment_status(
                            week_key,
                            st.session_state.week_route_plans[week_key],
                            status_row_index,
                            "Scheduled",
                            current_settings,
                            note="",
                        )
                        st.success("Appointment restored to scheduled.")

                    st.rerun()

                except Exception as exc:
                    st.error(f"Could not update appointment: {exc}")

        st.markdown("### Add a client to this week")
        st.caption(
            "Use this when someone is not in the generated draft but you want them on this week. "
            "This is a one-week override and does not change their normal recurrence."
        )

        all_households = household_due_table(
            st.session_state.clients,
            pd.Timestamp(week_start_input),
        )

        if not all_households.empty:
            manual_options = {}
            for _, r in all_households.iterrows():
                hid = str(r.get("Household ID", "") or "")
                label = (
                    f"{r.get('Owner', '')} — {r.get('Dogs', '')} "
                    f"— {r.get('Status', '')}"
                )
                manual_options[label] = hid

            selected_manual = st.selectbox(
                "Client to add",
                list(manual_options.keys()),
                key=f"manual_add_client_{week_key}",
            )
            manual_hid = manual_options[selected_manual]
            manual_row = all_households[
                all_households["Household ID"].astype(str) == str(manual_hid)
            ].iloc[0]

            ma1, ma2 = st.columns(2)
            with ma1:
                manual_day = st.selectbox(
                    "Add to day",
                    ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
                    key=f"manual_add_day_{week_key}",
                )
            with ma2:
                normal_groomer = str(manual_row.get("Groomer", "") or "")
                if normal_groomer == "Jen":
                    groomer_choices = ["Jen"]
                elif normal_groomer == "Haley":
                    groomer_choices = ["Haley"]
                else:
                    groomer_choices = ["Jen", "Haley"]
                manual_groomer = st.selectbox(
                    "Groomer for this week",
                    groomer_choices,
                    key=f"manual_add_groomer_{week_key}",
                )

            allowed_days = WORKDAYS.get(manual_groomer, [])
            if manual_day not in allowed_days:
                st.warning(
                    f"{manual_groomer} does not normally work on {manual_day}. "
                    "Choose one of their working days."
                )
            elif st.button(
                "Add client to this week",
                type="primary",
                key=f"manual_add_button_{week_key}",
            ):
                week_overrides[manual_hid] = {
                    "response_status": "Manually added",
                    "requested_day": manual_day,
                    "requested_groomer": manual_groomer,
                    "force_include": True,
                }
                st.session_state.week_client_responses[week_key] = week_overrides
                try:
                    save_week_override_db(week_key, manual_hid, week_overrides[manual_hid])
                except Exception as exc:
                    st.error(f"Could not permanently save this-week move: {exc}")

                adjusted_plan = build_week_plan(
                    st.session_state.clients,
                    pd.Timestamp(week_start_input),
                    daily_capacity_minutes=daily_capacity,
                    max_appointments_per_day=max_appointments,
                    week_overrides=week_overrides,
                )
                adjusted_plan = assign_times_to_weekly_plan(
                    adjusted_plan,
                    jen_start=jen_start,
                    haley_start=haley_start,
                    travel_buffer_minutes=travel_buffer,
                    service_buffer_minutes=service_buffer,
                )
                st.session_state.week_route_plans[week_key] = adjusted_plan
                st.session_state.week_plan_statuses[week_key] = "draft"
                st.session_state.week_plan_fingerprints[week_key] = current_fingerprint
                try:
                    save_week_draft_db(
                        week_key,
                        adjusted_plan,
                        current_settings,
                        current_fingerprint,
                    )
                    set_week_draft_status_db(week_key, "draft")
                except Exception as exc:
                    st.error(
                        "The move was applied on screen, but the weekly draft "
                        f"could not be saved permanently: {exc}"
                    )
                st.rerun()

        st.markdown("### Client replies / this-week changes")
        st.caption(
            "Use this after you text clients. A day change only affects this selected week; "
            "it does not change the client's normal recurring schedule. Clients you manually "
            "move to a day are kept on that day even if their saved Area labels differ."
        )

        due_households = household_due_table(
            st.session_state.clients,
            pd.Timestamp(week_start_input),
        )
        due_households = due_households[
            due_households["Days Until Due"].notna()
            & (due_households["Days Until Due"] <= 6)
        ].copy()

        if due_households.empty:
            st.info("No due/overdue households available for client-response changes.")
        else:
            response_options = {}
            for _, r in due_households.iterrows():
                hid = str(r.get("Household ID", "") or "")
                label = f"{r.get('Owner', '')} — {r.get('Dogs', '')}"
                response_options[label] = hid

            selected_response_client = st.selectbox(
                "Client / household",
                list(response_options.keys()),
                key=f"reply_client_{week_key}",
            )
            selected_hid = response_options[selected_response_client]
            current_override = week_overrides.get(selected_hid, {})

            reply_statuses = [
                "Not contacted",
                "Awaiting reply",
                "Confirmed",
                "Needs different day",
                "Skip this week",
            ]
            current_status = current_override.get("response_status", "Not contacted")
            if current_status not in reply_statuses:
                current_status = "Not contacted"

            reply_status = st.selectbox(
                "Client response",
                reply_statuses,
                index=reply_statuses.index(current_status),
                key=f"reply_status_{week_key}",
            )

            requested_day = current_override.get("requested_day", "")
            if reply_status == "Needs different day":
                day_choices = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
                default_day_index = (
                    day_choices.index(requested_day)
                    if requested_day in day_choices
                    else 0
                )
                requested_day = st.selectbox(
                    "Move them to",
                    day_choices,
                    index=default_day_index,
                    key=f"requested_day_{week_key}",
                )
            else:
                requested_day = ""

            c_reply1, c_reply2 = st.columns(2)
            with c_reply1:
                if st.button("Apply this-week change", type="primary"):
                    previous_override = week_overrides.get(selected_hid, {})
                    week_overrides[selected_hid] = {
                        "response_status": reply_status,
                        "requested_day": requested_day,
                        "requested_groomer": previous_override.get("requested_groomer", ""),
                        "force_include": previous_override.get("force_include", False),
                    }
                    st.session_state.week_client_responses[week_key] = week_overrides
                    try:
                        save_week_override_db(week_key, selected_hid, week_overrides[selected_hid])
                    except Exception as exc:
                        st.error(f"Could not permanently save this-week change: {exc}")

                    adjusted_plan = build_week_plan(
                        st.session_state.clients,
                        pd.Timestamp(week_start_input),
                        daily_capacity_minutes=daily_capacity,
                        max_appointments_per_day=max_appointments,
                        week_overrides=week_overrides,
                    )
                    adjusted_plan = assign_times_to_weekly_plan(
                        adjusted_plan,
                        jen_start=jen_start,
                        haley_start=haley_start,
                        travel_buffer_minutes=travel_buffer,
                        service_buffer_minutes=service_buffer,
                    )
                    st.session_state.week_route_plans[week_key] = adjusted_plan
                    st.session_state.week_plan_statuses[week_key] = "draft"
                    adjusted_fingerprint = schedule_data_fingerprint(
                        st.session_state.clients
                    )
                    st.session_state.week_plan_fingerprints[week_key] = adjusted_fingerprint
                    try:
                        save_week_draft_db(
                            week_key,
                            adjusted_plan,
                            current_settings,
                            adjusted_fingerprint,
                        )
                        set_week_draft_status_db(week_key, "draft")
                    except Exception as exc:
                        st.error(
                            "The move was applied on screen, but the weekly draft "
                            f"could not be saved permanently: {exc}"
                        )
                    st.rerun()

            with c_reply2:
                if st.button("Clear this week's changes"):
                    st.session_state.week_client_responses[week_key] = {}
                    try:
                        clear_week_overrides_db(week_key)
                    except Exception as exc:
                        st.error(f"Could not clear saved weekly changes: {exc}")
                    cleared_plan = build_week_plan(
                        st.session_state.clients,
                        pd.Timestamp(week_start_input),
                        daily_capacity_minutes=daily_capacity,
                        max_appointments_per_day=max_appointments,
                        week_overrides={},
                        exclude_household_ids=already_scheduled_households,
                    )
                    cleared_plan = assign_times_to_weekly_plan(
                        cleared_plan,
                        jen_start=jen_start,
                        haley_start=haley_start,
                        travel_buffer_minutes=travel_buffer,
                        service_buffer_minutes=service_buffer,
                    )
                    st.session_state.week_route_plans[week_key] = cleared_plan
                    st.session_state.week_plan_statuses[week_key] = "draft"
                    cleared_fingerprint = schedule_data_fingerprint(
                        st.session_state.clients
                    )
                    st.session_state.week_plan_fingerprints[week_key] = cleared_fingerprint
                    try:
                        save_week_draft_db(
                            week_key,
                            cleared_plan,
                            current_settings,
                            cleared_fingerprint,
                        )
                        set_week_draft_status_db(week_key, "draft")
                    except Exception as exc:
                        st.error(
                            "The changes were cleared on screen, but the weekly draft "
                            f"could not be saved permanently: {exc}"
                        )
                    st.rerun()

            if week_overrides:
                st.markdown("#### Active this-week adjustments")
                adjustment_rows = []
                for hid, override in week_overrides.items():
                    match = due_households[
                        due_households["Household ID"].astype(str) == str(hid)
                    ]
                    owner = match.iloc[0]["Owner"] if not match.empty else hid
                    dogs = match.iloc[0]["Dogs"] if not match.empty else ""
                    adjustment_rows.append({
                        "Client": owner,
                        "Dogs": dogs,
                        "Response": override.get("response_status", ""),
                        "Requested Day": override.get("requested_day", ""),
                        "Groomer": override.get("requested_groomer", ""),
                    })
                st.dataframe(
                    pd.DataFrame(adjustment_rows),
                    use_container_width=True,
                    hide_index=True,
                )

        # Never silently lose a due client. Show every overdue / due-this-week
        # household that did not fit the current generated draft.
        due_for_week = household_due_table(
            st.session_state.clients,
            pd.Timestamp(week_start_input),
        )
        due_for_week = due_for_week[
            due_for_week["Days Until Due"].notna()
            & (due_for_week["Days Until Due"] <= 6)
        ].copy()

        scheduled_ids = set(valid["Household ID"].astype(str).tolist())
        skipped_ids = {
            str(hid)
            for hid, override in week_overrides.items()
            if override.get("response_status") == "Skip this week"
        }
        unplaced_due = due_for_week[
            ~due_for_week["Household ID"].astype(str).isin(scheduled_ids | skipped_ids)
        ].copy()

        if not unplaced_due.empty:
            st.warning(
                f"{len(unplaced_due)} due/overdue household(s) did not fit the current draft."
            )
            st.markdown("#### Due but not placed")
            st.dataframe(
                unplaced_due[[
                    "Owner", "Dogs", "Area", "Groomer", "Status", "Minutes", "Price"
                ]],
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                "Use Add a client to this week above to place any of these manually."
            )

        with st.expander("View full weekly table"):
            st.dataframe(
                valid[
                    [
                        "Date",
                        "Day",
                        "Groomer",
                        "Area Cluster",
                        "Start Time",
                        "End Time",
                        "Owner",
                        "Dogs",
                        "Status",
                        "Minutes",
                        "Price",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

        st.markdown("#### Day summaries")
        day_summary = (
            active_valid.groupby(["Date", "Day", "Groomer", "Area Cluster"], dropna=False)
            .agg(
                Appointments=("Owner", "count"),
                Minutes=("Minutes", "sum"),
                Revenue=("Price", "sum"),
            )
            .reset_index()
        )

        st.dataframe(
            day_summary,
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("#### Open capacity by groomer/day")
        capacity_rows = []

        for day_ts in week_dates(pd.Timestamp(week_start_input)):
            day_date = day_ts.date()
            day_name = day_ts.strftime("%A")

            for groomer, allowed_days in WORKDAYS.items():
                if day_name not in allowed_days:
                    continue

                used = active_valid[
                    (active_valid["Date"] == day_date)
                    & (active_valid["Groomer"] == groomer)
                ]["Minutes"].sum()

                capacity_rows.append({
                    "Date": day_date,
                    "Day": day_name,
                    "Groomer": groomer,
                    "Used Minutes": int(used),
                    "Open Minutes": max(int(daily_capacity - used), 0),
                })

        capacity_df = pd.DataFrame(capacity_rows)
        st.dataframe(
            capacity_df,
            use_container_width=True,
            hide_index=True,
        )

        st.download_button(
            "Download weekly draft CSV",
            valid.to_csv(index=False).encode("utf-8"),
            file_name="weekly_route_draft.csv",
            mime="text/csv",
        )

        st.info(
            "This is a draft scheduler, not a final optimized route yet. "
            "The next routing upgrade can use actual drive times between client addresses."
        )


# ---------- Client manager ----------

with clients_tab:
    if supabase_configured() and st.session_state.clients.empty:
        st.info(
            "Your private database is connected and currently empty. "
            "Add your first real dog below."
        )

    st.markdown("### Add a household")
    st.caption(
        "Enter the client once, then add all of their dogs together. "
        "The app saves each dog separately but keeps them grouped as one household stop."
    )

    household_dog_count = st.number_input(
        "How many dogs are in this household?",
        min_value=1,
        max_value=6,
        value=1,
        step=1,
        key="new_household_dog_count",
    )

    with st.form("add_household_form", clear_on_submit=True):
        st.markdown("#### Client / household")

        r1c1, r1c2 = st.columns(2)

        with r1c1:
            new_owner = st.text_input("Owner name")

        with r1c2:
            new_phone = st.text_input("Phone")

        r2c1, r2c2 = st.columns(2)

        with r2c1:
            new_area = st.text_input("Area")

        with r2c2:
            new_groomer = st.selectbox(
                "Groomer",
                ["Either", "Jen", "Haley"],
                help=(
                    "Choose Either when this household can go on Jen's or Haley's route."
                ),
            )

        new_full_address = st.text_input(
            "Full address",
            placeholder="123 Main St, Conroe, TX 77301",
            help=(
                "Paste the whole address in one line. The app will split it into "
                "street, city, state, and ZIP when you save."
            ),
        )

        household_override = st.number_input(
            "Household total-time override (optional)",
            min_value=0,
            value=0,
            step=15,
            help=(
                "Only use this if the combined household appointment takes a different "
                "amount of time than the dogs' individual groom times added together."
            ),
        )

        new_notes = st.text_area("Household notes")

        st.markdown("#### Dogs")
        dog_entries = []

        for dog_number in range(1, int(household_dog_count) + 1):
            st.markdown(f"**Dog {dog_number}**")

            d1, d2 = st.columns(2)
            with d1:
                dog_name = st.text_input(
                    "Dog name",
                    key=f"new_dog_name_{dog_number}",
                )
            with d2:
                dog_last_groom = st.date_input(
                    "Last groom",
                    value=date.today(),
                    key=f"new_dog_last_groom_{dog_number}",
                )

            st.caption("Set Bath and Groom on their own schedules.")

            c1, c2 = st.columns(2)
            with c1:
                bath_frequency = st.selectbox(
                    "Bath every",
                    [0, 2, 3, 4, 5, 6, 8, 10, 12],
                    index=0,
                    format_func=lambda x: "Not scheduled" if x == 0 else f"{x} weeks",
                    key=f"new_dog_bath_frequency_{dog_number}",
                )
            with c2:
                groom_frequency = st.selectbox(
                    "Groom every",
                    [0, 2, 3, 4, 5, 6, 8, 10, 12],
                    index=4,
                    format_func=lambda x: "Not scheduled" if x == 0 else f"{x} weeks",
                    key=f"new_dog_groom_frequency_{dog_number}",
                )

            c3, c4 = st.columns(2)
            with c3:
                last_bath = st.date_input(
                    "Last bath",
                    value=dog_last_groom,
                    key=f"new_dog_last_bath_{dog_number}",
                )
            with c4:
                last_groom_service = st.date_input(
                    "Last full groom",
                    value=dog_last_groom,
                    key=f"new_dog_last_groom_service_{dog_number}",
                )

            if bath_frequency and groom_frequency:
                service_pattern = "Scheduled Bath + Groom"
            elif bath_frequency:
                service_pattern = "Bath only"
            else:
                service_pattern = "Groom only"

            next_service = "Groom"

            b1, b2 = st.columns(2)
            with b1:
                bath_price = st.number_input(
                    "Bath price",
                    min_value=0.0,
                    value=0.0,
                    step=5.0,
                    key=f"new_dog_bath_price_{dog_number}",
                )
            with b2:
                bath_minutes = st.number_input(
                    "Bath time (minutes)",
                    min_value=0,
                    value=0,
                    step=15,
                    key=f"new_dog_bath_minutes_{dog_number}",
                )

            g1, g2 = st.columns(2)
            with g1:
                groom_price = st.number_input(
                    "Full groom price",
                    min_value=0.0,
                    value=0.0,
                    step=5.0,
                    key=f"new_dog_groom_price_{dog_number}",
                )
            with g2:
                groom_minutes = st.number_input(
                    "Full groom time (minutes)",
                    min_value=0,
                    value=0,
                    step=15,
                    key=f"new_dog_groom_minutes_{dog_number}",
                )

            dog_entries.append({
                "Dog": dog_name,
                "Last Groom": dog_last_groom,
                "Frequency Weeks": (
                    groom_frequency
                    if groom_frequency > 0
                    else bath_frequency
                ),
                "Service Pattern": service_pattern,
                "Next Service": next_service,
                "Bath Frequency Weeks": bath_frequency if bath_frequency > 0 else None,
                "Last Bath": last_bath if bath_frequency > 0 else None,
                "Groom Frequency Weeks": groom_frequency if groom_frequency > 0 else None,
                "Last Groom Service": last_groom_service if groom_frequency > 0 else None,
                "Bath Price": bath_price if bath_price > 0 else None,
                "Bath Minutes": bath_minutes if bath_minutes > 0 else None,
                "Groom Price": groom_price if groom_price > 0 else None,
                "Groom Minutes": groom_minutes if groom_minutes > 0 else None,
            })

        submitted = st.form_submit_button("Save household")

        if submitted:
            missing_names = [
                str(i + 1)
                for i, dog in enumerate(dog_entries)
                if not str(dog["Dog"]).strip()
            ]

            missing_service_details = []
            for i, dog in enumerate(dog_entries):
                if dog["Bath Frequency Weeks"]:
                    if not dog["Bath Price"] or not dog["Bath Minutes"]:
                        missing_service_details.append(
                            f"Dog {i + 1}: bath price/time"
                        )
                if dog["Groom Frequency Weeks"]:
                    if not dog["Groom Price"] or not dog["Groom Minutes"]:
                        missing_service_details.append(
                            f"Dog {i + 1}: groom price/time"
                        )
                if not dog["Bath Frequency Weeks"] and not dog["Groom Frequency Weeks"]:
                    missing_service_details.append(
                        f"Dog {i + 1}: choose a bath or groom schedule"
                    )

            if not new_owner.strip():
                st.error("Owner name is required.")
            elif missing_names:
                st.error(
                    "Enter a dog name for dog "
                    + ", ".join(missing_names)
                    + "."
                )
            elif missing_service_details:
                st.error(
                    "Complete the required service pricing/time: "
                    + "; ".join(missing_service_details)
                )
            else:
                household_id = get_or_create_household_id(
                    new_owner,
                    st.session_state.clients,
                )

                new_address, new_city, new_state, new_zip = parse_full_address(
                    new_full_address
                )

                new_rows = []

                for dog in dog_entries:
                    new_rows.append({
                        "Owner": new_owner.strip(),
                        "Dog": str(dog["Dog"]).strip(),
                        "Household ID": household_id,
                        "Phone": new_phone.strip(),
                        "Area": new_area.strip(),
                        "Groomer": new_groomer,
                        "Last Groom": pd.Timestamp(dog["Last Groom"]),
                        "Frequency Weeks": dog["Frequency Weeks"],
                        "Service Pattern": dog["Service Pattern"],
                        "Next Service": dog["Next Service"],
                        "Bath Frequency Weeks": dog["Bath Frequency Weeks"],
                        "Last Bath": (
                            pd.Timestamp(dog["Last Bath"])
                            if dog["Last Bath"] is not None
                            else pd.NaT
                        ),
                        "Groom Frequency Weeks": dog["Groom Frequency Weeks"],
                        "Last Groom Service": (
                            pd.Timestamp(dog["Last Groom Service"])
                            if dog["Last Groom Service"] is not None
                            else pd.NaT
                        ),
                        "Bath Price": dog["Bath Price"],
                        "Bath Minutes": dog["Bath Minutes"],
                        "Groom Price": dog["Groom Price"],
                        "Groom Minutes": dog["Groom Minutes"],
                        # Legacy fields remain populated for backward compatibility.
                        "Price": dog["Groom Price"] or dog["Bath Price"],
                        "Minutes": dog["Groom Minutes"] or dog["Bath Minutes"],
                        "Household Override Minutes": (
                            household_override if household_override > 0 else None
                        ),
                        "Address": new_address.strip(),
                        "City": new_city.strip(),
                        "State": new_state.strip(),
                        "ZIP": new_zip.strip(),
                        "Latitude": None,
                        "Longitude": None,
                        "Last Contacted": pd.NaT,
                        "Notes": new_notes.strip(),
                    })

                if supabase_configured():
                    try:
                        get_supabase.clear()
                        sb = get_supabase()
                        payloads = [dog_row_to_db(row) for row in new_rows]
                        sb.table("dogs").insert(payloads).execute()
                        st.session_state.clients = load_dogs_from_db()

                        dog_names = ", ".join(row["Dog"] for row in new_rows)
                        st.success(
                            f"Saved {new_owner} with {len(new_rows)} dog(s): {dog_names}."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not save household: {exc}")
                else:
                    st.session_state.clients = pd.concat(
                        [
                            st.session_state.clients,
                            pd.DataFrame(new_rows),
                        ],
                        ignore_index=True,
                    )
                    st.success(
                        f"Saved {new_owner} with {len(new_rows)} dog(s) for this session."
                    )
                    st.rerun()

    st.markdown("### Edit an existing dog")
    st.caption(
        "Use this form for normal edits. It is easier than editing the wide spreadsheet."
    )

    if st.session_state.clients.empty:
        st.info("No saved dogs yet.")
    else:
        client_options = []
        option_to_index = {}

        for idx, row in st.session_state.clients.reset_index(drop=True).iterrows():
            label = f"{row.get('Owner', '')} — {row.get('Dog', '')}"
            client_options.append(label)
            option_to_index[label] = idx

        selected_client = st.selectbox(
            "Choose client / dog",
            client_options,
            key="edit_existing_dog_selector",
        )

        selected_idx = option_to_index[selected_client]
        selected_row = st.session_state.clients.reset_index(drop=True).iloc[selected_idx]

        current_full_address = client_full_address(selected_row)

        with st.form("edit_existing_dog_form"):
            e1, e2 = st.columns(2)
            with e1:
                edit_owner = st.text_input(
                    "Owner name",
                    value=str(selected_row.get("Owner", "") or ""),
                )
            with e2:
                edit_dog = st.text_input(
                    "Dog name",
                    value=str(selected_row.get("Dog", "") or ""),
                )

            e3, e4 = st.columns(2)
            with e3:
                edit_phone = st.text_input(
                    "Phone",
                    value=str(selected_row.get("Phone", "") or ""),
                )
            with e4:
                groomer_options = ["Either", "Jen", "Haley"]
                current_groomer = str(selected_row.get("Groomer", "") or "")
                groomer_index = (
                    groomer_options.index(current_groomer)
                    if current_groomer in groomer_options
                    else 0
                )
                edit_groomer = st.selectbox(
                    "Groomer",
                    groomer_options,
                    index=groomer_index,
                )

            e5, e6 = st.columns(2)
            with e5:
                edit_area = st.text_input(
                    "Area",
                    value=str(selected_row.get("Area", "") or ""),
                )
            with e6:
                freq_options = [2, 3, 4, 5, 6, 8, 10, 12]
                current_freq = selected_row.get("Frequency Weeks")
                try:
                    current_freq = int(current_freq)
                except Exception:
                    current_freq = 4
                freq_index = (
                    freq_options.index(current_freq)
                    if current_freq in freq_options
                    else 2
                )
                edit_frequency = st.selectbox(
                    "Frequency",
                    freq_options,
                    index=freq_index,
                    format_func=lambda x: f"Every {x} weeks",
                )

            last_groom_value = selected_row.get("Last Groom")
            if pd.isna(last_groom_value):
                last_groom_value = date.today()
            else:
                last_groom_value = pd.Timestamp(last_groom_value).date()

            edit_last_groom = st.date_input(
                "Last groom",
                value=last_groom_value,
            )

            st.markdown("##### Service schedule")
            st.caption(
                "Bath and full groom can each have their own recurring schedule."
            )

            def _int_or_zero(value):
                try:
                    if pd.isna(value):
                        return 0
                    return int(value)
                except Exception:
                    return 0

            cadence_options = [0, 2, 3, 4, 5, 6, 8, 10, 12]

            current_bath_freq = _int_or_zero(selected_row.get("Bath Frequency Weeks"))
            current_groom_freq = _int_or_zero(selected_row.get("Groom Frequency Weeks"))

            # Existing pre-v14.2 clients default their prior single cadence to groom.
            if current_bath_freq == 0 and current_groom_freq == 0:
                current_groom_freq = _int_or_zero(selected_row.get("Frequency Weeks")) or 4

            f1, f2 = st.columns(2)
            with f1:
                edit_bath_frequency = st.selectbox(
                    "Bath every",
                    cadence_options,
                    index=(
                        cadence_options.index(current_bath_freq)
                        if current_bath_freq in cadence_options
                        else 0
                    ),
                    format_func=lambda x: "Not scheduled" if x == 0 else f"{x} weeks",
                )
            with f2:
                edit_groom_frequency = st.selectbox(
                    "Groom every",
                    cadence_options,
                    index=(
                        cadence_options.index(current_groom_freq)
                        if current_groom_freq in cadence_options
                        else 0
                    ),
                    format_func=lambda x: "Not scheduled" if x == 0 else f"{x} weeks",
                )

            current_last_bath = selected_row.get("Last Bath")
            if pd.isna(current_last_bath):
                current_last_bath = selected_row.get("Last Groom")
            current_last_groom_service = selected_row.get("Last Groom Service")
            if pd.isna(current_last_groom_service):
                current_last_groom_service = selected_row.get("Last Groom")

            d1, d2 = st.columns(2)
            with d1:
                edit_last_bath = st.date_input(
                    "Last bath",
                    value=(
                        pd.Timestamp(current_last_bath).date()
                        if not pd.isna(current_last_bath)
                        else date.today()
                    ),
                )
            with d2:
                edit_last_groom_service = st.date_input(
                    "Last full groom",
                    value=(
                        pd.Timestamp(current_last_groom_service).date()
                        if not pd.isna(current_last_groom_service)
                        else date.today()
                    ),
                )

            if edit_bath_frequency and edit_groom_frequency:
                edit_pattern = "Scheduled Bath + Groom"
            elif edit_bath_frequency:
                edit_pattern = "Bath only"
            else:
                edit_pattern = "Groom only"

            # Kept only for backward compatibility; calendar due dates drive the planner.
            edit_next_service = "Groom"

            bp = selected_row.get("Bath Price")
            bm = selected_row.get("Bath Minutes")
            gp = selected_row.get("Groom Price")
            gm = selected_row.get("Groom Minutes")

            # Existing pre-v14 clients use legacy Price/Minutes as their groom defaults.
            if pd.isna(gp):
                gp = selected_row.get("Price")
            if pd.isna(gm):
                gm = selected_row.get("Minutes")

            def _num_or_zero(value):
                try:
                    if pd.isna(value):
                        return 0.0
                    return float(value)
                except Exception:
                    return 0.0

            s1, s2 = st.columns(2)
            with s1:
                edit_bath_price = st.number_input(
                    "Bath price",
                    min_value=0.0,
                    value=_num_or_zero(bp),
                    step=5.0,
                )
            with s2:
                edit_bath_minutes = st.number_input(
                    "Bath time (minutes)",
                    min_value=0,
                    value=int(_num_or_zero(bm)),
                    step=15,
                )

            s3, s4 = st.columns(2)
            with s3:
                edit_groom_price = st.number_input(
                    "Full groom price",
                    min_value=0.0,
                    value=_num_or_zero(gp),
                    step=5.0,
                )
            with s4:
                edit_groom_minutes = st.number_input(
                    "Full groom time (minutes)",
                    min_value=0,
                    value=int(_num_or_zero(gm)),
                    step=15,
                )

            edit_full_address = st.text_input(
                "Full address",
                value=current_full_address,
            )

            edit_notes = st.text_area(
                "Notes",
                value=str(selected_row.get("Notes", "") or ""),
            )

            save_existing = st.form_submit_button(
                "Save client changes",
                type="primary",
            )

            if save_existing:
                edit_address, edit_city, edit_state, edit_zip = parse_full_address(
                    edit_full_address
                )

                if not edit_owner.strip() or not edit_dog.strip():
                    st.error("Owner name and dog name are required.")
                elif edit_bath_frequency and (
                    edit_bath_price <= 0 or edit_bath_minutes <= 0
                ):
                    st.error("Enter the bath price and bath time.")
                elif edit_groom_frequency and (
                    edit_groom_price <= 0 or edit_groom_minutes <= 0
                ):
                    st.error("Enter the full groom price and groom time.")
                elif not edit_bath_frequency and not edit_groom_frequency:
                    st.error("Choose at least one bath or groom schedule.")
                else:
                    record_id = selected_row.get("Record ID")

                    update_row = {
                        "Owner": edit_owner.strip(),
                        "Dog": edit_dog.strip(),
                        "Household ID": selected_row.get("Household ID"),
                        "Phone": edit_phone.strip(),
                        "Area": edit_area.strip(),
                        "Groomer": edit_groomer,
                        "Last Groom": pd.Timestamp(edit_last_groom),
                        "Frequency Weeks": edit_frequency,
                        "Service Pattern": edit_pattern,
                        "Next Service": edit_next_service,
                        "Bath Frequency Weeks": (
                            edit_bath_frequency if edit_bath_frequency > 0 else None
                        ),
                        "Last Bath": (
                            pd.Timestamp(edit_last_bath)
                            if edit_bath_frequency > 0
                            else pd.NaT
                        ),
                        "Groom Frequency Weeks": (
                            edit_groom_frequency if edit_groom_frequency > 0 else None
                        ),
                        "Last Groom Service": (
                            pd.Timestamp(edit_last_groom_service)
                            if edit_groom_frequency > 0
                            else pd.NaT
                        ),
                        "Bath Price": edit_bath_price if edit_bath_price > 0 else None,
                        "Bath Minutes": edit_bath_minutes if edit_bath_minutes > 0 else None,
                        "Groom Price": edit_groom_price if edit_groom_price > 0 else None,
                        "Groom Minutes": edit_groom_minutes if edit_groom_minutes > 0 else None,
                        "Price": edit_groom_price or edit_bath_price,
                        "Minutes": edit_groom_minutes or edit_bath_minutes,
                        "Household Override Minutes": selected_row.get(
                            "Household Override Minutes"
                        ),
                        "Address": edit_address,
                        "City": edit_city,
                        "State": edit_state,
                        "ZIP": edit_zip,
                        "Latitude": selected_row.get("Latitude"),
                        "Longitude": selected_row.get("Longitude"),
                        "Last Contacted": selected_row.get("Last Contacted"),
                        "Notes": edit_notes.strip(),
                    }

                    if supabase_configured():
                        try:
                            payload = dog_row_to_db(update_row)
                            get_supabase().table("dogs").update(payload).eq(
                                "id",
                                str(record_id),
                            ).execute()
                            st.session_state.clients = load_dogs_from_db()
                            st.success("Client changes saved.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Could not save changes: {exc}")
                    else:
                        for key, value in update_row.items():
                            if key in st.session_state.clients.columns:
                                st.session_state.clients.at[selected_idx, key] = value
                        st.success("Client changes saved for this session.")
                        st.rerun()

    st.markdown("### Advanced spreadsheet editor")
    st.caption(
        "Use this only for bulk edits. For one client, the form above is easier."
    )

    if supabase_configured() and not st.session_state.clients.empty:
        if st.button(
            "Remove exact duplicate dogs",
            help=(
                "Keeps one copy and removes only database rows whose client/dog "
                "details are exactly identical."
            ),
        ):
            try:
                removed = remove_exact_duplicate_dogs_db()
                st.session_state.clients = load_dogs_from_db()
                if removed:
                    st.success(f"Removed {removed} exact duplicate record(s).")
                else:
                    st.info("No exact duplicate records found.")
                st.rerun()
            except Exception as exc:
                st.error(f"Duplicate cleanup failed: {exc}")

    editor_df = clients_for_download(st.session_state.clients).copy()

    if "Record ID" in st.session_state.clients.columns:
        editor_df.insert(
            0,
            "Record ID",
            st.session_state.clients["Record ID"].values,
        )

    original_ids = set(
        str(x)
        for x in editor_df.get("Record ID", pd.Series(dtype=str)).dropna().tolist()
        if str(x).strip()
    )

    edited = st.data_editor(
        editor_df,
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        disabled=["Record ID"] if "Record ID" in editor_df.columns else [],
        column_config={
            "Frequency Weeks": st.column_config.NumberColumn("Frequency Weeks", min_value=1, step=1),
            "Service Pattern": st.column_config.TextColumn("Service Pattern"),
            "Next Service": st.column_config.TextColumn("Next Service"),
            "Bath Frequency Weeks": st.column_config.NumberColumn(
                "Bath Frequency Weeks", min_value=0, step=1
            ),
            "Groom Frequency Weeks": st.column_config.NumberColumn(
                "Groom Frequency Weeks", min_value=0, step=1
            ),
            "Bath Price": st.column_config.NumberColumn("Bath Price", format="$%.2f"),
            "Bath Minutes": st.column_config.NumberColumn("Bath Minutes", min_value=0, step=15),
            "Groom Price": st.column_config.NumberColumn("Groom Price", format="$%.2f"),
            "Groom Minutes": st.column_config.NumberColumn("Groom Minutes", min_value=0, step=15),
            "Price": st.column_config.NumberColumn("Legacy Price", format="$%.2f"),
            "Minutes": st.column_config.NumberColumn("Legacy Minutes", min_value=0, step=15),
            "Household Override Minutes": st.column_config.NumberColumn(
                "Household Override Minutes",
                min_value=0,
                step=15,
            ),
        },
        key="client_editor",
    )

    st.caption(
        "The checkbox on the left is only the table's row selector. "
        "Save dog edits saves the whole edited table, not just the checked row."
    )

    if st.button("Save dog edits", type="primary"):
        if supabase_configured():
            try:
                current_ids = set(
                    str(x)
                    for x in edited.get("Record ID", pd.Series(dtype=str)).dropna().tolist()
                    if str(x).strip()
                )
                deleted_ids = original_ids - current_ids

                if deleted_ids:
                    delete_dog_ids_db(deleted_ids)

                upsert_dogs_db(edited)
                st.session_state.clients = load_dogs_from_db()
                st.success("Changes saved permanently.")
                st.rerun()
            except Exception as exc:
                st.error(f"Database save failed: {exc}")
        else:
            st.session_state.clients = normalize_clients(edited)
            st.success("Changes saved for this session only.")

    if supabase_configured():
        st.caption(
            "Each dog gets its own time and price. Changes here save to the private database."
        )
    else:
        st.caption(
            "Session mode: changes disappear when the app restarts. Connect Supabase for permanent storage."
        )

# ---------- Due list ----------

with due_tab:
    due_date = st.date_input(
        "Due list as of",
        value=date.today(),
        key="due_list_date",
    )

    due_df = household_due_table(
        st.session_state.clients,
        due_date,
    )

    due_filter = st.selectbox(
        "Show",
        [
            "Due this week",
            "Overdue",
            "All clients",
        ],
    )

    due_date_ts = pd.Timestamp(due_date).normalize()
    days_to_week_end = max(4 - due_date_ts.weekday(), 0)

    if due_filter == "Due this week":
        # Due this week = today through Friday only.
        # Overdue clients stay separate in the Overdue view.
        due_df = due_df[
            (due_df["Days Until Due"] >= 0)
            & (due_df["Days Until Due"] <= days_to_week_end)
        ]
    elif due_filter == "Overdue":
        due_df = due_df[
            due_df["Days Until Due"] < 0
        ]

    due_df = due_df.sort_values(
        ["Days Until Due", "Groomer", "Area"],
        ascending=[True, True, True],
    )

    show_cols = [
        "Owner",
        "Dogs",
        "Groomer",
        "Area",
        "Status",
        "Days Until Due",
        "Price",
        "Minutes",
        "Last Contacted",
    ]

    st.dataframe(
        due_df[show_cols],
        use_container_width=True,
        hide_index=True,
    )

    metric1, metric2, metric3 = st.columns(3)

    metric1.metric(
        "Clients shown",
        len(due_df),
    )

    metric2.metric(
        "Potential revenue",
        f"${due_df['Price'].fillna(0).sum():,.0f}",
    )

    overdue_count = int(
        (due_df["Days Until Due"] < 0).sum()
    )

    metric3.metric(
        "Overdue",
        overdue_count,
    )

# ---------- Privacy / export ----------

with export_tab:
    st.markdown("### Keep real client data out of GitHub")

    st.write(
        "Your public GitHub repository should contain only the app code and "
        "sample data. Load your real dog/client CSV through the sidebar when you "
        "open the app, then download the updated private file when you are done."
    )

    st.warning(
        "Do not replace sample_clients.csv in the public repository with your "
        "real client list."
    )

    private_export = clients_for_download(
        st.session_state.clients
    ).to_csv(index=False).encode("utf-8")

    st.download_button(
        "Download my current private dog/client file",
        private_export,
        file_name="grooming_dogs_private.csv",
        mime="text/csv",
    )

    if maps_key:
        missing_coords = st.session_state.clients[
            st.session_state.clients["Latitude"].isna()
            | st.session_state.clients["Longitude"].isna()
        ]

        st.write(
            f"{len(missing_coords)} clients are missing coordinates."
        )

        if (
            not missing_coords.empty
            and st.button("Geocode missing addresses")
        ):
            updated = st.session_state.clients.copy()
            progress = st.progress(0)

            for count, (idx, row) in enumerate(
                missing_coords.iterrows(),
                start=1,
            ):
                address = client_full_address(row)

                if address:
                    try:
                        lat, lon = geocode_address(
                            address,
                            maps_key,
                        )

                        if lat is not None:
                            updated.at[idx, "Latitude"] = lat
                            updated.at[idx, "Longitude"] = lon
                    except Exception:
                        pass

                progress.progress(
                    count / len(missing_coords)
                )

            st.session_state.clients = updated
            st.success("Geocoding finished for this session.")

    with st.expander("Connect permanent private database"):
        st.write("1. Create a Supabase project.")
        st.write("2. Run the included `supabase_schema.sql` in the Supabase SQL editor.")
        st.write("3. In Streamlit → App settings → Secrets, add:")
        st.code(
            'SUPABASE_URL = "https://YOURPROJECT.supabase.co"\n'
            'SUPABASE_SERVICE_ROLE_KEY = "your-service-role-key"\n'
            'APP_PASSWORD = "choose-a-strong-password"'
        )
        st.write(
            "Never put the service-role key in GitHub. Streamlit Secrets keeps it server-side."
        )

    with st.expander("Optional password protection"):
        st.write(
            "In Streamlit app settings → Secrets, add:"
        )
        st.code('APP_PASSWORD = "choose-a-password"')

    with st.expander("Optional Google Maps"):
        st.code('GOOGLE_MAPS_API_KEY = "your-key-here"')

st.caption(
    "Mobile Grooming Planner v24.3 · private Supabase data · recurring service schedules · "
    "weekly/monthly planning · completion tracking · optional Google Maps routing."
)

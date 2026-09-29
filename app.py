
import os
import json
import re
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
    page_title="Mobile Grooming Planner v17",
    page_icon="🐾",
    layout="wide",
)

st.title("🐾 Mobile Grooming Planner v17")
st.caption("Private client manager + due-date intelligence + cancellation filling + optional real drive-time scoring.")

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

def route_metrics(origin, destination, key):
    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    payload = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": float(origin[0]),
                    "longitude": float(origin[1]),
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": float(destination[0]),
                    "longitude": float(destination[1]),
                }
            }
        },
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
                start_time = str(row.get("Start Time", "") or "").strip()
                end_time = str(row.get("End Time", "") or "").strip()
                minutes = int(row.get("Minutes", 0) or 0)
                price = float(row.get("Price", 0) or 0)

                if not owner:
                    continue

                dog_text = f" / {dogs}" if dogs else ""
                st.markdown(
                    f"- **{start_time}–{end_time}** · **{owner}{dog_text}** "
                    f"· {minutes} min · ${price:,.0f} · {status}"
                )

            used_minutes = int(groomer_group["Minutes"].fillna(0).sum())
            appt_count = len(groomer_group)
            travel_total = max(appt_count - 1, 0) * int(travel_buffer)
            open_minutes = max(
                int(daily_capacity) - used_minutes - travel_total,
                0,
            )

            st.caption(
                f"{groomer}: {open_minutes} open min remaining "
                f"(using {travel_buffer}-min estimated travel/setup buffers)"
            )

        st.divider()

    st.caption(
        "All scheduled workdays are shown together. Travel/setup time is still "
        "an estimate until real map routing is connected."
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
    st.sidebar.success("Google Maps connected")
else:
    st.sidebar.caption("Google Maps not connected yet.")

clients = st.session_state.clients
appointments = st.session_state.appointments

# ---------- Main tabs ----------

planner_tab, monthly_tab, weekly_tab, clients_tab, due_tab, export_tab = st.tabs(
    [
        "📅 Planner",
        "🗓️ Monthly Planner",
        "📆 Weekly Route Builder",
        "👥 Client Manager",
        "⏰ Due List",
        "🔐 Private Data",
    ]
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
        "Only confirmed weeks count as your real schedule here. Old test drafts stay hidden."
    )
    st.caption(
        "See the whole month at once. This page shows CONFIRMED weekly schedules only, "
        "including spillover weekdays from the month before or after."
    )

    month_pick = st.date_input(
        "Month",
        value=st.session_state.get("monthly_planner_date", date.today()),
        key="monthly_planner_date",
    )
    month_start = pd.Timestamp(month_pick).replace(day=1).normalize()
    month_end = (
        month_start + pd.offsets.MonthEnd(1)
    ).normalize()

    month_plan = month_plan_dataframe(month_start, month_end)

    if month_plan.empty:
        st.info(
            "No confirmed appointments are on this month yet. Build and adjust a week in "
            "Weekly Route Builder, then tap Confirm this week."
        )
    else:
        month_dates = pd.to_datetime(month_plan["Date"], errors="coerce")
        month_only = month_plan[
            (month_dates >= month_start)
            & (month_dates <= month_end)
        ].copy()

        total_appts = len(month_only)
        total_revenue = float(
            month_only.get("Price", pd.Series(dtype=float)).sum()
        )
        total_minutes = int(
            month_only.get("Minutes", pd.Series(dtype=float)).sum()
        )

        mc1, mc2, mc3 = st.columns(3)
        mc1.metric(f"{month_start:%B} appointments", total_appts)
        mc2.metric(f"{month_start:%B} projected revenue", f"${total_revenue:,.0f}")
        mc3.metric(f"{month_start:%B} groom minutes", total_minutes)

        st.caption(
            "The totals above count only the selected month. The schedule below "
            "shows complete Monday-Friday weeks, so spillover days from the prior "
            "or next month still match Weekly Route Builder."
        )

        st.markdown(f"### {month_start:%B %Y}")

        # One compact section per calendar week. This keeps the full month visible
        # without forcing a tiny 5-column phone layout.
        month_plan["_Date Sort"] = pd.to_datetime(month_plan["Date"], errors="coerce")
        monday_series = (
            month_plan["_Date Sort"]
            - pd.to_timedelta(month_plan["_Date Sort"].dt.weekday, unit="D")
        )
        month_plan["_Week Monday"] = monday_series.dt.date

        for week_monday, week_group in month_plan.groupby("_Week Monday", sort=True):
            week_monday_ts = pd.Timestamp(week_monday)
            week_friday_ts = week_monday_ts + pd.Timedelta(days=4)
            week_revenue = float(week_group["Price"].sum()) if "Price" in week_group.columns else 0
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
                    st.markdown(f"**{day_ts:%A · %b %d}** — _open_")
                    continue

                day_rows["_Start Sort"] = day_rows.get(
                    "Start Time",
                    pd.Series([""] * len(day_rows), index=day_rows.index),
                ).apply(clock_sort_minutes)
                day_rows = day_rows.sort_values(
                    ["_Start Sort", "Groomer", "Owner"],
                    kind="stable",
                )

                day_revenue = float(day_rows["Price"].sum()) if "Price" in day_rows.columns else 0
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

                    details = " · ".join(
                        [part for part in [groomer, area] if part]
                    )
                    time_prefix = f"{start_time} · " if start_time else ""
                    dog_text = f" / {dogs}" if dogs else ""

                    st.markdown(
                        f"- **{time_prefix}{owner}{dog_text}**"
                        f"{' · ' + details if details else ''} · ${price:,.0f}"
                    )

            st.divider()

    # Show who is due this month but not yet in any saved monthly appointment.
    month_due = household_due_table(
        st.session_state.clients,
        month_start,
    )

    if not month_due.empty and "Days Until Due" in month_due.columns:
        month_due = month_due.copy()
        month_due["Due Date"] = month_due["Days Until Due"].apply(
            lambda x: (
                month_start + pd.Timedelta(days=float(x))
                if pd.notna(x)
                else pd.NaT
            )
        )

        scheduled_hids = set(
            month_plan.get("Household ID", pd.Series(dtype=str))
            .dropna()
            .astype(str)
            .tolist()
        )

        unscheduled = month_due[
            month_due["Due Date"].notna()
            & (month_due["Due Date"] >= month_start)
            & (month_due["Due Date"] <= month_end)
            & (~month_due["Household ID"].astype(str).isin(scheduled_hids))
        ].copy()

        st.markdown("### Due this month but not scheduled")
        if unscheduled.empty:
            st.success("Everyone currently due this month is already on a saved week.")
        else:
            display_cols = [
                c for c in [
                    "Owner", "Dogs", "Area", "Groomer", "Due Date", "Status",
                    "Minutes", "Price",
                ]
                if c in unscheduled.columns
            ]
            if "Due Date" in unscheduled.columns:
                unscheduled["Due Date"] = pd.to_datetime(
                    unscheduled["Due Date"], errors="coerce"
                ).dt.date
            st.dataframe(
                unscheduled[display_cols].sort_values("Due Date"),
                hide_index=True,
                use_container_width=True,
            )

    st.markdown("### Jump to a week")
    first_monday = month_start - pd.Timedelta(days=month_start.weekday())
    week_choices = []
    cursor = first_monday
    while cursor <= month_end:
        week_choices.append(cursor.date())
        cursor += pd.Timedelta(days=7)

    chosen_week = st.selectbox(
        "Week",
        week_choices,
        format_func=lambda d: f"Week of {pd.Timestamp(d):%b %d}",
        key="monthly_week_jump",
    )

    if st.button("Set Weekly Route Builder to this week"):
        st.session_state["week_builder_date"] = chosen_week
        st.success(
            f"Weekly Route Builder is set to the week of "
            f"{pd.Timestamp(chosen_week):%B %d}. Tap the Weekly Route Builder tab."
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
            "Jen start time",
            value=saved_settings.get("jen_start", time(8, 0)),
            key=f"jen_week_start_{week_key}",
        )

    with t2:
        haley_start = st.time_input(
            "Haley start time",
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

        m1, m2, m3 = st.columns(3)
        m1.metric("Appointments", len(valid))
        m2.metric("Projected revenue", f"${valid['Price'].sum():,.0f}")
        m3.metric("Scheduled minutes", int(valid["Minutes"].sum()))

        st.markdown("### Weekly schedule")
        render_weekly_cards(
            valid,
            daily_capacity=daily_capacity,
            travel_buffer=travel_buffer,
        )

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
            valid.groupby(["Date", "Day", "Groomer", "Area Cluster"], dropna=False)
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

                used = valid[
                    (valid["Date"] == day_date)
                    & (valid["Groomer"] == groomer)
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
            "Overdue + next 2 weeks",
            "Overdue only",
            "All clients",
        ],
    )

    if due_filter == "Overdue + next 2 weeks":
        due_df = due_df[
            due_df["Days Until Due"] <= 14
        ]
    elif due_filter == "Overdue only":
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
    "v8 prototype: private runtime client data + client manager + due list + planner. "
    "A later version can add a persistent private database so edits save automatically."
)

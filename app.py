
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
    page_title="Mobile Grooming Planner v12.9",
    page_icon="🐾",
    layout="wide",
)

st.title("🐾 Mobile Grooming Planner v12.9")
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

    for c in ["Last Groom", "Last Contacted"]:
        df[c] = pd.to_datetime(df[c], errors="coerce")

    for c in [
        "Frequency Weeks",
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
        dogs = ", ".join([str(x) for x in group["Dog"].dropna() if str(x).strip()])
        area = str(group["Area"].dropna().iloc[0]) if group["Area"].notna().any() else ""
        groomer = str(group["Groomer"].dropna().iloc[0]) if group["Groomer"].notna().any() else ""

        # Household is considered due by the most urgent dog.
        days_until_due = group["Days Until Due"].min() if group["Days Until Due"].notna().any() else None

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

        override = group["Household Override Minutes"].dropna()
        if not override.empty and float(override.iloc[0]) > 0:
            total_minutes = float(override.iloc[0])
        else:
            total_minutes = group["Minutes"].fillna(0).sum()

        total_price = group["Price"].fillna(0).sum()

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
    return group[["Owner", "Dog", "Minutes", "Price", "Frequency Weeks", "Last Groom"]]


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

    sorted_plan = valid_plan.sort_values(
        ["Date", "Groomer", "Start Time", "Owner"],
        ascending=[True, True, True, True],
    ).copy()

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
):
    output_columns = [
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

    # Automatic weekly planning considers overdue clients and clients due
    # within the next three weeks.
    pool = due[due["Days Until Due"] <= 21].copy()
    pool["Weekly Score"] = pool.apply(schedule_score, axis=1)

    days = week_dates(pd.Timestamp(week_start))
    scheduled_households = set()
    results = []

    for day_ts in days:
        day_date = day_ts.date()
        day_name = day_ts.strftime("%A")

        for groomer, allowed_days in WORKDAYS.items():
            if day_name not in allowed_days:
                continue

            groomer_pool = pool[
                (
                    (pool["Groomer"] == groomer)
                    | (pool["Groomer"].astype(str).str.strip().str.lower() == "either")
                )
                & (~pool["Owner"].isin(scheduled_households))
                & (pool["Minutes"].notna())
            ].copy()

            if groomer_pool.empty:
                results.append({
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

            # ONE primary area per groomer/day.
            preferred_area = choose_area_for_day(groomer_pool)

            area_pool = groomer_pool[
                groomer_pool["Area"].astype(str).str.strip().str.lower()
                == str(preferred_area).strip().lower()
            ].copy()

            area_pool = area_pool.sort_values(
                ["Weekly Score", "Days Until Due", "Price"],
                ascending=[False, True, False],
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
                scheduled_households.add(row["Owner"])

            if not selected:
                results.append({
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
                        "Date": day_date,
                        "Day": day_name,
                        "Groomer": groomer,
                        "Area Cluster": preferred_area or row["Area"],
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

    if db_col in {"last_groom", "last_contacted"}:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date().isoformat()

    if db_col in {
        "frequency_weeks",
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

planner_tab, weekly_tab, clients_tab, due_tab, export_tab = st.tabs(
    ["📅 Planner", "🗓️ Weekly Route Builder", "👥 Client Manager", "⏰ Due List", "🔐 Private Data"]
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

def household_due_table(dog_df, target_date):
    """Aggregate dog-level rows into one schedulable household appointment."""
    due = calculate_due_fields(dog_df, target_date).copy()

    rows = []
    for household_id, group in due.groupby("Household ID", dropna=False):
        group = group.copy()

        owner = str(group["Owner"].dropna().iloc[0]) if group["Owner"].notna().any() else ""
        dogs = ", ".join([str(x) for x in group["Dog"].dropna() if str(x).strip()])
        area = str(group["Area"].dropna().iloc[0]) if group["Area"].notna().any() else ""
        groomer = str(group["Groomer"].dropna().iloc[0]) if group["Groomer"].notna().any() else ""

        # Household is considered due by the most urgent dog.
        days_until_due = group["Days Until Due"].min() if group["Days Until Due"].notna().any() else None

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

        override = group["Household Override Minutes"].dropna()
        if not override.empty and float(override.iloc[0]) > 0:
            total_minutes = float(override.iloc[0])
        else:
            total_minutes = group["Minutes"].fillna(0).sum()

        total_price = group["Price"].fillna(0).sum()

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

    return pd.DataFrame(rows)

def household_member_detail(dog_df, household_id):
    group = dog_df[dog_df["Household ID"] == household_id].copy()
    return group[["Owner", "Dog", "Minutes", "Price", "Frequency Weeks", "Last Groom"]]


# ---------- Weekly route builder ----------

with weekly_tab:
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

    week_start_input = st.date_input(
        "Week of",
        value=date.today(),
        key="week_builder_date",
    )

    c1, c2 = st.columns(2)

    with c1:
        daily_capacity = st.selectbox(
            "Approximate grooming minutes available per groomer/day",
            [300, 360, 420, 480, 540],
            index=2,
            format_func=lambda x: f"{x} minutes ({x/60:.1f} hrs)",
        )

    with c2:
        max_appointments = st.selectbox(
            "Maximum appointments per groomer/day",
            [3, 4, 5, 6],
            index=1,
        )

    st.markdown("#### Time assumptions")

    t1, t2 = st.columns(2)

    with t1:
        jen_start = st.time_input(
            "Jen start time",
            value=time(8, 0),
            key="jen_week_start",
        )

    with t2:
        haley_start = st.time_input(
            "Haley start time",
            value=time(8, 0),
            key="haley_week_start",
        )

    t3, t4 = st.columns(2)

    with t3:
        travel_buffer = st.selectbox(
            "Travel / setup buffer",
            [10, 15, 20, 25, 30, 45],
            index=2,
            format_func=lambda x: f"{x} min",
        )

    with t4:
        service_buffer = st.selectbox(
            "Extra service buffer per household",
            [0, 5, 10, 15],
            index=0,
            format_func=lambda x: f"{x} min",
        )

    weekly_plan = build_week_plan(
        st.session_state.clients,
        pd.Timestamp(week_start_input),
        daily_capacity_minutes=daily_capacity,
        max_appointments_per_day=max_appointments,
    )

    weekly_plan = assign_times_to_weekly_plan(
        weekly_plan,
        jen_start=jen_start,
        haley_start=haley_start,
        travel_buffer_minutes=travel_buffer,
        service_buffer_minutes=service_buffer,
    )

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

    st.markdown("### Add a dog")
    st.caption("One row = one dog. Household ID is automatic, and you can paste the full address in one line.")

    with st.form("add_dog_form", clear_on_submit=True):
        r1c1, r1c2, r1c3 = st.columns(3)

        with r1c1:
            new_owner = st.text_input("Owner name")

        with r1c2:
            new_dog = st.text_input("Dog name")

        with r1c3:
            new_phone = st.text_input("Phone")

        r2c1, r2c2, r2c3 = st.columns(3)

        with r2c1:
            st.text_input(
                "Household ID",
                value="Auto-generated when saved",
                disabled=True,
                help=(
                    "The app creates this automatically. If this owner already has another dog, "
                    "the same Household ID will be reused so they stay grouped as one stop."
                ),
            )

        with r2c2:
            new_area = st.text_input("Area")

        with r2c3:
            new_groomer = st.selectbox(
                "Groomer",
                ["Either", "Jen", "Haley"],
                help=(
                    "Choose Either when this dog can go on Jen's or Haley's route. "
                    "The weekly planner can then place the household with either groomer."
                ),
            )

        r3c1, r3c2, r3c3 = st.columns(3)

        with r3c1:
            new_last_groom = st.date_input("Last groom", value=date.today())

        with r3c2:
            new_frequency = st.selectbox(
                "Frequency",
                [2, 3, 4, 5, 6, 8, 10, 12],
                index=2,
                format_func=lambda x: f"Every {x} weeks",
            )

        with r3c3:
            new_minutes = st.number_input(
                "This dog's groom time (minutes)",
                min_value=15,
                value=75,
                step=15,
            )

        r4c1, r4c2 = st.columns(2)

        with r4c1:
            new_price = st.number_input(
                "This dog's price",
                min_value=0.0,
                value=100.0,
                step=5.0,
            )

        with r4c2:
            household_override = st.number_input(
                "Household total-time override (optional)",
                min_value=0,
                value=0,
                step=15,
                help="Example: two dogs total 150 minutes individually, but together you know they take 125."
            )

        new_full_address = st.text_input(
            "Full address",
            placeholder="123 Main St, Conroe, TX 77301",
            help=(
                "Paste the whole address in one line. The app will split it into "
                "street, city, state, and ZIP when you save."
            ),
        )

        new_notes = st.text_area("Notes")

        submitted = st.form_submit_button("Add dog")

        if submitted:
            if not new_owner.strip() or not new_dog.strip():
                st.error("Owner name and dog name are required.")
            else:
                household_id = get_or_create_household_id(
                    new_owner,
                    st.session_state.clients,
                )

                new_address, new_city, new_state, new_zip = parse_full_address(
                    new_full_address
                )

                new_row = {
                    "Owner": new_owner.strip(),
                    "Dog": new_dog.strip(),
                    "Household ID": household_id,
                    "Phone": new_phone.strip(),
                    "Area": new_area.strip(),
                    "Groomer": new_groomer,
                    "Last Groom": pd.Timestamp(new_last_groom),
                    "Frequency Weeks": new_frequency,
                    "Price": new_price,
                    "Minutes": new_minutes,
                    "Household Override Minutes": household_override if household_override > 0 else None,
                    "Address": new_address.strip(),
                    "City": new_city.strip(),
                    "State": new_state.strip(),
                    "ZIP": new_zip.strip(),
                    "Latitude": None,
                    "Longitude": None,
                    "Last Contacted": pd.NaT,
                    "Notes": new_notes.strip(),
                }

                if supabase_configured():
                    try:
                        get_supabase.clear()
                        insert_dog_db(new_row)
                        st.session_state.clients = load_dogs_from_db()
                        st.success(f"{new_dog} added for {new_owner} and saved.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Could not save to database: {exc}")
                else:
                    st.session_state.clients = pd.concat(
                        [st.session_state.clients, pd.DataFrame([new_row])],
                        ignore_index=True,
                    )
                    st.success(f"{new_dog} added for {new_owner} for this session.")
                    st.rerun()

    st.markdown("### Edit dogs")

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
        num_rows="dynamic",
        disabled=["Record ID"] if "Record ID" in editor_df.columns else [],
        column_config={
            "Frequency Weeks": st.column_config.NumberColumn("Frequency Weeks", min_value=1, step=1),
            "Price": st.column_config.NumberColumn("Price", format="$%.2f"),
            "Minutes": st.column_config.NumberColumn("Dog Minutes", min_value=15, step=15),
            "Household Override Minutes": st.column_config.NumberColumn(
                "Household Override Minutes",
                min_value=0,
                step=15,
            ),
        },
        key="client_editor",
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


import os
import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, time

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Mobile Grooming Planner v4", page_icon="🐾", layout="wide")
st.title("🐾 Mobile Grooming Planner v4")
st.caption("Natural-language planning + real-drive-time ready + automatic geocoding + client ranking.")

WORKDAYS = {
    "Jen": ["Tuesday", "Wednesday", "Thursday"],
    "Haley": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
}

CLIENT_COLUMNS = [
    "Client","Area","Groomer","Last Groom","Frequency Weeks","Price","Minutes",
    "Address","City","State","ZIP","Latitude","Longitude","Last Contacted"
]
APPT_COLUMNS = ["Date","Start Time","End Time","Client","Area","Groomer"]

def get_api_key():
    try:
        return st.secrets.get("GOOGLE_MAPS_API_KEY", "")
    except Exception:
        return os.getenv("GOOGLE_MAPS_API_KEY", "")

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
        "origin": {"location": {"latLng": {"latitude": float(origin[0]), "longitude": float(origin[1])}}},
        "destination": {"location": {"latLng": {"latitude": float(destination[0]), "longitude": float(destination[1])}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "units": "IMPERIAL",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }
    result = get_json(url, headers=headers, data=json.dumps(payload).encode("utf-8"))
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

def load_clients(file):
    df = pd.read_csv(file if file else "sample_clients.csv")
    for c in CLIENT_COLUMNS:
        if c not in df.columns:
            df[c] = None
    df["Last Groom"] = pd.to_datetime(df["Last Groom"], errors="coerce")
    df["Last Contacted"] = pd.to_datetime(df["Last Contacted"], errors="coerce")
    for c in ["Frequency Weeks", "Price", "Minutes", "Latitude", "Longitude"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["Next Due"] = df["Last Groom"] + pd.to_timedelta(df["Frequency Weeks"] * 7, unit="D")
    return df

def load_appointments(file):
    df = pd.read_csv(file if file else "sample_appointments.csv")
    for c in APPT_COLUMNS:
        if c not in df.columns:
            df[c] = None
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    df["Start Parsed"] = df["Start Time"].apply(parse_time)
    df["End Parsed"] = df["End Time"].apply(parse_time)
    return df

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

def infer_from_question(question, groomers, areas):
    q = question.lower()
    inferred = {"groomer": None, "area": None, "minutes": None, "time_hint": None}

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

    minute_match = re.search(r"\b(\d{2,3})\s*(?:min|mins|minute|minutes)\b", q)
    if minute_match:
        inferred["minutes"] = int(minute_match.group(1))

    if "hour and a half" in q or "90 minute" in q:
        inferred["minutes"] = 90
    elif "two hours" in q or "2 hours" in q:
        inferred["minutes"] = 120
    elif "one hour" in q or "1 hour" in q:
        inferred["minutes"] = 60

    return inferred

def client_full_address(row):
    pieces = [
        str(row.get("Address") or "").strip(),
        str(row.get("City") or "").strip(),
        str(row.get("State") or "").strip(),
        str(row.get("ZIP") or "").strip(),
    ]
    return ", ".join([p for p in pieces if p and p.lower() != "nan"])

def nearest_booked_neighbors(day_appts, target_time):
    before = None
    after = None
    timed = day_appts.dropna(subset=["Start Parsed", "End Parsed"]).copy().sort_values("Start Parsed")
    for _, row in timed.iterrows():
        if row["End Parsed"] <= target_time:
            before = row["Client"]
        elif row["Start Parsed"] >= target_time and after is None:
            after = row["Client"]
    return before, after

def coords_for(client_name, clients):
    if not client_name:
        return None
    match = clients[clients["Client"] == client_name]
    if match.empty:
        return None
    row = match.iloc[0]
    if pd.isna(row["Latitude"]) or pd.isna(row["Longitude"]):
        return None
    return (float(row["Latitude"]), float(row["Longitude"]))

def score_candidates(candidates, target_date, groomer, area, available_minutes,
                     before_client, after_client, clients, maps_key):
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

        if row["Groomer"] == groomer:
            score += 30
            reasons.append(f"assigned to {groomer}")
        else:
            score -= 100

        if area != "Any":
            if str(row["Area"]).lower() == area.lower():
                score += 35
                reasons.append("same area")
            else:
                score -= 15

        if pd.notna(row["Minutes"]) and row["Minutes"] <= available_minutes:
            score += 20
            fill_ratio = float(row["Minutes"]) / max(available_minutes, 1)
            score += int(fill_ratio * 10)
        else:
            score -= 100

        if pd.notna(row["Price"]):
            score += min(int(float(row["Price"]) / 15), 15)

        if pd.notna(row["Last Contacted"]):
            days_since_contact = (pd.Timestamp(target_date) - row["Last Contacted"]).days
            if days_since_contact < 3:
                score -= 30
                reasons.append("contacted recently")

        drive_minutes = None
        drive_miles = None

        if maps_key and not pd.isna(row["Latitude"]) and not pd.isna(row["Longitude"]):
            candidate_coords = (float(row["Latitude"]), float(row["Longitude"]))
            legs = []
            try:
                if before_coords:
                    miles, mins = route_metrics(before_coords, candidate_coords, maps_key)
                    if mins is not None:
                        legs.append((miles, mins))
                if after_coords:
                    miles, mins = route_metrics(candidate_coords, after_coords, maps_key)
                    if mins is not None:
                        legs.append((miles, mins))
            except Exception:
                legs = []

            if legs:
                drive_miles = sum(x[0] for x in legs if x[0] is not None)
                drive_minutes = sum(x[1] for x in legs if x[1] is not None)
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

clients_upload = st.sidebar.file_uploader("Upload clients CSV", type=["csv"])
appts_upload = st.sidebar.file_uploader("Upload appointments CSV", type=["csv"])

clients = load_clients(clients_upload)
appointments = load_appointments(appts_upload)
maps_key = get_api_key()

if maps_key:
    st.sidebar.success("Google Maps connected")
else:
    st.sidebar.info("No Maps key yet — the app still works without road-time scoring.")

st.sidebar.header("Planner settings")
target_date = st.sidebar.date_input("Date", value=date.today())

groomers = sorted(clients["Groomer"].dropna().astype(str).unique().tolist())
areas = sorted(clients["Area"].dropna().astype(str).unique().tolist())

selected_groomer = st.sidebar.selectbox("Groomer", groomers or ["Jen"])
selected_area = st.sidebar.selectbox("Area", ["Any"] + areas)
available_minutes = st.sidebar.slider("Available time", 30, 300, 120, 15)

st.subheader("Ask the planner")
question = st.text_input(
    "Type a normal request",
    placeholder="Example: Jen lost her 1 PM Thursday in The Woodlands. Who should I contact?",
)

if question:
    inferred = infer_from_question(question, groomers, areas)
    if inferred["groomer"]:
        selected_groomer = inferred["groomer"]
    if inferred["area"]:
        selected_area = inferred["area"]
    if inferred["minutes"]:
        available_minutes = inferred["minutes"]

    st.caption(
        f"Understood as: {selected_groomer} • {selected_area} • {available_minutes} minutes"
    )

day_name = target_date.strftime("%A")
if day_name not in WORKDAYS.get(selected_groomer, []):
    st.warning(f"{selected_groomer} normally does not work on {day_name}.")

clients["Days Until Due"] = (
    clients["Next Due"].dt.date - target_date
).apply(lambda x: x.days if pd.notna(x) else None)
clients["Status"] = clients["Days Until Due"].apply(due_status)

day_appts = appointments[
    (appointments["Date"] == target_date)
    & (appointments["Groomer"] == selected_groomer)
].copy()

col1, col2 = st.columns(2)

with col1:
    st.markdown("### Booked appointments")
    if day_appts.empty:
        st.info("No appointments loaded for this groomer/date.")
    else:
        st.dataframe(
            day_appts[["Start Time", "End Time", "Client", "Area"]],
            use_container_width=True,
            hide_index=True,
        )

with col2:
    st.markdown("### Location setup")
    missing_coords = clients[clients["Latitude"].isna() | clients["Longitude"].isna()]
    st.write(f"{len(clients) - len(missing_coords)} of {len(clients)} clients have coordinates.")

    if maps_key and not missing_coords.empty:
        if st.button("Geocode missing client addresses"):
            updated = clients.copy()
            progress = st.progress(0)

            for count, (idx, row) in enumerate(missing_coords.iterrows(), start=1):
                address = client_full_address(row)
                if address:
                    try:
                        lat, lon = geocode_address(address, maps_key)
                        if lat is not None:
                            updated.at[idx, "Latitude"] = lat
                            updated.at[idx, "Longitude"] = lon
                    except Exception:
                        pass
                progress.progress(count / len(missing_coords))

            export = updated[CLIENT_COLUMNS].to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download clients with coordinates",
                export,
                file_name="clients_geocoded.csv",
                mime="text/csv",
            )
            st.success("Finished geocoding. Download the updated client file above.")

st.divider()
st.markdown("### Best clients to contact")

target_time = time(13, 0)
if question:
    inferred = infer_from_question(question, groomers, areas)
    if inferred["time_hint"]:
        target_time = inferred["time_hint"]

before_client, after_client = nearest_booked_neighbors(day_appts, target_time)

candidates = clients[
    (clients["Groomer"] == selected_groomer)
    & (clients["Minutes"] <= available_minutes)
    & (clients["Days Until Due"] <= 21)
].copy()

ranked = score_candidates(
    candidates,
    target_date,
    selected_groomer,
    selected_area,
    available_minutes,
    before_client,
    after_client,
    clients,
    maps_key,
)

if ranked.empty:
    st.warning("No matching clients found.")
else:
    top = ranked.head(10).copy()
    display = top[
        ["Client","Area","Status","Price","Minutes","Drive Miles","Drive Minutes","Score","Why"]
    ].copy()
    display["Drive Miles"] = display["Drive Miles"].round(1)
    display["Drive Minutes"] = display["Drive Minutes"].round(0)

    st.dataframe(display, use_container_width=True, hide_index=True)

    best = top.iloc[0]
    st.success(
        f"Best match: {best['Client']} — {best['Status']} — "
        f"{best['Area']} — ${best['Price']:.0f} — {int(best['Minutes'])} min"
    )

    text_message = (
        f"Hi {best['Client']}! We had an opening come up on "
        f"{target_date:%A, %B %d}. Would you like to grab it?"
    )

    st.markdown("#### Suggested client text")
    st.code(text_message)

st.divider()
st.markdown("### Export planner data")
export_clients = clients[CLIENT_COLUMNS].to_csv(index=False).encode("utf-8")
st.download_button(
    "Download current client database",
    export_clients,
    file_name="grooming_clients.csv",
    mime="text/csv",
)

with st.expander("How to add Google Maps"):
    st.write(
        "Add a secret named GOOGLE_MAPS_API_KEY. "
        "When present, the app can geocode addresses and use real road distance/time."
    )

with st.expander("Standing staff rules"):
    st.write("**Jen:** Tuesday–Thursday")
    st.write("**Haley:** Monday–Friday")

st.caption(
    "v4 prototype. Next commercial step: accounts, database storage, saved schedules, "
    "automatic contact history, and a true AI model behind the chat box."
)

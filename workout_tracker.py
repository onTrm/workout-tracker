# app.py
#
# Multi-user Streamlit workout tracker with per-user Google Drive storage.
# - Each user: single JSON file in their own Drive (plans + logs).
# - App uses Google OAuth 2 with drive.file scope; only accesses files it created.
#
# Requirements:
#   pip install streamlit google-auth google-auth-oauthlib google-api-python-client pandas


import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
import io

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

APP_FOLDER_NAME = "WorkoutTracker"
DATA_FILE_NAME = "workout_data.json"
EXERCISE_DB_PATH = "workout_muscle_database.csv"

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
]


# ---------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------

def get_client_config():
    cfg = st.secrets["google_oauth"]
    client_config = {
        "web": {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [cfg["redirect_uri"]],
        }
    }
    return client_config


def get_flow(state=None):
    client_config = get_client_config()
    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        state=state,
    )
    flow.redirect_uri = client_config["web"]["redirect_uris"][0]
    return flow


def get_user_info(creds: Credentials):
    # Use OAuth2 userinfo endpoint
    from googleapiclient.discovery import build as build_oauth2
    oauth2_service = build_oauth2("oauth2", "v2", credentials=creds)
    user_info = oauth2_service.userinfo().get().execute()
    return user_info  # contains "email", "id", etc.


def ensure_google_login():
    """
    Ensure the user is logged in with Google and return (creds, user_info).
    Uses st.session_state["google_creds"] for this session.
    """
    # Check existing creds in session
    creds_dict = st.session_state.get("google_creds")
    creds = None
    if creds_dict:
        creds = Credentials.from_authorized_user_info(creds_dict, SCOPES)

    # Refresh token if possible
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None

    # If still no valid creds, run OAuth flow
    if not creds or not creds.valid:
        query_params = st.experimental_get_query_params()
        if "code" in query_params:
            # Callback from Google: exchange code for tokens
            state = query_params.get("state", [None])[0]
            flow = get_flow(state=state)
            flow.fetch_token(code=query_params["code"][0])
            creds = flow.credentials
            st.session_state["google_creds"] = json.loads(creds.to_json())
            # Clear query params (optional)
            st.experimental_set_query_params()
        else:
            # Start login: build auth URL and show button/link
            flow = get_flow()
            auth_url, state = flow.authorization_url(
                access_type="offline",
                include_granted_scopes="true",
                prompt="consent",
            )
            st.session_state["oauth_state"] = state
            st.markdown("### Sign in with Google")
            st.write(
                "To use this app and store your plans/logs privately in your own "
                "Google Drive, please sign in with Google."
            )
            st.markdown(f"[Sign in with Google]({auth_url})")
            st.stop()

    # At this point we have valid creds
    user_info = get_user_info(creds)
    return creds, user_info


# ---------------------------------------------------------------------
# Drive helpers (folder + JSON file)
# ---------------------------------------------------------------------

def get_drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds)


def get_or_create_app_folder(drive_service):
    # Search for folder with APP_FOLDER_NAME created by this app
    query = (
        f"mimeType='application/vnd.google-apps.folder' "
        f"and name='{APP_FOLDER_NAME}' "
        f"and trashed=false"
    )
    results = (
        drive_service.files()
        .list(q=query, spaces="drive", fields="files(id, name)")
        .execute()
    )
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    # Create folder
    file_metadata = {
        "name": APP_FOLDER_NAME,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = drive_service.files().create(body=file_metadata, fields="id").execute()
    return folder["id"]


def get_or_create_data_file(drive_service, folder_id):
    query = (
        f"name='{DATA_FILE_NAME}' and '{folder_id}' in parents and trashed=false"
    )
    results = (
        drive_service.files()
        .list(q=query, spaces="drive", fields="files(id, name)")
        .execute()
    )
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    # Create new JSON file with default content
    default_data = {"plans": {}, "logs": []}
    buf = io.BytesIO(json.dumps(default_data).encode("utf-8"))
    media = MediaIoBaseUpload(buf, mimetype="application/json")

    file_metadata = {
        "name": DATA_FILE_NAME,
        "mimeType": "application/json",
        "parents": [folder_id],
    }
    new_file = (
        drive_service.files()
        .create(body=file_metadata, media_body=media, fields="id")
        .execute()
    )
    return new_file["id"]


def load_user_data(drive_service, file_id):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    raw = fh.read().decode("utf-8")
    try:
        data = json.loads(raw)
    except Exception:
        data = {"plans": {}, "logs": []}
    # Ensure structure
    data.setdefault("plans", {})
    data.setdefault("logs", [])
    return data


def save_user_data(drive_service, file_id, data):
    raw = json.dumps(data)
    buf = io.BytesIO(raw.encode("utf-8"))
    media = MediaIoBaseUpload(buf, mimetype="application/json", resumable=False)
    updated = (
        drive_service.files()
        .update(fileId=file_id, media_body=media)
        .execute()
    )
    return updated


# ---------------------------------------------------------------------
# Exercise DB
# ---------------------------------------------------------------------

@st.cache_data
def load_exercise_db(path: str):
    df = pd.read_csv(path)
    # Try common column names
    for col in ["Exercise", "exercise", "name", "Name"]:
        if col in df.columns:
            exercises = sorted(df[col].dropna().unique())
            return df, exercises
    # Fallback to first column
    first_col = df.columns[0]
    exercises = sorted(df[first_col].dropna().unique())
    return df, exercises


# ---------------------------------------------------------------------
# Planner UI
# ---------------------------------------------------------------------

def planner_page(data, exercise_options):
    st.header("Workout Planner")

    plans = data["plans"]

    # Editing state
    editing_name = st.session_state.get("edit_plan_name")
    editing_plan = plans.get(editing_name) if editing_name else None

    if editing_plan:
        default_name = editing_name
        default_start_date = date.fromisoformat(editing_plan["start_date"])
        default_days = editing_plan.get("num_days", 28)
    else:
        default_name = ""
        default_start_date = date.today()
        default_days = 28

    st.subheader("Create or edit plan")

    plan_name = st.text_input("Plan name", value=default_name)

    c1, c2 = st.columns(2)
    with c1:
        start_date = st.date_input("Start date", value=default_start_date)
    with c2:
        num_days = st.number_input(
            "Duration (days)", min_value=1, step=1, value=default_days
        )

    st.markdown("#### Daily workouts")

    workouts = {}
    for i in range(int(num_days)):
        day_date = start_date + timedelta(days=i)
        day_iso = day_date.isoformat()
        label = f"{day_date.strftime('%a')} – {day_iso}"

        if editing_plan:
            default_day_exs = editing_plan.get("workouts", {}).get(day_iso, [])
        else:
            default_day_exs = []

        selected = st.multiselect(
            label,
            options=exercise_options,
            default=default_day_exs,
            key=f"day_{day_iso}",
        )
        if selected:
            workouts[day_iso] = selected

    save_col, cancel_col = st.columns(2)

    with save_col:
        if st.button("Save plan", type="primary"):
            if not plan_name.strip():
                st.error("Plan name cannot be empty.")
            else:
                if editing_plan and plan_name != editing_name and editing_name in plans:
                    del plans[editing_name]

                plans[plan_name] = {
                    "name": plan_name,
                    "start_date": start_date.isoformat(),
                    "num_days": int(num_days),
                    "workouts": workouts,
                }
                data["plans"] = plans
                st.session_state["edit_plan_name"] = None
                st.success("Plan updated in memory. Remember to save to Drive (top bar).")

    with cancel_col:
        if st.button("Cancel editing"):
            st.session_state["edit_plan_name"] = None
            st.experimental_rerun()

    st.markdown("---")
    st.subheader("Existing plans")

    if not plans:
        st.info("No plans yet.")
        return

    plan_names = sorted(plans.keys())
    selected_name = st.selectbox("Select a plan", plan_names)
    if selected_name:
        p = plans[selected_name]
        st.markdown(f"**Start date:** {p['start_date']}")
        st.markdown(f"**Duration:** {p['num_days']} days")

        rows = [
            {"Date": d, "Exercises": ", ".join(exs)}
            for d, exs in p["workouts"].items()
        ]
        if rows:
            df_plan = pd.DataFrame(rows).sort_values("Date")
            st.dataframe(df_plan, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            if st.button("Edit this plan"):
                st.session_state["edit_plan_name"] = selected_name
                st.experimental_rerun()
        with c2:
            if st.button("Delete this plan"):
                del plans[selected_name]
                data["plans"] = plans
                st.success("Plan removed from memory. Save to Drive to persist.")
                st.experimental_rerun()


# ---------------------------------------------------------------------
# Logger UI
# ---------------------------------------------------------------------

def logger_page(data):
    st.header("Workout Logger")

    plans = data["plans"]
    if not plans:
        st.info("No plans found. Create one in the Planner.")
        return

    plan_names = sorted(plans.keys())
    active_plan_name = st.selectbox("Active plan", plan_names)
    active_plan = plans[active_plan_name]

    log_date = st.date_input("Log date", value=date.today())
    log_date_iso = log_date.isoformat()

    todays_exs = active_plan["workouts"].get(log_date_iso, [])
    st.subheader(f"Planned workouts for {log_date_iso}")
    if not todays_exs:
        st.info("No workouts assigned for this date in the selected plan.")
        return

    st.write(todays_exs)

    st.markdown("### Log your sets")
    logs = data["logs"]

    for ex in todays_exs:
        with st.expander(ex):
            weight = st.number_input(
                f"{ex} – weight", min_value=0.0, step=1.0,
                key=f"{log_date_iso}_{ex}_weight",
            )
            reps = st.number_input(
                f"{ex} – reps", min_value=0, step=1,
                key=f"{log_date_iso}_{ex}_reps",
            )
            sets = st.number_input(
                f"{ex} – sets", min_value=0, step=1,
                key=f"{log_date_iso}_{ex}_sets",
            )
            if st.button(f"Add set for {ex}", key=f"{log_date_iso}_{ex}_add"):
                if reps <= 0 or sets <= 0:
                    st.warning("Reps and sets must be > 0.")
                else:
                    volume = float(weight) * int(reps) * int(sets)
                    entry = {
                        "date": log_date_iso,
                        "plan": active_plan_name,
                        "exercise": ex,
                        "weight": float(weight),
                        "reps": int(reps),
                        "sets": int(sets),
                        "volume": volume,
                    }
                    logs.append(entry)
                    data["logs"] = logs
                    st.success("Set added. Save to Drive to persist.")

    st.markdown("---")
    st.subheader("Training history")

    if not logs:
        st.info("No logs yet.")
        return

    df = pd.DataFrame(logs)
    df = df.sort_values(["date", "exercise"])
    st.dataframe(df, use_container_width=True)


# ---------------------------------------------------------------------
# Debug / account page
# ---------------------------------------------------------------------

def debug_page(data, user_info):
    st.header("Account / Data (debug)")

    st.markdown("### User info")
    st.json(user_info)

    st.markdown("### In-memory data")
    st.json(data)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Workout Tracker (Drive-backed)", layout="wide")

    creds, user_info = ensure_google_login()
    drive_service = get_drive_service(creds)

    # Get folder + file in Drive for this user
    folder_id = get_or_create_app_folder(drive_service)
    data_file_id = get_or_create_data_file(drive_service, folder_id)

    if "data" not in st.session_state:
        st.session_state["data"] = load_user_data(drive_service, data_file_id)
        st.session_state["data_file_id"] = data_file_id
        st.session_state["folder_id"] = folder_id

    data = st.session_state["data"]

    # Top bar: save to Drive
    with st.sidebar:
        st.markdown(f"**Signed in as:** {user_info.get('email', 'Unknown')}")
        if st.button("Save to Google Drive (overwrite JSON)"):
            save_user_data(drive_service, data_file_id, data)
            st.success("Data saved to your Google Drive JSON file.")

        st.markdown("---")
        page = st.radio("Navigate", ["Planner", "Logger", "Debug"])

    # Exercise DB
    try:
        _, exercise_options = load_exercise_db(EXERCISE_DB_PATH)
    except Exception:
        exercise_options = []

    if page == "Planner":
        planner_page(data, exercise_options)
    elif page == "Logger":
        logger_page(data)
    else:
        debug_page(data, user_info)


if __name__ == "__main__":
    main()

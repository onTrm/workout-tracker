# workout_tracker.py
#
# Multi-user Streamlit workout tracker with per-user Google Drive storage.
# - Each user: single JSON file in their own Drive (plans + logs).
# - App uses Google OAuth 2 with drive.file scope; only accesses files it created.
#
# Requirements:
#   pip install streamlit google-auth google-auth-oauthlib google-api-python-client pandas


import json
import time
from datetime import date, timedelta, datetime

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
# Styling helpers
# ---------------------------------------------------------------------

def apply_custom_style():
    """Inject a bit of custom CSS for tighter, nicer layout."""
    st.markdown(
        """
        <style>
        /* Make the main container a bit wider */
        .block-container {
            max-width: 1100px;
            padding-top: 1.5rem;
        }
        /* General button styling */
        .stButton > button {
            border-radius: 0.5rem;
            padding: 0.25rem 0.9rem;
            font-size: 0.9rem;
        }
        /* Sidebar title spacing */
        section[data-testid="stSidebar"] .block-container {
            padding-top: 1rem;
        }
        /* Make tables and markdown wrap nicely */
        .stMarkdown, .stText, .stDataFrame {
            word-break: break-word;
        }

        /* Responsive adjustments for small screens */
        @media (max-width: 800px) {
            .block-container {
                max-width: 95vw !important;
                padding-left: 0.6rem !important;
                padding-right: 0.6rem !important;
            }
            /* Make buttons full-width for easier tapping */
            .stButton > button {
                width: 100% !important;
                box-sizing: border-box !important;
            }
            /* Make inputs and sliders shrink and wrap */
            input[type="number"], .stSlider > div {
                min-width: 0 !important;
                width: 100% !important;
            }
            /* Ensure dataframes and tables scroll inside container */
            .stDataFrame > div, .stDataFrame table {
                width: 100% !important;
                overflow-x: auto;
            }
            /* Stack Streamlit columns vertically on narrow screens */
            /* Targets the column wrapper produced by st.columns */
            div[data-testid="column"] {
                min-width: 100% !important;
                display: block !important;
            }
            /* Fallback: target common Streamlit column wrapper classes */
            .stColumns > div {
                min-width: 100% !important;
                display: block !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


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
    oauth2_service = build("oauth2", "v2", credentials=creds)
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


def now_ts():
    return int(time.time())


def iso_now():
    return datetime.utcnow().isoformat() + "Z"


def _ensure_timestamps(data: dict):
    # Ensure plans have updated_at and logs have ts
    if not isinstance(data, dict):
        return
    plans = data.setdefault("plans", {})
    for p in plans.values():
        if "updated_at" not in p:
            p["updated_at"] = iso_now()
    logs = data.setdefault("logs", [])
    for e in logs:
        if "ts" not in e:
            e["ts"] = now_ts()


def merge_user_data(local: dict, remote: dict) -> dict:
    """Merge local and remote user data.

    - Plans: choose per-plan the one with the newest `updated_at`.
    - Logs: treat as append-only; deduplicate by (date, exercise, ts).
    """
    local = local or {"plans": {}, "logs": []}
    remote = remote or {"plans": {}, "logs": []}

    # Ensure timestamps exist for correct comparison
    _ensure_timestamps(local)
    _ensure_timestamps(remote)

    merged = {"plans": {}, "logs": []}

    # Merge plans by name, prefer newest updated_at
    plan_names = set(local.get("plans", {}).keys()) | set(remote.get("plans", {}).keys())
    for name in plan_names:
        lp = local.get("plans", {}).get(name)
        rp = remote.get("plans", {}).get(name)
        if lp and rp:
            if lp.get("updated_at", "") >= rp.get("updated_at", ""):
                merged["plans"][name] = lp
            else:
                merged["plans"][name] = rp
        else:
            merged["plans"][name] = lp or rp

    # Merge logs: dedupe by (date, exercise, ts)
    seen = {}
    for e in remote.get("logs", []):
        key = (e.get("date"), e.get("exercise"), e.get("ts"))
        seen[key] = e
    for e in local.get("logs", []):
        key = (e.get("date"), e.get("exercise"), e.get("ts"))
        seen[key] = e

    merged_logs = list(seen.values())
    # Sort logs by date then newest ts first
    try:
        merged_logs.sort(key=lambda x: (x.get("date", ""), -int(x.get("ts", 0))))
    except Exception:
        pass
    merged["logs"] = merged_logs

    return merged


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
# Planner UI (weekly timetable)
# ---------------------------------------------------------------------

def planner_page(data, exercise_options):
    st.header("Workout Planner – Weekly Timetable")

    plans = data["plans"]

    # Editing state
    editing_name = st.session_state.get("edit_plan_name")
    editing_plan = plans.get(editing_name) if editing_name else None

    # Derive defaults
    if editing_plan:
        default_name = editing_name
        default_start_date = date.fromisoformat(editing_plan["start_date"])
        # Prefer explicit num_weeks if present, else infer from num_days
        default_weeks = editing_plan.get("num_weeks")
        if default_weeks is None:
            num_days = editing_plan.get("num_days", 7)
            default_weeks = max(1, int(num_days) // 7)
    else:
        default_name = ""
        default_start_date = date.today()
        default_weeks = 4  # 4-week block as default

    st.subheader("Create or edit weekly plan")

    plan_name = st.text_input("Plan name", value=default_name)

    c1, c2 = st.columns(2)
    with c1:
        start_date = st.date_input("Start date", value=default_start_date)
    with c2:
        num_weeks = st.number_input(
            "Duration (weeks)",
            min_value=1,
            step=1,
            value=default_weeks,
        )

    st.markdown("#### Weekly timetable")
    st.caption(
        "Define what you do on each weekday. The same weekly schedule will repeat "
        "for the selected number of weeks starting from the start date."
    )

    # Build / infer weekly template
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekly_template = {day: [] for day in weekday_names}

    # If editing an existing plan, infer per-weekday defaults from stored workouts
    if editing_plan:
        stored_workouts = editing_plan.get("workouts", {})
        for offset in range(0, editing_plan.get("num_days", int(default_weeks) * 7)):
            d = default_start_date + timedelta(days=offset)
            iso = d.isoformat()
            if iso in stored_workouts:
                wd = d.strftime("%A")
                # Only take the first example we find for that weekday
                if not weekly_template[wd]:
                    weekly_template[wd] = stored_workouts[iso]

    # UI for weekly template
    for day in weekday_names:
        # Layout days in two columns for nicer use of space
        col_left, col_right = st.columns([1, 2])
        with col_left:
            st.markdown(f"**{day}**")
        with col_right:
            default_list = weekly_template.get(day, [])
            # Only allow selection from the exercise DB (no free-text 'other' items)
            selected = st.multiselect(
                f"Exercises for {day}",
                options=exercise_options,
                default=default_list,
                key=f"week_{day}",
            )
            weekly_template[day] = list(selected)

    # Build per-date workout mapping from weekly template
    workouts = {}
    total_days = int(num_weeks) * 7
    for i in range(total_days):
        d = start_date + timedelta(days=i)
        iso = d.isoformat()
        wd = d.strftime("%A")
        day_exs = weekly_template.get(wd, [])
        if day_exs:
            workouts[iso] = day_exs

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
                    "num_weeks": int(num_weeks),
                    "num_days": int(num_weeks) * 7,
                    "workouts": workouts,
                    "updated_at": iso_now(),
                }
                data["plans"] = plans
                st.session_state["edit_plan_name"] = None
                st.success("Plan updated in memory. Remember to save to Drive (sidebar).")

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
        # Prefer num_weeks if present
        num_weeks_display = p.get("num_weeks")
        if num_weeks_display is None:
            num_weeks_display = max(1, int(p.get("num_days", 7)) // 7)
        st.markdown(f"**Duration:** {num_weeks_display} week(s)")

        rows = [
            {"Date": d, "Exercises": ", ".join(exs)}
            for d, exs in p["workouts"].items()
        ]
        if rows:
            df_plan = pd.DataFrame(rows).sort_values("Date")
            st.dataframe(df_plan, use_container_width=True, height=260)

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

    # Compact header row: active plan + log date
    col_plan, col_date = st.columns([2, 1])
    with col_plan:
        plan_names = sorted(plans.keys())
        active_plan_name = st.selectbox("Active plan", plan_names)
    with col_date:
        log_date = st.date_input("Log date", value=date.today())

    active_plan = plans[active_plan_name]
    log_date_iso = log_date.isoformat()

    todays_exs = active_plan["workouts"].get(log_date_iso, [])

    if not todays_exs:
        st.info("No workouts assigned for this date in the selected plan.")
        return

    st.markdown("### Log your sets")

    logs = data["logs"]

    # Header row for inputs (display once)
    header_cols = st.columns([3, 1.5, 1, 1, 1])
    header_cols[0].markdown("**Exercise**")
    header_cols[1].markdown("**Weight**")
    header_cols[2].markdown("**Reps**")
    header_cols[3].markdown("**Sets**")
    header_cols[4].markdown("**RPE**")

    # One row per exercise: name inline + input boxes (no per-row labels)
    for ex in todays_exs:
        row = st.columns([3, 1.5, 1, 1, 1])
        with row[0]:
            st.markdown(f"**{ex}**")
        with row[1]:
            # weight as float
            st.number_input(
                "",
                min_value=0.0,
                step=1.0,
                key=f"{log_date_iso}_{ex}_weight",
                label_visibility="collapsed",
            )
        with row[2]:
            st.number_input(
                "",
                min_value=0,
                step=1,
                key=f"{log_date_iso}_{ex}_reps",
                label_visibility="collapsed",
            )
        with row[3]:
            st.number_input(
                "",
                min_value=0,
                step=1,
                key=f"{log_date_iso}_{ex}_sets",
                label_visibility="collapsed",
            )
        with row[4]:
            st.slider(
                "",
                min_value=1,
                max_value=10,
                value=7,
                key=f"{log_date_iso}_{ex}_rpe",
                label_visibility="collapsed",
            )

    # Single action button for logging all non-empty rows
    if st.button("Log selected sets"):
        any_saved = False
        for ex in todays_exs:
            w = st.session_state.get(f"{log_date_iso}_{ex}_weight", 0.0)
            r = st.session_state.get(f"{log_date_iso}_{ex}_reps", 0)
            s = st.session_state.get(f"{log_date_iso}_{ex}_sets", 0)
            rp = st.session_state.get(f"{log_date_iso}_{ex}_rpe", 7)

            # Only save fully-filled entries (>0 for weight, reps, sets)
            if float(w) > 0 and int(r) > 0 and int(s) > 0:
                volume = float(w) * int(r) * int(s)
                entry = {
                    "date": log_date_iso,
                    "plan": active_plan_name,
                    "exercise": ex,
                    "weight": float(w),
                    "reps": int(r),
                    "sets": int(s),
                        "rpe": int(rp),
                        "volume": volume,
                        "ts": now_ts(),
                }
                # Remove any existing entry for same date+exercise, then append
                logs = [le for le in logs if not (le.get("date") == log_date_iso and le.get("exercise") == ex)]
                logs.append(entry)
                data["logs"] = logs
                any_saved = True

        if any_saved:
            st.success("Saved completed sets.")
        else:
            st.info("No complete entries to save (weight/reps/sets must be > 0).")

    st.markdown("---")
    st.subheader("Training history")

    if not logs:
        st.info("No logs yet.")
        return

    df = pd.DataFrame(logs)

    # Backward compatibility: some older entries may have no RPE
    if "rpe" not in df.columns:
        df["rpe"] = None

    # Drop plan column from view if present
    if "plan" in df.columns:
        df = df.drop(columns=["plan"])

    df = df.sort_values(["date", "exercise"])
    st.dataframe(df, use_container_width=True, height=320)


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
    apply_custom_style()

    creds, user_info = ensure_google_login()
    drive_service = get_drive_service(creds)

    # Get folder + file in Drive for this user
    folder_id = get_or_create_app_folder(drive_service)
    data_file_id = get_or_create_data_file(drive_service, folder_id)

    # Load data from Drive when the session has no data yet, or when the
    # data_file_id differs from what's stored in session (e.g. user logged
    # in on a new device). This ensures users see their saved JSON rather
    # than only ephemeral cached state.
    if ("data" not in st.session_state) or (st.session_state.get("data_file_id") != data_file_id):
        st.session_state["data"] = load_user_data(drive_service, data_file_id)
        st.session_state["data_file_id"] = data_file_id
        st.session_state["folder_id"] = folder_id

    data = st.session_state["data"]

    # Sidebar: save + navigation
    with st.sidebar:
        st.markdown(f"**Signed in as:** {user_info.get('email', 'Unknown')}")
        if st.button("Save to Google Drive (merge & save JSON)"):
            # Fetch remote, merge with local, then save merged data so that
            # multi-device / multi-session edits are preserved.
            try:
                remote = load_user_data(drive_service, data_file_id)
            except Exception:
                remote = {"plans": {}, "logs": []}

            merged = merge_user_data(data, remote)
            # Update session data and persist
            st.session_state["data"] = merged
            save_user_data(drive_service, data_file_id, merged)
            st.success("Data merged with Drive and saved.")

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

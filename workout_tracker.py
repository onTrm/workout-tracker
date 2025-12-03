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
import sqlite3
import os
import uuid
from typing import Optional

try:
    from cryptography.fernet import Fernet
    _FERNET_AVAILABLE = True
except Exception:
    Fernet = None
    _FERNET_AVAILABLE = False

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
            /* Do not force all columns to stack; instead make inputs inline-friendly */
            /* Make number inputs and their +/- buttons render inline and limit width */
            div[data-baseweb="numberinput"], div[data-testid="stNumberInput"] {
                display: inline-flex !important;
                flex-direction: row !important;
                align-items: center !important;
                gap: 0.25rem !important;
            }
            /* Broad selector: target all numeric inputs Streamlit may render */
            input[type="number"], div[data-baseweb="numberinput"] input, div[data-testid="stNumberInput"] input {
                width: 4.4rem !important;
                min-width: 3.2rem !important;
                max-width: 5.5rem !important;
                box-sizing: border-box !important;
                padding: 0.18rem 0.25rem !important;
                font-size: 0.95rem !important;
            }
            /* Fallback selectors for Streamlit's internal classes */
            .stNumberInput, .stNumberInput input {
                display: inline-flex !important;
                width: auto !important;
            }
            /* Make sliders compact */
            div[data-baseweb="slider"] {
                max-width: 6.5rem !important;
            }
            /* Exercise card styling for stacked/mobile logger. */
            /* Use Streamlit expanders for grouping (handled in Python). Keep styling conservative so it fits both light/dark themes. */
            .exercise-card {
                border-radius: 8px;
                padding: 0.45rem 0.6rem;
                margin-bottom: 0.5rem;
                box-sizing: border-box;
            }
                .exercise-card h3 { margin: 0 0 0.35rem 0; padding: 0; font-size: 1.05rem; }
                /* Make number inputs layout-friendly: let the input flex so +/- step buttons stay visible */
                div[data-baseweb="numberinput"], div[data-testid="stNumberInput"] {
                    display: flex !important;
                    align-items: center !important;
                    gap: 0.35rem !important;
                }
                div[data-baseweb="numberinput"] input, div[data-testid="stNumberInput"] input {
                    flex: 1 1 auto !important;
                    width: auto !important;
                    min-width: 3.2rem !important;
                    box-sizing: border-box !important;
                    padding: 0.28rem 0.36rem !important;
                    font-size: 1rem !important;
                }
                /* Ensure stepper buttons are visible and tappable on mobile */
                div[data-baseweb="numberinput"] button, div[data-testid="stNumberInput"] button {
                    min-width: 2.1rem !important;
                    height: auto !important;
                    padding: 0.18rem 0.3rem !important;
                }
                /* Keep sliders full width where appropriate */
                .stSlider > div, div[data-baseweb="slider"] { width: 100% !important; }
            /* Sidebar nav larger buttons */
            section[data-testid="stSidebar"] .stButton > button {
                font-size: 1.05rem !important;
                padding: 0.6rem 0.8rem !important;
                margin-bottom: 0.5rem !important;
                text-align: left !important;
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
        # If a persist token is present in the query params, try to load stored creds
        persist_token = query_params.get("persist_token", [None])[0]
        if persist_token and not creds:
            loaded = load_creds_for_token(persist_token)
            if loaded:
                try:
                    creds = Credentials.from_authorized_user_info(json.loads(loaded), SCOPES)
                    if creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                    st.session_state["google_creds"] = json.loads(creds.to_json())
                except Exception:
                    creds = None

        if "code" in query_params:
            # Callback from Google: exchange code for tokens
            state = query_params.get("state", [None])[0]
            flow = get_flow(state=state)
            flow.fetch_token(code=query_params["code"][0])
            creds = flow.credentials
            st.session_state["google_creds"] = json.loads(creds.to_json())

            # After successful login, attempt to persist creds for this device
            try:
                # Generate a device token and save encrypted creds if possible
                token = uuid.uuid4().hex
                saved = save_creds_for_token(token, creds.to_json())
                if saved:
                    # Ask the browser to store the token in localStorage and reload with the token in the URL
                    js = f"""
                    <script>
                    try {{
                        localStorage.setItem('wt_persist', '{token}');
                        const u = new URL(window.location.href);
                        u.searchParams.set('persist_token', '{token}');
                        // remove OAuth code/state params to avoid reprocessing
                        u.searchParams.delete('code');
                        u.searchParams.delete('state');
                        window.location.replace(u.toString());
                    }} catch(e) {{ console.warn(e); }}
                    </script>
                    """
                    st.markdown(js, unsafe_allow_html=True)
            except Exception:
                pass

            # Clear query params server-side if possible
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
            # Inject a small script: if the browser has a saved token in localStorage, redirect with that token
            pre_js = """
                <script>
                try {
                    const t = localStorage.getItem('wt_persist');
                    if (t) {
                        const u = new URL(window.location.href);
                        u.searchParams.set('persist_token', t);
                        window.location.replace(u.toString());
                    }
                } catch(e) { console.warn(e); }
                </script>
            """
            st.markdown(pre_js, unsafe_allow_html=True)
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


# ---------------------------------------------------------------------
# Encrypted credential persistence (sqlite + Fernet)
# ---------------------------------------------------------------------

DB_PATH = os.path.join(os.path.expanduser("~"), ".workout_tracker_creds.db")


def _get_fernet() -> Optional[object]:
    """Return a Fernet instance using `st.secrets['fernet_key']`.

    If the key is not present or cryptography isn't installed, return None.
    """
    if not _FERNET_AVAILABLE:
        return None
    key = None
    try:
        key = st.secrets.get("fernet_key")
    except Exception:
        key = None
    if not key:
        return None
    try:
        return Fernet(key)
    except Exception:
        return None


def init_cred_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            creds BLOB NOT NULL
        )"""
    )
    conn.commit()
    conn.close()


def save_creds_for_token(token: str, creds_json: str) -> bool:
    f = _get_fernet()
    if f is None:
        return False
    init_cred_db()
    encrypted = f.encrypt(creds_json.encode("utf-8"))
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("REPLACE INTO tokens(token, creds) VALUES (?,?)", (token, encrypted))
    conn.commit()
    conn.close()
    return True


def load_creds_for_token(token: str) -> Optional[str]:
    f = _get_fernet()
    if f is None:
        return None
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT creds FROM tokens WHERE token=?", (token,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        decrypted = f.decrypt(row[0])
        return decrypted.decode("utf-8")
    except Exception:
        return None


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

    mobile_layout = st.session_state.get("logger_card_layout", True)

    if mobile_layout:
        # Stacked layout: use Streamlit expanders so widgets are grouped reliably
        for ex in todays_exs:
            with st.expander(ex, expanded=True):
                st.number_input("Weight", min_value=0.0, step=1.0, key=f"{log_date_iso}_{ex}_weight")
                st.number_input("Reps", min_value=0, step=1, key=f"{log_date_iso}_{ex}_reps")
                st.number_input("Sets", min_value=0, step=1, key=f"{log_date_iso}_{ex}_sets")
                st.slider("RPE", min_value=1, max_value=10, value=7, key=f"{log_date_iso}_{ex}_rpe")

                # Timing controls: Start / Stop / Reset
                start_key = f"{log_date_iso}_{ex}_start_ts"
                end_key = f"{log_date_iso}_{ex}_end_ts"
                tcols = st.columns([1,1,1,3])
                with tcols[0]:
                    if st.button("Start", key=f"{start_key}_btn"):
                        st.session_state[start_key] = now_ts()
                        # clear any previous end
                        if end_key in st.session_state:
                            del st.session_state[end_key]
                with tcols[1]:
                    if st.button("Stop", key=f"{end_key}_btn"):
                        # only stop if started
                        if st.session_state.get(start_key):
                            st.session_state[end_key] = now_ts()
                with tcols[2]:
                    if st.button("Reset", key=f"{start_key}_reset"):
                        if start_key in st.session_state:
                            del st.session_state[start_key]
                        if end_key in st.session_state:
                            del st.session_state[end_key]
                # Display current timing status
                ts_display = []
                if st.session_state.get(start_key):
                    stt = datetime.fromtimestamp(int(st.session_state[start_key])).strftime('%Y-%m-%d %H:%M:%S')
                    ts_display.append(f"Start: {stt}")
                if st.session_state.get(end_key):
                    edt = datetime.fromtimestamp(int(st.session_state[end_key])).strftime('%Y-%m-%d %H:%M:%S')
                    ts_display.append(f"End: {edt}")
                if st.session_state.get(start_key) and st.session_state.get(end_key):
                    dur_min = (int(st.session_state[end_key]) - int(st.session_state[start_key])) / 60.0
                    ts_display.append(f"Duration: {dur_min:.2f} min")
                if ts_display:
                    st.markdown("  \n".join(ts_display))
            st.markdown("&nbsp;")
    else:
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
            # Inline timing controls for desktop: small buttons beneath the row
            start_key = f"{log_date_iso}_{ex}_start_ts"
            end_key = f"{log_date_iso}_{ex}_end_ts"
            tcols = st.columns([3, 1, 1, 1])
            with tcols[1]:
                if st.button("Start", key=f"{start_key}_btn_desktop"):
                    st.session_state[start_key] = now_ts()
                    if end_key in st.session_state:
                        del st.session_state[end_key]
            with tcols[2]:
                if st.button("Stop", key=f"{end_key}_btn_desktop"):
                    if st.session_state.get(start_key):
                        st.session_state[end_key] = now_ts()
            with tcols[3]:
                if st.button("Reset", key=f"{start_key}_reset_desktop"):
                    if start_key in st.session_state:
                        del st.session_state[start_key]
                    if end_key in st.session_state:
                        del st.session_state[end_key]
            # Show timings inline
            ts_parts = []
            if st.session_state.get(start_key):
                ts_parts.append("Start: " + datetime.fromtimestamp(int(st.session_state[start_key])).strftime('%Y-%m-%d %H:%M:%S'))
            if st.session_state.get(end_key):
                ts_parts.append("End: " + datetime.fromtimestamp(int(st.session_state[end_key])).strftime('%Y-%m-%d %H:%M:%S'))
            if st.session_state.get(start_key) and st.session_state.get(end_key):
                dur_min = (int(st.session_state[end_key]) - int(st.session_state[start_key])) / 60.0
                ts_parts.append(f"Duration: {dur_min:.2f} min")
            if ts_parts:
                st.markdown(" — ".join(ts_parts))

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
                        "start_ts": int(st.session_state.get(start_key)) if start_key in st.session_state else None,
                        "end_ts": int(st.session_state.get(end_key)) if end_key in st.session_state else None,
                        "duration_min": round(((int(st.session_state[end_key]) - int(st.session_state[start_key])) / 60.0) if (start_key in st.session_state and end_key in st.session_state) else 0.0, 2),
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

    # Backward compatibility: ensure columns exist and prepare filtered history
    if "rpe" not in df.columns:
        df["rpe"] = None

    # Ensure common columns exist
    for col in ["date", "exercise", "weight", "reps", "sets", "rpe", "volume", "ts", "start_ts", "end_ts", "duration_min", "plan"]:
        if col not in df.columns:
            df[col] = None

    # Filter logs to only those for the active plan and today's exercises
    filtered = df[(df["plan"] == active_plan_name) & (df["exercise"].isin(todays_exs))].copy()
    if "plan" in filtered.columns:
        filtered = filtered.drop(columns=["plan"])

    # Sort for display
    try:
        filtered = filtered.sort_values(["date", "exercise"])
    except Exception:
        pass

    # st.markdown("#### History — only today's exercises")

    # Use Streamlit's data editor for inline edit/delete if available
    try:
        # st.data_editor is available in newer Streamlit versions
        edited = None
        if hasattr(st, "data_editor"):
            edited = st.data_editor(filtered, key="history_editor", use_container_width=True)
        else:
            # Fallback to experimental API
            edited = st.experimental_data_editor(filtered, key="history_editor")
    except Exception:
        # Last-resort: show read-only table
        st.dataframe(filtered, use_container_width=True, height=240)
        edited = None

    # If the user edited the table (or removed rows), update session logs accordingly
    if edited is not None:
        # Build remaining logs (those not part of today's exercises for this plan)
        remaining = [l for l in logs if not (l.get("plan") == active_plan_name and l.get("exercise") in todays_exs)]

        # Convert edited DataFrame back to log dicts and ensure required fields
        new_entries = []
        for _, row in edited.iterrows():
            d = row.to_dict()
            # Ensure ts exists and is int
            ts_val = d.get("ts")
            if pd.isna(ts_val) or ts_val in (None, ""):
                d["ts"] = now_ts()
            else:
                try:
                    d["ts"] = int(d["ts"])
                except Exception:
                    d["ts"] = now_ts()
            # Restore plan field
            d["plan"] = active_plan_name
            # Normalize numeric fields
            for ncol in ["weight", "reps", "sets", "rpe", "volume", "start_ts", "end_ts", "duration_min"]:
                if ncol in d and (pd.isna(d[ncol]) or d[ncol] is None):
                    d[ncol] = 0 if ncol in ("reps", "sets", "rpe") else 0.0
            new_entries.append(d)

        # Combine and save back to session (user must still click Save to Drive to persist)
        st.session_state["data"]["logs"] = remaining + new_entries
        st.success("Changes saved to session. Click 'Save to Google Drive' in the sidebar to persist.")


# ---------------------------------------------------------------------
# Debug / account page
# ---------------------------------------------------------------------

def debug_page(data, user_info):
    st.header("Account / Data (debug)")

    st.markdown("### User info")
    st.json(user_info)

    st.markdown("### In-memory data")
    st.json(data)

    # Runtime diagnostics (non-sensitive)
    st.markdown("---")
    st.subheader("Runtime diagnostics")
    # Packages
    try:
        import streamlit as _st_mod
        st_ver = getattr(_st_mod, "__version__", "unknown")
    except Exception:
        st_ver = "unknown"
    try:
        import cryptography as _crypt
        crypt_ver = getattr(_crypt, "__version__", "unknown")
    except Exception:
        crypt_ver = None

    st.markdown("**Packages**")
    st.markdown(f"- streamlit: {st_ver}")
    st.markdown(f"- cryptography: {crypt_ver if crypt_ver else 'missing'}")

    # Fernet availability and secrets presence (do not print secrets)
    try:
        f_ok = _get_fernet() is not None
    except Exception:
        f_ok = False
    st.markdown("**Fernet / credential persistence**")
    st.markdown(f"- Fernet available: {f_ok}")
    try:
        key_present = bool(st.secrets.get('fernet_key', None))
    except Exception:
        key_present = False
    st.markdown(f"- Fernet key present in secrets: {key_present}")

    # DB status
    try:
        db_exists = os.path.exists(DB_PATH)
    except Exception:
        db_exists = False
    st.markdown("**Credential DB**")
    st.markdown(f"- DB file exists: {db_exists}")
    if db_exists:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT count(1) FROM tokens")
            cnt = cur.fetchone()[0]
            conn.close()
            st.markdown(f"- Stored device tokens: {cnt}")
        except Exception as e:
            st.markdown(f"- Stored device tokens: error ({e})")

    # Token management helpers: allow restoring a token into browser localStorage
    st.markdown("---")
    st.subheader("Token management")
    try:
        tokens = []
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT token FROM tokens ORDER BY rowid DESC LIMIT 50")
            rows = cur.fetchall()
            conn.close()
            tokens = [r[0] for r in rows if r and r[0]]
    except Exception:
        tokens = []

    if not tokens:
        st.markdown("No stored tokens available on the server.")
    else:
        # Present short labels but keep full token for actions
        labels = [t[:8] + '...' for t in tokens]
        sel_i = st.selectbox("Select stored token (server)", range(len(tokens)), format_func=lambda i: labels[i])
        sel_token = tokens[sel_i]

        if st.button("Restore selected token to browser localStorage"):
            # Inject JS to set localStorage and reload — token is sensitive so only set here
            js_token = json.dumps(sel_token)
            js = f"""
            <script>
            try {{
                localStorage.setItem('wt_persist', {js_token});
                // reload so app reads the token
                window.location.reload();
            }} catch(e) {{ console.warn(e); }}
            </script>
            """
            st.markdown(js, unsafe_allow_html=True)

        if st.button("Delete selected token from server and clear localStorage"):
            try:
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("DELETE FROM tokens WHERE token=?", (sel_token,))
                conn.commit()
                conn.close()
                st.success("Token removed from server. Clearing localStorage and reloading...")
            except Exception as e:
                st.error(f"Failed to remove token: {e}")
            # Clear client localStorage as well
            clear_js = """
            <script>
            try { localStorage.removeItem('wt_persist'); window.location.reload(); } catch(e) { console.warn(e); }
            </script>
            """
            st.markdown(clear_js, unsafe_allow_html=True)

    # Session flags
    st.markdown("**Session state**")
    ss = {
        "has_google_creds": "google_creds" in st.session_state,
        "data_file_id": st.session_state.get("data_file_id"),
        "wt_persist_in_session": bool(st.session_state.get("oauth_state")),
    }
    st.json(ss)


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

    # Default page and logger layout preferences
    if "page" not in st.session_state:
        st.session_state["page"] = "Logger"
    if "logger_card_layout" not in st.session_state:
        # default to mobile-friendly stacked cards (user can toggle)
        st.session_state["logger_card_layout"] = True

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
        st.markdown("### Navigate")
        # Remove 'Navigate' heading and provide two primary actions
        if st.button("Log Workouts", key="nav_logger"):
            st.session_state["page"] = "Logger"
        if st.button("Plan Workouts", key="nav_planner"):
            st.session_state["page"] = "Planner"

        # Spacer then small debug control at the bottom (less prominent)
        st.markdown("<div style='height:200px'></div>", unsafe_allow_html=True)
        st.checkbox("Show Debug", value=False, key="show_debug")
        if st.session_state.get("show_debug"):
            st.session_state["page"] = "Debug"

        st.markdown("---")
        # Mobile-friendly stacked card layout toggle for logger
        st.checkbox("Mobile-friendly logger layout (stacked cards)", value=st.session_state.get("logger_card_layout", True), key="logger_card_layout")

        page = st.session_state.get("page", "Logger")

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

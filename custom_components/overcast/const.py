"""Constants for the Overcast integration."""

DOMAIN = "overcast"

OVERCAST_BASE_URL = "https://overcast.fm"
LOGIN_URL = f"{OVERCAST_BASE_URL}/login"
PODCASTS_URL = f"{OVERCAST_BASE_URL}/podcasts"
QR_VERIFY_URL = f"{OVERCAST_BASE_URL}/main/login_qr_verify"

USER_AGENT = "HomeAssistant/Overcast-Integration (overcast.fm)"

# Sent as p= to mark an episode finished
PROGRESS_FINISHED_SENTINEL = 2147483647  # 0x7FFFFFFF

# Default speed (1.0x)
DEFAULT_SPEED_ID = 0

# Speed ID -> playback rate
SPEED_MAP = {
    750: 0.75,
    0: 1.0,
    1125: 1.125,
    1250: 1.25,
    1375: 1.375,
    1500: 1.5,
    1750: 1.75,
    2000: 2.0,
    2250: 2.25,
}

# Polling / refresh intervals (seconds)
SYNC_INTERVAL_SECONDS = 30
SUBSCRIPTION_REFRESH_SECONDS = 3600
EPISODE_REFRESH_SECONDS = 300

# QR login polling
QR_POLL_INITIAL_INTERVAL = 1
QR_POLL_MID_INTERVAL = 5
QR_POLL_SLOW_INTERVAL = 10
QR_POLL_TIMEOUT = 300

# Config entry keys
CONF_COOKIE = "cookie"
CONF_AUTH_METHOD = "auth_method"
CONF_EMAIL = "email"

# Services
SERVICE_PLAY_LATEST = "play_latest"
SERVICE_MARK_PLAYED = "mark_played"
SERVICE_REFRESH = "refresh"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_PODCASTS = "podcasts"
ATTR_TARGET_PLAYER = "target_player"
ATTR_MARK_PLAYED = "mark_played"
ATTR_RESUME_POSITION = "resume_position"
ATTR_SPEED = "speed"
ATTR_EPISODE_URL = "episode_url"

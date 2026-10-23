import os

env_path = os.path.join(os.path.dirname(__file__), ".env")

try:
    from dotenv import load_dotenv  # type: ignore[import-not-found]
    load_dotenv(env_path)
except ImportError:
    pass

# Fallback .env loader if python-dotenv is not installed
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                if key:
                    os.environ[key] = val


class Config:
    MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "aakri_db")
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
    # SMTP/email settings for OTP
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "").strip()
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
    EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER).strip()

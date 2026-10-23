from flask import Flask, jsonify, render_template, request, redirect

try:
    import mysql.connector
except ImportError:  # pragma: no cover - depends on environment
    mysql = None

from config import Config
from werkzeug.security import generate_password_hash, check_password_hash
from flask import session, url_for
import traceback
import secrets
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
import re
import json
import hashlib
import math
from market_api import fetch_live_market_scrap_rates

app = Flask(__name__)
app.config.from_object(Config)


def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculates the great-circle distance in kilometers between two points
    on Earth using the Haversine formula.
    """
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return float('inf')
    try:
        lat1, lon1, lat2, lon2 = map(float, [lat1, lon1, lat2, lon2])
    except (ValueError, TypeError):
        return float('inf')

    # Convert decimal degrees to radians
    lat1_rad, lon1_rad, lat2_rad, lon2_rad = map(math.radians, [lat1, lon1, lat2, lon2])

    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = math.sin(dlat / 2.0)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    r = 6371.0  # Earth's radius in km
    return round(c * r, 2)


def find_nearest_collector(user_lat, user_lng):
    """
    Distance-Based Nearest Neighbour Algorithm:
    Selects the closest available collector (availability_status = 1) using Haversine distance.
    """
    if user_lat is None or user_lng is None:
        return None, None

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, username, email, phone, address, latitude, longitude, availability_status
            FROM collectors
            WHERE availability_status = 1 AND latitude IS NOT NULL AND longitude IS NOT NULL
            """
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return None, None

        nearest_collector = None
        min_distance = float('inf')

        for r in rows:
            c_lat = r[5]
            c_lng = r[6]
            if c_lat is not None and c_lng is not None:
                dist = haversine_distance(user_lat, user_lng, c_lat, c_lng)
                if dist < min_distance:
                    min_distance = dist
                    nearest_collector = {
                        "id": r[0],
                        "username": r[1],
                        "email": r[2],
                        "phone": r[3],
                        "address": r[4],
                        "latitude": float(c_lat),
                        "longitude": float(c_lng),
                        "distance_km": dist
                    }

        if nearest_collector and min_distance != float('inf'):
            return nearest_collector, min_distance
        return None, None
    except Exception as exc:
        traceback.print_exc()
        return None, None


def optimize_collector_route(collector_lat, collector_lng, pickups):
    """
    Nearest Neighbor TSP Route Optimization:
    Orders assigned pickups sequentially starting from the collector's current location.
    """
    if not pickups:
        return [], 0.0

    unvisited = list(pickups)
    ordered_route = []
    total_distance = 0.0

    current_lat = float(collector_lat) if collector_lat is not None else 12.9716
    current_lng = float(collector_lng) if collector_lng is not None else 77.5946

    while unvisited:
        nearest_next = None
        min_dist = float('inf')

        for p in unvisited:
            p_lat = p.get('latitude')
            p_lng = p.get('longitude')
            if p_lat is not None and p_lng is not None:
                dist = haversine_distance(current_lat, current_lng, p_lat, p_lng)
            else:
                dist = 999.0

            if dist < min_dist:
                min_dist = dist
                nearest_next = p

        if not nearest_next:
            nearest_next = unvisited[0]
            min_dist = 0.0

        nearest_next['leg_distance_km'] = min_dist if min_dist != float('inf') else 0.0
        total_distance += nearest_next['leg_distance_km']
        ordered_route.append(nearest_next)

        if nearest_next.get('latitude') is not None and nearest_next.get('longitude') is not None:
            current_lat = float(nearest_next['latitude'])
            current_lng = float(nearest_next['longitude'])

        unvisited.remove(nearest_next)

    return ordered_route, round(total_distance, 2)


def is_valid_password(password):
    if len(password) < 6:
        return False, "Password must be at least 6 characters long."
    if not re.search(r"[a-zA-Z]", password):
        return False, "Password must contain at least one letter."
    if not re.search(r"\d", password):
        return False, "Password must contain at least one number."
    if not re.search(r"[^a-zA-Z0-9]", password):
        return False, "Password must contain at least one special character (e.g. !@#$%^&*)."
    return True, ""


def send_otp_email(email, otp):
    smtp_user = app.config.get("SMTP_USER")
    smtp_password = app.config.get("SMTP_PASSWORD")
    if not smtp_user or not smtp_password:
        raise RuntimeError(
            "Email is not configured. Set SMTP_USER and SMTP_PASSWORD in .env "
            "and restart the Flask server. For Gmail, SMTP_PASSWORD must be a "
            "Google app password, not your normal account password."
        )

    msg = EmailMessage()
    msg["Subject"] = "Your WasteTech verification OTP"
    msg["From"] = app.config["EMAIL_FROM"] or smtp_user
    msg["To"] = email
    msg.set_content(f"Your verification code is: {otp}\nIt expires in 15 minutes.")

    with smtplib.SMTP(app.config["SMTP_HOST"], app.config["SMTP_PORT"], timeout=10) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def get_db_connection():
    if mysql is None:
        raise RuntimeError("mysql-connector-python is not installed.")

    return mysql.connector.connect(
        host=app.config["MYSQL_HOST"],
        port=app.config["MYSQL_PORT"],
        user=app.config["MYSQL_USER"],
        password=app.config["MYSQL_PASSWORD"],
        database=app.config["MYSQL_DATABASE"],
        autocommit=True,
    )


# Default Base Scrap Rates definitions
BASE_SCRAP_ITEMS = [
    # Paper
    {"category": "Paper", "code": "paper_newspaper", "name": "Newspaper", "base_rate": 16.0, "unit": "kg", "icon": "fa-newspaper"},
    {"category": "Paper", "code": "paper_books", "name": "Books & Magazines", "base_rate": 14.0, "unit": "kg", "icon": "fa-book"},
    {"category": "Paper", "code": "paper_carton", "name": "Carton / Cardboard", "base_rate": 12.0, "unit": "kg", "icon": "fa-box"},
    {"category": "Paper", "code": "paper_office", "name": "Office White Paper", "base_rate": 15.0, "unit": "kg", "icon": "fa-file-lines"},

    # Plastic
    {"category": "Plastic", "code": "plastic_pet", "name": "PET Bottles (Water/Soda)", "base_rate": 18.0, "unit": "kg", "icon": "fa-bottle-water"},
    {"category": "Plastic", "code": "plastic_hard", "name": "Hard Plastic (Buckets/Chairs)", "base_rate": 15.0, "unit": "kg", "icon": "fa-chair"},
    {"category": "Plastic", "code": "plastic_container", "name": "Plastic Containers", "base_rate": 14.0, "unit": "kg", "icon": "fa-box-open"},
    {"category": "Plastic", "code": "plastic_poly", "name": "Mix Plastic / Covers", "base_rate": 10.0, "unit": "kg", "icon": "fa-bag-shopping"},

    # Metals
    {"category": "Metals", "code": "metal_iron", "name": "Iron & Steel Scrap", "base_rate": 28.0, "unit": "kg", "icon": "fa-cubes"},
    {"category": "Metals", "code": "metal_copper", "name": "Copper Wires / Scrap", "base_rate": 450.0, "unit": "kg", "icon": "fa-bolt"},
    {"category": "Metals", "code": "metal_aluminium", "name": "Aluminium Utensils / Scrap", "base_rate": 110.0, "unit": "kg", "icon": "fa-shield-cat"},
    {"category": "Metals", "code": "metal_brass", "name": "Brass / Bronze Scrap", "base_rate": 320.0, "unit": "kg", "icon": "fa-award"},

    # E-Waste
    {"category": "E-Waste", "code": "ewaste_mobile", "name": "Mobile Phones / Tablets", "base_rate": 25.0, "unit": "unit", "icon": "fa-mobile-screen"},
    {"category": "E-Waste", "code": "ewaste_laptop", "name": "Laptops & Computers", "base_rate": 300.0, "unit": "unit", "icon": "fa-laptop"},
    {"category": "E-Waste", "code": "ewaste_tv", "name": "TV & Monitors", "base_rate": 200.0, "unit": "unit", "icon": "fa-tv"},
    {"category": "E-Waste", "code": "ewaste_appliance", "name": "Refrigerators & ACs", "base_rate": 500.0, "unit": "unit", "icon": "fa-plug"},

    # Vehicle
    {"category": "Vehicle", "code": "vehicle_bike", "name": "Two Wheeler / Motorbike", "base_rate": 2500.0, "unit": "unit", "icon": "fa-motorcycle"},
    {"category": "Vehicle", "code": "vehicle_car", "name": "Four Wheeler / Car", "base_rate": 15000.0, "unit": "unit", "icon": "fa-car"},
    {"category": "Vehicle", "code": "vehicle_auto", "name": "Auto Rickshaw", "base_rate": 8000.0, "unit": "unit", "icon": "fa-truck-monster"},
]


def ensure_tables_exist():
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Ensure users table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INT PRIMARY KEY AUTO_INCREMENT,
                username VARCHAR(255),
                email VARCHAR(255),
                password VARCHAR(255),
                phone VARCHAR(50),
                address TEXT,
                latitude DECIMAL(10, 8) DEFAULT 12.9716,
                longitude DECIMAL(11, 8) DEFAULT 77.5946,
                location_label VARCHAR(50) DEFAULT 'Home',
                city VARCHAR(100) DEFAULT 'Bengaluru',
                pincode VARCHAR(20) DEFAULT '560001',
                is_verified TINYINT DEFAULT 0,
                otp_code VARCHAR(10),
                otp_expires DATETIME,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        for col, col_def in [
            ('email', 'VARCHAR(255)'),
            ('username', 'VARCHAR(255)'),
            ('phone', 'VARCHAR(50)'),
            ('address', 'TEXT'),
            ('latitude', 'DECIMAL(10, 8) DEFAULT 12.9716'),
            ('longitude', 'DECIMAL(11, 8) DEFAULT 77.5946'),
            ('location_label', "VARCHAR(50) DEFAULT 'Home'"),
            ('city', "VARCHAR(100) DEFAULT 'Bengaluru'"),
            ('pincode', "VARCHAR(20) DEFAULT '560001'"),
            ('account_status', "VARCHAR(50) DEFAULT 'Active'"),
            ('is_verified', 'TINYINT DEFAULT 0'),
            ('otp_code', 'VARCHAR(10)'),
            ('otp_expires', 'DATETIME')
        ]:
            try:
                cur.execute(f"SHOW COLUMNS FROM users LIKE '{col}'")
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def}")
            except Exception:
                pass

        # Ensure collectors table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS collectors (
                id INT PRIMARY KEY AUTO_INCREMENT,
                username VARCHAR(255),
                email VARCHAR(255),
                phone VARCHAR(50),
                password VARCHAR(255),
                address TEXT,
                latitude DECIMAL(10, 8) DEFAULT 12.9716,
                longitude DECIMAL(11, 8) DEFAULT 77.5946,
                availability_status TINYINT DEFAULT 1,
                approval_status VARCHAR(50) DEFAULT 'Approved',
                account_status VARCHAR(50) DEFAULT 'Active',
                is_verified TINYINT DEFAULT 0,
                otp_code VARCHAR(10),
                otp_expires DATETIME,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        for col, col_def in [
            ('email', 'VARCHAR(255)'),
            ('username', 'VARCHAR(255)'),
            ('phone', 'VARCHAR(50)'),
            ('address', 'TEXT'),
            ('latitude', 'DECIMAL(10, 8) DEFAULT 12.9716'),
            ('longitude', 'DECIMAL(11, 8) DEFAULT 77.5946'),
            ('availability_status', 'TINYINT DEFAULT 1'),
            ('approval_status', "VARCHAR(50) DEFAULT 'Approved'"),
            ('account_status', "VARCHAR(50) DEFAULT 'Active'"),
            ('is_verified', 'TINYINT DEFAULT 0'),
            ('otp_code', 'VARCHAR(10)'),
            ('otp_expires', 'DATETIME')
        ]:
            try:
                cur.execute(f"SHOW COLUMNS FROM collectors LIKE '{col}'")
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE collectors ADD COLUMN {col} {col_def}")
            except Exception:
                pass

        # Ensure admins table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                id INT PRIMARY KEY AUTO_INCREMENT,
                username VARCHAR(255),
                email VARCHAR(255),
                password VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Seed default Admin account if empty
        try:
            cur.execute("SELECT COUNT(*) FROM admins")
            if cur.fetchone()[0] == 0:
                admin_pass = generate_password_hash("Admin@123")
                cur.execute(
                    "INSERT INTO admins (username, email, password) VALUES (%s, %s, %s)",
                    ("System Admin", "admin@wastetech.com", admin_pass)
                )
        except Exception as adm_exc:
            app.logger.warning(f"Admin seed warning: {adm_exc}")

        # Ensure feedback_complaints table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_complaints (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT,
                user_name VARCHAR(255),
                collector_id INT NULL,
                pickup_id INT NULL,
                type VARCHAR(50) DEFAULT 'Feedback',
                subject VARCHAR(255),
                message TEXT,
                rating INT DEFAULT 5,
                status VARCHAR(50) DEFAULT 'Pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Ensure scrap_rates table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scrap_rates (
                id INT PRIMARY KEY AUTO_INCREMENT,
                category VARCHAR(100),
                item_code VARCHAR(100),
                item_name VARCHAR(255),
                rate_per_unit DECIMAL(10, 2),
                unit VARCHAR(50),
                rate_date DATE,
                trend VARCHAR(20) DEFAULT 'stable',
                change_percent DECIMAL(5, 2) DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY unique_item_date (item_code, rate_date)
            )
            """
        )

        # Ensure pickup_requests table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pickup_requests (
                id INT PRIMARY KEY AUTO_INCREMENT,
                user_id INT,
                user_name VARCHAR(255),
                collector_id INT NULL,
                scrap_type VARCHAR(255),
                estimated_weight DECIMAL(10, 2) DEFAULT 0.0,
                actual_weight DECIMAL(10, 2) DEFAULT 0.0,
                items_json TEXT,
                actual_items_json TEXT,
                total_price DECIMAL(10, 2) DEFAULT 0.0,
                final_amount DECIMAL(10, 2) DEFAULT 0.0,
                pickup_date DATE,
                time_slot VARCHAR(100),
                phone VARCHAR(50),
                address TEXT,
                latitude DECIMAL(10, 8) DEFAULT 12.9716,
                longitude DECIMAL(11, 8) DEFAULT 77.5946,
                distance_km DECIMAL(10, 2) DEFAULT 0.0,
                notes TEXT,
                status VARCHAR(50) DEFAULT 'Pending',
                completed_at DATETIME NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        for col, col_def in [
            ('collector_id', 'INT NULL'),
            ('scrap_type', 'VARCHAR(255)'),
            ('estimated_weight', 'DECIMAL(10, 2) DEFAULT 0.0'),
            ('actual_weight', 'DECIMAL(10, 2) DEFAULT 0.0'),
            ('actual_items_json', 'TEXT'),
            ('final_amount', 'DECIMAL(10, 2) DEFAULT 0.0'),
            ('latitude', 'DECIMAL(10, 8) DEFAULT 12.9716'),
            ('longitude', 'DECIMAL(11, 8) DEFAULT 77.5946'),
            ('distance_km', 'DECIMAL(10, 2) DEFAULT 0.0'),
            ('completed_at', 'DATETIME NULL')
        ]:
            try:
                cur.execute(f"SHOW COLUMNS FROM pickup_requests LIKE '{col}'")
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE pickup_requests ADD COLUMN {col} {col_def}")
            except Exception:
                pass

        # Seed default sample collectors across city zones if collectors table is empty
        try:
            cur.execute("SELECT COUNT(*) FROM collectors")
            col_cnt = cur.fetchone()[0]
            if col_cnt == 0:
                demo_cols = [
                    ("Rahul Verma (Indiranagar Zone)", "rahul@wastetech.com", "9876543210", generate_password_hash("Collector@123"), 1, "Indiranagar 100ft Road, Bengaluru", 12.9784, 77.6408, 1),
                    ("Vikram Singh (Koramangala Zone)", "vikram@wastetech.com", "9876543211", generate_password_hash("Collector@123"), 1, "Koramangala 5th Block, Bengaluru", 12.9352, 77.6245, 1),
                    ("Anil Kumar (Whitefield Zone)", "anil@wastetech.com", "9876543212", generate_password_hash("Collector@123"), 1, "ITPL Main Road, Whitefield, Bengaluru", 12.9698, 77.7499, 1),
                    ("Suresh Patel (Jayanagar Zone)", "suresh@wastetech.com", "9876543213", generate_password_hash("Collector@123"), 1, "Jayanagar 4th Block, Bengaluru", 12.9250, 77.5938, 1),
                ]
                for c in demo_cols:
                    cur.execute(
                        """
                        INSERT INTO collectors (username, email, phone, password, is_verified, address, latitude, longitude, availability_status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        c
                    )
        except Exception as seed_exc:
            app.logger.warning(f"Collector seed warning: {seed_exc}")

        cur.close()
        conn.close()
    except Exception as exc:
        app.logger.warning(f"DB auto-migration warning: {exc}")


def get_today_scrap_rates():
    """Fetches today's live rates from DB or auto-generates today's daily updated market rates."""
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    formatted_date = today.strftime("%d %b %Y")

    rates = []
    try:
        ensure_tables_exist()
        conn = get_db_connection()
        cur = conn.cursor()

        # Check if rates for today exist in scrap_rates table
        cur.execute("SELECT category, item_code, item_name, rate_per_unit, unit, trend, change_percent FROM scrap_rates WHERE rate_date=%s", (today_str,))
        rows = cur.fetchall()

        if rows:
            for row in rows:
                # Find matching icon
                matched_icon = "fa-recycle"
                for b in BASE_SCRAP_ITEMS:
                    if b["code"] == row[1]:
                        matched_icon = b["icon"]
                        break
                rates.append({
                    "category": row[0],
                    "code": row[1],
                    "name": row[2],
                    "rate": float(row[3]),
                    "unit": row[4],
                    "trend": row[5],
                    "change": float(row[6]),
                    "icon": matched_icon
                })
        else:
            # Fetch real live market rates from commodity & exchange APIs
            live_market_rates = fetch_live_market_scrap_rates()
            for item in live_market_rates:
                code = item["code"]
                rate = item["rate"]
                trend = item["trend"]
                change = item["change"]

                # Match icon from BASE_SCRAP_ITEMS
                matched_icon = "fa-recycle"
                for b in BASE_SCRAP_ITEMS:
                    if b["code"] == code:
                        matched_icon = b["icon"]
                        break

                try:
                    cur.execute(
                        """
                        INSERT INTO scrap_rates (category, item_code, item_name, rate_per_unit, unit, rate_date, trend, change_percent)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE rate_per_unit=%s, trend=%s, change_percent=%s
                        """,
                        (item["category"], code, item["name"], rate, item["unit"], today_str, trend, change, rate, trend, change)
                    )
                except Exception:
                    pass

                rates.append({
                    "category": item["category"],
                    "code": code,
                    "name": item["name"],
                    "rate": float(rate),
                    "unit": item["unit"],
                    "trend": trend,
                    "change": change,
                    "icon": matched_icon
                })
        cur.close()
        conn.close()
    except Exception as exc:
        app.logger.warning(f"DB access notice: {exc}. Using direct live market API fetch.")
        live_rates = fetch_live_market_scrap_rates()
        for item in live_rates:
            matched_icon = "fa-recycle"
            for b in BASE_SCRAP_ITEMS:
                if b["code"] == item["code"]:
                    matched_icon = b["icon"]
                    break
            rates.append({
                "category": item["category"],
                "code": item["code"],
                "name": item["name"],
                "rate": float(item["rate"]),
                "unit": item["unit"],
                "trend": item["trend"],
                "change": item["change"],
                "icon": matched_icon
            })

    categories = {}
    for r in rates:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(r)

    return {
        "date_str": today_str,
        "date_display": formatted_date,
        "categories": categories,
        "flat_rates": rates
    }


@app.before_request
def validate_and_sanitize_session():
    """
    Validates logged-in session IDs against MySQL database on every request.
    If session credentials point to non-existent or deactivated user/collector/admin records,
    automatically purges stale session data so the website opens cleanly without manual logouts.
    """
    if request.endpoint == 'static' or (request.path and request.path.startswith('/static')):
        return

    user_id = session.get("user_id")
    collector_id = session.get("collector_id")
    admin_id = session.get("admin_id")
    is_collector = session.get("is_collector")
    is_admin = session.get("is_admin")

    if not user_id and not collector_id and not admin_id:
        if session.get("username") or session.get("collector_name") or is_collector or is_admin:
            session.clear()
        return

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        if user_id:
            cur.execute("SELECT id, account_status FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
            if not row or (len(row) > 1 and row[1] == "Deactivated"):
                session.clear()
                cur.close()
                conn.close()
                return

        if is_collector or collector_id:
            if not collector_id:
                session.clear()
                cur.close()
                conn.close()
                return
            cur.execute("SELECT id, account_status FROM collectors WHERE id=%s", (collector_id,))
            row = cur.fetchone()
            if not row or (len(row) > 1 and row[1] == "Deactivated"):
                session.clear()
                cur.close()
                conn.close()
                return

        if is_admin or admin_id:
            if not admin_id:
                session.clear()
                cur.close()
                conn.close()
                return
            cur.execute("SELECT id FROM admins WHERE id=%s", (admin_id,))
            row = cur.fetchone()
            if not row:
                session.clear()
                cur.close()
                conn.close()
                return

        cur.close()
        conn.close()
    except Exception as exc:
        app.logger.warning(f"Session validation DB check notice: {exc}")


@app.route("/")
def home():
    scrap_data = get_today_scrap_rates()
    return render_template("index.html", scrap_data=scrap_data)


@app.route("/api/scrap-rates")
def api_scrap_rates():
    if not session.get("user_id") and not session.get("is_collector") and not session.get("is_admin"):
        return jsonify({"error": "Authentication required. Please log in."}), 401
    return jsonify(get_today_scrap_rates())


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "GET":
        success_msg = None
        message = request.args.get("message")
        if request.args.get("verified") == "true":
            success_msg = "OTP successfully verified! You can now log in."
        return render_template("login.html", success_message=success_msg, message=message)

    identifier = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not identifier or not password:
        return render_template("login.html", message="Please provide username/email and password.")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # look up by email or username
        cur.execute("SELECT id, username, email, password, is_verified FROM users WHERE email=%s OR username=%s", (identifier, identifier))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return render_template("login.html", message="Invalid credentials.")

        user_id, db_username, db_email, db_password, db_verified = row
        if not check_password_hash(db_password, password):
            return render_template("login.html", message="Invalid credentials.")

        if not db_verified:
            session["pending_otp_email"] = db_email
            return render_template("login.html", message="Account not verified. Check your email for the OTP.", show_verify_link=True)

        # successful login
        session.clear()
        session["user_id"] = user_id
        session["username"] = db_username or db_email
        next_page = request.args.get("next") or request.form.get("next") or "/"
        return redirect(next_page)
    except Exception as exc:
        traceback.print_exc()
        return render_template("login.html", message=f"Login error: {str(exc)}")


@app.route("/register", methods=("GET", "POST"))
def register():
    if request.method == "GET":
        return render_template("register.html")

    # POST - create new user
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    if not username or not email or not password:
        return render_template("register.html", message="Please provide username, email and password.")

    # Validate password requirements (>= 6 chars, letters, numbers, special chars)
    valid, pwd_msg = is_valid_password(password)
    if not valid:
        return render_template("register.html", message=pwd_msg)

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # ensure users table exists (minimal schema)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INT PRIMARY KEY AUTO_INCREMENT,
                username VARCHAR(255),
                email VARCHAR(255),
                password VARCHAR(255),
                is_verified TINYINT DEFAULT 0,
                otp_code VARCHAR(10),
                otp_expires DATETIME,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Ensure expected columns exist; if table pre-exists with different schema, add missing columns
        try:
            cur.execute("SHOW COLUMNS FROM users LIKE 'email'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE users ADD COLUMN email VARCHAR(255)")
        except Exception:
            pass
        try:
            cur.execute("SHOW COLUMNS FROM users LIKE 'username'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE users ADD COLUMN username VARCHAR(255)")
        except Exception:
            pass
        try:
            cur.execute("SHOW COLUMNS FROM users LIKE 'is_verified'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE users ADD COLUMN is_verified TINYINT DEFAULT 0")
        except Exception:
            pass
        try:
            cur.execute("SHOW COLUMNS FROM users LIKE 'otp_code'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE users ADD COLUMN otp_code VARCHAR(10)")
        except Exception:
            pass
        try:
            cur.execute("SHOW COLUMNS FROM users LIKE 'otp_expires'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE users ADD COLUMN otp_expires DATETIME")
        except Exception:
            pass

        # Add unique keys if they don't exist (ignore errors)
        try:
            cur.execute("ALTER TABLE users ADD UNIQUE KEY unique_email (email)")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE users ADD UNIQUE KEY unique_username (username)")
        except Exception:
            pass

        # check existing user
        cur.execute("SELECT id FROM users WHERE email=%s OR username=%s", (email, username))
        if cur.fetchone():
            cur.close()
            conn.close()
            return render_template("register.html", message="A user with that email or username already exists.")

        hashed = generate_password_hash(password)
        cur.execute(
            "INSERT INTO users (username, email, password, is_verified) VALUES (%s, %s, %s, 0)",
            (username, email, hashed),
        )
        user_id = cur.lastrowid

        # generate OTP and expiry
        otp = str(secrets.randbelow(900000) + 100000)
        expires = datetime.now() + timedelta(minutes=15)
        cur.execute(
            "UPDATE users SET otp_code=%s, otp_expires=%s WHERE id=%s",
            (otp, expires.strftime("%Y-%m-%d %H:%M:%S"), user_id),
        )

        # Send the OTP before redirecting to verification.
        try:
            send_otp_email(email, otp)
        except Exception as exc:
            app.logger.exception("Unable to send registration OTP")
            cur.close()
            conn.close()
            return render_template("register.html", message=f"Could not send OTP email: {exc}")

        cur.close()
        conn.close()

        # Save email in session for verify screen so user doesn't need to retype it
        session["pending_otp_email"] = email
        return redirect(url_for("verify_otp"))
    except Exception as exc:
        traceback.print_exc()
        return render_template("register.html", message=f"Error creating user: {str(exc)}")


@app.route("/verify-otp", methods=("GET", "POST"))
def verify_otp():
    email = session.get("pending_otp_email")

    if request.method == "GET":
        if not email:
            return render_template("verify.html", message="Please register or log in first to verify your OTP.")
        return render_template("verify.html", email=email)

    # POST method - verify OTP code
    code = request.form.get("code", "").strip()
    if not code:
        return render_template("verify.html", email=email, message="Please enter the 6-digit OTP code.")

    if not email:
        return render_template("verify.html", message="Session expired. Please log in or register again.")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, otp_code, otp_expires, is_verified FROM users WHERE email=%s", (email,))
        row = cur.fetchone()

        if not row:
            cur.close()
            conn.close()
            return render_template("verify.html", email=email, message="User record not found.")

        user_id, db_otp_code, db_otp_expires, is_verified = row

        if is_verified:
            cur.close()
            conn.close()
            session.pop("pending_otp_email", None)
            return redirect(url_for("login", verified="true"))

        if not db_otp_code or str(db_otp_code).strip() != str(code).strip():
            cur.close()
            conn.close()
            return render_template("verify.html", email=email, message="Invalid OTP code. Please check your email and try again.")

        if db_otp_expires:
            if isinstance(db_otp_expires, str):
                try:
                    db_otp_expires = datetime.strptime(db_otp_expires, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
            if datetime.now() > db_otp_expires:
                cur.close()
                conn.close()
                return render_template("verify.html", email=email, message="OTP code has expired. Please click Resend OTP.")

        cur.execute("UPDATE users SET is_verified = 1, otp_code = NULL, otp_expires = NULL WHERE id = %s", (user_id,))
        cur.close()
        conn.close()

        session.pop("pending_otp_email", None)
        return redirect(url_for("login", verified="true"))

    except Exception as exc:
        traceback.print_exc()
        return render_template("verify.html", email=email, message=f"Verification error: {str(exc)}")


@app.route("/resend-otp", methods=("POST",))
def resend_otp():
    email = session.get("pending_otp_email")
    if not email:
        return render_template("verify.html", message="Session expired. Please try registering or logging in again.")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email=%s", (email,))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return render_template("verify.html", message="User record not found.")

        user_id = row[0]
        otp = str(secrets.randbelow(900000) + 100000)
        expires = datetime.now() + timedelta(minutes=15)

        cur.execute(
            "UPDATE users SET otp_code=%s, otp_expires=%s WHERE id=%s",
            (otp, expires.strftime("%Y-%m-%d %H:%M:%S"), user_id),
        )
        cur.close()
        conn.close()

        try:
            send_otp_email(email, otp)
        except Exception as exc:
            app.logger.exception("Unable to resend registration OTP")
            return render_template("verify.html", email=email, message=f"Could not resend OTP email: {exc}")

        return render_template("verify.html", email=email, success_message="A new OTP code has been sent to your email.")
    except Exception as exc:
        traceback.print_exc()
        return render_template("verify.html", email=email, message=f"Error resending OTP: {str(exc)}")




@app.route("/request-pickup", methods=("GET", "POST"))
def request_pickup():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login", message="Please log in to calculate scrap payouts and schedule a pickup.", next="/request-pickup"))

    if request.method == "GET":
        scrap_data = get_today_scrap_rates()
        user_info = {
            "phone": "",
            "address": "",
            "latitude": 12.9716,
            "longitude": 77.5946,
            "location_label": "Home",
            "city": "Bengaluru",
            "pincode": "560001"
        }
        if user_id:
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT phone, address, latitude, longitude, location_label, city, pincode FROM users WHERE id=%s",
                    (user_id,)
                )
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row:
                    user_info["phone"] = row[0] or ""
                    user_info["address"] = row[1] or ""
                    user_info["latitude"] = float(row[2]) if row[2] is not None else 12.9716
                    user_info["longitude"] = float(row[3]) if row[3] is not None else 77.5946
                    user_info["location_label"] = row[4] or "Home"
                    user_info["city"] = row[5] or "Bengaluru"
                    user_info["pincode"] = row[6] or "560001"
            except Exception:
                pass
        return render_template("request_pickup.html", scrap_data=scrap_data, user_info=user_info)

    # POST - create pickup request
    items_json_raw = request.form.get("items_json", "[]")
    total_price = request.form.get("total_price", "0")
    pickup_date = request.form.get("pickup_date", "")
    time_slot = request.form.get("time_slot", "")
    phone = request.form.get("phone", "").strip()
    address = request.form.get("address", "").strip()
    notes = request.form.get("notes", "").strip()
    lat_raw = request.form.get("latitude")
    lng_raw = request.form.get("longitude")

    try:
        user_lat = float(lat_raw) if lat_raw else 12.9716
        user_lng = float(lng_raw) if lng_raw else 77.5946
    except (ValueError, TypeError):
        user_lat, user_lng = 12.9716, 77.5946

    if not phone or not address or not pickup_date or not time_slot:
        scrap_data = get_today_scrap_rates()
        return render_template(
            "request_pickup.html",
            scrap_data=scrap_data,
            user_info={"phone": phone, "address": address, "latitude": user_lat, "longitude": user_lng},
            message="Please provide phone, address, pickup date, and time slot.",
        )

    user_name = session.get("username", "Guest User")
    try:
        total_val = float(total_price)
    except ValueError:
        total_val = 0.0

    # Parse items to derive scrap_type and estimated_weight
    try:
        parsed_items = json.loads(items_json_raw)
        categories_set = set(item.get("category", "General") for item in parsed_items)
        scrap_type_str = ", ".join(sorted(categories_set)) if categories_set else "General Scrap"
        estimated_weight_val = sum(float(item.get("qty", 0)) for item in parsed_items)
    except Exception:
        scrap_type_str = "General Scrap"
        estimated_weight_val = 0.0

    # Run Distance-Based Nearest Neighbour Algorithm
    nearest_collector, min_dist = find_nearest_collector(user_lat, user_lng)

    if nearest_collector:
        collector_id = nearest_collector["id"]
        distance_km = min_dist
        status = "Assigned"
    else:
        collector_id = None
        distance_km = 0.0
        status = "Pending"

    try:
        ensure_tables_exist()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pickup_requests (
                user_id, user_name, collector_id, scrap_type, estimated_weight,
                items_json, total_price, pickup_date, time_slot, phone, address,
                latitude, longitude, distance_km, notes, status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user_id, user_name, collector_id, scrap_type_str, estimated_weight_val,
                items_json_raw, total_val, pickup_date, time_slot, phone, address,
                user_lat, user_lng, distance_km, notes, status
            ),
        )
        cur.close()
        conn.close()

        if user_id:
            return redirect(url_for("my_pickups", success="true"))
        return render_template(
            "request_pickup.html",
            scrap_data=get_today_scrap_rates(),
            user_info={"phone": phone, "address": address, "latitude": user_lat, "longitude": user_lng},
            success_message="Pickup request scheduled successfully! Nearest collector assigned.",
        )
    except Exception as exc:
        traceback.print_exc()
        return render_template(
            "request_pickup.html",
            scrap_data=get_today_scrap_rates(),
            user_info={"phone": phone, "address": address, "latitude": user_lat, "longitude": user_lng},
            message=f"Error scheduling pickup: {str(exc)}",
        )


@app.route("/scrap-prices")
def scrap_prices():
    if not session.get("user_id"):
        return redirect(url_for("login", message="Please log in to view scrap rates and calculate payouts.", next="/scrap-prices"))
    scrap_data = get_today_scrap_rates()
    return render_template("scrap_prices.html", scrap_data=scrap_data)


@app.route("/my-pickups")
def my_pickups():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    ensure_tables_exist()
    pickups = []
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.id, p.items_json, p.total_price, p.pickup_date, p.time_slot, p.phone, p.address,
                   p.notes, p.status, p.created_at, p.scrap_type, p.estimated_weight, p.actual_weight,
                   p.final_amount, p.actual_items_json, p.distance_km, p.latitude, p.longitude,
                   c.username, c.phone
            FROM pickup_requests p
            LEFT JOIN collectors c ON p.collector_id = c.id
            WHERE p.user_id=%s
            ORDER BY p.created_at DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        for row in rows:
            try:
                parsed_items = json.loads(row[1]) if row[1] else []
            except Exception:
                parsed_items = []

            try:
                actual_items = json.loads(row[14]) if row[14] else []
            except Exception:
                actual_items = []

            pickups.append({
                "id": row[0],
                "items": parsed_items,
                "total_price": float(row[2]) if row[2] else 0.0,
                "pickup_date": str(row[3]),
                "time_slot": row[4],
                "phone": row[5],
                "address": row[6],
                "notes": row[7],
                "status": row[8],
                "created_at": str(row[9]),
                "scrap_type": row[10] or "General Scrap",
                "estimated_weight": float(row[11]) if row[11] else 0.0,
                "actual_weight": float(row[12]) if row[12] else 0.0,
                "final_amount": float(row[13]) if row[13] else (float(row[2]) if row[2] else 0.0),
                "actual_items": actual_items,
                "distance_km": float(row[15]) if row[15] else 0.0,
                "latitude": float(row[16]) if row[16] else 12.9716,
                "longitude": float(row[17]) if row[17] else 77.5946,
                "collector_name": row[18] or "Unassigned",
                "collector_phone": row[19] or "N/A"
            })
    except Exception as exc:
        traceback.print_exc()

    success_msg = "Your scrap pickup request has been scheduled successfully!" if request.args.get("success") == "true" else None
    return render_template("my_pickups.html", pickups=pickups, success_message=success_msg)


@app.route("/cancel-pickup/<int:pickup_id>", methods=("POST",))
def cancel_pickup(pickup_id):
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE pickup_requests SET status='Cancelled' WHERE id=%s AND user_id=%s AND status IN ('Pending', 'Assigned')", (pickup_id, user_id))
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()
    return redirect(url_for("my_pickups"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/profile", methods=("GET", "POST"))
def profile():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    ensure_tables_exist()

    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        label = request.form.get("location_label", "Home").strip()
        city = request.form.get("city", "Bengaluru").strip()
        pincode = request.form.get("pincode", "560001").strip()
        lat_raw = request.form.get("latitude")
        lng_raw = request.form.get("longitude")

        try:
            lat_val = float(lat_raw) if lat_raw else 12.9716
            lng_val = float(lng_raw) if lng_raw else 77.5946
        except (ValueError, TypeError):
            lat_val, lng_val = 12.9716, 77.5946

        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE users
                SET phone=%s, address=%s, location_label=%s, city=%s, pincode=%s, latitude=%s, longitude=%s
                WHERE id=%s
                """,
                (phone, address, label, city, pincode, lat_val, lng_val, user_id)
            )
            cur.close()
            conn.close()
            return render_template(
                "profile.html",
                success_message="Saved location & profile updated successfully!",
                user={
                    "username": session.get("username"),
                    "email": session.get("email"),
                    "phone": phone,
                    "address": address,
                    "location_label": label,
                    "city": city,
                    "pincode": pincode,
                    "latitude": lat_val,
                    "longitude": lng_val
                },
            )
        except Exception as exc:
            traceback.print_exc()
            return render_template(
                "profile.html",
                message=f"Error updating profile: {str(exc)}",
                user={
                    "username": session.get("username"),
                    "email": session.get("email"),
                    "phone": phone,
                    "address": address,
                    "location_label": label,
                    "city": city,
                    "pincode": pincode,
                    "latitude": 12.9716,
                    "longitude": 77.5946
                },
            )

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT username, email, phone, address, latitude, longitude, location_label, city, pincode FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        user_data = {
            "username": row[0] if row else session.get("username"),
            "email": row[1] if row else "",
            "phone": row[2] if row and row[2] else "",
            "address": row[3] if row and row[3] else "",
            "latitude": float(row[4]) if row and row[4] is not None else 12.9716,
            "longitude": float(row[5]) if row and row[5] is not None else 77.5946,
            "location_label": row[6] if row and row[6] else "Home",
            "city": row[7] if row and row[7] else "Bengaluru",
            "pincode": row[8] if row and row[8] else "560001"
        }
        return render_template("profile.html", user=user_data)
    except Exception as exc:
        traceback.print_exc()
        return render_template("profile.html", message=f"Error loading profile: {str(exc)}", user={"username": session.get("username"), "email": "", "phone": "", "address": "", "latitude": 12.9716, "longitude": 77.5946})


# ==========================================
# SCRAP COLLECTOR ROUTES
# ==========================================

@app.route("/collector/login", methods=("GET", "POST"))
def collector_login():
    if request.method == "GET":
        success_msg = None
        if request.args.get("verified") == "true":
            success_msg = "OTP successfully verified! You can now log in."
        return render_template("collector_login.html", success_message=success_msg)

    identifier = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not identifier or not password:
        return render_template("collector_login.html", message="Please provide collector username/email and password.")

    try:
        ensure_tables_exist()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, username, email, password, is_verified, account_status, approval_status FROM collectors WHERE email=%s OR username=%s", (identifier, identifier))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return render_template("collector_login.html", message="Invalid collector credentials.")

        col_id, db_username, db_email, db_password, db_verified, acc_status, app_status = row
        if not check_password_hash(db_password, password):
            return render_template("collector_login.html", message="Invalid collector credentials.")

        if not db_verified:
            session["pending_collector_otp_email"] = db_email
            return render_template("collector_login.html", message="Collector account not verified. Check your email for the OTP.", show_verify_link=True)

        if acc_status == "Deactivated":
            return render_template("collector_login.html", message="Your collector account has been deactivated. Please contact admin.")

        session.clear()
        session["collector_id"] = col_id
        session["collector_name"] = db_username or db_email
        session["is_collector"] = True
        return redirect(url_for("collector_dashboard"))
    except Exception as exc:
        traceback.print_exc()
        return render_template("collector_login.html", message=f"Collector login error: {str(exc)}")


@app.route("/collector/register", methods=("GET", "POST"))
def collector_register():
    if request.method == "GET":
        return render_template("collector_register.html")

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    password = request.form.get("password", "")

    if not username or not email or not password:
        return render_template("collector_register.html", message="Please fill in all required fields.")

    valid, pwd_msg = is_valid_password(password)
    if not valid:
        return render_template("collector_register.html", message=pwd_msg)

    try:
        ensure_tables_exist()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM collectors WHERE email=%s OR username=%s", (email, username))
        if cur.fetchone():
            cur.close()
            conn.close()
            return render_template("collector_register.html", message="Collector with this username or email already exists.")

        hashed_pass = generate_password_hash(password)
        cur.execute(
            """
            INSERT INTO collectors (username, email, phone, password, is_verified, approval_status, account_status)
            VALUES (%s, %s, %s, %s, 0, 'Approved', 'Active')
            """,
            (username, email, phone, hashed_pass),
        )
        col_id = cur.lastrowid

        # generate OTP and expiry
        otp = str(secrets.randbelow(900000) + 100000)
        expires = datetime.now() + timedelta(minutes=15)
        cur.execute(
            "UPDATE collectors SET otp_code=%s, otp_expires=%s WHERE id=%s",
            (otp, expires.strftime("%Y-%m-%d %H:%M:%S"), col_id),
        )

        try:
            send_otp_email(email, otp)
        except Exception as exc:
            app.logger.exception("Unable to send collector registration OTP email")
            print(f"[OTP LOG] Collector registration OTP for {email}: {otp}")

        cur.close()
        conn.close()

        session["pending_collector_otp_email"] = email
        return redirect(url_for("collector_verify_otp"))
    except Exception as exc:
        traceback.print_exc()
        return render_template("collector_register.html", message=f"Registration error: {str(exc)}")


@app.route("/collector/verify-otp", methods=("GET", "POST"))
def collector_verify_otp():
    email = session.get("pending_collector_otp_email")

    if request.method == "GET":
        if not email:
            return render_template("collector_verify.html", message="Please register or log in as a collector first.")
        return render_template("collector_verify.html", email=email)

    code = request.form.get("code", "").strip()
    if not code:
        return render_template("collector_verify.html", email=email, message="Please enter the 6-digit OTP code.")

    if not email:
        return render_template("collector_verify.html", message="Session expired. Please log in again to receive a new OTP code.")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, otp_code, otp_expires, is_verified FROM collectors WHERE email=%s", (email,))
        row = cur.fetchone()

        if not row:
            cur.close()
            conn.close()
            return render_template("collector_verify.html", email=email, message="Collector record not found.")

        col_id, db_otp_code, db_otp_expires, is_verified = row

        if is_verified:
            cur.close()
            conn.close()
            session.pop("pending_collector_otp_email", None)
            return redirect(url_for("collector_login", verified="true"))

        if not db_otp_code or str(db_otp_code).strip() != str(code).strip():
            cur.close()
            conn.close()
            return render_template("collector_verify.html", email=email, message="Invalid OTP code. Please try again.")

        if db_otp_expires:
            if isinstance(db_otp_expires, str):
                try:
                    db_otp_expires = datetime.strptime(db_otp_expires, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
            if datetime.now() > db_otp_expires:
                cur.close()
                conn.close()
                return render_template("collector_verify.html", email=email, message="OTP code has expired. Please click Resend OTP.")

        cur.execute("UPDATE collectors SET is_verified = 1, otp_code = NULL, otp_expires = NULL WHERE id = %s", (col_id,))
        cur.close()
        conn.close()

        session.pop("pending_collector_otp_email", None)
        return redirect(url_for("collector_login", verified="true"))

    except Exception as exc:
        traceback.print_exc()
        return render_template("collector_verify.html", email=email, message=f"Verification error: {str(exc)}")


@app.route("/collector/resend-otp", methods=("POST",))
def collector_resend_otp():
    email = session.get("pending_collector_otp_email")
    if not email:
        return render_template("collector_verify.html", message="Session expired. Please try logging in again.")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM collectors WHERE email=%s", (email,))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return render_template("collector_verify.html", message="Collector record not found.")

        col_id = row[0]
        otp = str(secrets.randbelow(900000) + 100000)
        expires = datetime.now() + timedelta(minutes=15)

        cur.execute(
            "UPDATE collectors SET otp_code=%s, otp_expires=%s WHERE id=%s",
            (otp, expires.strftime("%Y-%m-%d %H:%M:%S"), col_id),
        )
        cur.close()
        conn.close()

        try:
            send_otp_email(email, otp)
        except Exception as exc:
            app.logger.exception("Unable to resend collector OTP")
            return render_template("collector_verify.html", email=email, message=f"Could not resend OTP email: {exc}")

        return render_template("collector_verify.html", email=email, success_message="A new OTP code has been sent to your email.")
    except Exception as exc:
        traceback.print_exc()
        return render_template("collector_verify.html", email=email, message=f"Error resending OTP: {str(exc)}")




@app.route("/collector/dashboard")
def collector_dashboard():
    if not session.get("is_collector") or not session.get("collector_id"):
        return redirect(url_for("collector_login"))

    collector_id = session["collector_id"]
    ensure_tables_exist()
    collector_info = {
        "username": session.get("collector_name"),
        "email": "",
        "phone": "Not set",
        "address": "Not set",
        "latitude": 12.9716,
        "longitude": 77.5946,
        "availability_status": 1
    }
    pending_pickups = []
    my_pickups = []

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT username, email, phone, address, latitude, longitude, availability_status FROM collectors WHERE id=%s", (collector_id,))
        row = cur.fetchone()
        if row:
            collector_info = {
                "username": row[0],
                "email": row[1],
                "phone": row[2] or "Not set",
                "address": row[3] or "Not set",
                "latitude": float(row[4]) if row[4] is not None else 12.9716,
                "longitude": float(row[5]) if row[5] is not None else 77.5946,
                "availability_status": row[6] if row[6] is not None else 1
            }

        # Available pending pickups
        cur.execute(
            """
            SELECT id, user_name, items_json, total_price, pickup_date, time_slot, phone, address, notes, created_at,
                   latitude, longitude, scrap_type, estimated_weight
            FROM pickup_requests WHERE status='Pending' ORDER BY created_at DESC
            """
        )
        for r in cur.fetchall():
            try:
                items = json.loads(r[2]) if r[2] else []
            except Exception:
                items = []
            pending_pickups.append({
                "id": r[0], "user_name": r[1], "items": items, "items_list": items, "total_price": float(r[3]) if r[3] else 0.0,
                "pickup_date": str(r[4]), "time_slot": r[5], "phone": r[6], "address": r[7], "notes": r[8], "created_at": str(r[9]),
                "latitude": float(r[10]) if r[10] else 12.9716, "longitude": float(r[11]) if r[11] else 77.5946,
                "scrap_type": r[12] or "General Scrap", "estimated_weight": float(r[13]) if r[13] else 0.0
            })

        # My assigned/completed pickups
        cur.execute(
            """
            SELECT id, user_name, items_json, total_price, pickup_date, time_slot, phone, address, notes, status, created_at,
                   latitude, longitude, scrap_type, estimated_weight, actual_weight, final_amount, actual_items_json, distance_km
            FROM pickup_requests WHERE collector_id=%s ORDER BY created_at DESC
            """,
            (collector_id,)
        )
        for r in cur.fetchall():
            try:
                items = json.loads(r[2]) if r[2] else []
            except Exception:
                items = []

            try:
                actual_items = json.loads(r[17]) if r[17] else []
            except Exception:
                actual_items = []

            my_pickups.append({
                "id": r[0], "user_name": r[1], "items": items, "items_list": items, "total_price": float(r[3]) if r[3] else 0.0,
                "pickup_date": str(r[4]), "time_slot": r[5], "phone": r[6], "address": r[7], "notes": r[8], "status": r[9], "created_at": str(r[10]),
                "latitude": float(r[11]) if r[11] else 12.9716, "longitude": float(r[12]) if r[12] else 77.5946,
                "scrap_type": r[13] or "General Scrap", "estimated_weight": float(r[14]) if r[14] else 0.0,
                "actual_weight": float(r[15]) if r[15] else 0.0,
                "final_amount": float(r[16]) if r[16] else (float(r[3]) if r[3] else 0.0),
                "actual_items": actual_items,
                "distance_km": float(r[18]) if r[18] else 0.0
            })

        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()

    # Calculate optimal route sequence for active assigned pickups
    active_assigned = [p for p in my_pickups if p["status"] == "Assigned"]
    ordered_route, total_route_dist = optimize_collector_route(collector_info["latitude"], collector_info["longitude"], active_assigned)

    gmaps_origin = f"{collector_info['latitude']},{collector_info['longitude']}"
    gmaps_destination = f"{ordered_route[-1]['latitude']},{ordered_route[-1]['longitude']}" if ordered_route else ""
    waypoints_list = [f"{p['latitude']},{p['longitude']}" for p in ordered_route[:-1]]
    gmaps_waypoints = "|".join(waypoints_list)

    return render_template(
        "collector_dashboard.html",
        collector=collector_info,
        pending_pickups=pending_pickups,
        my_pickups=my_pickups,
        ordered_route=ordered_route,
        total_route_dist=total_route_dist,
        gmaps_origin=gmaps_origin,
        gmaps_destination=gmaps_destination,
        gmaps_waypoints=gmaps_waypoints
    )


@app.route("/collector/update-status", methods=("POST",))
def collector_update_status():
    if not session.get("is_collector") or not session.get("collector_id"):
        return redirect(url_for("collector_login"))

    collector_id = session["collector_id"]
    address = request.form.get("address", "").strip()
    availability = 1 if request.form.get("availability_status") == "1" else 0
    lat_raw = request.form.get("latitude")
    lng_raw = request.form.get("longitude")

    try:
        lat_val = float(lat_raw) if lat_raw else 12.9716
        lng_val = float(lng_raw) if lng_raw else 77.5946
    except (ValueError, TypeError):
        lat_val, lng_val = 12.9716, 77.5946

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE collectors
            SET address=%s, availability_status=%s, latitude=%s, longitude=%s
            WHERE id=%s
            """,
            (address, availability, lat_val, lng_val, collector_id)
        )
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()

    return redirect(url_for("collector_dashboard"))


@app.route("/collector/accept-pickup/<int:pickup_id>", methods=("POST",))
def collector_accept_pickup(pickup_id):
    if not session.get("is_collector") or not session.get("collector_id"):
        return redirect(url_for("collector_login"))

    collector_id = session["collector_id"]
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE pickup_requests SET status='Assigned', collector_id=%s WHERE id=%s AND status='Pending'", (collector_id, pickup_id))
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()
    return redirect(url_for("collector_dashboard"))


@app.route("/collector/verify-pickup/<int:pickup_id>", methods=("POST",))
def collector_verify_pickup(pickup_id):
    if not session.get("is_collector") or not session.get("collector_id"):
        return redirect(url_for("collector_login"))

    collector_id = session["collector_id"]
    actual_items_raw = request.form.get("actual_items_json", "[]")

    try:
        parsed_actual = json.loads(actual_items_raw)
        total_verified_weight = sum(float(it.get("qty", 0)) for it in parsed_actual)
        total_verified_amount = sum(float(it.get("subtotal", 0)) for it in parsed_actual)
    except Exception:
        total_verified_weight = 0.0
        total_verified_amount = 0.0

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE pickup_requests
            SET status='Completed', actual_weight=%s, final_amount=%s, actual_items_json=%s, completed_at=NOW()
            WHERE id=%s AND collector_id=%s
            """,
            (total_verified_weight, total_verified_amount, actual_items_raw, pickup_id, collector_id)
        )
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()
    return redirect(url_for("collector_dashboard"))


@app.route("/collector/complete-pickup/<int:pickup_id>", methods=("POST",))
def collector_complete_pickup(pickup_id):
    if not session.get("is_collector") or not session.get("collector_id"):
        return redirect(url_for("collector_login"))

    collector_id = session["collector_id"]
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE pickup_requests SET status='Completed', completed_at=NOW() WHERE id=%s AND collector_id=%s", (pickup_id, collector_id))
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()
    return redirect(url_for("collector_dashboard"))


@app.route("/collector/logout")
def collector_logout():
    session.clear()
    return redirect("/")


# ==========================================
# USER FEEDBACK & COMPLAINTS
# ==========================================

@app.route("/submit-feedback", methods=("POST",))
def submit_feedback():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))

    user_name = session.get("username", "User")
    collector_id_raw = request.form.get("collector_id")
    pickup_id_raw = request.form.get("pickup_id")
    feedback_type = request.form.get("type", "Feedback").strip()
    subject = request.form.get("subject", "").strip()
    message = request.form.get("message", "").strip()
    rating_raw = request.form.get("rating", "5")

    try:
        collector_id = int(collector_id_raw) if collector_id_raw else None
    except ValueError:
        collector_id = None

    try:
        pickup_id = int(pickup_id_raw) if pickup_id_raw else None
    except ValueError:
        pickup_id = None

    try:
        rating = int(rating_raw)
    except ValueError:
        rating = 5

    try:
        ensure_tables_exist()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO feedback_complaints (user_id, user_name, collector_id, pickup_id, type, subject, message, rating, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'Pending')
            """,
            (user_id, user_name, collector_id, pickup_id, feedback_type, subject, message, rating),
        )
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()

    return redirect(url_for("my_pickups"))


# ==========================================
# ADMIN MODULE ROUTES
# ==========================================

@app.route("/admin")
def admin_alias():
    return redirect(url_for("admin_login"))


@app.route("/admin/login", methods=("GET", "POST"))
def admin_login():
    if request.method == "GET":
        return render_template("admin_login.html")

    identifier = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not identifier or not password:
        return render_template("admin_login.html", message="Please provide admin username/email and password.")

    try:
        ensure_tables_exist()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, username, email, password FROM admins WHERE email=%s OR username=%s", (identifier, identifier))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return render_template("admin_login.html", message="Invalid admin credentials.")

        admin_id, db_username, db_email, db_password = row
        if not check_password_hash(db_password, password):
            return render_template("admin_login.html", message="Invalid admin credentials.")

        session.clear()
        session["admin_id"] = admin_id
        session["admin_name"] = db_username or db_email
        session["is_admin"] = True
        return redirect(url_for("admin_dashboard"))
    except Exception as exc:
        traceback.print_exc()
        return render_template("admin_login.html", message=f"Admin login error: {str(exc)}")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("is_admin") or not session.get("admin_id"):
        return redirect(url_for("admin_login"))

    ensure_tables_exist()

    users_list = []
    collectors_list = []
    pickups_list = []
    feedback_list = []

    metrics = {
        "total_users": 0,
        "total_collectors": 0,
        "active_collectors": 0,
        "total_pickups": 0,
        "pending_pickups": 0,
        "completed_pickups": 0,
        "cancelled_pickups": 0,
        "total_volume_kg": 0.0,
        "total_payout": 0.0,
        "pending_complaints": 0
    }

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Users list
        cur.execute("SELECT id, username, email, phone, address, account_status, is_verified, created_at FROM users ORDER BY created_at DESC")
        for r in cur.fetchall():
            users_list.append({
                "id": r[0], "username": r[1], "email": r[2], "phone": r[3] or "N/A", "address": r[4] or "N/A",
                "account_status": r[5] or "Active", "is_verified": r[6], "created_at": str(r[7])
            })
        metrics["total_users"] = len(users_list)

        # Collectors list
        cur.execute("SELECT id, username, email, phone, address, latitude, longitude, availability_status, approval_status, account_status, is_verified, created_at FROM collectors ORDER BY created_at DESC")
        for r in cur.fetchall():
            is_avail = (r[7] == 1)
            if is_avail:
                metrics["active_collectors"] += 1
            collectors_list.append({
                "id": r[0], "username": r[1], "email": r[2], "phone": r[3] or "N/A", "address": r[4] or "N/A",
                "latitude": float(r[5]) if r[5] else 12.9716, "longitude": float(r[6]) if r[6] else 77.5946,
                "availability_status": 1 if is_avail else 0, "approval_status": r[8] or "Approved",
                "account_status": r[9] or "Active", "is_verified": r[10], "created_at": str(r[11])
            })
        metrics["total_collectors"] = len(collectors_list)

        # Pickups master list
        cur.execute(
            """
            SELECT p.id, p.user_name, p.scrap_type, p.estimated_weight, p.actual_weight, p.total_price, p.final_amount,
                   p.pickup_date, p.time_slot, p.phone, p.address, p.status, p.created_at, p.distance_km, c.username
            FROM pickup_requests p
            LEFT JOIN collectors c ON p.collector_id = c.id
            ORDER BY p.created_at DESC
            """
        )
        for r in cur.fetchall():
            st = r[11] or "Pending"
            if st == "Pending":
                metrics["pending_pickups"] += 1
            elif st == "Completed":
                metrics["completed_pickups"] += 1
                metrics["total_volume_kg"] += float(r[4]) if r[4] else 0.0
                metrics["total_payout"] += float(r[6]) if r[6] else (float(r[5]) if r[5] else 0.0)
            elif st == "Cancelled":
                metrics["cancelled_pickups"] += 1

            pickups_list.append({
                "id": r[0], "user_name": r[1], "scrap_type": r[2] or "General Scrap",
                "estimated_weight": float(r[3]) if r[3] else 0.0, "actual_weight": float(r[4]) if r[4] else 0.0,
                "total_price": float(r[5]) if r[5] else 0.0, "final_amount": float(r[6]) if r[6] else 0.0,
                "pickup_date": str(r[7]), "time_slot": r[8], "phone": r[9], "address": r[10],
                "status": st, "created_at": str(r[12]), "distance_km": float(r[13]) if r[13] else 0.0,
                "collector_name": r[14] or "Unassigned"
            })
        metrics["total_pickups"] = len(pickups_list)

        # Feedback / Complaints list
        cur.execute(
            """
            SELECT f.id, f.user_name, f.type, f.subject, f.message, f.rating, f.status, f.created_at, c.username, f.pickup_id
            FROM feedback_complaints f
            LEFT JOIN collectors c ON f.collector_id = c.id
            ORDER BY f.created_at DESC
            """
        )
        for r in cur.fetchall():
            if r[6] == "Pending":
                metrics["pending_complaints"] += 1
            feedback_list.append({
                "id": r[0], "user_name": r[1], "type": r[2], "subject": r[3], "message": r[4],
                "rating": r[5], "status": r[6], "created_at": str(r[7]), "collector_name": r[8] or "N/A", "pickup_id": r[9] or "N/A"
            })

        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()

    return render_template(
        "admin_dashboard.html",
        admin_name=session.get("admin_name", "Admin"),
        metrics=metrics,
        users=users_list,
        collectors=collectors_list,
        pickups=pickups_list,
        feedback=feedback_list
    )


@app.route("/admin/user/toggle-status/<int:user_id>", methods=("POST",))
def admin_toggle_user_status(user_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))

    status = request.form.get("status", "Active")
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET account_status=%s WHERE id=%s", (status, user_id))
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/delete/<int:user_id>", methods=("POST",))
def admin_delete_user(user_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        # Reset AUTO_INCREMENT sequence so if table is empty or last row deleted, sequence resets
        cur.execute("SELECT MAX(id) FROM users")
        max_res = cur.fetchone()
        max_id = max_res[0] if max_res and max_res[0] is not None else 0
        next_auto = max_id + 1
        cur.execute(f"ALTER TABLE users AUTO_INCREMENT = {next_auto}")
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/collector/approve/<int:collector_id>", methods=("POST",))
def admin_approve_collector(collector_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))

    approval = request.form.get("approval_status", "Approved")
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE collectors SET approval_status=%s WHERE id=%s", (approval, collector_id))
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/collector/toggle-status/<int:collector_id>", methods=("POST",))
def admin_toggle_collector_status(collector_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))

    status = request.form.get("status", "Active")
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE collectors SET account_status=%s WHERE id=%s", (status, collector_id))
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/complaint/resolve/<int:complaint_id>", methods=("POST",))
def admin_resolve_complaint(complaint_id):
    if not session.get("is_admin"):
        return redirect(url_for("admin_login"))

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE feedback_complaints SET status='Resolved' WHERE id=%s", (complaint_id,))
        cur.close()
        conn.close()
    except Exception as exc:
        traceback.print_exc()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)


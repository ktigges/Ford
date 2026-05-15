# Installation Guide

## System Requirements

### Operating System
Linux (Ubuntu 20.04+, Debian 11+) or macOS with Homebrew
Python 3.10+
Docker (required for PostgreSQL container)

### Hardware (Recommended)
2+ CPU cores
4GB RAM minimum (8GB recommended for background jobs)
100GB+ storage for telemetry history

---

## Step 1: System Dependencies

### On Ubuntu/Debian:

Install Python, development tools, and Docker:

```bash
sudo apt-get update
sudo apt-get upgrade

sudo apt-get install python3 python3-venv python3-dev build-essential git
sudo apt-get install docker.io
sudo apt-get install libpq-dev openssl libssl-dev
```

Add your user to the docker group:

```bash
sudo usermod -aG docker $USER
```

### On macOS (with Homebrew):

```bash
brew update
brew install python@3.10
brew install docker
```

Verify Docker is running. If using Docker Desktop, it should be available automatically.

---

## Step 2: Clone Repository

```bash
cd /home/sysadmin/  # Or your preferred directory
git clone https://github.com/ktigges/Ford.git
cd Ford
git checkout dev/server-sandbox  # Or your working branch
```

---

## Step 3: Python Virtual Environment

```bash
# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate  # On Linux/macOS
# OR: venv\Scripts\activate  # On Windows

# Upgrade pip
pip install --upgrade pip setuptools wheel
```

---

## Step 4: Install Python Dependencies

```bash
pip install -r requirements.txt
```

What gets installed: Flask, psycopg2-binary, requests, xgboost, scikit-learn, pandas, numpy, matplotlib, joblib

---

## Step 5: PostgreSQL Database Setup (Docker Container)

### 5A: Run the Installation Script

The install script automatically sets up a Docker container with PostgreSQL and PostGIS:

```bash
cd /path/to/Ford-dev
./scripts/install.sh
```

This script will:
1. Create a Docker volume for data persistence
2. Pull the postgres:16-postgis image (includes PostGIS extension)
3. Create and start the database container
4. Initialize the schema
5. Set up PostGIS spatial extension

The database will be configured with:
- Container: lightning-db
- Host: localhost
- Port: 5432
- User: lightning
- Password: lightningpass
- Database: lightning

### 5B: Verify Database Connection

```bash
python3 << 'EOF'
import config
import db

config.load()
db.init_pool()

# Test connection
try:
    result = db.fetch_one("SELECT 1")
    print("Database connection successful")
except Exception as e:
    print(f"Connection failed: {e}")

db.close_pool()
EOF
```

### 5C: Verify PostGIS is Available

PostGIS is automatically enabled during installation. Verify it:

```bash
python3 << 'EOF'
import config
import db

config.load()
db.init_pool()

try:
    result = db.fetch_one("SELECT postgis_version();")
    if result:
        print(f"PostGIS is enabled: {result[0]}")
    else:
        print("PostGIS extension not found (charger lookups will use Haversine fallback)")
except Exception as e:
    print(f"PostGIS check failed: {e}")

db.close_pool()
EOF
```

---

## Step 6: Configuration

### Create `config.json`

```bash
cp config.json.example config.json
```

Or create manually:

```json
{
  "environment": "development",
  "port": 5000,
  "logging": {
    "level": "INFO",
    "log_sql": false
  },
  "database": {
    "host": "localhost",
    "port": 5432,
    "name": "lightning",
    "user": "lightning",
    "password": "lightningpass",
    "connect_timeout": 10
  },
  "ssl": {
    "enabled": true,
    "cert_file": "certs/cert.pem",
    "key_file": "certs/key.pem"
  }
}
```

---

## Step 7: Backup Tool Setup (pg_dump)

Backups require pg_dump, which is installed inside the Docker container. The app uses docker exec to run backups:

```bash
docker exec lightning-db pg_dump -U lightning lightning > backup.sql
```

This is automatically used by the backup scheduler in the app. Manual backup command:

```bash
# From Ford-dev directory
python3 << 'EOF'
import subprocess
result = subprocess.run(
    ["docker", "exec", "lightning-db", "pg_dump", "-U", "lightning", "lightning"],
    capture_output=True,
    text=True
)
with open("backup.sql", "w") as f:
    f.write(result.stdout)
print("Backup created: backup.sql")
EOF
```

---

## Step 8: Run the Application

### Option A: Development Server

```bash
source venv/bin/activate
python3 app.py
```

Expected output shows database pool initialization and Flask server starting.
### Option B: Production Server (with Gunicorn)

```bash
pip install gunicorn
gunicorn --workers 4 --bind 0.0.0.0:5000 --certfile=certs/cert.pem --keyfile=certs/key.pem app:app
```

---

## Step 9: Verify Installation

### Check Database Connection

```bash
source venv/bin/activate
python3 << 'EOF'
import config
import db

config.load()
db.init_pool()

# Test connection
result = db.fetch_one("SELECT version();")
print(f"Database: {result[0]}")

db.close_pool()
EOF
```

### Check PostGIS Availability

```bash
source venv/bin/activate
python3 << 'EOF'
import config
import db

config.load()
db.init_pool()

result = db.fetch_one("SELECT postgis_version();")
if result:
    print(f"PostGIS: {result[0]}")
else:
    print("PostGIS: Not available (charger lookups will use Haversine)")

db.close_pool()
EOF
```

### Check Python Packages

```bash
source venv/bin/activate
python3 << 'EOF'
packages = ['flask', 'psycopg2', 'requests', 'xgboost', 'sklearn', 'numpy', 'pandas']
for pkg in packages:
    try:
        __import__(pkg)
        print(f"OK: {pkg}")
    except ImportError:
        print(f"MISSING: {pkg}")
EOF
```

---

## Troubleshooting

### Docker Container Issues

Check if container is running:

```bash
docker ps | grep lightning-db
```

Start container if stopped:

```bash
./scripts/start_db_container.sh
```

View container logs:

```bash
docker logs lightning-db
```

### Database Connection Failed

Test from container:

```bash
docker exec lightning-db pg_isready -U lightning -d lightning
```

Check network:

```bash
docker network inspect bridge | grep lightning
```

### PostGIS Issues

PostGIS is automatically installed in the postgres:16-postgis image. If queries fail, reinitialize:

```bash
docker exec lightning-db psql -U lightning -d lightning -c "CREATE EXTENSION IF NOT EXISTS postgis;"
docker exec lightning-db psql -U lightning -d lightning -c "CREATE INDEX IF NOT EXISTS idx_ev_stations_location ON ev_stations USING GIST(location);"
```

### Backup/pg_dump Issues

pg_dump is available in the Docker container. Verify:

```bash
docker exec lightning-db pg_dump --version
```

Test backup:

```bash
docker exec lightning-db pg_dump -U lightning lightning | head -5
```

### Python Virtual Environment Issues

Recreate if broken:

```bash
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Database Schema Errors

Reset schema (WARNING: deletes all data):

```bash
source venv/bin/activate
python3 << 'EOF'
import config
import db

config.load()
db.init_pool()

db.execute("DROP SCHEMA public CASCADE;")
db.execute("CREATE SCHEMA public;")

success, msg = db.apply_schema()
print(f"Schema reset: {msg}")
db.close_pool()
EOF
```

---

## Component Requirements

Python 3.10+ - Required for runtime
Docker - Required for PostgreSQL container
Flask 3.1.3 - Required for web framework
PostgreSQL 16 with PostGIS - Required (Docker container includes both)
psycopg2-binary - Required for database driver
xgboost, scikit-learn, pandas, numpy - Required for ML energy model
Gunicorn - Optional for production deployment

---

## Quick Start Steps

1. Install system dependencies (Python, Docker)
2. Clone repository and checkout dev/server-sandbox
3. Create Python virtual environment
4. Install Python dependencies from requirements.txt
5. Run ./scripts/install.sh to create Docker container and initialize database
6. Update config.json with your settings
7. Run the application (python3 app.py for development)

---

## Next Steps

After installation is complete:

1. Ford OAuth Configuration: Add Ford API credentials to config.json
2. Vehicle Pairing: Use Settings page to authorize your vehicle with OAuth
3. Poller Setup: Enable vehicle polling in Settings for telemetry collection
4. Charger Database: Import public charger data from Settings
5. Trip Planner: Calculate test routes using the Trip Planner page

For detailed component documentation, see README.md.

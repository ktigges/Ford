# Installation Guide

## System Requirements

### Operating System
- Linux (Ubuntu 20.04+, Debian 11+) or macOS with Homebrew
- Python 3.10+
- PostgreSQL 14+ with proper development libraries

### Hardware (Recommended)
- 2+ CPU cores
- 4GB RAM minimum (8GB recommended for background jobs)
- 100GB+ storage for telemetry history

---

## Step 1: System Dependencies

### On Ubuntu/Debian:

```bash
# Update package manager
sudo apt-get update
sudo apt-get upgrade

# Install Python and development tools
sudo apt-get install python3 python3-venv python3-dev build-essential git

# Install PostgreSQL and client tools (REQUIRED)
sudo apt-get install postgresql postgresql-contrib postgresql-client

# Install PostGIS (OPTIONAL but recommended for 5-10x charger lookup speedup)
sudo apt-get install postgresql-16-postgis-3

# Install PostgreSQL client tools for backups (RECOMMENDED)
sudo apt-get install postgresql-client

# Install system libraries for Python packages
sudo apt-get install libpq-dev openssl libssl-dev
```

### On macOS (with Homebrew):

```bash
# Update Homebrew
brew update

# Install Python
brew install python@3.10

# Install PostgreSQL (if not already installed)
brew install postgresql

# Install PostGIS (optional)
brew install postgis

# Verify PostgreSQL is running
brew services list | grep postgresql
```

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
# From the Ford-dev directory with venv activated
pip install -r requirements.txt
```

**What gets installed:**
- `Flask` - Web framework
- `psycopg2-binary` - PostgreSQL driver
- `requests` - HTTP library
- `xgboost` - ML model for energy prediction
- `scikit-learn`, `pandas`, `numpy` - ML dependencies
- `matplotlib`, `joblib` - Data visualization and utilities

---

## Step 5: PostgreSQL Database Setup

### 5A: Create Database and User (if not already done)

```bash
# Connect to PostgreSQL default database
sudo -u postgres psql

# In psql, run:
CREATE DATABASE lightning;
CREATE USER lightning WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE lightning TO lightning;
\q
```

### 5B: Initialize Database Schema

```bash
# From Ford-dev directory with venv activated
python3 << 'EOF'
import config
import db

config.load()
db.init_pool()
success, msg = db.apply_schema()
print(f"Schema applied: {msg}")
db.close_pool()
EOF
```

### 5C: Enable PostGIS (if installed)

```bash
# If you installed postgresql-16-postgis-3
python3 enable_postgis.py
```

**Output should be:**
```
Database connection pool initialised (host=localhost, db=lightning)
Enabling PostGIS extension...
✓ PostGIS extension enabled
✓ PostGIS version: 3.4.0
✓ Spatial index already exists (or created)
✓ PostGIS setup complete!
```

---

## Step 6: Configuration

### Create `config.json`

```bash
cp config.json.example config.json  # If example provided
# OR create from scratch:
```

**Minimal `config.json` template:**

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
    "password": "your_secure_password",
    "connect_timeout": 10
  },
  "ssl": {
    "enabled": true,
    "cert_file": "/path/to/cert.pem",
    "key_file": "/path/to/key.pem"
  },
  "collector": {
    "poll_interval_default": 60,
    "poll_interval_max_failures": 3
  }
}
```

---

## Step 7: Run the Application

### Option A: Development Server

```bash
# Activate virtual environment
source venv/bin/activate

# Run Flask development server
python3 app.py
```

**Output:**
```
Starting MLLighting app (env=development)
Database connection pool initialised...
WARNING: This is a development server. Do not use it in production deployment.
Running on https://localhost:5000
```

### Option B: Production Server (with Gunicorn)

```bash
# Install Gunicorn
pip install gunicorn

# Run with Gunicorn
gunicorn --workers 4 --bind 0.0.0.0:5000 --certfile=cert.pem --keyfile=key.pem app:app
```

---

## Step 8: Verify Installation

### Check Database Connection

```bash
# From Ford-dev directory with venv activated
python3 << 'EOF'
import config
import db

try:
    config.load()
    db.init_pool()
    if db.is_available():
        print("✓ Database connection successful!")
        
        # Check for PostgreSQL version
        result = db.fetch_one("SELECT version();")
        print(f"✓ {result['version'].split(',')[0]}")
        
        # Check for PostGIS
        postgis = db.fetch_one("SELECT postgis_version();")
        if postgis:
            print(f"✓ PostGIS available: {postgis['postgis_version']}")
        else:
            print("⚠ PostGIS not available (optional)")
    else:
        print("✗ Database not available")
except Exception as e:
    print(f"✗ Error: {e}")
finally:
    db.close_pool()
EOF
```

### Check Python Dependencies

```bash
python3 << 'EOF'
required = ['flask', 'psycopg2', 'requests', 'xgboost', 'sklearn', 'numpy', 'pandas']
for pkg in required:
    try:
        __import__(pkg)
        print(f"✓ {pkg}")
    except ImportError:
        print(f"✗ {pkg} - MISSING!")
EOF
```

### Check Flask Routes

```bash
curl -k https://localhost:5000/
# Should return HTML dashboard (or startup_wait.html if initializing)
```

---

## Troubleshooting

### PostgreSQL Connection Failed

```bash
# Check if PostgreSQL is running
sudo systemctl status postgresql

# Start if not running
sudo systemctl start postgresql

# Check connection
psql -U lightning -d lightning -c "SELECT 1;"
```

### PostGIS Not Found

```bash
# Verify PostGIS package is installed
sudo apt list --installed | grep postgis

# If not, install:
sudo apt-get install postgresql-16-postgis-3

# Then enable:
python3 enable_postgis.py
```

### pg_dump Not Found (Backups Failing)

```bash
# Check if postgresql-client is installed
which pg_dump

# If not found, install:
sudo apt-get install postgresql-client

# Verify it works
pg_dump --version
```

### Python Virtual Environment Issues

```bash
# If venv is broken, recreate it
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Database Schema Errors

```bash
# Reset database (WARNING: deletes all data!)
python3 << 'EOF'
import config
import db

config.load()
db.init_pool()

# Drop all tables
db.execute("DROP SCHEMA public CASCADE;")
db.execute("CREATE SCHEMA public;")

# Re-apply schema
success, msg = db.apply_schema()
print(f"Schema reset: {msg}")
db.close_pool()
EOF
```

---

## Dependency Summary

| Component | Type | Status | Used For |
|-----------|------|--------|----------|
| Python 3.10+ | System | **Required** | Runtime |
| PostgreSQL 14+ | System | **Required** | Data storage |
| psycopg2-binary | Python | **Required** | DB connection |
| Flask | Python | **Required** | Web framework |
| xgboost | Python | **Required** | ML energy model |
| PostGIS | System | Optional | 5-10x faster charger lookups |
| postgresql-client | System | Recommended | Backups (pg_dump) |
| Gunicorn | Python | Optional | Production WSGI server |

---

## Quick Start Checklist

- [ ] System dependencies installed (`postgresql`, `python3-dev`, `build-essential`)
- [ ] Python virtual environment created and activated
- [ ] Python dependencies installed (`pip install -r requirements.txt`)
- [ ] PostgreSQL database created and `config.json` configured
- [ ] Database schema applied (`python3 enable_postgis.py`)
- [ ] Flask development server started (`python3 app.py`)
- [ ] Dashboard accessible at https://localhost:5000/
- [ ] PostGIS installed (optional, run `python3 enable_postgis.py` if available)
- [ ] pg_dump available (optional, for backups: `apt-get install postgresql-client`)

---

## Next Steps

1. **Ford OAuth Configuration**: Add Ford API credentials to `config.json`
2. **Vehicle Pairing**: Use Settings → OAuth Config to authorize your vehicle
3. **Poller Setup**: Enable polling in Settings → General Options
4. **Charger Database**: Import public charger data (Settings → Chargers)
5. **Trip Planner**: Calculate test routes (Trip Planner page)

For detailed component documentation, see `README.md`.

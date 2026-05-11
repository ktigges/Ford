# Startup Issues Diagnosis & Solutions

## PostgreSQL and PostGIS Setup

PostGIS is automatically included in your Docker PostgreSQL container. If charger lookups are slow, verify PostGIS is enabled:

Check if PostGIS is available:

```bash
source venv/bin/activate
python3 << 'EOF'
import config
import db

config.load()
db.init_pool()

result = db.fetch_one("SELECT postgis_version();")
if result:
    print(f"PostGIS enabled: {result[0]}")
else:
    print("PostGIS not available")
    
db.close_pool()
EOF
```

If PostGIS is not available, enable it in the container:

```bash
docker exec lightning-db psql -U lightning -d lightning -c "CREATE EXTENSION IF NOT EXISTS postgis;"
```

---

## Backup (pg_dump) Setup

pg_dump is available in the Docker container. The app automatically uses it for scheduled backups via docker exec.

Test backup functionality:

```bash
docker exec lightning-db pg_dump -U lightning lightning | head -5
```

If you need to restore a backup:

```bash
docker exec -i lightning-db psql -U lightning lightning < backup.sql
```

---

## Startup "Loses Config" Issue - What Was Fixed

Previous versions had startup configuration issues that have been fixed:

Problems that were occurring:
- Database settings read during startup would timeout if DB was slow
- No retries for slow database connections
- Startup wait page did not auto-refresh
- Settings would fall back to defaults

Fixes applied:
- Retry logic added (up to 5 attempts) to read DB settings during startup
- 1-second delays between retry attempts
- Auto-refresh every 2 seconds on startup_wait.html page
- Better logging for debugging startup issues

---

## Startup Sequence

How the application starts:

1. App starts immediately, accepts requests
2. Daemon thread attempts to read DB settings (up to 5 retries)
3. During delay, requests get 503 with auto-refresh every 2 seconds
4. Page auto-reloads - user doesn't need to refresh manually
5. Settings gracefully fall back to defaults if DB unavailable (with logging)

---

## Testing the Improvements

### Test 1: Verify Settings Are Retained
```bash
# After restart, check logs:
tail -20 logs/lightning_app.log | grep -i "startup\|develop\|delay"

# Should see:
# INFO: Read startup settings from database (develop=off, delay=30)
```

### Test 2: Browser Auto-Refresh
1. Restart the app: `./restart.sh`
2. Go to https://devbox.tigges-us.com:5000/
3. Should see "startup_wait.html" with spinner
4. Page will auto-refresh every 2 seconds
5. When ready, you'll be automatically taken to dashboard

### Test 3: Check Charger Lookups (Even Without PostGIS)
1. Go to Trip Planner
2. Calculate a route
3. Check logs:
```bash
tail -50 logs/lightning_app.log | grep -i "charger"

# Should see fallback messages:
# ERROR: Charger search failed (ST_DWithin): type "geography" does not exist... falling back to Haversine
```

---

## Configuration Recommendations

### For Production Stability

1. **Keep startup_delay at 0 or low** (currently 30 seconds)
   - Only needed if DB requires warm-up time
   - Set in Settings → Startup Options
   - Default is fine for most systems

2. **Enable autostart_poller** (currently off)
   - Automatically starts polling on app boot
   - Set in Settings → Poller

3. **Install PostGIS** (optional, improves performance 5-10x)
   - Trip planning is still functional without it
   - Worthwhile if you frequently use Trip Planner

4. **Install pg_dump** (recommended)
   - Enables working backup scheduler
   - Critical for data protection

---

## File Changes

| File | Changes |
|------|---------|
| `schema.sql` | Added `CREATE EXTENSION IF NOT EXISTS postgis;` |
| `app.py` | Enhanced `_delayed_startup()` with retry logic |
| `templates/startup_wait.html` | Added auto-refresh every 2 seconds |
| `enable_postgis.py` | New script to enable PostGIS (requires system install) |

---

## Next Steps

1. **Immediate**: Test the app with new startup sequence
   - Restart app
   - Verify auto-refresh works
   - Check settings are loaded correctly
   
2. **Short-term**: Install pg_dump
   ```bash
   sudo apt-get install postgresql-client
   ```
   
3. **Optional**: Install PostGIS for 5-10x charger lookup speedup
   ```bash
   sudo apt-get install postgresql-16-postgis-3
   python enable_postgis.py
   ```

---

## Monitoring

Add these to your monitoring to catch future issues:

**Check PostgreSQL extension status**:
```bash
docker exec postgres psql -U lightning -d lightning -c "\dx" | grep postgis
```

**Check backup status**:
```bash
tail logs/lightning_app.log | grep -i backup
```

**Check startup delay settings**:
Visit Settings → Startup Options and note the configured delay.

**Check charger lookup performance**:
Calculate a route and check logs for "Charger search" timing.

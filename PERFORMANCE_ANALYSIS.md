# Performance Analysis: Ford Dev Trip Planner

## Executive Summary
The application exhibits post-restart slowness, primarily caused by inefficient database queries for charger lookups and suboptimal use of spatial indexes. The trip planning pipeline can make up to 24 database queries per route calculation.

---

## Identified Bottlenecks

### 1. **N+1 Query Problem in Charger Lookups** (CRITICAL)
**Location**: `trip_planner.py` - `optimize_charging_stops()` function (lines 1370-1412)

**Issue**: 
- The function loops up to 8 times (max charging stops)
- For each stop, it calls `find_nearby_chargers()` up to 3 times (with radii: 25km, 40km, 60km)
- **Total**: Up to 8 × 3 = **24 database queries per trip calculation**
- Each query is inefficient (see below)

**Example**: Payson AZ → Santa Fe NM trip = 2-3 charging stops × 3 queries each = **6-9 DB queries**

**Impact**: High latency (visible as "boggy" feeling), especially on cold start or with connection pool contention.

---

### 2. **Inefficient Spatial Query** (HIGH PRIORITY)
**Location**: `trip_planner.py` - `find_nearby_chargers()` function (lines 1285-1320)

**Current Query**:
```sql
WHERE SQRT(POW(s.latitude - ?, 2) + POW(s.longitude - ?, 2)) * 111 < ?
```

**Problems**:
- Uses **Haversine approximation** with arithmetic functions (POW, SQRT)
- Does NOT use the existing PostGIS GIST index (`idx_ev_stations_location`)
- PostgreSQL cannot efficiently optimize the distance calculation, must scan many rows
- GIST index (`ll_to_earth()`) is available but unused

**Better Approach**: Use PostGIS `ST_DWithin()`:
```sql
WHERE ST_DWithin(
    ST_Point(longitude, latitude)::geography,
    ST_Point(?, ?)::geography,
    ?  -- distance in meters
)
```

**Benefit**: 
- Utilizes the GIST spatial index
- ~10-50x faster for large charger datasets
- PostgreSQL can use index to quickly filter candidates

---

### 3. **Repeated Charger Lookups for Same Region**
**Location**: `optimize_charging_stops()` - charger search loop

**Issue**: 
- If 3 charging stops are within a city, each queries for chargers independently
- Same chargers may be retrieved multiple times
- No caching of charger data during trip planning

**Solution Ideas**:
1. Cache charger data in memory at startup (load all active chargers into a spatial in-memory index)
2. Batch charger queries (pass multiple stop locations in one query)
3. Use in-memory spatial index (e.g., Python library like `rtree`) for fast lookups during trip planning

---

### 4. **Cold-Start Database Connection Pool**
**Location**: `db.py` - Pool initialization

**Current**: 
- Pool config: `minconn=2, maxconn=15` (expanded from 5)
- Pool is initialized at app startup but first query still "warms" connections

**Observations**:
- First trip calculation after restart is noticeably slower
- Likely due to: DB connection establishment, charger table indexes warming, model loading

**Mitigation**:
- Pre-warm connection pool: Open 2+ connections at startup
- Pre-load charger data into memory (see #3 above)
- Load energy model in background thread during startup

---

### 5. **Weather Fetch Parallelization** (MEDIUM)
**Location**: `trip_planner.py` - `get_route_weather_timeline()` function

**Current**: 
- ThreadPoolExecutor with 4 workers
- 4-second timeout per weather fetch
- Fetches ~10 weather checkpoints in parallel

**Impact**: 
- Can add 4-8 seconds to route calculation
- Falls back gracefully if timeout, but reduces accuracy

**Optimization**:
- Weather fetching is parallelized, so less critical
- Could reduce number of waypoints (currently ~20-mile intervals)

---

### 6. **Energy Model Loading** (LOW)
**Location**: `energy_model.py` module initialization

**Issue**: 
- ML model deserialized from disk on first import
- Minimal impact if model is small (<10MB)
- But worth profiling

**Optimization**:
- Could lazy-load on first trip calculation to defer startup delay
- Or load in background thread at app startup

---

## Performance Profile: Cold Start (Post-Restart)

### Timeline
1. **App startup** (~2-3 sec)
   - Flask initialization
   - DB pool creation (fast)
   - Energy model import (unknown, needs profiling)
   
2. **First trip calculation** (~10-15 sec total)
   - Geocoding source/destination: 2-4 sec (external API)
   - OSRM routing: 2-4 sec (external API)
   - Weather fetch: 2-4 sec (parallel, 4 workers)
   - Charger lookups: **3-6 sec** ← **MAIN BOTTLENECK** (24 DB queries)
   - Energy calculations: <500ms
   - Total: ~10-15 seconds

3. **Subsequent trip calculations** (~6-10 sec)
   - Pool warmed, index cached
   - Charger lookups still ~3-5 sec

---

## Recommendations (Priority Order)

### 🔴 CRITICAL - Do First
**1. Fix Spatial Query (High Impact, Low Effort)**
- Rewrite `find_nearby_chargers()` to use PostGIS `ST_DWithin()` instead of Haversine
- Expected improvement: **5-10x faster charger lookups** (~300-600ms → 30-60ms)
- Effort: ~30 min

**2. In-Memory Charger Cache**
- Load all US chargers into memory at startup (or lazy on first trip)
- Use `rtree` or similar spatial index for O(log n) lookups
- Skip DB queries for charger discovery
- Expected improvement: **10-20x faster** (~3-5 sec → ~300-500ms)
- Effort: ~2-3 hours

### 🟡 HIGH - Do Second
**3. Reduce N+1 Queries**
- Batch multiple stop locations into single query
- Or use memory cache (see #2)
- Expected improvement: Already covered by #1 and #2

**4. Connection Pool Pre-warming**
- Initialize 2-4 connections at startup
- Expected improvement: ~200-300ms on first query
- Effort: ~15 min

### 🟢 MEDIUM - Nice to Have
**5. Weather Fetch Optimization**
- Reduce waypoint density (currently ~20 miles, could be ~30-40 miles)
- Async weather fetch in background (return initial results faster)
- Expected improvement: ~1-2 sec
- Effort: ~1 hour

**6. Background Model Loading**
- Move energy model import to background thread
- Defer until first trip calculation or warm at startup
- Expected improvement: ~1-2 sec app startup time
- Effort: ~1 hour

---

## Quick Wins (Already Applied)

✅ **DB Pool Expansion**: Increased from 5 to 15 max connections
- Helps with concurrent requests
- Doesn't fix the root issue (inefficient queries)

✅ **Virtual Charging Stops**: Fallback when DB has sparse coverage
- Ensures trip plans complete even with sparse charger DB
- Doesn't solve performance, but improves reliability

---

## Testing Strategy

### Baseline Measurement (Current State)
```bash
# Measure charger lookup time
time curl https://devbox.tigges-us.com:5000/trip-planner \
  -d "source=Payson, AZ&destination=Santa Fe, NM&action=calculate" \
  | grep "Duration"
# Current: ~10-15 seconds
```

### After Optimization
- Target: **5-7 seconds total** for first trip
- Target: **3-4 seconds total** for subsequent trips

---

## Code Locations

| Issue | File | Lines |
|-------|------|-------|
| N+1 queries | `trip_planner.py` | 1370-1412 |
| Inefficient spatial query | `trip_planner.py` | 1285-1320 |
| Charger lookup loop | `trip_planner.py` | 1406-1413 |
| DB pool config | `db.py` | Pool initialization |
| Energy model | `energy_model.py` | Top-level import |

---

## Next Steps

1. **Profile First** (5 min)
   - Add timing logs to trip_planner.py
   - Measure charger lookup time: ~300-500ms per query
   - Measure weather fetch time: ~2-4 sec
   
2. **Implement Spatial Fix** (30 min)
   - Update `find_nearby_chargers()` to use `ST_DWithin()`
   - Test with existing charger DB
   - Verify 5-10x speedup

3. **Implement Cache** (2-3 hours)
   - Add charger data cache at app startup
   - Use `rtree` or similar for spatial indexing
   - Fallback to DB if cache is empty

4. **Measure Results** (10 min)
   - Run full trip calculation
   - Compare before/after
   - Document improvements

---

## Dependencies

- **PostGIS**: Already available (GIST index exists)
- **rtree**: Optional, for in-memory spatial indexing
  ```bash
  pip install rtree  # Requires libspatialindex system library
  ```

---

## Long-Term Improvements

- Switch to dedicated map tile server (reduce OSRM latency)
- Cache route polylines for common routes
- Implement trip plan history (reduce re-calculation)
- Add instrumentation/monitoring (APM tool) for production

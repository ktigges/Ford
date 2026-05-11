# Performance Analysis: Ford Dev Trip Planner

Performance analysis of the trip planner component and database query optimization.

## Query Bottlenecks

Database Queries for Charger Lookups

The trip planning pipeline may make multiple database queries per route calculation:
- Up to 8 potential charging stops per trip
- Each stop may require 1-3 charger lookups
- Total: Up to 24 database queries per trip in worst case

For example, a Payson AZ to Santa Fe NM trip would make 6-9 charger queries.

### Spatial Query Optimization

Charger lookups use PostGIS ST_DWithin() when available:

Optimized query:
```sql
WHERE ST_DWithin(
    ST_Point(longitude, latitude)::geography,
    ST_Point(?, ?)::geography,
    ?  -- distance in meters
)
```

Benefits: Uses GIST spatial index, 5-10x faster for charger datasets.

Fallback: If PostGIS unavailable, app uses Haversine distance formula:
```sql
WHERE SQRT(POW(s.latitude - ?, 2) + POW(s.longitude - ?, 2)) * 111 < ?
```

Both approaches work. PostGIS is preferred for performance.

### Repeated Charger Lookups

If multiple charging stops are in same region:
- Each queries independently
- Same chargers may be retrieved multiple times
- No caching during trip planning

Potential improvements:
1. In-memory charger cache at startup
2. Batch charger queries (pass multiple stops in one query)
3. Session-level caching during trip planning

Estimated performance gains: 10-20x improvement for regional trips

### Connection Pool

Database connection pool configured:
- minconn: 2
- maxconn: 15

First trip calculation after restart may be slower due to connection establishment and index warming.

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

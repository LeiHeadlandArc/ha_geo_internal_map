import datetime
import getpass
import http.server
import json
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import zipfile

# ── Settings ─────────────────────────────────────────────────────────────────
PAGE = "geophys_internal.html"
DATA_FILE = "geophys_rlb.geojson"          # permanent RLB records (EPSG:4326)
BACKUP_DIR = "backups"
KEEP_BACKUPS = 10
LOCK_FILE = DATA_FILE + ".lock"
LOCK_TIMEOUT = 20      # seconds to wait for someone else's save to finish
STALE_LOCK = 120       # a lock older than this is treated as left over from a crash
MAX_UPLOAD_MB = 50
ALLOWED_EXT = {".zip", ".gpkg", ".kml", ".kmz", ".geojson", ".json"}
EDITABLE_PROPS = {"proj_code", "sub_label", "descriptio", "Notes"}
UK_IE_BBOX = (-11.0, 49.0, 2.5, 61.0)      # lon_min, lat_min, lon_max, lat_max

USER = getpass.getuser()

# Always work in the folder this script lives in (works on C:, M:, Q:, A: ...)
os.chdir(os.path.dirname(os.path.abspath(__file__)))

try:
    from osgeo import ogr, osr
    ogr.UseExceptions()
    osr.UseExceptions()
    GDAL_OK = True
except ImportError:
    GDAL_OK = False


class UserError(Exception):
    """An error whose message is shown to the user."""


# ── Reading uploaded files (tender preview) ──────────────────────────────────
def ogr_source(path, ext):
    if ext != ".zip":
        return path
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
    shps = [n for n in names if n.lower().endswith(".shp")]
    if len(shps) != 1:
        raise UserError(f"The zip must contain exactly one .shp file (found {len(shps)}).")
    stem = shps[0][:-4].lower()
    if not any(n.lower() == stem + ".prj" for n in names):
        raise UserError("The shapefile has no .prj file, so its coordinate system is unknown.")
    return f"/vsizip/{path.replace(os.sep, '/')}/{shps[0]}"


def round_coords(c, nd=7):
    if isinstance(c, (int, float)):
        return round(c, nd)
    return [round_coords(x, nd) for x in c]


def to_wgs84(path, ext):
    ds = ogr.Open(ogr_source(path, ext))
    if ds is None:
        raise UserError("The file could not be read.")
    try:
        layers = [ds.GetLayer(i) for i in range(ds.GetLayerCount())]
        layers = [l for l in layers if l.GetGeomType() != ogr.wkbNone]
        if not layers:
            raise UserError("No spatial layer found in the file.")
        if ext == ".gpkg" and len(layers) != 1:
            raise UserError(f"The GeoPackage must contain exactly one spatial layer (found {len(layers)}).")

        wgs84 = osr.SpatialReference()
        wgs84.ImportFromEPSG(4326)
        wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        features, crs_names = [], set()
        xmin = ymin = float("inf")
        xmax = ymax = float("-inf")

        for lyr in layers:
            srs = lyr.GetSpatialRef()
            if srs is None:
                raise UserError(f"Layer '{lyr.GetName()}' has no coordinate system information.")
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            auth, code = srs.GetAuthorityName(None), srs.GetAuthorityCode(None)
            crs_names.add(f"{auth}:{code}" if auth and code else srs.GetName())
            ct = osr.CoordinateTransformation(srs, wgs84)

            lyr.ResetReading()
            for feat in lyr:
                g = feat.GetGeometryRef()
                if g is None or g.IsEmpty():
                    continue
                g = g.Clone()
                if g.HasCurveGeometry():
                    g = g.GetLinearGeometry()
                g.FlattenTo2D()
                g.Transform(ct)
                e = g.GetEnvelope()   # (minX, maxX, minY, maxY)
                xmin, xmax = min(xmin, e[0]), max(xmax, e[1])
                ymin, ymax = min(ymin, e[2]), max(ymax, e[3])
                geom = json.loads(g.ExportToJson())
                geom["coordinates"] = round_coords(geom.get("coordinates", []))
                features.append({"type": "Feature", "geometry": geom, "properties": feat.items()})
    finally:
        ds = None

    if not features:
        raise UserError("The file contains no features with geometry.")
    if xmin < -180 or xmax > 180 or ymin < -90 or ymax > 90:
        raise UserError("Coordinates are out of range after conversion - "
                        "the coordinate system in the file is probably wrong.")

    warning = None
    b = UK_IE_BBOX
    if xmax < b[0] or xmin > b[2] or ymax < b[1] or ymin > b[3]:
        warning = "The data is outside the UK and Ireland - please check its coordinate system."

    return {"type": "FeatureCollection", "features": features,
            "source_crs": ", ".join(sorted(crs_names)), "warning": warning}


# ── Safe writing of the shared data file ─────────────────────────────────────
class DataLock:
    """Only one person (on any computer) can change the data file at a time."""

    def __enter__(self):
        start = time.time()
        while True:
            try:
                fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write(f"{USER} {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(LOCK_FILE) > STALE_LOCK:
                        os.remove(LOCK_FILE)
                        continue
                except OSError:
                    pass
                if time.time() - start > LOCK_TIMEOUT:
                    try:
                        with open(LOCK_FILE, encoding="utf-8") as f:
                            holder = f.read().strip()
                    except OSError:
                        holder = "someone else"
                    raise UserError(f"The data is being saved by {holder}. Please try again in a moment.")
                time.sleep(0.5)

    def __exit__(self, *args):
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


def read_data():
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def prune_backups():
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".geojson"))
    for f in files[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, f))
        except OSError:
            pass


def write_data(data):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = os.path.splitext(DATA_FILE)[0]
    shutil.copy2(DATA_FILE, os.path.join(BACKUP_DIR, f"{stem}_{stamp}_{USER}.geojson"))
    prune_backups()

    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    for _ in range(20):
        try:
            os.replace(tmp, DATA_FILE)   # atomic: the file is never half-written
            return
        except PermissionError:
            time.sleep(0.5)
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise UserError("Could not update the data file - it may be open in another program "
                    "(e.g. being edited in QGIS). Nothing was changed.")


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def fid_of(feature):
    return (feature.get("properties") or {}).get("fid")


# ── API actions ──────────────────────────────────────────────────────────────
def api_save(body):
    feats = body.get("features") or []
    props_in = body.get("props") or {}
    code = str(props_in.get("proj_code") or "").strip().upper()
    if not code:
        raise UserError("Project code is required.")
    if not feats:
        raise UserError("There is nothing to save.")
    for f in feats:
        gtype = (f.get("geometry") or {}).get("type")
        if gtype not in ("Polygon", "MultiPolygon"):
            raise UserError("Only polygon data can be saved as a permanent RLB.")

    clean = {}
    for k in EDITABLE_PROPS:
        v = props_in.get(k)
        v = str(v).strip() if v is not None else ""
        clean[k] = v or None
    clean["proj_code"] = code

    stamp = now_str()
    with DataLock():
        data = read_data()   # always re-read the latest version inside the lock
        fids = []
        for f in data.get("features", []):
            try:
                fids.append(int(fid_of(f)))
            except (TypeError, ValueError):
                pass
        next_fid = max(fids or [0])
        new_fids = []
        for f in feats:
            next_fid += 1
            props = dict(clean)
            props.update({
                "fid": next_fid,
                "CreatedBy": USER, "CreatedAt": stamp,
                "EditedBy": USER, "EditedAt": stamp,
            })
            data["features"].append({"type": "Feature", "properties": props, "geometry": f["geometry"]})
            new_fids.append(next_fid)
        write_data(data)

    print(f"[{stamp}] {USER} saved {len(new_fids)} record(s) for {code}: fid {new_fids}")
    return {"fids": new_fids}


def api_delete(body):
    fid = body.get("fid")
    with DataLock():
        data = read_data()
        feats = data.get("features", [])
        idx = next((i for i, f in enumerate(feats) if str(fid_of(f)) == str(fid)), None)
        if idx is None:
            raise UserError(f"Record fid {fid} was not found - it may already have been deleted.")
        props = feats[idx].get("properties") or {}
        owner = str(props.get("CreatedBy") or "")
        if owner.lower() != USER.lower():
            raise UserError(f"You can only delete records you created. "
                            f"This one was created by {owner or 'unknown'}.")
        del feats[idx]
        write_data(data)
    print(f"[{now_str()}] {USER} deleted fid {fid} ({props.get('proj_code')})")
    return {"deleted": fid}


# ── HTTP server ──────────────────────────────────────────────────────────────
def free_port(start=8000):
    for port in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("No free port found between 8000 and 8049.")


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass

    def send_json(self, status, obj):
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == "/api/whoami":
            return self.send_json(200, {"user": USER})
        return super().do_GET()

    def do_POST(self):
        # Read the whole request FIRST - replying before reading it makes the
        # browser show "Failed to fetch" instead of our error message
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length > MAX_UPLOAD_MB * 1024 * 1024:
            self.close_connection = True
            return self.send_json(400, {"error": f"The file is larger than {MAX_UPLOAD_MB} MB."})
        data = self.rfile.read(length) if length > 0 else b""

        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/preview":
                return self.send_json(200, self.handle_preview(data))
            if path in ("/api/save", "/api/delete"):
                try:
                    body = json.loads(data.decode("utf-8") or "{}")
                except ValueError:
                    raise UserError("The request could not be read.")
                result = api_save(body) if path == "/api/save" else api_delete(body)
                return self.send_json(200, result)
            return self.send_json(404, {"error": "Unknown endpoint."})
        except UserError as e:
            return self.send_json(400, {"error": str(e)})
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self.send_json(500, {"error": f"Server error: {type(e).__name__}: {e}"})
            except Exception:
                pass

    def handle_preview(self, data):
        if not GDAL_OK:
            raise UserError("GDAL is not available. Please start the map with "
                            "'Open RLB Map.bat' so it uses QGIS's Python.")
        name = urllib.parse.unquote(self.headers.get("X-Filename", ""))
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED_EXT:
            raise UserError(f"Unsupported file type '{ext or '?'}'. "
                            "Use zipped SHP, GPKG, KML/KMZ or GeoJSON.")
        if not data:
            raise UserError("The file is empty.")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "upload" + ext)
            with open(path, "wb") as f:
                f.write(data)
            try:
                return to_wgs84(path, ext)
            except UserError:
                raise
            except Exception as e:
                raise UserError(f"The file could not be read: {e}")


port = free_port()
url = f"http://127.0.0.1:{port}/{PAGE}"

with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as httpd:
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print("=" * 60)
    print(" Geophysics RLB Map is running")
    print(f" {url}")
    print(f" User: {USER} | GDAL: {'OK' if GDAL_OK else 'NOT FOUND'}")
    print(f" Python: {sys.executable}")
    print()
    print(" Keep this window open while using the map.")
    print(" Close this window to stop the map.")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
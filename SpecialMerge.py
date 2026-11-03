import re
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsWkbTypes, QgsFeature, QgsField,
    QgsGeometry, QgsCoordinateReferenceSystem, QgsCoordinateTransform
)
from qgis.PyQt.QtCore import QVariant
from qgis.utils import iface

TARGET_CRS = "EPSG:4326"          # final CRS of the merged layer
KEEP_ORIGINAL_FIELDS = True        # also copy original attributes (as text) for tidying
SUB_FIELD = "sub_label"            # name of the field for the part after proj_code
OUT_NAME = "RLB_merged"

# 4 letters + 2 digits at the start, then optional rest
NAME_RE = re.compile(r'^\s*([A-Za-z]{4}\d{2})(.*)$')

def parse_name(name):
    m = NAME_RE.match(name)
    if not m:
        return None, None
    code = m.group(1).upper()
    rest = m.group(2).strip(" \t-_")   # drop leading/trailing spaces, '-' and '_'
    return code, (rest or None)

# ── Collect selected polygon layers ──────────────────────────────────────────
layers = [l for l in iface.layerTreeView().selectedLayers()
          if isinstance(l, QgsVectorLayer)
          and l.geometryType() == QgsWkbTypes.PolygonGeometry]

if not layers:
    raise SystemExit("Select at least one polygon layer in the Layers panel.")

dest_crs = QgsCoordinateReferenceSystem(TARGET_CRS)
project = QgsProject.instance()

# ── Build output fields ──────────────────────────────────────────────────────
new_fields = [
    QgsField("proj_code", QVariant.String, "String", 10),
    QgsField(SUB_FIELD, QVariant.String, "String", 255),
    QgsField("source_layer", QVariant.String, "String", 255),
    QgsField("source_crs", QVariant.String, "String", 50),
]
reserved = {f.name().lower() for f in new_fields}

orig_field_names = []
if KEEP_ORIGINAL_FIELDS:
    seen = set(reserved)
    for lyr in layers:
        for f in lyr.fields():
            key = f.name().lower()
            if key in seen or key == "fid":
                continue
            seen.add(key)
            orig_field_names.append(f.name())

out = QgsVectorLayer(f"MultiPolygon?crs={TARGET_CRS}", OUT_NAME, "memory")
prov = out.dataProvider()
prov.addAttributes(new_fields + [QgsField(n, QVariant.String, "String", 255)
                                 for n in orig_field_names])
out.updateFields()
out_names = [f.name() for f in out.fields()]

# ── Copy features ────────────────────────────────────────────────────────────
total, unmatched, geom_errors = 0, [], []

for lyr in layers:
    code, sub = parse_name(lyr.name())
    if code is None:
        unmatched.append(lyr.name())

    xform = QgsCoordinateTransform(lyr.crs(), dest_crs, project)
    src_crs_id = lyr.crs().authid() or "(none)"
    lyr_field_lookup = {f.name().lower(): f.name() for f in lyr.fields()}

    batch = []
    for feat in lyr.getFeatures():
        geom = QgsGeometry(feat.geometry())          # own copy, keeps it alive
        if geom.isNull() or geom.isEmpty():
            geom_errors.append(f"{lyr.name()} fid={feat.id()} (empty geometry)")
            continue
        if geom.transform(xform) != 0:
            geom_errors.append(f"{lyr.name()} fid={feat.id()} (transform failed)")
            continue
        geom.get().dropZValue()
        geom.get().dropMValue()
        geom.convertToMultiType()

        attrs = [None] * len(out_names)
        attrs[out_names.index("proj_code")] = code
        attrs[out_names.index(SUB_FIELD)] = sub
        attrs[out_names.index("source_layer")] = lyr.name()
        attrs[out_names.index("source_crs")] = src_crs_id
        for n in orig_field_names:
            src_name = lyr_field_lookup.get(n.lower())
            if src_name is not None:
                v = feat[src_name]
                attrs[out_names.index(n)] = None if v is None or str(v) == "NULL" else str(v)

        f = QgsFeature(out.fields())
        f.setGeometry(geom)
        f.setAttributes(attrs)
        batch.append(f)

    prov.addFeatures(batch)
    total += len(batch)
    print(f"{lyr.name():<35} -> proj_code={code}, {SUB_FIELD}={sub}, {len(batch)} feature(s)")

out.updateExtents()
project.addMapLayer(out)

print(f"\nMerged {len(layers)} layer(s), {total} feature(s) into '{OUT_NAME}' ({TARGET_CRS})")
if unmatched:
    print("\nLayer names NOT matching 4 letters + 2 digits (proj_code left empty):")
    for n in unmatched:
        print("  ", n)
if geom_errors:
    print("\nSkipped features:")
    for e in geom_errors:
        print("  ", e)
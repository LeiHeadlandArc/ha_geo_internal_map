from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer, QgsWkbTypes

TARGET = "entities"   

view = iface.layerTreeView()
nodes = view.selectedNodes()


targets = []
for node in nodes:
    if isinstance(node, QgsLayerTreeGroup):
        for lnode in node.findLayers():       
            if lnode.name().lower().startswith(TARGET):
                targets.append(lnode)
    elif isinstance(node, QgsLayerTreeLayer):
        targets.append(node)

# 去重
seen, uniq = set(), []
for n in targets:
    if n.layerId() not in seen:
        seen.add(n.layerId())
        uniq.append(n)

by_parent = {}
for n in uniq:
    parent = n.parent()
    if not isinstance(parent, QgsLayerTreeGroup) or parent.parent() is None:
        print(f"skip {n.name()}：not in group ")
        continue
    by_parent.setdefault(parent, []).append(n)

geom_names = {
    QgsWkbTypes.PointGeometry: "Point",
    QgsWkbTypes.LineGeometry: "Line",
    QgsWkbTypes.PolygonGeometry: "Polygon",
}

for parent, lnodes in by_parent.items():
    gname = parent.name()
    for n in lnodes:
        lyr = n.layer()
        if lyr is None:
            continue
        new_name = gname
        if len(lnodes) > 1 and hasattr(lyr, "geometryType"):
            new_name = f"{gname}_{geom_names.get(lyr.geometryType(), 'Other')}"
        old = lyr.name()
        lyr.setName(new_name)
        print(f"{old} -> {new_name}")   
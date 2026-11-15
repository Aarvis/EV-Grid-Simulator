"""Estimate mapped footprint, not an official utility service territory."""
import json
import math
from pathlib import Path
root=Path(__file__).resolve().parents[1]
network=json.loads((root/'runs/network.json').read_text())
points=[(n['x'],n['y']) for n in network['nodes'] if n['x'] and n['y'] and math.isfinite(n['x']) and math.isfinite(n['y'])]
lon0=sum(p[0] for p in points)/len(points)
lat0=sum(p[1] for p in points)/len(points)
phi=math.radians(lat0)
a=6378137.0
e2=6.69437999014e-3
N=a/math.sqrt(1-e2*math.sin(phi)**2)
M=a*(1-e2)/(1-e2*math.sin(phi)**2)**1.5
xy=sorted(set(((math.radians(lon-lon0)*N*math.cos(phi)),math.radians(lat-lat0)*M) for lon,lat in points))
def cross(o,a,b):
    return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])
lower=[]
for p in xy:
    while len(lower)>=2 and cross(lower[-2],lower[-1],p)<=0:
        lower.pop()
    lower.append(p)
upper=[]
for p in reversed(xy):
    while len(upper)>=2 and cross(upper[-2],upper[-1],p)<=0:
        upper.pop()
    upper.append(p)
hull=lower[:-1]+upper[:-1]
area=abs(sum(hull[i][0]*hull[(i+1)%len(hull)][1]-hull[(i+1)%len(hull)][0]*hull[i][1] for i in range(len(hull))))/2
width=max(p[0] for p in xy)-min(p[0] for p in xy)
height=max(p[1] for p in xy)-min(p[1] for p in xy)
result={'station':'uhs19_1247','valid_coordinate_nodes':len(points),'excluded_zero_coordinate_nodes':len(network['nodes'])-len(points),
        'bounds_lon_lat':[min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)],
        'east_west_extent_km':width/1000,'north_south_extent_km':height/1000,'bounding_rectangle_km2':width*height/1e6,
        'convex_hull_km2':area/1e6,'convex_hull_m2':area,
        'method':'Convex hull of all mapped buses using local WGS84 ellipsoid longitude/latitude scale factors. Approximate geographic footprint, not surveyed service territory.',
        'source':'SMART-DS v1.0 2018 uhs19_1247 Buscoords via runs/network.json'}
(root/'runs/layout-area.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
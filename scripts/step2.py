import arcpy, os, math

arcpy.env.overwriteOutput = True
out_folder = r'C:\Users\Shea\GIS_Work'
dsm_folder = r'C:\Users\Shea\Downloads\lds-canterbury-christchurch-lidar-1m-dsm-2024-2025-AAIGrid (3)'
gdb = os.path.join(out_folder, 'gradient.gdb')
arcpy.env.workspace = gdb

# Step 1: Project trail to NZTM2000
askins_line = r'C:\Users\Shea\GIS_Work\askins_lyr.gdb\Placemarks\Polylines'
nztm = arcpy.SpatialReference(2193)
askins_nztm = os.path.join(gdb, 'askins_nztm')
arcpy.management.Project(askins_line, askins_nztm, nztm)
desc = arcpy.Describe(askins_nztm)
ext = desc.extent
print(f'Trail extent NZTM: {ext.XMin:.0f},{ext.YMin:.0f} to {ext.XMax:.0f},{ext.YMax:.0f}')

# Step 2: Find relevant DSM tiles
asc_files = [os.path.join(dsm_folder, f) for f in os.listdir(dsm_folder) if f.endswith('.asc')]
relevant_tiles = []
for asc in asc_files:
    try:
        rd = arcpy.Describe(asc)
        re = rd.extent
        if re.XMin < ext.XMax and re.XMax > ext.XMin and re.YMin < ext.YMax and re.YMax > ext.YMin:
            relevant_tiles.append(asc)
            print(f'  Tile: {os.path.basename(asc)}')
    except Exception as e:
        pass
print(f'Found {len(relevant_tiles)} tiles')

# Step 3: Mosaic relevant tiles
mosaic = os.path.join(gdb, 'dsm_mosaic')
if relevant_tiles:
    arcpy.management.MosaicToNewRaster(relevant_tiles, gdb, 'dsm_mosaic', nztm, '32_BIT_FLOAT', 1, 1)
    print('Mosaic created')
else:
    print('ERROR: no tiles found')

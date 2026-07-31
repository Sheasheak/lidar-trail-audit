import arcpy, os

kml_gdb = r'C:\Users\Shea\GIS_Work\askins_lyr.gdb'
with open(r'C:\Users\Shea\GIS_Work\walk.txt', 'w') as f:
    for dirpath, dirnames, filenames in arcpy.da.Walk(kml_gdb):
        for fc in filenames:
            full = os.path.join(dirpath, fc)
            f.write(full + '\n')
print('done')

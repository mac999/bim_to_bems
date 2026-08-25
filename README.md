# BIM to BEM Energy Analyzer

Convert BIM models (IFC) into Building Energy Models, run **EnergyPlus** simulations, and explore per-zone results in an interactive **3D web viewer** with energy color mapping.

<p align="center">   
   <img src="./doc/demo1.gif" height="250"></img></br>   
   <img src="./doc/img1.png" height="125"></img>
   <img src="./doc/img2.png" height="125"></img></br>
   <img src="./doc/img3.png" height="260"></img>
</p>

```
 IFC / IDF ──▶  pipeline (CLI)  ──▶ EnergyPlus ──▶ results.json ──▶ 3D viewer
               convert · simulate · results · geometry           (Flask + Three.js)
```
![architecture](doc/cd.svg)


## Features

- **IFC → IDF conversion with real geometry** — thermal zones are derived from
  `IfcSpace` solids; boundary surfaces are reconstructed from the space-shell mesh
  (planar clustering + boundary-loop extraction), not from property sets. Windows and
  external doors are projected from their `IfcWindow` / `IfcCurtainWall` / `IfcDoor`
  meshes onto the host wall.
- **Orientation and site data taken from the model and the weather file** — the IFC
  `TrueNorth` sets the EnergyPlus building north axis, and monthly ground temperatures
  come from the EPW header, damped towards the indoor temperature instead of a single
  fixed value.
- **Space-type internal loads** — occupant, lighting and equipment densities are chosen
  per zone from `config.json` → `space_profiles` by keyword in the space name or
  `IfcSpace.LongName` (corridor, toilet, storage, meeting, kitchen, bedroom), falling
  back to the base office values.
- **EnergyPlus integration** — auto-discovers the local EnergyPlus install, runs an
  annual simulation with per-zone `IdealLoadsAirSystem`, and parses monthly per-zone
  heating/cooling energy and air temperatures.
- **Load and estimated energy use side by side** — the ideal thermal load is converted
  into estimated delivered energy with the heat-source efficiency, cooling COP and
  distribution efficiency from `config.json` → `efficiency`, and lighting and equipment
  electricity is reported per zone, so a whole-building EUI can be compared against a
  bill. Load and estimate stay in separate fields.
- **Conversion quality report** — every run records where the zones came from, the
  share of adiabatic surfaces, how the geometric floor area compares with the areas
  stated in the IFC, and the EnergyPlus geometry warnings (unenclosed zones, coincident
  vertices). The figures land in `results.json` and as chips under the results panel, so
  it is visible when a model is too coarse to trust.
- **Runs hand-written IDFs too** — missing monthly output variables are appended to a
  supplied IDF, models with a real air system are read through the
  `Zone Air System Sensible ...` variables, and relative-coordinate geometry (zone
  origin, relative north, multiplier) is resolved for the viewer.
- **3D result viewer** — zones are rendered in the browser (Three.js) and colored by
  the selected metric (heating/cooling load, estimated energy use, kWh/m², air
  temperature) with sequential / diverging / viridis color schemes, annual or monthly
  periods, a legend, a sortable zone table, and a per-zone monthly chart.
- **Web app is a thin shell** — every analysis runs by spawning the CLI pipeline as a
  subprocess, so the pipeline is fully usable standalone (batch scripts, CI, other UIs).
- **Demo datasets + validation command** included.

## Requirements

| Component | Version tested |
|---|---|
| Python | 3.11 (conda env `venv_lmm` on this machine) |
| EnergyPlus | 25.2 (`F:\Program\EnergyPlusV25-2-0`, auto-discovered) |
| ifcopenshell | 0.8.4 |
| Flask, numpy, pandas, tqdm | see `requirements.txt` |

```powershell
pip install -r requirements.txt
```

[EnergyPlus](https://energyplus.net/downloads) is found automatically (config → `PATH` → common install roots such as
`C:\EnergyPlusV*`, `F:\Program\EnergyPlus*`). To pin a specific install or weather
file, edit `config.json`:

```json
{
  "energyplus_dir": "F:/Program/EnergyPlusV25-2-0",
  "weather_file": "datasets/weather/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
}
```

## CLI pipeline

The pipeline is an independent CLI module (`python -m pipeline`). Stages are
composable or can run end-to-end:

```powershell
# full pipeline: IFC -> IDF -> EnergyPlus -> results.json + geometry.json
python -m pipeline run -i datasets/Duplex_A.ifc -o out/duplex `
    -w datasets/weather/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw

# individual stages
python -m pipeline convert  -i building.ifc -o out_dir --wwr 0.35 -w weather.epw
python -m pipeline geometry -i building.ifc -o geometry.json     # also accepts .idf
python -m pipeline simulate -i model.idf -o ep_out -w weather.epw
python -m pipeline results  -d ep_out -o results.json -m model.json

python -m pipeline weather                # list available EPW files
python -m pipeline validate               # run all demo datasets end-to-end
```

Input rules for `run`:

| Input | Behavior |
|---|---|
| `.ifc` only | converted to IDF, simulated, geometry from IFC spaces |
| `.idf` only | simulated directly, viewer geometry parsed from IDF surfaces |
| `.ifc` + `--idf` | the given IDF drives the simulation; IFC adds visual context |

A job directory contains: `model.idf`, `model.json` (zone metadata), `geometry.json`
(viewer meshes), `ep/` (raw EnergyPlus outputs incl. `eplustbl.htm`), `results.json`,
`run_summary.json`.

## Web application

```powershell
python webapp/app.py --port 5006
# open http://127.0.0.1:5006
```

1. Upload an IFC and/or IDF (plus an optional EPW), or click a demo dataset.
2. Pick the weather file, window mode and WWR, then **Run analysis**. Progress and the
   pipeline log stream into the sidebar.
3. When the run finishes the model appears in the 3D viewer:
   - **Metric** — heating/cooling load, estimated energy use (kWh, kWh/m²),
     mean/min/max air temperature
   - **Period** — annual or any single month
   - **Colors** — sequential blue (magnitude), diverging blue↔red (temperature), viridis
   - Click a zone (or a zone-table row) for its metrics and monthly heating/cooling chart.
   - Download links: generated IDF, `results.json`, EnergyPlus HTML report, `.err` file.
   - Quality chips under the totals flag a coarse conversion (shoebox zones, adiabatic
     share, floor-area deviation, unenclosed zones).

The Flask layer only manages job folders and progress polling; each run executes
`python -m pipeline run ...` as a subprocess.

## Conversion algorithm

Improvements over naive pset-based converters (details in `pipeline/ifc_parser.py`):

1. **Meshing** — every product is meshed once via `ifcopenshell.geom` in world
   coordinates (meters), multi-core.
2. **Space shell → planar surfaces** — triangles of each `IfcSpace` solid are clustered
   into planar regions (normal angle ≤ 5°, plane offset ≤ 2 cm); each region's outer
   boundary loop is chained from edges used exactly once, simplified (duplicate /
   collinear vertex removal), wound so the Newell normal points out of the zone
   (EnergyPlus convention), and classified Floor / Ceiling / Wall by normal.
3. **Inter-zone adjacency** — opposite-facing surfaces of different zones that overlap
   in-plane (Sutherland–Hodgman clipped area ≥ 30 % of the smaller surface, gap ≤ 0.5 m)
   are paired. EnergyPlus requires paired surfaces to be **exact mirrors**, so both are
   rebuilt from the intersection of their convex hulls, one winding reversed — the same
   idea as OpenStudio's *surface intersection*. Interior surfaces that overlapped a
   neighbor but lost the one-to-one match become **Adiabatic** instead of being wrongly
   exposed to outdoors.
4. **Ground / roof detection** — unmatched floors within 0.3 m of the lowest floor →
   `Ground`; unmatched upward-facing surfaces → `Roof`/`Outdoors`.
5. **Windows and doors** — each `IfcWindow`, `IfcCurtainWall` and `IfcDoor` mesh is
   projected onto its best-matching external wall and inscribed as a rectangle,
   iteratively shrunk until strictly inside the host polygon (an EnergyPlus
   requirement); doors get their own opaque construction. Models without usable windows
   fall back to a configurable **window-to-wall ratio** (default 0.3).
6. **No-space fallback** — IFC files without `IfcSpace` (e.g. `SimpleHouse.ifc`,
   `WellnessCenter.ifc`) get one box zone per `IfcBuildingStorey` from that storey's
   element bounding box (the classic "shoebox" BEM simplification), so the pipeline
   still runs end-to-end.
7. **BEM defaults** — U-value-driven constructions, occupancy-scheduled internal loads
   (people / lights / equipment) picked per space profile, 0.5 ACH infiltration,
   20/26 °C dual setpoints, ideal-loads air systems, monthly output variables. The
   building north axis comes from the IFC `TrueNorth` and the ground temperatures from
   the EPW header. All values in `config.json`.

Known limitations: holes in walls other than windows are ignored; curved surfaces are
faceted; zone volumes for the storey fallback are bounding boxes; interior windows and
shading devices are not exported. See [Known limitations & roadmap](#known-limitations--roadmap)
for the full scope and accuracy boundaries.

## Demo datasets (`datasets/`)

| File | Source | What it exercises |
|---|---|---|
| `Duplex_A.ifc` | buildingSMART / NIBS *Common BIM Files* | 21 spaces, 2nd-level space boundaries, IfcWindow projection |
| `Office_A.ifc` | buildingSMART / NIBS *Common BIM Files* | 99 spaces, 487 walls — scale test |
| `SimpleHouse.ifc` | ifcopenshell sample | no `IfcSpace` → shoebox fallback |
| `WellnessCenter.ifc` | project sample | no spaces, multi-storey fallback |
| `sample.idf` | generated by this pipeline | IDF-only input path |
| `weather/*.epw` | EnergyPlus distribution (TMY3) | San Francisco, Chicago |

Run them all with one command:

```powershell
python -m pipeline validate -w datasets/weather/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw
```

Each case must convert, simulate (EnergyPlus completes), and produce non-empty
per-zone results to PASS.

## Project layout

```
bim_to_bem/
├─ pipeline/            # CLI pipeline (standalone)
│  ├─ cli.py            #   subcommands: convert/geometry/simulate/results/run/validate
│  ├─ ifc_parser.py     #   IFC -> BuildingModel (geometry algorithms)
│  ├─ idf_generator.py  #   BuildingModel -> IDF
│  ├─ ep_runner.py      #   EnergyPlus wrapper
│  ├─ results_parser.py #   eplusout.csv -> results.json
│  ├─ geometry_export.py#   viewer geometry.json (from IFC or IDF)
│  ├─ idf_io.py         #   minimal IDF geometry reader
│  ├─ model.py          #   intermediate model + polygon math
│  └─ config.py         #   config.json, EnergyPlus/EPW discovery
├─ webapp/              # Flask app + Three.js viewer (calls the CLI)
├─ datasets/            # demo IFC/IDF + weather
├─ config.json          # pipeline defaults (loads, constructions, tolerances)
└─ convert_ifc_to_ep.py # legacy prototype (superseded by pipeline/)
```

## Known limitations & roadmap

What the numbers are and are not. Read this before comparing the output against a
utility bill.

### 1. Not taken from the IFC

| IFC data | Effect |
|---|---|
| `IfcZone` grouping | spaces are never merged — a 20-space floor on one AHU stays 20 zones |
| HVAC / MEP entities | the mechanical design has **no effect** on the results |
| Material and layer properties | six generic U-value assemblies are used instead |
| `IfcSite` location | the weather file defines the site |
| Second and further `IfcBuilding` | merged into one flat model |

Zones come from `IfcSpace` solids, or from storey / building bounding boxes when a model
has no spaces. Every zone then gets the same ideal-loads system and a 20 / 26 °C
thermostat, so two buildings with different mechanical designs give the same answer if
their envelopes match.

### 2. Load, not metered energy

- `heating_kwh` / `cooling_kwh` are **thermal demand** from a lossless ideal-loads
  system: no COP or part-load behaviour, boiler efficiency, fan and pump power, duct
  losses, heat recovery, economizer or defrost.
- `energy_kwh` divides that demand by the constant factors in `config.json` →
  `efficiency` and adds the reported lighting and equipment electricity. It is a
  bill-scale sanity check, not a plant model, and end uses outside the model (hot water,
  lifts, external lighting) are not counted.
- No design days and no sizing run, so peak loads and equipment capacities are missing.
- Monthly output only: the reported minimum / maximum temperatures are extremes of
  monthly means, not hourly peaks.

### 3. Modelling assumptions

- Space profiles are keyword-matched, so an unnamed space falls back to the office
  defaults. One occupancy schedule for every zone, no daylighting control.
- Infiltration is a constant 0.5 ACH; no natural ventilation or operable windows.
- Solar is distributed onto the floor, exterior reflections are ignored and surrounding
  buildings never become shading surfaces.
- Windows and doors are rectangles inscribed in external walls, without frame or
  shading device; interior doors are dropped.
- Geometry is simplified: holes and tiny surfaces are discarded, surfaces with very many
  vertices become their convex hull, curves are faceted.
- Interior surfaces that lost their pair become adiabatic rather than exterior.
- Ground contact is the weather-file profile damped towards the heating setpoint by a
  single factor — not a slab model.

The `quality` block in `results.json` reports zone source, adiabatic share, floor-area
deviation against the IFC and the EnergyPlus geometry warnings. It flags a conversion
that is too coarse to trust; it does not make the numbers correct.

### 4. Hand-written IDF input

Supplied IDFs run as written (only the monthly output variables the parser needs are
appended), and are re-read by a text reader rather than a validating parser:

- Only `Zone`, `BuildingSurface:Detailed` and `FenestrationSurface:Detailed` are drawn.
  Simplified geometry objects, shading surfaces and internal mass stay invisible.
- Vertex blocks are located heuristically, so EpMacro / `##include` files break the
  reader. No version transition is attempted.
- Relative coordinates are resolved per zone, but the building north axis is not applied.
- Zone area and volume are derived from the geometry times the multiplier; a zone with
  no floor surface gets 0 m² and therefore no kWh/m².
- Monthly variables only, and per-surface results rely on the pipeline's own surface
  naming.
- `config.json` barely applies — setpoints, loads, constructions and the run period come
  from the IDF — and side files referenced by relative path are not copied.

### Roadmap

1. **`IfcZone` support** — merge the spaces of a zone, or group them when reporting.
2. **System discovery** — follow the IFC route from a zone to the system serving it and
   model a real air loop instead of applying constant efficiency factors.
3. **Construction take-off** — read material layer sets instead of generic assemblies.
4. **Wider IDF coverage** — simplified surface objects and the building north axis.
5. **Peak loads** — design days and a sizing run alongside the annual demand.

# License
MIT License

# Author 
Taewook Kang, Ph.D, laputa99999@gmail.com

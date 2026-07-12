"""BIM to BEM conversion pipeline.

Converts IFC building models to EnergyPlus IDF, runs the simulation,
parses per-zone results and exports viewer geometry. Designed to be used
both as a CLI (``python -m pipeline``) and as a library from the web app.
"""

__version__ = "1.0.0"

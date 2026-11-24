from .base import RadarSource
from .recorded import RecordedRadar
from .simulator import SimConfig, SimulatedPLFM, SimulatedRadar, Target

__all__ = ["RadarSource", "SimulatedRadar", "SimulatedPLFM", "SimConfig", "Target",
           "RecordedRadar", "CantennaRDK", "UsrpRadar"]


def __getattr__(name):            # lazy: pyvisa / uhd only needed for real hardware
    if name == "CantennaRDK":
        from .cantenna import CantennaRDK
        return CantennaRDK
    if name == "UsrpRadar":
        from .usrp import UsrpRadar
        return UsrpRadar
    raise AttributeError(name)

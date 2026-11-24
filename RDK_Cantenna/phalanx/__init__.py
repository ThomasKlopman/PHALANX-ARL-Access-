"""PHALANX radar software — Phase 1 (Pasternack Cantenna RDK) with a hardware
layer designed to be swapped for the CN0566 Phaser + USRP B210 in Phase 2.

    from phalanx import cantenna_fmcw, SimulatedRadar, RangeDopplerProcessor
"""
__version__ = "0.4.0"

from .frame import Frame
from .params import RadarParams, b210_plfm, cantenna_cw, cantenna_fmcw, phaser_plfm
from .hardware import RecordedRadar, SimConfig, SimulatedPLFM, SimulatedRadar, Target
from .capture import load_capture, save_capture
from .dsp import CWProcessor, ProcessorConfig, RangeDopplerProcessor, Result

__all__ = ["Frame", "RadarParams", "cantenna_fmcw", "cantenna_cw", "phaser_plfm", "b210_plfm",
           "SimulatedRadar", "SimulatedPLFM", "SimConfig", "Target", "RecordedRadar",
           "save_capture", "load_capture",
           "RangeDopplerProcessor", "CWProcessor", "ProcessorConfig", "Result"]

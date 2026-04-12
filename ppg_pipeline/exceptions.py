class PipelineError(Exception):
    """Base exception for the absorbance pipeline."""


class ConfigMismatchError(PipelineError):
    """Raised when calibration and runtime configurations do not match."""


class CalibrationInvalidError(PipelineError):
    """Raised when a calibration file is invalid or unusable."""

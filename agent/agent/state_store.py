import os
from pathlib import Path

from pydantic import BaseModel


class JsonStore[T: BaseModel]:
    """One model in one JSON file. Holds no value: the file is the only copy."""

    def __init__(self, model: type[T], path: Path) -> None:
        self._model = model
        self._path = path

    def load(self) -> T:
        """Raises FileNotFoundError if missing, ValidationError if corrupt."""
        return self._model.model_validate_json(self._path.read_text())

    def save(self, value: T) -> None:
        """Atomic: write a temp file, fsync it, replace the real one, then fsync the folder so the
        rename itself survives a power loss."""
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w") as f:
            f.write(value.model_dump_json())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
        folder = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)

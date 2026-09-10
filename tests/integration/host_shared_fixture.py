"""Host-visible fixture directories for Docker-backed integration tests."""

import tempfile
from pathlib import Path


def create_host_shared_fixture_dir(repo_root: Path, prefix: str) -> Path:
    """Create a unique fixture directory visible to the host Docker daemon."""
    fixtures_root = repo_root / ".testing" / "integration-fixtures"
    fixtures_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=fixtures_root))

"""Guard the Colab notebooks: if an old copy is saved back to GitHub from Colab, these fail."""
import json
from pathlib import Path

import pytest

NOTEBOOKS = sorted(Path(__file__).resolve().parent.parent.glob("colab/*.ipynb"))


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_is_clean_and_safe(path):
    nb = json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    assert "github_pat_" not in text, "a token was saved into the notebook"
    for cell in nb["cells"]:
        if cell["cell_type"] == "code":
            assert not cell.get("outputs"), "notebook was saved with outputs (save from Colab?)"
    first = "".join(nb["cells"][1]["source"])
    assert "extraHeader" in first and 'os.chdir("/content")' in first
    assert "x-access-token:{token}@github.com" not in first

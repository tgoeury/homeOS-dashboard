"""
Racine conftest.py — ajoute le répertoire du projet au sys.path pour que
les imports `modules.*` et `config` fonctionnent depuis le répertoire tests/.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

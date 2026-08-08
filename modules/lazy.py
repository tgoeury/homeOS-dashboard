"""
HomeOS — modules/lazy.py
Import paresseux d'un module lourd.

Extrait de comfort_engine, qui en avait l'usage exclusif jusqu'à l'arrivée
d'anomaly_engine : les deux moteurs ont besoin de numpy et aucun des deux ne
doit le charger au démarrage du dashboard.
"""
from __future__ import annotations

import importlib


class LazyModule:
    """Proxy d'import paresseux : le module sous-jacent n'est réellement importé
    qu'au premier accès attribut. numpy + onnxruntime + pandas pèsent ~65 MB de RSS
    et ne servent QUE pendant une inférence (déclenchée à la demande, jamais dans
    le steady-state). Sur RPi 3 (1 Go RAM), les charger en permanence au démarrage
    est du gaspillage. Tous les usages `np.`/`pd.`/`ort.` restent inchangés ; les
    annotations `pd.DataFrame` sont des chaînes (cf. `from __future__ import
    annotations`) donc jamais évaluées à l'import."""

    def __init__(self, name: str):
        object.__setattr__(self, "_lazy_name", name)
        object.__setattr__(self, "_lazy_mod", None)

    def __getattr__(self, attr):
        mod = object.__getattribute__(self, "_lazy_mod")
        if mod is None:
            mod = importlib.import_module(object.__getattribute__(self, "_lazy_name"))
            object.__setattr__(self, "_lazy_mod", mod)
        return getattr(mod, attr)
